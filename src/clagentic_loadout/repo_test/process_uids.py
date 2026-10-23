"""repo_test.process_uids — which user ids the test command's process tree ran as.

A declared command may re-execute itself as a different unprivileged user partway
through, so the runner cannot know in advance which user will own the report it
creates. Instead the runner samples the command's session while it runs and keeps
every user id it sees; the report's owner must be one of them (or the caller).

Sampling is best effort: a process that held a user id for less time than the
sampling interval is not seen. That fails closed (the report is refused with the
owner named), never open.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

_PROC = Path("/proc")
_INTERVAL = 0.001
_FULL_SCAN_EVERY = 4
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
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="uid-watcher", daemon=True)

    def start(self) -> None:
        self._sample(full=True)
        self._thread.start()

    def stop(self) -> frozenset[int]:
        self._stop.set()
        self._thread.join()
        return frozenset(self._uids)

    def _sample(self, *, full: bool) -> None:
        self._uids |= _uids_of(self._leader)
        if full:
            for pid in _session_members(self._leader):
                self._uids |= _uids_of(pid)

    def _run(self) -> None:
        tick = 0
        while not self._stop.is_set():
            self._sample(full=tick % _FULL_SCAN_EVERY == 0)
            tick += 1
            time.sleep(_INTERVAL)


__all__ = ["UidWatcher"]
