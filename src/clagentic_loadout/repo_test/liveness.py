"""repo_test.liveness — tell a still-running detached run from one that died.

A pid alone is not enough: once a process ends its pid can be handed to an
unrelated one. The identity recorded at start is the pid plus the process start
time (clock ticks since boot, from ``/proc/<pid>/stat``) plus the boot id, so a
reused pid, or the same pid after a reboot, never reads as the run being alive.
A zombie (exited, not yet reaped) is not alive. Where ``/proc`` cannot be read the
identity carries the pid only and liveness falls back to a signal-0 probe.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_PROC = Path("/proc")
_BOOT_ID = _PROC / "sys" / "kernel" / "random" / "boot_id"
# Fields of /proc/<pid>/stat after the parenthesised command name: state is the
# first, the start time is the twentieth.
_STATE_FIELD = 0
_START_TICKS_FIELD = 19
_DEAD_STATES = frozenset("ZX")


def _boot_id() -> str | None:
    try:
        text = _BOOT_ID.read_text().strip()
    except OSError:
        return None
    return text or None


def _stat_fields(pid: int) -> tuple[str, int] | None:
    """(state, start ticks) of *pid*, or None when it has gone or cannot be read."""
    try:
        stat = (_PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    # The command name may contain spaces and parentheses; the rest starts after
    # the last closing parenthesis.
    fields = stat.rpartition(")")[2].split()
    try:
        return fields[_STATE_FIELD], int(fields[_START_TICKS_FIELD])
    except (IndexError, ValueError):
        return None


def capture_identity(pid: int) -> dict[str, Any]:
    """The identity of the live process *pid*, to record at start."""
    fields = _stat_fields(pid)
    return {
        "pid": pid,
        "start_ticks": fields[1] if fields is not None else None,
        "boot_id": _boot_id(),
    }


def is_alive(identity: Mapping[str, Any]) -> bool:
    """Whether the process *identity* names is still running."""
    pid = identity.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    ticks = identity.get("start_ticks")
    if isinstance(ticks, int) and not isinstance(ticks, bool):
        recorded_boot = identity.get("boot_id")
        if isinstance(recorded_boot, str) and recorded_boot != _boot_id():
            return False
        fields = _stat_fields(pid)
        return fields is not None and fields[0] not in _DEAD_STATES and fields[1] == ticks
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def child_identities(pid: int) -> list[dict[str, Any]]:
    """The identities of the live direct children of *pid*, read from ``/proc``. Empty
    where ``/proc`` cannot be listed."""
    try:
        entries = [entry for entry in os.listdir(_PROC) if entry.isdecimal()]
    except OSError:
        return []
    found: list[dict[str, Any]] = []
    for entry in entries:
        try:
            stat = (_PROC / entry / "stat").read_text()
        except OSError:
            continue
        rest = stat.rpartition(")")[2].split()
        # After the command name: state, then the parent pid.
        if len(rest) > 1 and rest[1] == str(pid) and rest[0] not in _DEAD_STATES:
            found.append(capture_identity(int(entry)))
    return found


__all__ = ["capture_identity", "child_identities", "is_alive"]
