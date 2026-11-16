"""The run timeout (flag, profile, default), the `timed_out` and `stopped` states, and
`clagentic-loadout test stop`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from _repo_test_support import make_repo, write_run, write_unfinished_run

from clagentic_loadout.repo_test import exits, liveness, stop_cmd, store
from clagentic_loadout.repo_test.spec import (
    DEFAULT_TIMEOUT_SECONDS,
    TIMEOUT_FROM_DEFAULT,
    TIMEOUT_FROM_FLAG,
    TIMEOUT_FROM_PROFILE,
    parse_test_block,
    resolve_timeout,
)
from clagentic_loadout.repo_test.verb import main

_SRC = str(Path(__file__).resolve().parents[1] / "src")
_WAIT = 20.0

# A command that records its own pid and a grandchild's, then waits far longer than
# any test does, so only a timeout or a stop ends it.
_HANG_WITH_CHILD = (
    "import subprocess, sys, time\n"
    "pids, junit = sys.argv[1], sys.argv[2]\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "import os\n"
    "open(pids + '.tmp', 'w').write(f'{os.getpid()} {child.pid}')\n"
    "os.rename(pids + '.tmp', pids)\n"
    "time.sleep(120)\n"
)

_SHA_OLD, _SHA_NEW = "a" * 40, "b" * 40


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reports"
    monkeypatch.setenv(store.REPORTS_DIR_ENV_VAR, str(root))
    # The supervisor is a fresh interpreter; it needs the import path pytest has.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [_SRC, os.environ.get("PYTHONPATH", "")])))
    # Not inside any git work tree, so `latest` is not scoped by the caller's repository.
    monkeypatch.chdir(tmp_path)
    return root


def _call(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict | None, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out.strip() else None), captured.err


def _hanging_repo(tmp_path: Path, **block: object) -> tuple[Path, Path]:
    """A repository whose `full` command hangs; returns it and the file the command
    writes its pid and its child's pid to."""
    script, pids = tmp_path / "hang.py", tmp_path / "pids"
    script.write_text(_HANG_WITH_CHILD)
    declared = {"full": [sys.executable, str(script), str(pids), "{junit}"], **block}
    return make_repo(tmp_path / "repo", profile="class: internal\ntest: " + json.dumps(declared) + "\n"), pids


def _live_processes(pids: Path) -> list[dict]:
    """Identities of the hanging command and its child, read once both are running."""
    deadline = time.monotonic() + _WAIT
    while not pids.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return [liveness.capture_identity(int(pid)) for pid in pids.read_text().split()]


@pytest.mark.parametrize(
    ("flag", "declared", "seconds", "source", "warns"),
    [
        (5, 100, 5, TIMEOUT_FROM_FLAG, False),
        (None, 100, 100, TIMEOUT_FROM_PROFILE, False),
        (None, 0.5, 0.5, TIMEOUT_FROM_PROFILE, False),
        (None, None, DEFAULT_TIMEOUT_SECONDS, TIMEOUT_FROM_DEFAULT, False),
        (5, "soon", 5, TIMEOUT_FROM_FLAG, False),
        *[
            (None, bad, DEFAULT_TIMEOUT_SECONDS, TIMEOUT_FROM_DEFAULT, True)
            for bad in (0, -3, "soon", True, float("nan"), float("inf"), [60], 10**400)
        ],
    ],
)
def test_the_effective_timeout_is_flag_then_profile_then_default(
    flag: float | None, declared: object, seconds: float, source: str, warns: bool
) -> None:
    timeout = resolve_timeout(flag, declared)

    assert (timeout.seconds, timeout.source) == (seconds, source)
    assert (timeout.warning is not None) == warns
    assert not warns or "test.timeout_seconds" in (timeout.warning or "")


def test_a_bad_profile_timeout_keeps_the_profile_valid_and_warns_at_run_start(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    block = {"full": [sys.executable, "runner.py", "{junit}"], "timeout_seconds": "soon"}
    assert parse_test_block(block).timeout_seconds == "soon"
    repo = make_repo(tmp_path / "repo", profile="class: internal\ntest: " + json.dumps(block) + "\n")

    code, summary, err = _call(capsys, "run", "full", "--repo-root", str(repo))

    assert code == exits.EXIT_OK
    assert summary["timeout"]["source"] == TIMEOUT_FROM_DEFAULT
    assert summary["timeout"]["seconds"] == DEFAULT_TIMEOUT_SECONDS == 14400
    assert "test.timeout_seconds" in err and "warning" in err


def test_a_detached_run_past_its_profile_timeout_reads_timed_out_and_leaves_nothing_running(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, pids = _hanging_repo(tmp_path, timeout_seconds=3)
    _, started, _ = _call(capsys, "run", "full", "--detach", "--repo-root", str(repo))
    assert started["timeout"] == {"seconds": 3, "source": TIMEOUT_FROM_PROFILE}
    processes = _live_processes(pids)

    code, summary, _ = _call(capsys, "report", started["run_id"], "--wait", str(_WAIT))

    assert code == exits.EXIT_OK
    assert summary["state"] == store.STATE_TIMED_OUT
    assert summary["counts"]["collected"] == 0
    assert summary["timeout"] == {"seconds": 3, "source": TIMEOUT_FROM_PROFILE}
    assert "profile" in summary["problem"]
    assert not any(liveness.is_alive(identity) for identity in processes)


def test_stop_ends_the_supervisor_and_the_command_session_and_the_run_reads_stopped(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, pids = _hanging_repo(tmp_path)
    _, started, _ = _call(capsys, "run", "full", "--detach", "--repo-root", str(repo))
    processes = [liveness.capture_identity(started["pid"]), *_live_processes(pids)]
    assert all(liveness.is_alive(identity) for identity in processes)

    code, stopped, err = _call(capsys, "stop", started["run_id"])

    assert code == exits.EXIT_OK
    assert stopped["state"] == store.STATE_STOPPED
    assert stopped["stopped_by"] and stopped["stopped_at"]
    assert "stopped" in err
    assert not any(liveness.is_alive(identity) for identity in processes)
    code, read, _ = _call(capsys, "report", started["run_id"], "--summary")
    assert (code, read["state"], read["counts"]) == (exits.EXIT_OK, store.STATE_STOPPED, None)
    # A second stop finds a terminal run and reports it, changing nothing.
    before = (reports / started["run_id"] / store.RESULT_FILE).read_bytes()
    assert _call(capsys, "stop", started["run_id"])[0] == exits.EXIT_OK
    assert (reports / started["run_id"] / store.RESULT_FILE).read_bytes() == before


@pytest.mark.parametrize("state", [store.STATE_COMPLETE, store.STATE_UNRUNNABLE, store.STATE_TIMED_OUT, store.STATE_STOPPED])
def test_stop_on_a_run_that_has_ended_prints_its_state_and_changes_nothing(
    reports: Path, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    run_dir = write_run(reports, "20260101T000000Z-1", state=state)
    before = {path.name: path.read_bytes() for path in run_dir.iterdir()}

    code, summary, _ = _call(capsys, "stop", "20260101T000000Z-1")

    assert (code, summary["state"]) == (exits.EXIT_OK, state)
    assert {path.name: path.read_bytes() for path in run_dir.iterdir()} == before


def test_stop_on_an_interrupted_run_prints_interrupted_and_writes_no_result(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_unfinished_run(reports, "20260101T000000Z-1", "/repos/alpha", alive=False)

    code, summary, _ = _call(capsys, "stop", "20260101T000000Z-1")

    assert (code, summary["state"]) == (exits.EXIT_OK, store.STATE_INTERRUPTED)
    assert not (reports / "20260101T000000Z-1" / store.RESULT_FILE).exists()


@pytest.mark.parametrize("run_id", ["latest", "../x", "20260101T000000Z-1"])
def test_stop_refuses_a_bad_or_unknown_run_id(
    reports: Path, capsys: pytest.CaptureFixture[str], run_id: str
) -> None:
    reports.mkdir()

    code, out, err = _call(capsys, "stop", run_id)

    assert (code, out) == (exits.EXIT_BAD_RUN_ID, None)
    assert run_id in err


def test_stop_will_not_signal_a_run_started_by_another_user(
    reports: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_unfinished_run(reports, "20260101T000000Z-1", "/repos/alpha", alive=True)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(reports).st_uid + 1)

    code, out, err = _call(capsys, "stop", "20260101T000000Z-1")

    assert (code, out) == (exits.EXIT_REPO_MISMATCH, None)
    assert "another user" in err
    assert not (reports / "20260101T000000Z-1" / store.RESULT_FILE).exists()


def test_stop_never_signals_a_supervisor_pid_that_no_longer_matches_its_identity(
    reports: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_unfinished_run(reports, "20260101T000000Z-1", "/repos/alpha", alive=True)
    signalled: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signalled.append(pid))

    def pid_recycled_after_the_run_was_read(*_args: object) -> list:
        monkeypatch.setattr(liveness, "is_alive", lambda _identity: False)
        return []

    monkeypatch.setattr(stop_cmd, "_command_groups", pid_recycled_after_the_run_was_read)

    _call(capsys, "stop", "20260101T000000Z-1")

    assert signalled == []


def test_stop_ends_a_supervisor_that_never_recorded_its_command_and_reports_it_unidentified(
    reports: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stop_cmd, "_SETTLE_SECONDS", 0.5)
    write_unfinished_run(reports, "20260101T000000Z-1", "/repos/alpha", alive=False)
    supervisor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    try:
        run_dir = reports / "20260101T000000Z-1"
        provenance = json.loads((run_dir / store.PROVENANCE_FILE).read_text())
        provenance[store.PROCESS_KEY] = {**liveness.capture_identity(supervisor.pid), "mode": "full"}
        (run_dir / store.PROVENANCE_FILE).write_text(json.dumps(provenance))

        code, out, err = _call(capsys, "stop", "20260101T000000Z-1")

        assert (code, out) == (exits.EXIT_REPO_MISMATCH, None)
        assert "could not be identified" in err
        assert supervisor.wait(timeout=_WAIT) != 0
        assert not (run_dir / store.RESULT_FILE).exists()
    finally:
        supervisor.kill()
        supervisor.wait()


@pytest.mark.parametrize("state", [store.STATE_TIMED_OUT, store.STATE_STOPPED])
def test_timed_out_and_stopped_runs_are_terminal_for_wait_and_never_latest_or_sha(
    reports: Path, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    write_run(reports, "20260101T000000Z-1", head_sha=_SHA_OLD)
    write_run(reports, "20260101T000001Z-1", head_sha=_SHA_NEW, state=state)

    before = time.monotonic()
    code, waited, _ = _call(capsys, "report", "20260101T000001Z-1", "--wait", "10")
    assert (code, waited["state"]) == (exits.EXIT_OK, state)
    assert time.monotonic() - before < 5

    code, latest, _ = _call(capsys, "report", "latest", "--repo", "alpha")
    assert (code, latest["run_id"]) == (exits.EXIT_OK, "20260101T000000Z-1")
    code, _, err = _call(capsys, "report", "--sha", _SHA_NEW)
    assert code == exits.EXIT_SHA_NO_MATCH


@pytest.mark.parametrize("state", [store.STATE_TIMED_OUT, store.STATE_STOPPED])
def test_failing_ids_and_diff_treat_the_new_states_as_having_no_results(
    reports: Path, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    write_run(reports, "20260101T000000Z-1")
    write_run(reports, "20260101T000001Z-1", state=state)

    code, out, _ = _call(capsys, "report", "20260101T000001Z-1", "--failing-ids")
    assert (code, out) == (exits.EXIT_DIFF_REFUSED, None)
    code, out, _ = _call(capsys, "report", "20260101T000000Z-1", "--diff", "20260101T000001Z-1")
    assert (code, out) == (exits.EXIT_DIFF_REFUSED, None)
