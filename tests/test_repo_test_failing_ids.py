"""Failing test ids are one per failed case, and a comparison cannot lose a
failure that shares a name with another."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _repo_test_support import make_repo, write_run

from clagentic_loadout.repo_test import exits, store
from clagentic_loadout.repo_test.junit import ID_SCHEME, collapse_duplicate_id, parse_junit
from clagentic_loadout.repo_test.run_cmd import execute_run
from clagentic_loadout.repo_test.verb import main

REMOTE = "https://h.example.test/o/alpha.git"
OLD, NEW = "20260101T000000Z-10", "20260102T000000Z-11"


def _junit(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>")
    return path


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reports"
    root.mkdir()
    monkeypatch.setenv(store.REPORTS_DIR_ENV_VAR, str(root))
    return root


def _report(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(["report", *argv])
    return code, capsys.readouterr().out


def test_a_failure_followed_by_a_teardown_error_gets_two_ids(tmp_path: Path) -> None:
    path = _junit(
        tmp_path,
        "<testcase classname='m.T' name='test_a'><failure message='x'/></testcase>"
        "<testcase classname='m.T' name='test_a'><error message='teardown'/></testcase>"
        "<testcase classname='m.T' name='test_ok'/>",
    )

    summary = parse_junit(path)

    assert summary.failed == 2
    assert summary.failing_ids == ("m.T::test_a", "m.T::test_a#dup2")


def test_ids_do_not_depend_on_document_order(tmp_path: Path) -> None:
    failure = "<testcase classname='m.T' name='t'><failure/></testcase>"
    error = "<testcase classname='m.T' name='t'><error/></testcase>"

    assert parse_junit(_junit(tmp_path, failure + error)).failing_ids == parse_junit(
        _junit(tmp_path, error + failure)
    ).failing_ids


def test_every_failed_case_has_its_own_id_across_causes(tmp_path: Path) -> None:
    path = _junit(
        tmp_path,
        # parametrized ids stay distinct by name
        "<testcase classname='m.T' name='t[1]'><failure/></testcase>"
        "<testcase classname='m.T' name='t[2]'><failure/></testcase>"
        # same name in two classes and two modules
        "<testcase classname='m.A' name='t'><failure/></testcase>"
        "<testcase classname='m.B' name='t'><failure/></testcase>"
        "<testcase classname='n.A' name='t'><error/></testcase>"
        # no class name
        "<testcase name='bare'><failure/></testcase>"
        # three entries under one name, two of them the same kind
        "<testcase classname='m.T' name='x'><failure/></testcase>"
        "<testcase classname='m.T' name='x'><failure/></testcase>"
        "<testcase classname='m.T' name='x'><error/></testcase>"
        # one case with several failure elements is still one failed case
        "<testcase classname='m.T' name='multi'><failure/><failure/><error/></testcase>",
    )

    summary = parse_junit(path)

    assert summary.failed == 10
    assert len(summary.failing_ids) == summary.failed
    assert len(set(summary.failing_ids)) == summary.failed
    assert "m.T::multi" in summary.failing_ids


def test_a_suffix_never_lands_on_a_real_test_id(tmp_path: Path) -> None:
    path = _junit(
        tmp_path,
        "<testcase classname='m.T' name='t'><failure/></testcase>"
        "<testcase classname='m.T' name='t'><error/></testcase>"
        "<testcase classname='m.T' name='t#dup2'><failure/></testcase>",
    )

    summary = parse_junit(path)

    assert len(set(summary.failing_ids)) == summary.failed == 3


def test_a_producer_without_pytest_attributes_gets_distinct_ids(tmp_path: Path) -> None:
    path = _junit(
        tmp_path,
        "<testcase name='case one'><failure/></testcase><testcase name='case one'><failure/></testcase>",
    )

    assert parse_junit(path).failing_ids == ("case one", "case one#dup2")


def test_collapse_removes_only_the_trailing_suffix() -> None:
    assert collapse_duplicate_id("m.T::t#dup2") == "m.T::t"
    assert collapse_duplicate_id("m.T::t#dup2[x]") == "m.T::t#dup2[x]"
    assert collapse_duplicate_id("m.T::t") == "m.T::t"


def test_a_recorded_run_carries_the_scheme_and_failed_equals_the_id_count(
    tmp_path: Path, reports: Path
) -> None:
    repo = make_repo(tmp_path / "repo", git_init=False)
    (repo / "runner.py").write_text(
        "import sys\n"
        "body = \"<testcase classname='s' name='t'><failure/></testcase>\" * 2 + "
        "\"<testcase classname='s' name='t'><error/></testcase>\"\n"
        "open(sys.argv[1], 'w').write('<testsuites><testsuite>' + body + '</testsuite></testsuites>')\n"
        "sys.exit(1)\n"
    )

    outcome = execute_run(
        mode="full",
        paths=[],
        repo_root=repo,
        base=None,
        timeout_seconds=60,
        env={store.REPORTS_DIR_ENV_VAR: str(reports)},
    )

    assert outcome.summary["counts"]["failed"] == outcome.summary["failing_id_count"] == 3
    result = json.loads((reports / outcome.summary["run_id"] / store.RESULT_FILE).read_text())
    assert result["id_scheme"] == ID_SCHEME and len(set(result["failing_ids"])) == 3


def test_diff_shows_a_new_failure_that_shares_a_name_with_an_old_one(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, OLD, failing=["m.T::t"], remote_url=REMOTE, id_scheme=ID_SCHEME)
    write_run(reports, NEW, failing=["m.T::t", "m.T::t#dup2"], remote_url=REMOTE, id_scheme=ID_SCHEME)

    code, out = _report(capsys, NEW, "--diff", OLD)

    diff = json.loads(out)
    assert code == exits.EXIT_OK
    assert diff["head_only"] == ["m.T::t#dup2"] and diff["base_only"] == []
    assert "id_basis" not in diff


def test_diff_between_an_old_run_and_a_new_run_compares_plain_names_and_says_so(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, OLD, failing=["m.T::t", "m.T::u"], remote_url=REMOTE)
    write_run(reports, NEW, failing=["m.T::t", "m.T::t#dup2", "m.T::v"], remote_url=REMOTE, id_scheme=ID_SCHEME)

    code, out = _report(capsys, NEW, "--diff", OLD)
    reverse_code, reverse_out = _report(capsys, OLD, "--diff", NEW)

    diff, reverse = json.loads(out), json.loads(reverse_out)
    assert code == reverse_code == exits.EXIT_OK
    assert diff["head_only"] == ["m.T::v"] and diff["base_only"] == ["m.T::u"] and diff["shared_count"] == 1
    assert diff["id_basis"] == "collapsed" and diff["id_basis_reason"]
    assert reverse["head_only"] == ["m.T::u"] and reverse["base_only"] == ["m.T::v"]


def test_two_old_runs_are_compared_as_recorded(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_run(reports, OLD, failing=["m.T::a"], remote_url=REMOTE)
    write_run(reports, NEW, failing=["m.T::b"], remote_url=REMOTE)

    _, out = _report(capsys, NEW, "--diff", OLD)

    diff = json.loads(out)
    assert diff["head_only"] == ["m.T::b"] and "id_basis" not in diff


def test_summary_of_an_old_run_states_why_the_counts_differ(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, OLD, failing=["m.T::a"], failed=3)
    write_run(reports, NEW, failing=["m.T::a", "m.T::a#dup2", "m.T::a#dup3"], id_scheme=ID_SCHEME)

    _, old_out = _report(capsys, OLD)
    _, new_out = _report(capsys, NEW)

    old, new = json.loads(old_out), json.loads(new_out)
    assert old["counts"]["failed"] == 3 and old["failing_id_count"] == 1
    assert "2 failed test case(s)" in old["failing_id_note"]
    assert new["counts"]["failed"] == new["failing_id_count"] == 3
    assert "failing_id_note" not in new
