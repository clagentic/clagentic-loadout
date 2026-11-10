"""Diagnosing a run through `clagentic-loadout test` alone: the output tail of an
unrunnable run in its report, the signal named in the problem text, and `test list`."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest
from _repo_test_support import make_repo, profile_text, tree_snapshot, write_run

from clagentic_loadout.repo_test import exits, liveness, store
from clagentic_loadout.repo_test.runner import TAIL_MAX_CHARS, describe_exit
from clagentic_loadout.repo_test.verb import main

_SRC = str(Path(__file__).resolve().parents[1] / "src")

_SIGPIPE_AFTER_REPORT = (
    "import os, signal, sys\n"
    "open(sys.argv[1], 'w').write('<testsuite><testcase classname=\"a\" name=\"t\"/></testsuite>')\n"
    "signal.signal(signal.SIGPIPE, signal.SIG_DFL)\n"
    "os.kill(os.getpid(), signal.SIGPIPE)\n"
)
_SIGPIPE_NO_REPORT = "import os, signal\nsignal.signal(signal.SIGPIPE, signal.SIG_DFL)\nos.kill(os.getpid(), signal.SIGPIPE)\n"


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reports"
    monkeypatch.setenv(store.REPORTS_DIR_ENV_VAR, str(root))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [_SRC, os.environ.get("PYTHONPATH", "")])))
    return root


def _call(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _script_repo(tmp_path: Path, source: str) -> Path:
    script = tmp_path / "command.py"
    script.write_text(source)
    return make_repo(tmp_path / "repo", profile=profile_text(full=[sys.executable, str(script), "{junit}"]))


# --- the output tail of an unrunnable run ---------------------------------


def test_a_detached_unrunnable_run_reports_the_end_of_its_output_redacted(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _script_repo(
        tmp_path,
        "print('collection exploded')\n"
        "print('fetching https://user:hunter2secret@host.example.test/x')\n",
    )

    code, out, _ = _call(capsys, "run", "full", "--detach", "--repo-root", str(repo))
    assert code == exits.EXIT_OK
    run_id = json.loads(out)["run_id"]
    code, out, _ = _call(capsys, "report", run_id, "--wait", "30", "--summary")

    summary = json.loads(out)
    assert (code, summary["state"]) == (exits.EXIT_OK, "unrunnable")
    assert "collection exploded" in summary["failure_log_tail"]
    assert "hunter2secret" not in summary["failure_log_tail"]


def test_the_tail_is_bounded_to_the_last_lines_and_characters(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _script_repo(tmp_path, "for n in range(500):\n    print('line', n)\nprint('x' * 50000)\nprint('last')\n")

    _, out, _ = _call(capsys, "run", "full", "--repo-root", str(repo))
    _, out, _ = _call(capsys, "report", json.loads(out)["run_id"], "--summary")

    tail = json.loads(out)["failure_log_tail"]
    assert len(tail) <= TAIL_MAX_CHARS
    assert tail.endswith("last")
    assert "line 0" not in tail


def test_a_complete_run_and_a_run_with_no_output_carry_no_tail(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    complete = make_repo(tmp_path / "complete")
    _, out, _ = _call(capsys, "run", "full", "--repo-root", str(complete))
    _, out, _ = _call(capsys, "report", json.loads(out)["run_id"])
    assert "failure_log_tail" not in json.loads(out)

    # A run id is the second plus the caller's pid; both runs here share a pid.
    time.sleep(1.05)
    silent = _script_repo(tmp_path, "pass\n")
    _, out, _ = _call(capsys, "run", "full", "--repo-root", str(silent))
    printed = json.loads(out)
    assert printed["state"] == "unrunnable"
    assert "failure_log_tail" not in printed
    _, out, _ = _call(capsys, "report", printed["run_id"])
    assert "failure_log_tail" not in json.loads(out)


def test_the_foreground_run_still_prints_the_tail_to_stderr(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _script_repo(tmp_path, "print('collection exploded')\n")

    _, out, err = _call(capsys, "run", "full", "--repo-root", str(repo))

    assert "collection exploded" in err
    assert "failure_log_tail" not in json.loads(out)


# --- the signal in the problem text ---------------------------------------


def test_a_command_killed_by_sigpipe_is_named_and_its_exit_code_is_unchanged(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _script_repo(tmp_path, _SIGPIPE_AFTER_REPORT)

    _, out, _ = _call(capsys, "run", "full", "--repo-root", str(repo))

    summary = json.loads(out)
    assert summary["command_exit_code"] == -13
    assert "killed by signal 13 (SIGPIPE)" in summary["problem"]
    _, out, _ = _call(capsys, "report", summary["run_id"])
    assert "killed by signal 13 (SIGPIPE)" in json.loads(out)["problem"]


def test_a_command_killed_by_a_signal_before_its_report_names_it_in_the_unrunnable_problem(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _script_repo(tmp_path, _SIGPIPE_NO_REPORT)

    _, out, _ = _call(capsys, "run", "full", "--repo-root", str(repo))

    summary = json.loads(out)
    assert summary["state"] == "unrunnable"
    assert summary["command_exit_code"] == -13
    assert summary["problem"].endswith("(the command was killed by signal 13 (SIGPIPE))")


def test_a_positive_exit_reads_as_before(tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _script_repo(tmp_path, "import sys\nsys.exit(3)\n")

    _, out, _ = _call(capsys, "run", "full", "--repo-root", str(repo))

    assert json.loads(out)["problem"].endswith("(the command exited 3)")


@pytest.mark.parametrize(
    ("code", "text"),
    [
        (0, "exited 0"),
        (64, "exited 64"),
        (-9, "was killed by signal 9 (SIGKILL)"),
        (-13, "was killed by signal 13 (SIGPIPE)"),
        (-250, "was killed by signal 250"),
    ],
)
def test_describe_exit(code: int, text: str) -> None:
    assert describe_exit(code) == text


# --- test list -------------------------------------------------------------


def _unfinished(root: Path, run_id: str, repo_path: str, *, alive: bool) -> None:
    run_dir = write_run(root, run_id, finished=False, repo_path=repo_path)
    identity = liveness.capture_identity(os.getpid()) if alive else {"pid": 2**30}
    provenance = json.loads((run_dir / store.PROVENANCE_FILE).read_text())
    provenance[store.PROCESS_KEY] = {**identity, "mode": "full", "started_at": "2026-01-01T00:00:00+00:00"}
    (run_dir / store.PROVENANCE_FILE).write_text(json.dumps(provenance))


@pytest.fixture
def populated(reports: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    write_run(reports, "20260101T000001Z-1", repo_path="/repos/alpha", head_sha="a" * 40)
    write_run(reports, "20260101T000002Z-1", repo_path="/repos/alpha", failing=["x::y"])
    write_run(reports, "20260101T000003Z-1", repo_path="/repos/alpha", state=store.STATE_UNRUNNABLE)
    _unfinished(reports, "20260101T000004Z-1", "/repos/alpha", alive=True)
    _unfinished(reports, "20260101T000005Z-1", "/repos/alpha", alive=False)
    write_run(reports, "20260101T000006Z-1", repo_path="/repos/beta")
    write_run(reports, "29990101T000000Z-1", repo_path="/repos/alpha")
    write_run(reports, "20261301T000000Z-1", repo_path="/repos/alpha")
    outside = tmp_path / "outside-git"
    outside.mkdir()
    monkeypatch.chdir(outside)
    return reports


def _rows(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines()]


def test_list_shows_every_state_for_the_repo_newest_first(populated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = _call(capsys, "list", "--repo", "/repos/alpha")

    rows = _rows(out)
    assert code == exits.EXIT_OK
    assert [row["run_id"] for row in rows] == [
        "29990101T000000Z-1",
        "20261301T000000Z-1",
        "20260101T000005Z-1",
        "20260101T000004Z-1",
        "20260101T000003Z-1",
        "20260101T000002Z-1",
        "20260101T000001Z-1",
    ]
    states = {row["run_id"]: row["state"] for row in rows}
    assert states["20260101T000004Z-1"] == "running"
    assert states["20260101T000005Z-1"] == "interrupted"
    assert states["20260101T000003Z-1"] == "unrunnable"
    assert states["20260101T000002Z-1"] == "complete"


def test_list_marks_future_dated_and_undatable_runs_instead_of_hiding_them(
    populated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = _call(capsys, "list", "--repo", "/repos/alpha")

    marks = {row["run_id"]: row["date_mark"] for row in _rows(out)}
    assert marks["29990101T000000Z-1"] == "future-dated"
    assert marks["20261301T000000Z-1"] == "undatable"
    assert marks["20260101T000002Z-1"] is None


def test_list_honours_limit_and_the_repo_scope(populated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = _call(capsys, "list", "--repo", "/repos/alpha", "--limit", "3")
    assert [row["run_id"] for row in _rows(out)] == [
        "29990101T000000Z-1",
        "20261301T000000Z-1",
        "20260101T000005Z-1",
    ]

    _, out, _ = _call(capsys, "list", "--repo", "/repos/beta")
    assert [row["run_id"] for row in _rows(out)] == ["20260101T000006Z-1"]


def test_list_row_carries_the_recorded_fields(populated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, out, _ = _call(capsys, "list", "--repo", "/repos/alpha")

    row = next(r for r in _rows(out) if r["run_id"] == "20260101T000001Z-1")
    assert row["head_sha"] == "a" * 40
    assert row["dirty"] is False
    assert row["command_exit_code"] == 0
    assert row["counts"] == {"collected": 3, "passed": 3, "failed": 0, "skipped": 0}
    assert row["mode"] == "full"


def test_list_without_a_repo_outside_git_refuses_when_several_repositories_are_on_record(
    populated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _call(capsys, "list")

    assert code == exits.EXIT_REPO_MISMATCH
    assert out == ""
    assert "--repo" in err


def test_list_is_scoped_to_the_repository_of_the_working_directory(
    tmp_path: Path, reports: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    write_run(reports, "20260101T000001Z-1", repo_path=str(repo.resolve()))
    write_run(reports, "20260101T000002Z-1", repo_path="/repos/other")
    monkeypatch.chdir(repo)

    code, out, err = _call(capsys, "list")

    assert code == exits.EXIT_OK
    assert [row["run_id"] for row in _rows(out)] == ["20260101T000001Z-1"]
    assert "the run list is scoped to the repository" in err


def test_list_changes_nothing_even_for_an_interrupted_run(populated: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = tree_snapshot(populated)

    _call(capsys, "list", "--repo", "/repos/alpha")

    assert tree_snapshot(populated) == before
    assert not (populated / "20260101T000005Z-1" / store.RESULT_FILE).exists()


def test_list_with_no_reports_root_or_no_match_exits_75(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _call(capsys, "list", "--repo", "x")[0] == exits.EXIT_NO_RUNS
    write_run(reports, "20260101T000001Z-1", repo_path="/repos/alpha")
    assert _call(capsys, "list", "--repo", "/repos/none")[0] == exits.EXIT_NO_RUNS


@pytest.mark.parametrize("limit", ["0", "-1", "many"])
def test_list_refuses_a_bad_limit(limit: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert _call(capsys, "list", "--limit", limit)[0] == exits.EXIT_USAGE


def test_list_has_help_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = _call(capsys, "list", "--help")
    assert code == exits.EXIT_OK and "--limit" in out
    code, out, _ = _call(capsys, "list", "--version")
    assert code == exits.EXIT_OK and out.strip()
