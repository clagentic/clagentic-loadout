"""`clagentic-loadout test report --failures`, and `id_scheme` in `--summary`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _repo_test_support import make_repo, write_run

from clagentic_loadout.repo_test import exits, failures, store
from clagentic_loadout.repo_test.junit import ID_SCHEME
from clagentic_loadout.repo_test.run_cmd import execute_run
from clagentic_loadout.repo_test.verb import main

RUN = "20260101T000000Z-10"
OTHER = "20260102T000000Z-11"


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reports"
    root.mkdir()
    monkeypatch.setenv(store.REPORTS_DIR_ENV_VAR, str(root))
    return root


def _report(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(["report", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_a_failure_prints_its_id_kind_message_and_excerpt(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = (
        "<testcase classname='m.T' name='test_ok'/>"
        "<testcase classname='m.T' name='test_a'>"
        "<failure message='assert 1 == 2'>Traceback\n  line 3\nAssertionError</failure></testcase>"
    )
    write_run(reports, RUN, failing=["m.T::test_a"], junit_body=body)

    code, out, _ = _report(capsys, RUN, "--failures")

    assert code == exits.EXIT_OK
    assert json.loads(out) == [
        {
            "id": "m.T::test_a",
            "kind": "failure",
            "message": "assert 1 == 2",
            "excerpt": "Traceback\n  line 3\nAssertionError",
        }
    ]


def test_an_error_is_reported_as_an_error(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    body = "<testcase classname='m.T' name='t'><error message='boom'>KeyError</error></testcase>"
    write_run(reports, RUN, failing=["m.T::t"], junit_body=body)

    _, out, _ = _report(capsys, RUN, "--failures")

    assert [(r["kind"], r["message"], r["excerpt"]) for r in json.loads(out)] == [("error", "boom", "KeyError")]


def test_cases_sharing_an_id_each_get_a_record_in_report_order(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = (
        "<testcase classname='m.T' name='t'><failure message='first'/></testcase>"
        "<testcase classname='m.T' name='u'><failure message='other'/></testcase>"
        "<testcase classname='m.T' name='t'><error message='teardown'/></testcase>"
    )
    write_run(reports, RUN, failing=["m.T::t", "m.T::t", "m.T::u"], junit_body=body)

    _, out, _ = _report(capsys, RUN, "--failures")

    records = json.loads(out)
    assert [(r["id"], r["kind"], r["message"]) for r in records] == [
        ("m.T::t", "failure", "first"),
        ("m.T::u", "failure", "other"),
        ("m.T::t", "error", "teardown"),
    ]


def test_a_passing_run_prints_an_empty_list(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_run(reports, RUN, junit_body="<testcase classname='m.T' name='t'/>")

    code, out, _ = _report(capsys, RUN, "--failures")

    assert code == exits.EXIT_OK and json.loads(out) == []


def test_huge_failure_text_is_cut_with_a_marker_stating_what_was_dropped(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    size = 5_000_000
    body = (
        "<testcase classname='m.T' name='t'>"
        f"<failure message='{'m' * 3000}'>{'x' * size}</failure></testcase>"
    )
    write_run(reports, RUN, failing=["m.T::t"], junit_body=body)

    code, out, _ = _report(capsys, RUN, "--failures")

    (record,) = json.loads(out)
    assert code == exits.EXIT_OK
    assert record["excerpt"] == "x" * failures.EXCERPT_CAP_CHARS + f"...[truncated: {size - failures.EXCERPT_CAP_CHARS} characters dropped]"
    assert record["message"] == "m" * failures.MESSAGE_CAP_CHARS + "...[truncated: 2000 characters dropped]"
    assert len(out) < failures.TOTAL_CAP_CHARS


def test_the_total_cap_holds_over_many_failed_cases(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    count = 200
    body = "".join(
        f"<testcase classname='m.T' name='t{i}'><failure message='m'>{'y' * 3000}</failure></testcase>"
        for i in range(count)
    )
    write_run(reports, RUN, failing=[f"m.T::t{i}" for i in range(count)], junit_body=body)

    _, out, _ = _report(capsys, RUN, "--failures")

    records = json.loads(out)
    marker = records[-1]
    shown = records[:-1]
    assert "id" not in marker and marker["omitted_cases"] == count - len(shown) > 0
    assert sum(len(r["id"]) + len(r["message"]) + len(r["excerpt"]) for r in shown) < failures.TOTAL_CAP_CHARS * 1.2
    assert len(out) < failures.TOTAL_CAP_CHARS * 2


def test_a_token_shaped_value_in_a_failure_is_redacted(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "s3cr3tT0kenValue99"
    body = (
        "<testcase classname='m.T' name='t'>"
        f"<failure message='GET https://user:{secret}@host.example.test/x failed'>"
        f"url=https://host.example.test/?token={secret}\n"
        f"Authorization: Bearer {secret}"
        "</failure></testcase>"
    )
    write_run(reports, RUN, failing=["m.T::t"], junit_body=body)

    _, out, _ = _report(capsys, RUN, "--failures")

    assert secret not in out
    (record,) = json.loads(out)
    assert "[REDACTED]" in record["message"] and "[REDACTED]" in record["excerpt"]


def test_an_unrunnable_run_is_refused_naming_the_run(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_run(reports, RUN, state=store.STATE_UNRUNNABLE)

    code, out, err = _report(capsys, RUN, "--failures")

    assert code == exits.EXIT_DIFF_REFUSED and out == "" and RUN in err


def test_a_complete_run_with_no_recorded_report_is_refused_naming_the_run(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, RUN, failing=["m.T::t"])

    code, out, err = _report(capsys, RUN, "--failures")

    assert code == exits.EXIT_DIFF_REFUSED and out == "" and RUN in err


def test_a_symlinked_report_is_refused(reports: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = write_run(reports, RUN, failing=["m.T::t"])
    target = tmp_path / "elsewhere.xml"
    target.write_text("<testsuites/>")
    (run_dir / store.JUNIT_FILE).symlink_to(target)

    code, out, err = _report(capsys, RUN, "--failures")

    assert code == exits.EXIT_DIFF_REFUSED and out == "" and RUN in err


def test_failures_is_exclusive_with_the_other_report_forms(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, RUN)
    write_run(reports, OTHER)

    for other in (["--summary"], ["--failing-ids"], ["--diff", OTHER]):
        code, out, _ = _report(capsys, RUN, "--failures", *other)
        assert code == exits.EXIT_USAGE and out == ""


def test_help_documents_failures(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["report", "--help"])

    assert code == exits.EXIT_OK
    assert "--failures" in capsys.readouterr().out


def test_summary_carries_the_id_scheme_and_null_for_an_old_run(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, RUN)
    write_run(reports, OTHER, id_scheme=ID_SCHEME)

    _, old_out, _ = _report(capsys, RUN)
    _, new_out, _ = _report(capsys, OTHER)

    assert json.loads(old_out)["id_scheme"] is None
    assert json.loads(new_out)["id_scheme"] == ID_SCHEME


def test_a_recorded_run_with_a_real_failure_can_be_read_back(tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = make_repo(tmp_path / "repo", git_init=False)
    (repo / "runner.py").write_text(
        "import sys\n"
        "body = \"<testcase classname='s' name='t'><failure message='bad'>detail</failure></testcase>\"\n"
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

    code, out, _ = _report(capsys, outcome.summary["run_id"], "--failures")

    assert code == exits.EXIT_OK
    assert json.loads(out) == [{"id": "s::t", "kind": "failure", "message": "bad", "excerpt": "detail"}]
