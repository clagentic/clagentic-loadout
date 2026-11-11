"""repo_test.store — where test runs are kept, and how they are read back.

One run is one directory, named by its run id, under the reports root::

    <reports root>/<run id>/provenance.json   what was run, against which commit
    <reports root>/<run id>/result.json       state, counts and failing test ids
    <reports root>/<run id>/junit.xml         the runner's own report
    <reports root>/<run id>/output.log        the runner's combined output
    <reports root>/<run id>/go-test.jsonl     the command's standard output, under report_format go-test-json
    <reports root>/<run id>/prepare.log       the prepare step's output, when one is declared

``result.json`` is written last, so a directory without one is a run that has not
finished and is invisible to ``latest`` and ``--sha``. A detached run also records a
``process`` object in ``provenance.json`` (see ``repo_test.detach``); an explicit
run id then opens it as ``running`` while that process is alive, or as ``interrupted``
when it is gone and left no result. The interrupted read is persisted once, as a
``result.json`` written atomically, so every later read agrees. A run id is ``YYYYMMDDTHHMMSSZ-<pid>``;
a reader never joins an id it was given onto a path before the id has matched that
grammar, and it refuses a run directory that is a symlink or does not resolve
directly under the reports root. Apart from that one interrupted record, the read
functions here open nothing for write and create nothing.

The reports root is, in order: the ``CLAGENTIC_LOADOUT_TEST_REPORTS_DIR``
environment variable, the user-level ``test.reports_dir`` setting, then
``<user config root>/state/test-reports``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from clagentic_loadout.repo_test import liveness
from clagentic_loadout.review.atomic_io import write_json_atomic
from clagentic_loadout.transport.provider_config import (
    DEFAULT_USER_CONFIG_ROOT,
    load_user_config_section,
)

REPORTS_DIR_ENV_VAR = "CLAGENTIC_LOADOUT_TEST_REPORTS_DIR"
CONFIG_SECTION = "test"
CONFIG_KEY_REPORTS_DIR = "reports_dir"

RUN_ID_RE = re.compile(r"^(\d{8}T\d{6}Z)-(\d{1,10})\Z")

PROVENANCE_FILE = "provenance.json"
RESULT_FILE = "result.json"
JUNIT_FILE = "junit.xml"
LOG_FILE = "output.log"
STREAM_FILE = "go-test.jsonl"
PREPARE_LOG_FILE = "prepare.log"

STATE_COMPLETE = "complete"
STATE_UNRUNNABLE = "unrunnable"
STATE_RUNNING = "running"
STATE_INTERRUPTED = "interrupted"
# States after which a run never changes.
TERMINAL_STATES = frozenset({STATE_COMPLETE, STATE_UNRUNNABLE, STATE_INTERRUPTED})

PROCESS_KEY = "process"
_RESULT_SCHEMA = 1

_MAX_RECORD_BYTES = 8 * 1024 * 1024

# How far ahead of the reader's clock a run may be dated before it counts as
# future-dated: enough for clock drift between the hosts sharing a reports root.
FUTURE_TOLERANCE = timedelta(minutes=5)


class StoreError(Exception):
    """A run or the reports root could not be resolved or read. The message names
    the id or path and what was found."""


@dataclass(frozen=True)
class RunRecord:
    """One finished run, as read back."""

    run_id: str
    result: Mapping[str, Any]
    provenance: Mapping[str, Any]

    @property
    def state(self) -> str:
        return str(self.result.get("state", ""))

    @property
    def head_sha(self) -> str | None:
        value = self.provenance.get("head_sha")
        return value if isinstance(value, str) and value else None

    @property
    def dirty(self) -> bool | None:
        value = self.provenance.get("dirty")
        return value if isinstance(value, bool) else None

    @property
    def repo(self) -> Mapping[str, Any]:
        value = self.provenance.get("repo")
        return value if isinstance(value, dict) else {}


def reports_root(
    env: Mapping[str, str] | None = None, *, config_root: str | Path | None = None
) -> Path:
    """The reports root. It is not created here."""
    environ = os.environ if env is None else env
    configured = environ.get(REPORTS_DIR_ENV_VAR, "").strip()
    if configured:
        return Path(configured).expanduser()
    section = load_user_config_section(CONFIG_SECTION, config_root=config_root)
    from_config = section.get(CONFIG_KEY_REPORTS_DIR)
    if isinstance(from_config, str) and from_config.strip():
        return Path(from_config.strip()).expanduser()
    root = Path(config_root) if config_root is not None else DEFAULT_USER_CONFIG_ROOT
    return root / "state" / "test-reports"


def new_run_id(now: datetime | None = None, pid: int | None = None) -> str:
    moment = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"{moment}-{os.getpid() if pid is None else pid}"


def run_sort_key(run_id: str) -> tuple[str, int]:
    """Orders run ids oldest first: by timestamp, then by process id."""
    match = RUN_ID_RE.match(run_id)
    if match is None:
        raise StoreError(f"{run_id!r} is not a run id")
    return match.group(1), int(match.group(2))


def run_id_time(run_id: str) -> datetime:
    """The UTC moment a run id encodes. Raises StoreError for an id that matches the
    grammar but is not a real date, such as month 13."""
    stamp, _ = run_sort_key(run_id)
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise StoreError(f"{run_id!r} encodes a date that does not exist") from exc


def _recorded_start(run: RunRecord) -> datetime | None:
    """The run's recorded start, or None when it recorded none. Raises ValueError
    when it recorded one that is not a timestamp."""
    value = run.result.get("started_at")
    if not isinstance(value, str):
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def dated_time(run: RunRecord) -> datetime | None:
    """The latest of the moments a run carries: its id time and its recorded start.
    None when either cannot be read, which makes the run undatable."""
    try:
        moments = [run_id_time(run.run_id)]
        started = _recorded_start(run)
    except (StoreError, ValueError):
        return None
    if started is not None:
        moments.append(started)
    return max(moments)


def is_future_dated(run: RunRecord, now: datetime | None = None) -> bool:
    """Whether the run is dated later than *now* plus FUTURE_TOLERANCE. A recorder
    with a skewed clock writes such a run, and its id would otherwise sort above
    every real run recorded after it. An undatable run is not future-dated."""
    moment = dated_time(run)
    if moment is None:
        return False
    reference = now if now is not None else datetime.now(timezone.utc)
    return moment > reference + FUTURE_TOLERANCE


def is_undatable(run: RunRecord) -> bool:
    """Whether the run's date cannot be read from its id or its recorded start."""
    return dated_time(run) is None


def _read_json_object(path: Path, run_id: str) -> dict[str, Any]:
    try:
        if path.stat().st_size > _MAX_RECORD_BYTES:
            raise StoreError(f"run {run_id}: {path.name} is larger than {_MAX_RECORD_BYTES} bytes")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreError(f"run {run_id}: cannot read {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise StoreError(f"run {run_id}: {path.name} is not a JSON object")
    return data


def _unfinished_result(run_id: str, process: Mapping[str, Any], state: str, problem: str | None) -> dict[str, Any]:
    """The result a run without a ``result.json`` reads as. ``counts`` is null: no
    test result exists, so none is invented."""
    return {
        "schema": _RESULT_SCHEMA,
        "run_id": run_id,
        "mode": process.get("mode"),
        "tier": process.get("tier"),
        "state": state,
        "problem": problem,
        "command": process.get("command"),
        "command_exit_code": None,
        "report_verification": None,
        "counts": None,
        "failing_ids": [],
        "started_at": process.get("started_at"),
        "finished_at": datetime.now(timezone.utc).isoformat() if state != STATE_RUNNING else None,
    }


def _open_unfinished(
    candidate: Path,
    run_id: str,
    provenance: dict[str, Any],
    process: Mapping[str, Any],
    persist: bool = True,
) -> RunRecord:
    """A detached run that has no ``result.json``: running while its process is
    alive, otherwise interrupted, and that is persisted unless *persist* is false
    (a listing reads the same state without writing)."""
    if liveness.is_alive(process):
        return RunRecord(run_id, _unfinished_result(run_id, process, STATE_RUNNING, None), provenance)
    problem = f"the run process (pid {process.get('pid')}) is gone and left no result"
    result = _unfinished_result(run_id, process, STATE_INTERRUPTED, problem)
    result_path = candidate / RESULT_FILE
    try:
        # A record only the owner may add to; another user's directory is read, not written.
        if persist and candidate.stat().st_uid == os.getuid() and not result_path.exists():
            write_json_atomic(result_path, result)
    except OSError:
        pass
    if result_path.is_file():
        # Another reader persisted the same fact first; agree with what is on disk.
        return RunRecord(run_id, _read_json_object(result_path, run_id), provenance)
    return RunRecord(run_id, result, provenance)


def load_run(
    root: Path, run_id: str, *, include_unfinished: bool = True, persist_interrupted: bool = True
) -> RunRecord:
    """The run named *run_id*. *run_id* is untrusted: it must match the run id
    grammar before it touches a path. A detached run without a result opens as
    running or interrupted unless *include_unfinished* is false; any other run
    without a result has not finished and is refused. An interrupted read is
    written down once unless *persist_interrupted* is false."""
    if RUN_ID_RE.match(run_id) is None:
        raise StoreError(
            f"{run_id!r} is not a run id (expected YYYYMMDDTHHMMSSZ-<pid>, or 'latest')"
        )
    candidate = root / run_id
    try:
        if candidate.is_symlink():
            raise StoreError(f"run {run_id}: {str(candidate)!r} is a symlink, refusing it")
        if not candidate.is_dir():
            raise StoreError(f"run {run_id} not found under reports root {str(root)!r}")
        if candidate.resolve().parent != root.resolve():
            raise StoreError(
                f"run {run_id}: {str(candidate)!r} does not resolve under {str(root)!r}"
            )
        result_path = candidate / RESULT_FILE
        provenance_path = candidate / PROVENANCE_FILE
        if not result_path.is_file():
            if include_unfinished and provenance_path.is_file():
                provenance = _read_json_object(provenance_path, run_id)
                process = provenance.get(PROCESS_KEY)
                if isinstance(process, dict):
                    return _open_unfinished(candidate, run_id, provenance, process, persist_interrupted)
            raise StoreError(f"run {run_id} under {str(root)!r} has not finished (no {RESULT_FILE})")
    except OSError as exc:
        raise StoreError(f"run {run_id}: cannot inspect {str(candidate)!r}: {exc}") from exc
    result = _read_json_object(result_path, run_id)
    provenance = _read_json_object(provenance_path, run_id) if provenance_path.is_file() else {}
    return RunRecord(run_id=run_id, result=result, provenance=provenance)


def list_runs(root: Path, *, include_unfinished: bool = False) -> list[RunRecord]:
    """Every finished, readable run under *root*, oldest first. Entries that are
    not run directories, are unfinished, or cannot be read are skipped. With
    *include_unfinished*, a detached run still running or interrupted is listed
    too, and nothing is written for it. Raises StoreError when *root* itself is
    missing or unreadable."""
    try:
        if not root.is_dir():
            raise StoreError(f"reports root {str(root)!r} does not exist (no test run has been recorded)")
        names = sorted(
            (entry.name for entry in root.iterdir() if RUN_ID_RE.match(entry.name)),
            key=run_sort_key,
        )
    except OSError as exc:
        raise StoreError(f"cannot read reports root {str(root)!r}: {exc}") from exc
    runs: list[RunRecord] = []
    for name in names:
        try:
            runs.append(
                load_run(root, name, include_unfinished=include_unfinished, persist_interrupted=False)
            )
        except StoreError:
            continue
    return runs


__all__ = [
    "CONFIG_KEY_REPORTS_DIR",
    "CONFIG_SECTION",
    "JUNIT_FILE",
    "LOG_FILE",
    "PREPARE_LOG_FILE",
    "PROVENANCE_FILE",
    "REPORTS_DIR_ENV_VAR",
    "RESULT_FILE",
    "STREAM_FILE",
    "RUN_ID_RE",
    "PROCESS_KEY",
    "STATE_COMPLETE",
    "STATE_INTERRUPTED",
    "STATE_RUNNING",
    "STATE_UNRUNNABLE",
    "TERMINAL_STATES",
    "FUTURE_TOLERANCE",
    "RunRecord",
    "StoreError",
    "dated_time",
    "is_future_dated",
    "is_undatable",
    "list_runs",
    "load_run",
    "new_run_id",
    "reports_root",
    "run_id_time",
    "run_sort_key",
]
