"""repo_checks.store — where lint and build runs are kept, and how they are read back.

One run is one directory, named by its run id, under the reports root::

    <reports root>/<run id>/report.json

The run id has the grammar of a test run id (``YYYYMMDDTHHMMSSZ-<pid>``). The
reports root is, in order: the ``CLAGENTIC_LOADOUT_CHECK_REPORTS_DIR``
environment variable, the user-level ``checks.reports_dir`` setting, then
``<user config root>/state/check-reports``. A reader never joins an id it was
given onto a path before the id has matched the grammar, and it refuses a run
directory that is a symlink or does not resolve directly under the root.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from clagentic_loadout.artifact_store import StoreKind, resolve_root
from clagentic_loadout.repo_test.store import RUN_ID_RE, new_run_id
from clagentic_loadout.review.atomic_io import ensure_private_dir, write_json_atomic

KIND = StoreKind(
    label="check reports",
    env_var="CLAGENTIC_LOADOUT_CHECK_REPORTS_DIR",
    section="checks",
    key="reports_dir",
    default_subdir="check-reports",
)
REPORT_FILE = "report.json"

_MAX_REPORT_BYTES = 8 * 1024 * 1024
# A second run started in the same second by the same process takes the next
# second's id rather than colliding.
_ID_ATTEMPTS = 60


class RunNotFound(Exception):
    """The run id is malformed, names no run, or names a run with no report yet."""


class ReportUnreadable(Exception):
    """The run exists but its report cannot be read."""


def reports_root(env: Mapping[str, str] | None = None) -> Path:
    """The reports root. It is not created here."""
    return resolve_root(KIND, env)


def create_run_dir(root: Path) -> tuple[str, Path]:
    """Create the private directory of a new run under *root* (made if missing)
    and return ``(run id, directory)``. Raises OSError when it cannot be made."""
    ensure_private_dir(root)
    now = datetime.now(timezone.utc)
    for attempt in range(_ID_ATTEMPTS):
        run_id = new_run_id(now + timedelta(seconds=attempt))
        run_dir = root / run_id
        try:
            run_dir.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return run_id, run_dir
    raise FileExistsError(f"no free run id under {str(root)!r} after {_ID_ATTEMPTS} attempts")


def write_report(run_dir: Path, report: Mapping[str, Any]) -> None:
    """Write the report of the run in *run_dir*. Raises OSError on failure."""
    write_json_atomic(run_dir / REPORT_FILE, report)


def load_report(root: Path, run_id: str) -> dict[str, Any]:
    """The report of run *run_id*. *run_id* is untrusted."""
    if RUN_ID_RE.match(run_id) is None:
        raise RunNotFound(f"{run_id!r} is not a run id (expected YYYYMMDDTHHMMSSZ-<pid>)")
    candidate = root / run_id
    try:
        if candidate.is_symlink():
            raise ReportUnreadable(f"run {run_id}: {str(candidate)!r} is a symlink, refusing it")
        if not candidate.is_dir():
            raise RunNotFound(f"run {run_id} not found under the reports root {str(root)!r}")
        if candidate.resolve().parent != root.resolve():
            raise ReportUnreadable(f"run {run_id}: {str(candidate)!r} does not resolve under {str(root)!r}")
        path = candidate / REPORT_FILE
        if not path.is_file():
            raise RunNotFound(f"run {run_id} under {str(root)!r} recorded no report (it did not finish)")
        if path.stat().st_size > _MAX_REPORT_BYTES:
            raise ReportUnreadable(f"run {run_id}: {REPORT_FILE} is larger than {_MAX_REPORT_BYTES} bytes")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportUnreadable(f"run {run_id}: cannot read {REPORT_FILE}: {exc}") from exc
    if not isinstance(data, dict):
        raise ReportUnreadable(f"run {run_id}: {REPORT_FILE} is not a JSON object")
    return data


__all__ = [
    "KIND",
    "REPORT_FILE",
    "ReportUnreadable",
    "RunNotFound",
    "create_run_dir",
    "load_report",
    "reports_root",
    "write_report",
]
