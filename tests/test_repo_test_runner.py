"""run_command reports how the command ended and every user id its leader held,
including one it dropped to just before exiting."""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

from clagentic_loadout.repo_test import runner
from clagentic_loadout.repo_test.runner import run_command

_DROP_AND_EXIT = "import os; os.setgid(65534); os.setuid(65534)"


def _run(tmp_path: Path, argv: list[str], timeout: float | None = None) -> runner.CommandOutcome:
    return run_command(argv, tmp_path, tmp_path / "log.txt", timeout)


@pytest.mark.skipif(os.geteuid() != 0, reason="switching user needs a root caller")
def test_a_leader_that_drops_user_and_exits_at_once_has_the_dropped_user_seen(tmp_path: Path) -> None:
    # No sleep in the command: only the final sample of the unreaped leader can see it.
    for _ in range(20):
        outcome = _run(tmp_path, [sys.executable, "-c", _DROP_AND_EXIT])

        assert outcome.exit_code == 0
        assert 65534 in outcome.process_uids


def test_the_exit_code_is_passed_through(tmp_path: Path) -> None:
    outcome = _run(tmp_path, [sys.executable, "-c", "raise SystemExit(7)"])

    assert outcome.exit_code == 7
    assert outcome.problem is None
    assert os.geteuid() in outcome.process_uids


def test_a_timeout_kills_the_command_and_returns_the_user_ids_seen(tmp_path: Path) -> None:
    outcome = _run(tmp_path, [sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.5)

    assert outcome.exit_code is None
    assert "timed out after 0.5 seconds" in (outcome.problem or "")
    assert os.geteuid() in outcome.process_uids


def test_a_command_that_is_not_found_is_reported_without_an_exit_code(tmp_path: Path) -> None:
    outcome = _run(tmp_path, ["/nonexistent/loadout-test-command"])

    assert outcome.exit_code is None
    assert "command not found" in (outcome.problem or "")
    assert outcome.process_uids == frozenset()


def test_without_waitid_the_exit_code_is_still_passed_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(os, "waitid")

    outcome = _run(tmp_path, [sys.executable, "-c", "raise SystemExit(5)"])

    assert outcome.exit_code == 5
    assert outcome.problem is None


@pytest.mark.parametrize("err", [errno.ENOSYS, errno.EINVAL])
def test_a_waitid_oserror_falls_back_to_a_plain_wait_and_stops_the_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: int
) -> None:
    def broken(*_args: object) -> None:
        raise OSError(err, os.strerror(err))

    stops: list[object] = []
    real_stop = runner.UidWatcher.stop

    def counting_stop(self: runner.UidWatcher, *args: object, **kwargs: object) -> frozenset[int]:
        stops.append(self)
        return real_stop(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "waitid", broken)
    monkeypatch.setattr(runner.UidWatcher, "stop", counting_stop)

    outcome = _run(tmp_path, [sys.executable, "-c", "raise SystemExit(9)"])

    assert outcome.exit_code == 9
    assert outcome.problem is None
    assert len(stops) == 1


def test_a_waitid_oserror_does_not_extend_a_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object) -> None:
        raise OSError(errno.ENOSYS, "unsupported")

    monkeypatch.setattr(os, "waitid", broken)

    outcome = _run(tmp_path, [sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.5)

    assert outcome.exit_code is None
    assert "timed out" in (outcome.problem or "")


def test_the_watcher_is_stopped_once_when_waiting_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stops: list[object] = []
    real_stop = runner.UidWatcher.stop

    def counting_stop(self: runner.UidWatcher, *args: object, **kwargs: object) -> frozenset[int]:
        stops.append(self)
        return real_stop(self, *args, **kwargs)  # type: ignore[arg-type]

    def boom(*_args: object) -> None:
        raise RuntimeError("wait failed")

    monkeypatch.setattr(runner.UidWatcher, "stop", counting_stop)
    monkeypatch.setattr(runner, "_await_exit", boom)

    with pytest.raises(RuntimeError):
        _run(tmp_path, [sys.executable, "-c", "pass"])

    assert len(stops) == 1


def test_without_waitid_a_timeout_still_ends_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "waitid")

    outcome = _run(tmp_path, [sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.5)

    assert outcome.exit_code is None
    assert "timed out" in (outcome.problem or "")
