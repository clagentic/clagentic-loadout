"""`clagentic-loadout test report --collected-ids` and `--diff BASE --collected`."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from _repo_test_support import make_repo, write_run, write_unfinished_run

from clagentic_loadout.repo_test import exits, store
from clagentic_loadout.repo_test.run_cmd import execute_run
from clagentic_loadout.repo_test.spec import MODE_TIER
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


def _cases(*ids: str, outcome: str = "") -> str:
    """Testcase elements for ids written as ``class::name``; *outcome* is a child element."""
    out = []
    for test_id in ids:
        classname, _, name = test_id.rpartition("::")
        child = f"<{outcome}/>" if outcome else ""
        out.append(f"<testcase classname='{classname}' name='{name}'>{child}</testcase>")
    return "".join(out)


def test_collected_ids_prints_every_case_sorted_including_failed_skipped_and_shared_ids(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = (
        _cases("m.T::b", "m.T::a")
        + _cases("m.T::f", outcome="failure")
        + _cases("m.T::s", outcome="skipped")
        + _cases("m.T::a")
        + "<testcase name='bare'/>"
    )
    write_run(reports, RUN, failing=["m.T::f"], junit_body=body)

    code, out, _ = _report(capsys, RUN, "--collected-ids")

    lines = out.splitlines()
    assert code == exits.EXIT_OK
    assert lines == sorted(["m.T::b", "m.T::a", "m.T::f", "m.T::s", "m.T::a", "bare"])
    assert lines.count("m.T::a") == 2


def test_the_collected_id_count_equals_the_recorded_collected_count_of_a_real_run(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo", git_init=False)
    (repo / "runner.py").write_text(
        "import sys\n"
        "body = \"<testcase classname='s' name='a'/><testcase classname='s' name='a'/>\"\n"
        "body += \"<testcase classname='s' name='b'><skipped/></testcase>\"\n"
        "body += \"<testcase classname='s' name='c'><failure message='x'/></testcase>\"\n"
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
    run_id = outcome.summary["run_id"]

    _, ids, _ = _report(capsys, run_id, "--collected-ids")
    _, summary, _ = _report(capsys, run_id, "--summary")

    assert ids.splitlines() == ["s::a", "s::a", "s::b", "s::c"]
    assert len(ids.splitlines()) == json.loads(summary)["counts"]["collected"]


def test_collected_diff_names_each_side_and_counts_the_shared_cases_as_a_multiset(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shared = [f"m.T::s{i}" for i in range(5)]
    write_run(reports, RUN, junit_body=_cases(*shared, "m.T::new1", "m.T::new2") + _cases("m.T::s0"))
    base_only = [f"old.T::t{i:02d}" for i in range(14)]
    write_run(reports, OTHER, junit_body=_cases(*shared, *base_only))

    code, out, _ = _report(capsys, RUN, "--diff", OTHER, "--collected")

    diff = json.loads(out)
    assert code == exits.EXIT_OK
    assert diff["head_only"] == ["m.T::new1", "m.T::new2", "m.T::s0"]
    assert diff["base_only"] == base_only
    assert diff["shared_count"] == 5
    assert (diff["head_run"], diff["base_run"], diff["repo_check"]) == (RUN, OTHER, "same")

    write_run(reports, "20260103T000000Z-12", junit_body=_cases(*shared))
    write_run(reports, "20260104T000000Z-13", junit_body=_cases(*shared))
    _, same, _ = _report(capsys, "20260103T000000Z-12", "--diff", "20260104T000000Z-13", "--collected")
    assert json.loads(same)["head_only"] == json.loads(same)["base_only"] == []


def test_default_diff_ignores_collected_ids(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_run(reports, RUN, junit_body=_cases("m.T::a"))
    write_run(reports, OTHER, junit_body=_cases("m.T::b"))

    code, out, _ = _report(capsys, RUN, "--diff", OTHER)

    assert code == exits.EXIT_OK
    assert json.loads(out)["head_only"] == json.loads(out)["base_only"] == []


def _symlinked(reports: Path, tmp_path: Path) -> None:
    run_dir = write_run(reports, RUN)
    target = tmp_path / "elsewhere.xml"
    target.write_text("<testsuites/>")
    (run_dir / store.JUNIT_FILE).symlink_to(target)


def _unreadable(reports: Path, tmp_path: Path) -> None:
    run_dir = write_run(reports, RUN)
    (run_dir / store.JUNIT_FILE).write_text("<testsuites><testsuite>")


# Each row: how the first run (RUN) is set up, the report arguments, the exit code,
# and what the message must name. Refusals print nothing to stdout.
_REFUSALS: list[tuple[str, Callable[[Path, Path], None], list[str], int, str]] = [
    ("no-report", lambda r, t: write_run(r, RUN), ["--collected-ids"], exits.EXIT_DIFF_REFUSED, "no JUnit report"),
    ("unreadable", _unreadable, ["--collected-ids"], exits.EXIT_DIFF_REFUSED, "not readable XML"),
    ("symlink", _symlinked, ["--collected-ids"], exits.EXIT_DIFF_REFUSED, "symlink"),
    (
        "running",
        lambda r, t: write_unfinished_run(r, RUN, "/repos/alpha", alive=True),
        ["--collected-ids"],
        exits.EXIT_STILL_RUNNING,
        "still running",
    ),
    (
        "unrunnable",
        lambda r, t: write_run(r, RUN, state=store.STATE_UNRUNNABLE),
        ["--collected-ids"],
        exits.EXIT_DIFF_REFUSED,
        store.STATE_UNRUNNABLE,
    ),
    (
        "go-test-json",
        lambda r, t: write_run(r, RUN, report_format="go-test-json"),
        ["--collected-ids"],
        exits.EXIT_DIFF_REFUSED,
        "go-test-json",
    ),
    (
        "diff-head-symlink",
        _symlinked,
        ["--diff", OTHER, "--collected"],
        exits.EXIT_DIFF_REFUSED,
        "symlink",
    ),
    (
        "diff-other-repository",
        lambda r, t: write_run(r, RUN, repo_path="/repos/other", junit_body=_cases("m.T::a")),
        ["--diff", OTHER, "--collected"],
        exits.EXIT_DIFF_REFUSED,
        "different repositories",
    ),
    (
        "diff-other-tier",
        lambda r, t: write_run(r, RUN, mode=MODE_TIER, tier="unit", junit_body=_cases("m.T::a")),
        ["--diff", OTHER, "--collected"],
        exits.EXIT_DIFF_REFUSED,
        "different tiers",
    ),
    (
        "diff-tier-against-non-tier",
        lambda r, t: write_run(r, RUN, mode=MODE_TIER, tier="unit", junit_body=_cases("m.T::a")),
        ["--diff", OTHER, "--collected"],
        exits.EXIT_DIFF_REFUSED,
        "not a tier run",
    ),
]


@pytest.mark.parametrize(
    ("setup", "argv", "code", "message"),
    [row[1:] for row in _REFUSALS],
    ids=[row[0] for row in _REFUSALS],
)
def test_refusals_exit_with_the_reason_and_print_nothing(
    reports: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    setup: Callable[[Path, Path], None],
    argv: list[str],
    code: int,
    message: str,
) -> None:
    setup(reports, tmp_path)
    if "--diff" in argv:
        # The comparison partner: a tier run of another tier for that row, else a plain run.
        other_tier = message == "different tiers"
        write_run(
            reports,
            OTHER,
            mode=MODE_TIER if other_tier else "full",
            tier="integration" if other_tier else None,
            junit_body=_cases("m.T::a"),
        )

    got, out, err = _report(capsys, RUN, *argv)

    assert (got, out) == (code, "")
    assert message in err and RUN in err


def test_a_collected_diff_refuses_a_pair_exactly_as_the_failing_id_diff_does(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, RUN, repo_path="/repos/other", junit_body=_cases("m.T::a"))
    write_run(reports, OTHER, junit_body=_cases("m.T::a"))

    plain = _report(capsys, RUN, "--diff", OTHER)
    collected = _report(capsys, RUN, "--diff", OTHER, "--collected")

    assert plain == collected and plain[0] == exits.EXIT_DIFF_REFUSED


@pytest.mark.parametrize(
    "argv",
    [
        ["--collected"],
        ["--collected-ids", "--summary"],
        ["--collected-ids", "--failing-ids"],
        ["--collected-ids", "--failures"],
        ["--collected-ids", "--diff", OTHER],
    ],
)
def test_conflicting_flags_are_usage_errors(
    reports: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    write_run(reports, RUN, junit_body=_cases("m.T::a"))
    write_run(reports, OTHER, junit_body=_cases("m.T::a"))

    code, out, err = _report(capsys, RUN, *argv)

    assert (code, out) == (exits.EXIT_USAGE, "")
    assert "--collected" in err


def test_help_documents_the_collected_views(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["report", "--help"])

    out = capsys.readouterr().out
    assert code == exits.EXIT_OK and "--collected-ids" in out and "--collected" in out
