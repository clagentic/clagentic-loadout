"""`clagentic-loadout test run --detach` and `test report --wait`: a run that outlives its
caller, read back as running, complete or interrupted."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest
from _repo_test_support import make_repo, profile_text

from clagentic_loadout.repo_test import exits, liveness, store
from clagentic_loadout.repo_test.verb import main

_SRC = str(Path(__file__).resolve().parents[1] / "src")

# A test command that waits for a marker file, then writes a one-test report. The
# bound keeps an orphaned command from outliving a failed test for long.
_WAIT_THEN_PASS = (
    "import os, sys, time\n"
    "go, junit = sys.argv[1], sys.argv[2]\n"
    "end = time.monotonic() + 30\n"
    "while not os.path.exists(go) and time.monotonic() < end:\n"
    "    time.sleep(0.05)\n"
    "open(junit, 'w').write(\"<testsuite><testcase classname='a' name='t'/></testsuite>\")\n"
)
_GOOD_ENOUGH_TO_WAIT = 20.0


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reports"
    monkeypatch.setenv(store.REPORTS_DIR_ENV_VAR, str(root))
    # The supervisor is a fresh interpreter; it needs the same import path pytest has.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [_SRC, os.environ.get("PYTHONPATH", "")])))
    return root


def _call(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict | None, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out.strip() else None), captured.err


def _script_repo(tmp_path: Path, source: str, *args: str) -> Path:
    """A repository whose `full` command runs *source* from a file outside the tree (a
    profile argument cannot hold a newline)."""
    script = tmp_path / "command.py"
    script.write_text(source)
    return make_repo(tmp_path / "repo", profile=profile_text(full=[sys.executable, str(script), *args, "{junit}"]))


def _gated_repo(tmp_path: Path) -> tuple[Path, Path]:
    go = tmp_path / "go"
    return _script_repo(tmp_path, _WAIT_THEN_PASS, str(go)), go


def _next_second() -> None:
    # A run id is the second plus the caller's pid, and these tests start several runs
    # from one pid; a real caller is a new process each time.
    time.sleep(1.05)


def _await_exit(pid: int) -> None:
    deadline = time.monotonic() + _GOOD_ENOUGH_TO_WAIT
    while liveness.is_alive(liveness.capture_identity(pid)) and time.monotonic() < deadline:
        time.sleep(0.05)


def test_detach_returns_at_once_and_the_run_finishes_in_the_background(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, go = _gated_repo(tmp_path)

    code, started, _ = _call(capsys, "run", "full", "--detach", "--repo-root", str(repo))

    assert code == exits.EXIT_OK
    assert started["state"] == "running"
    assert started["dirty"] is False
    assert len(started["head_sha"]) == 40
    assert started["run_dir"] == str(reports / started["run_id"])
    run_id = started["run_id"]

    code, running, _ = _call(capsys, "report", run_id, "--summary")
    assert (code, running["state"], running["counts"]) == (exits.EXIT_OK, "running", None)
    assert (running["head_sha"], running["dirty"]) == (started["head_sha"], False)

    go.write_text("")
    code, done, _ = _call(capsys, "report", run_id, "--wait", str(_GOOD_ENOUGH_TO_WAIT), "--summary")
    assert code == exits.EXIT_OK
    assert done["state"] == "complete"
    assert done["counts"] == {"collected": 1, "passed": 1, "failed": 0, "skipped": 0}


def test_a_finished_detached_summary_has_the_shape_of_a_foreground_one(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    _, foreground, _ = _call(capsys, "run", "full", "--repo-root", str(repo))
    _, foreground_read, _ = _call(capsys, "report", foreground["run_id"])
    _next_second()
    _, started, _ = _call(capsys, "run", "full", "--detach", "--repo-root", str(repo))

    code, detached_read, _ = _call(capsys, "report", started["run_id"], "--wait", str(_GOOD_ENOUGH_TO_WAIT))

    assert code == exits.EXIT_OK
    assert detached_read.keys() == foreground_read.keys()
    assert detached_read["counts"] == foreground_read["counts"]
    assert detached_read["report_verification"]["status"] == foreground_read["report_verification"]["status"]


def test_a_killed_detached_run_reads_interrupted_and_stays_that_way(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, go = _gated_repo(tmp_path)
    _, started, _ = _call(capsys, "run", "full", "--detach", "--repo-root", str(repo))
    run_id = started["run_id"]
    try:
        os.kill(started["pid"], signal.SIGKILL)
        _await_exit(started["pid"])

        code, first, _ = _call(capsys, "report", run_id)
        persisted = json.loads((reports / run_id / store.RESULT_FILE).read_text())
        code_again, second, _ = _call(capsys, "report", run_id)
    finally:
        go.write_text("")

    assert (code, first["state"], first["counts"]) == (exits.EXIT_OK, "interrupted", None)
    assert persisted["state"] == "interrupted"
    assert (code_again, second) == (exits.EXIT_OK, first)
    assert second["finished_at"] == persisted["finished_at"]


def test_an_interrupted_run_is_never_what_latest_or_sha_selects(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, go = _gated_repo(tmp_path)
    go.write_text("")
    _, finished, _ = _call(capsys, "run", "full", "--repo-root", str(repo))
    go.unlink()
    _next_second()
    _, started, _ = _call(capsys, "run", "full", "--detach", "--repo-root", str(repo))
    try:
        os.kill(started["pid"], signal.SIGKILL)
        _await_exit(started["pid"])
        _call(capsys, "report", started["run_id"])
    finally:
        go.write_text("")

    code, latest, _ = _call(capsys, "report", "latest", "--repo", str(repo))

    assert (code, latest["run_id"]) == (exits.EXIT_OK, finished["run_id"])


def test_wait_times_out_with_the_still_running_code_and_prints_the_state(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, go = _gated_repo(tmp_path)
    _, started, _ = _call(capsys, "run", "full", "--detach", "--repo-root", str(repo))
    try:
        before = time.monotonic()
        code, summary, _ = _call(capsys, "report", started["run_id"], "--wait", "0.6")
        waited = time.monotonic() - before
    finally:
        go.write_text("")

    assert code == exits.EXIT_STILL_RUNNING == 79
    assert summary["state"] == "running"
    assert waited >= 0.5
    _call(capsys, "report", started["run_id"], "--wait", str(_GOOD_ENOUGH_TO_WAIT))


def test_failing_ids_of_a_running_run_exit_still_running_with_the_state_named(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, go = _gated_repo(tmp_path)
    _, started, _ = _call(capsys, "run", "full", "--detach", "--repo-root", str(repo))
    try:
        code, out, err = _call(capsys, "report", started["run_id"], "--failing-ids")
    finally:
        go.write_text("")

    assert (code, out) == (exits.EXIT_STILL_RUNNING, None)
    assert "still running" in err
    _call(capsys, "report", started["run_id"], "--wait", str(_GOOD_ENOUGH_TO_WAIT))


def test_the_report_trust_checks_apply_to_a_detached_run(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A command that leaves its report world-writable is refused in a foreground run;
    # the detached supervisor must refuse it the same way.
    repo = _script_repo(
        tmp_path,
        "import os, sys\n"
        "open(sys.argv[1], 'w').write(\"<testsuite><testcase classname='a' name='t'/></testsuite>\")\n"
        "os.chmod(sys.argv[1], 0o666)\n",
    )
    _, started, _ = _call(capsys, "run", "full", "--detach", "--repo-root", str(repo))

    code, summary, _ = _call(capsys, "report", started["run_id"], "--wait", str(_GOOD_ENOUGH_TO_WAIT))

    assert code == exits.EXIT_OK
    assert summary["state"] == store.STATE_UNRUNNABLE
    assert summary["report_verification"]["status"] == "refused"
    assert "writable by any user" in summary["problem"]


@pytest.mark.parametrize(
    "argv, code",
    [
        (["run", "files", "--detach", "--repo-root", "{repo}"], exits.EXIT_USAGE),
        (["run", "full", "--detach", "--repo-root", "{repo}", "--expect-head", "0" * 40], exits.EXIT_REPO_MISMATCH),
        (["run", "tier", "nope", "--detach", "--repo-root", "{repo}"], exits.EXIT_NOT_DECLARED),
    ],
)
def test_detach_checks_synchronously_with_the_foreground_exit_codes_and_records_nothing(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str], argv: list[str], code: int
) -> None:
    repo = make_repo(tmp_path / "repo")

    got, out, _ = _call(capsys, *[part.format(repo=repo) for part in argv])

    assert (got, out) == (code, None)
    assert not reports.exists() or list(reports.iterdir()) == []


def test_a_foreground_run_records_no_process_and_is_not_changed(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")

    code, summary, err = _call(capsys, "run", "full", "--repo-root", str(repo))

    assert code == exits.EXIT_OK
    assert "run_dir" not in summary and "pid" not in summary
    assert store.PROCESS_KEY not in json.loads((reports / summary["run_id"] / store.PROVENANCE_FILE).read_text())
    assert "started:" in err and "finished (complete)" in err


def test_a_negative_or_non_numeric_wait_is_a_usage_error(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for value in ("-1", "soon", "nan"):
        assert main(["report", "latest", "--wait", value]) == exits.EXIT_USAGE
    capsys.readouterr()


@pytest.mark.parametrize("state_field", ["Z", "X"])
def test_a_process_that_has_exited_but_is_not_reaped_is_not_alive(
    monkeypatch: pytest.MonkeyPatch, state_field: str
) -> None:
    identity = liveness.capture_identity(os.getpid())
    assert liveness.is_alive(identity)
    monkeypatch.setattr(liveness, "_stat_fields", lambda pid: (state_field, identity["start_ticks"]))
    assert not liveness.is_alive(identity)


def test_a_reused_pid_or_another_boot_is_not_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    identity = liveness.capture_identity(os.getpid())
    assert not liveness.is_alive({**identity, "start_ticks": identity["start_ticks"] + 1})
    assert not liveness.is_alive({**identity, "boot_id": "another-boot"})
