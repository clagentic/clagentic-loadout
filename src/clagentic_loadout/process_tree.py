"""process_tree -- end a process and everything it started, wherever it went.

Killing a child's process group is not enough when the child starts a command
in a session of its own (a test verb starting its test runner does exactly
that): the group kill never reaches it and it is left running. The one
primitive here, shared by every caller that has to end a command (push
verification, the test verb's foreground and detached runs, `test stop`,
review carriers), finds the processes a leader started by its parent links and
by the sessions and groups those processes lead, and kills them all, repeating
until none is left to catch a process that forked while the sweep ran.

It reads ``/proc``. Where that is unavailable only the leader's process group
is signalled. A process that daemonised (its parent exited, and it shares no
session or group with anything in the tree) cannot be found and is not reached.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

#: Seconds the sweep keeps looking for survivors before it reports failure.
DEFAULT_SETTLE_SECONDS = 5.0

_PROC = Path("/proc")
_ROUND_PAUSE = 0.01
# Fields of /proc/<pid>/stat after the parenthesised command name.
_STATE_FIELD, _PPID_FIELD, _PGRP_FIELD, _SESSION_FIELD, _TICKS_FIELD = 0, 1, 2, 3, 19
_DEAD_STATES = frozenset("ZX")


@dataclass(frozen=True)
class _Proc:
    pid: int
    ppid: int
    pgrp: int
    session: int
    ticks: int
    state: str


def _read(pid: int) -> _Proc | None:
    try:
        stat = (_PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    # The command name may contain spaces and parentheses; the rest starts
    # after the last closing parenthesis.
    fields = stat.rpartition(")")[2].split()
    try:
        return _Proc(
            pid,
            int(fields[_PPID_FIELD]),
            int(fields[_PGRP_FIELD]),
            int(fields[_SESSION_FIELD]),
            int(fields[_TICKS_FIELD]),
            fields[_STATE_FIELD],
        )
    except (IndexError, ValueError):
        return None


def _scan() -> dict[int, _Proc]:
    try:
        names = os.listdir(_PROC)
    except OSError:
        return {}
    table: dict[int, _Proc] = {}
    for name in names:
        if name.isdigit():
            proc = _read(int(name))
            if proc is not None:
                table[proc.pid] = proc
    return table


def _tree(table: dict[int, _Proc], known: dict[int, int], leader: int) -> list[_Proc]:
    """The leader's tree in *table*: the leader and the processes seen in
    earlier rounds (*known*: pid to start ticks, so a reused pid is not one),
    their descendants, and every process in a group or session one of them
    leads. The caller's own groups and pid are never part of it."""
    own = {os.getpid(), 0, 1}
    shielded = {os.getpgrp(), os.getsid(0)}
    members: dict[int, _Proc] = {}
    for pid in [leader, *known]:
        proc = table.get(pid)
        if proc is not None and known.get(pid, proc.ticks) == proc.ticks and pid not in own:
            members[pid] = proc
    changed = True
    while changed:
        changed = False
        # Only a group or session a member actually leads is followed, so a
        # leader that shares the caller's session cannot widen the sweep to it.
        led = {
            pid for pid, proc in members.items() if pid not in shielded and proc.pid in (proc.pgrp, proc.session)
        }
        for proc in table.values():
            if proc.pid in members or proc.pid in own:
                continue
            if proc.ppid in members or proc.pgrp in led or proc.session in led:
                members[proc.pid] = proc
                changed = True
    return list(members.values())


def end_process_tree(leader_pid: int, *, settle_seconds: float = DEFAULT_SETTLE_SECONDS) -> bool:
    """SIGKILL the process *leader_pid* and every process it started, in whatever
    session, until none is alive. Call it while the leader is still running or
    unreaped, so its pid cannot belong to anyone else. Returns True when the tree
    is gone, False when a process was not ours to signal or was still alive after
    *settle_seconds*."""
    if leader_pid <= 1 or leader_pid == os.getpid():
        raise ValueError(f"refusing to end the process tree of pid {leader_pid}")
    if not _PROC.is_dir():
        with contextlib.suppress(OSError):
            os.killpg(leader_pid, signal.SIGKILL)
        return True
    known: dict[int, int] = {}
    deadline = time.monotonic() + settle_seconds
    refused = False
    while True:
        members = _tree(_scan(), known, leader_pid)
        known.update({proc.pid: proc.ticks for proc in members})
        alive = [proc for proc in members if proc.state not in _DEAD_STATES]
        if not alive:
            return True
        if refused or time.monotonic() >= deadline:
            return False
        for proc in alive:
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                refused = True
        time.sleep(_ROUND_PAUSE)


@contextlib.contextmanager
def raise_on_termination() -> Iterator[None]:
    """For the duration of the block, a catchable termination signal (SIGTERM, SIGHUP)
    raises ``SystemExit(128 + signal)`` in the main thread instead of killing the
    process outright, so the caller's cleanup (ending its command's tree, recording
    the run) runs. Outside the main thread, where handlers cannot be set, it does
    nothing."""
    def handler(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    previous: dict[int, object] = {}
    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            previous[number] = signal.signal(number, handler)
        except ValueError:
            break
    try:
        yield
    finally:
        for number, old in previous.items():
            if old is not None:
                signal.signal(number, old)  # type: ignore[arg-type]


__all__ = ["DEFAULT_SETTLE_SECONDS", "end_process_tree", "raise_on_termination"]
