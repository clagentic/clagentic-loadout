"""repo_test.store — where test runs are kept, and how they are read back.

One run is one directory, named by its run id, under the reports root::

    <reports root>/<run id>/provenance.json   what was run, against which commit
    <reports root>/<run id>/result.json       state, counts and failing test ids
    <reports root>/<run id>/junit.xml         the runner's own report
    <reports root>/<run id>/output.log        the runner's combined output

``result.json`` is written last, so a directory without one is a run that has not
finished and is invisible to every reader. A run id is ``YYYYMMDDTHHMMSSZ-<pid>``;
a reader never joins an id it was given onto a path before the id has matched that
grammar, and it refuses a run directory that is a symlink or does not resolve
directly under the reports root. The read functions here open nothing for write
and create nothing.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

STATE_COMPLETE = "complete"
STATE_UNRUNNABLE = "unrunnable"

_MAX_RECORD_BYTES = 8 * 1024 * 1024


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


def load_run(root: Path, run_id: str) -> RunRecord:
    """The finished run named *run_id*. *run_id* is untrusted: it must match the
    run id grammar before it touches a path."""
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
        if not result_path.is_file():
            raise StoreError(f"run {run_id} under {str(root)!r} has not finished (no {RESULT_FILE})")
    except OSError as exc:
        raise StoreError(f"run {run_id}: cannot inspect {str(candidate)!r}: {exc}") from exc
    result = _read_json_object(result_path, run_id)
    provenance_path = candidate / PROVENANCE_FILE
    provenance = _read_json_object(provenance_path, run_id) if provenance_path.is_file() else {}
    return RunRecord(run_id=run_id, result=result, provenance=provenance)


def list_runs(root: Path) -> list[RunRecord]:
    """Every finished, readable run under *root*, oldest first. Entries that are
    not run directories, are unfinished, or cannot be read are skipped. Raises
    StoreError when *root* itself is missing or unreadable."""
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
            runs.append(load_run(root, name))
        except StoreError:
            continue
    return runs


__all__ = [
    "CONFIG_KEY_REPORTS_DIR",
    "CONFIG_SECTION",
    "JUNIT_FILE",
    "LOG_FILE",
    "PROVENANCE_FILE",
    "REPORTS_DIR_ENV_VAR",
    "RESULT_FILE",
    "RUN_ID_RE",
    "STATE_COMPLETE",
    "STATE_UNRUNNABLE",
    "RunRecord",
    "StoreError",
    "list_runs",
    "load_run",
    "new_run_id",
    "reports_root",
    "run_sort_key",
]
