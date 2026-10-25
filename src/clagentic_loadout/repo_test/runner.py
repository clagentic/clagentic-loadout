"""repo_test.runner — run one declared test command and capture its output.

The command is run without a shell, with no stdin, in its own session so a
timeout can end the whole process tree it started. Its combined output goes to a
log file; its own exit code is reported as data and is never this process's exit
code.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.repo_test.process_uids import UidWatcher


@dataclass(frozen=True)
class CommandOutcome:
    """How the command ended. ``exit_code`` is None when it never produced one;
    ``problem`` then says why."""

    exit_code: int | None
    problem: str | None
    # User ids the command's session was seen running as.
    process_uids: frozenset[int] = frozenset()


_EXIT_POLL = 0.002


def _plain_wait(proc: subprocess.Popen[bytes], timeout_seconds: float | None) -> tuple[bool, bool, int | None]:
    try:
        return True, False, proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return False, False, None


def _await_exit(proc: subprocess.Popen[bytes], timeout_seconds: float | None) -> tuple[bool, bool, int | None]:
    """Wait for *proc* to exit. Returns (exited, held, code): *held* is True when
    the leader has exited but is not yet reaped, so its pid cannot belong to any
    other process and its /proc entry still shows its final user ids.

    Where the platform lacks waitid or WNOWAIT, or the child cannot be waited on
    without reaping it, this falls back to a plain wait and *held* is False."""
    if not (hasattr(os, "waitid") and hasattr(os, "WNOWAIT") and hasattr(os, "WNOHANG")):
        return _plain_wait(proc, timeout_seconds)
    flags = os.WEXITED | os.WNOWAIT | os.WNOHANG
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    while True:
        try:
            if os.waitid(os.P_PID, proc.pid, flags) is not None:
                return True, True, None
        except ChildProcessError:
            remaining = None if deadline is None else max(deadline - time.monotonic(), 0)
            return _plain_wait(proc, remaining)
        if deadline is not None and time.monotonic() >= deadline:
            return False, False, None
        time.sleep(_EXIT_POLL)


def run_command(
    argv: Sequence[str], cwd: Path, log_path: Path, timeout_seconds: float | None
) -> CommandOutcome:
    try:
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(
                list(argv),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            watcher = UidWatcher(proc.pid)
            watcher.start()
            exited, held, code = _await_exit(proc, timeout_seconds)
            if not exited:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                return CommandOutcome(None, f"timed out after {timeout_seconds} seconds", watcher.stop())
            # The leader is still a zombie here, so this last sample reads only
            # its own pid before it can be reused.
            uids = watcher.stop(sample_leader_last=held)
            code = proc.wait()
    except FileNotFoundError:
        return CommandOutcome(None, f"command not found: {argv[0]!r} (working directory {str(cwd)!r})")
    except OSError as exc:
        return CommandOutcome(None, f"cannot run {argv[0]!r}: {exc}")
    return CommandOutcome(code, None, uids)


def log_tail(log_path: Path, lines: int) -> str:
    """The last *lines* lines of the run's output, for a failure message."""
    try:
        text = log_path.read_bytes()[-64 * 1024 :].decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


__all__ = ["CommandOutcome", "log_tail", "run_command"]
