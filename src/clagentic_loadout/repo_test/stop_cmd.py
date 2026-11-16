"""repo_test.stop_cmd — ``clagentic-loadout test stop``.

Ends a detached run that is still running: its supervisor and the test command's own
process group, which is a session of its own and so is not reached by ending the
supervisor's. The run then reads ``stopped``, with the time and the stopping user.

Only a process that is verified as the run's is signalled. The supervisor is checked
by its recorded identity (pid, start ticks, boot id: ``repo_test.liveness``); the
command by the identity its supervisor recorded in ``command-process.json`` or, when
that was not yet written, by being a direct child of the supervisor. Either must also
lead its own process group, so a recycled pid is never signalled. A process the
command moved out of its group (a daemon) is not reached.

A run that is not running (complete, unrunnable, interrupted, timed out or already
stopped) is reported as it stands and nothing is changed or signalled. Only the user
that owns the run's directory may stop it.
"""

from __future__ import annotations

import json
import os
import pwd
import signal
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clagentic_loadout.repo_test import exits, liveness, store
from clagentic_loadout.repo_test.store import RunRecord, StoreError

# How long to wait for the signalled processes to be gone before saying they are not.
_SETTLE_SECONDS = 5.0
_POLL_SECONDS = 0.05


class StopRefused(Exception):
    """The run cannot be stopped. Carries the verb's exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StopOutcome:
    run: RunRecord
    # True when this call ended the run; False when it was not running to begin with.
    ended_now: bool


def stopping_user() -> str:
    uid = os.getuid()
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid {uid}"


def _leads_own_group(pid: int) -> bool:
    try:
        return os.getpgid(pid) == pid
    except OSError:
        return False


def _recorded_command(run_dir: Path) -> Mapping[str, Any] | None:
    try:
        data = json.loads((run_dir / store.COMMAND_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _command_groups(run_dir: Path, supervisor_pid: int) -> list[Mapping[str, Any]]:
    """The identities of the commands the supervisor started that are still live and
    lead their own process group."""
    candidates = list(liveness.child_identities(supervisor_pid))
    recorded = _recorded_command(run_dir)
    if recorded is not None:
        candidates.append(recorded)
    found: list[Mapping[str, Any]] = []
    for identity in candidates:
        pid = identity.get("pid")
        if (
            isinstance(pid, int)
            and all(pid != seen["pid"] for seen in found)
            and liveness.is_alive(identity)
            and _leads_own_group(pid)
        ):
            found.append(identity)
    return found


def _end_group(identity: Mapping[str, Any]) -> None:
    """Kill the process group *identity* leads, if it is still that process: a pid
    that no longer matches its recorded identity may belong to an unrelated process
    and is never signalled."""
    pid = identity["pid"]
    if not liveness.is_alive(identity):
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise StopRefused(
            f"process {pid} of this run is not yours to signal: {exc}", exits.EXIT_REPO_MISMATCH
        ) from exc


def _gone(identities: list[Mapping[str, Any]]) -> bool:
    return not any(liveness.is_alive(identity) for identity in identities)


def stop_run(root: Path, run_id: str) -> StopOutcome:
    """Stop the run *run_id* under *root* if it is running. Raises StopRefused when the
    run is unknown, is another user's, cannot be ended, or its end cannot be recorded."""
    try:
        run = store.load_run(root, run_id, persist_interrupted=False)
    except StoreError as exc:
        raise StopRefused(str(exc), exits.EXIT_BAD_RUN_ID) from exc
    if run.state != store.STATE_RUNNING:
        return StopOutcome(run, False)
    run_dir = root / run_id
    try:
        owner = run_dir.stat().st_uid
    except OSError as exc:
        raise StopRefused(f"run {run_id}: cannot inspect {str(run_dir)!r}: {exc}", exits.EXIT_BAD_RUN_ID) from exc
    if owner != os.getuid():
        raise StopRefused(
            f"run {run_id} was started by another user (uid {owner}); only that user can stop it",
            exits.EXIT_REPO_MISMATCH,
        )
    process = run.provenance.get(store.PROCESS_KEY)
    supervisor = process if isinstance(process, dict) else {}
    supervisor_pid = supervisor.get("pid")
    if not isinstance(supervisor_pid, int):
        raise StopRefused(f"run {run_id} records no process to stop", exits.EXIT_REPO_MISMATCH)

    # The commands are found before the supervisor is ended: once it is gone its
    # children are reparented and no longer name it. The supervisor goes first so it
    # cannot record a result of its own for a command that is being ended.
    commands = _command_groups(run_dir, supervisor_pid)
    _end_group(supervisor)
    for command in commands:
        _end_group(command)

    gone = [supervisor, *commands]
    deadline = time.monotonic() + _SETTLE_SECONDS
    while not _gone(gone) and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
    if not _gone(gone):
        raise StopRefused(
            f"run {run_id}: its processes were signalled but are still running after "
            f"{_SETTLE_SECONDS:g} seconds",
            exits.EXIT_REPO_MISMATCH,
        )

    user = stopping_user()
    try:
        ended = store.record_end(
            run_dir,
            run_id,
            supervisor,
            store.STATE_STOPPED,
            f"stopped by {user} with `test stop`",
            stopped_by=user,
            stopped_at=datetime.now(timezone.utc).isoformat(),
        )
    except (OSError, StoreError) as exc:
        raise StopRefused(
            f"run {run_id} was ended but its record could not be written: {exc}", exits.EXIT_STORE_WRITE
        ) from exc
    return StopOutcome(ended, ended.state == store.STATE_STOPPED)


__all__ = ["StopOutcome", "StopRefused", "stop_run", "stopping_user"]
