"""repo_test.process_uids — which user ids the test command's process tree ran as.

A declared command may re-execute itself as a different unprivileged user partway
through, so the runner cannot know in advance which user will own the report it
creates. Instead the runner samples the command's session while it runs and keeps
every user id it sees; the report's owner must be one of them (or the caller).

Sampling is best effort: a process that held a user id for less time than the
sampling interval is not seen. That fails closed (the report is refused with the
owner named), never open.

The host-wide scan for new session members is the expensive part, so its interval
backs off while the session is stable: every scan that finds nothing new doubles
the wait up to a ceiling, and any new process or user id resets it. Processes
already known are re-checked every fast tick, so a user switch by a known process
is seen at the fast interval; only a process that is born and switches user within
one backed-off interval, with nothing else changing, can be missed.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

_PROC = Path("/proc")
_INTERVAL = 0.001
_FULL_SCAN_MIN = 0.004
_FULL_SCAN_MAX = 0.1
# Fields of /proc/<pid>/stat after the parenthesised command name: state, ppid,
# pgrp, session.
_SESSION_FIELD = 3


def _uids_of(pid: int) -> set[int]:
    """Real, effective, saved and filesystem user ids of *pid*; empty when it has
    gone or /proc is unavailable."""
    try:
        text = (_PROC / str(pid) / "status").read_text()
    except OSError:
        return set()
    for line in text.splitlines():
        if line.startswith("Uid:"):
            try:
                return {int(part) for part in line.split()[1:]}
            except ValueError:
                return set()
    return set()


def _session_of(pid: int) -> int | None:
    try:
        stat = (_PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    # The command name may contain spaces and parentheses; the rest starts after
    # the last closing parenthesis.
    fields = stat.rpartition(")")[2].split()
    try:
        return int(fields[_SESSION_FIELD])
    except (IndexError, ValueError):
        return None


def _session_members(session: int) -> list[int]:
    try:
        entries = os.listdir(_PROC)
    except OSError:
        return []
    return [int(name) for name in entries if name.isdigit() and _session_of(int(name)) == session]


class UidWatcher:
    """Collects the user ids of the processes in the session led by *leader_pid*
    until stopped."""

    def __init__(self, leader_pid: int) -> None:
        self._leader = leader_pid
        self._uids: set[int] = set()
        self._known: set[int] = {leader_pid}
        self._scan_wait = _FULL_SCAN_MIN
        self._next_scan = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="uid-watcher", daemon=True)

    def start(self) -> None:
        self._sample()
        self._thread.start()

    def stop(self) -> frozenset[int]:
        self._stop.set()
        self._thread.join()
        return frozenset(self._uids)

    def _record(self, pid: int) -> bool:
        """Read *pid*'s user ids; True when that added a user id. A process that
        has gone is forgotten, except the leader, which may not have started yet."""
        found = _uids_of(pid)
        if not found:
            if pid != self._leader:
                self._known.discard(pid)
            return False
        new = not found <= self._uids
        self._uids |= found
        return new

    def _sample(self) -> None:
        changed = False
        for pid in list(self._known):
            changed = self._record(pid) or changed
        now = time.monotonic()
        if now < self._next_scan:
            return
        for pid in _session_members(self._leader):
            if pid not in self._known:
                self._known.add(pid)
                self._record(pid)
                changed = True
        self._scan_wait = _FULL_SCAN_MIN if changed else min(self._scan_wait * 2, _FULL_SCAN_MAX)
        self._next_scan = now + self._scan_wait

    def _run(self) -> None:
        while not self._stop.is_set():
            self._sample()
            time.sleep(_INTERVAL)


__all__ = ["UidWatcher"]
