"""end_process_tree's result and its last-resort group kill."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from clagentic_loadout import process_tree


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (None, True),
        (ProcessLookupError(), True),
        (PermissionError(), False),
        (OSError(), False),
    ],
)
def test_without_proc_the_group_kill_result_is_the_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raised: OSError | None, expected: bool
) -> None:
    def fake_killpg(_pid: int, _sig: int) -> None:
        if raised is not None:
            raise raised

    monkeypatch.setattr(process_tree, "_PROC", tmp_path / "no-proc")
    monkeypatch.setattr(os, "killpg", fake_killpg)

    assert process_tree.end_process_tree(2**22 + 5) is expected


def test_a_handler_not_installed_from_python_is_restored_as_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, object]] = []

    def fake_signal(number: int, handler: object) -> None:
        calls.append((number, handler))
        return None

    monkeypatch.setattr(signal, "signal", fake_signal)

    with process_tree.raise_on_termination():
        pass

    restored = [handler for _number, handler in calls[len(calls) // 2 :]]
    assert restored and all(handler == signal.SIG_DFL for handler in restored)


def test_a_refused_sweep_still_kills_the_leaders_group_before_returning_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    try:

        def refuse(_pid: int, _sig: int) -> None:
            raise PermissionError

        monkeypatch.setattr(os, "kill", refuse)

        assert process_tree.end_process_tree(proc.pid) is False
        monkeypatch.undo()
        assert proc.wait(timeout=10) == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
