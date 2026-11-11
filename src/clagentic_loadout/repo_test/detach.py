"""repo_test.detach — start a test run that outlives the caller.

``start_detached`` takes a run that ``run_cmd.prepare_run`` has already validated
and recorded. It starts a supervisor process in a new session, with no stdin and
its output going to the run's ``output.log``, so the supervisor survives the
caller exiting and the caller's process group being killed. It then adds a
``process`` object to ``provenance.json`` naming the supervisor (pid, start time,
boot id) so a reader can tell a run still going from one that died.

The supervisor is this module run with ``-m``. It does what a foreground run does
after its checks, through ``run_cmd.finish_run``: the same scratch directory, the
same user-id sampling of the command's session and the same report verification,
so a detached run is held to the trust checks a foreground run is.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clagentic_loadout.repo_test import exits, liveness, store
from clagentic_loadout.repo_test.run_cmd import PreparedRun, RunRefused, finish_run
from clagentic_loadout.repo_test.spec import REPORT_JUNIT
from clagentic_loadout.review.atomic_io import write_json_atomic

_SUPERVISOR_MODULE = "clagentic_loadout.repo_test.detach"


def _job_of(prepared: PreparedRun) -> str:
    return json.dumps(
        {
            "run_id": prepared.run_id,
            "run_dir": str(prepared.run_dir),
            "root": str(prepared.root),
            "mode": prepared.mode,
            "tier": prepared.tier,
            "template": list(prepared.template),
            "files": list(prepared.files),
            "prepare": list(prepared.prepare) if prepared.prepare else None,
            "base_sha": prepared.base_sha,
            "timeout_seconds": prepared.timeout_seconds,
            "report_format": prepared.report_format,
        }
    )


def _prepared_of(job: str) -> PreparedRun:
    data = json.loads(job)
    return PreparedRun(
        run_id=data["run_id"],
        run_dir=Path(data["run_dir"]),
        root=Path(data["root"]),
        mode=data["mode"],
        tier=data["tier"],
        template=tuple(data["template"]),
        files=tuple(data["files"]),
        prepare=tuple(data["prepare"]) if data.get("prepare") else None,
        base_sha=data["base_sha"],
        timeout_seconds=data["timeout_seconds"],
        report_format=data.get("report_format") or REPORT_JUNIT,
        # The supervisor prints no summary, so it has no use for the provenance.
        recorded={},
    )


def _kill_session(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def start_detached(prepared: PreparedRun) -> dict[str, Any]:
    """Start the supervisor for *prepared* and record it. Returns the summary to
    print: the run is ``running``. Raises RunRefused when the supervisor cannot be
    started or recorded."""
    log_path = prepared.run_dir / store.LOG_FILE
    try:
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", _SUPERVISOR_MODULE, _job_of(prepared)],
                # The run directory is private to the caller, so nothing planted in a
                # repository can be imported in place of the package.
                cwd=prepared.run_dir,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError as exc:
        raise RunRefused(
            f"cannot start the detached run {prepared.run_id}: {exc}", exits.EXIT_UNRUNNABLE
        ) from exc
    started_at = datetime.now(timezone.utc).isoformat()
    process = {
        **liveness.capture_identity(proc.pid),
        "mode": prepared.mode,
        "tier": prepared.tier,
        "command": [*prepared.template, *prepared.files],
        "started_at": started_at,
    }
    try:
        write_json_atomic(
            prepared.run_dir / store.PROVENANCE_FILE, {**prepared.recorded, store.PROCESS_KEY: process}
        )
    except OSError as exc:
        _kill_session(proc.pid)
        raise RunRefused(
            f"cannot record run {prepared.run_id} under {str(prepared.run_dir.parent)!r}: {exc}",
            exits.EXIT_STORE_WRITE,
        ) from exc
    recorded = prepared.recorded
    return {
        "run_id": prepared.run_id,
        "state": store.STATE_RUNNING,
        "mode": prepared.mode,
        "tier": prepared.tier,
        "repo": recorded.get("repo"),
        "head_sha": recorded.get("head_sha"),
        "dirty": recorded.get("dirty"),
        "pid": proc.pid,
        "started_at": started_at,
        "run_dir": str(prepared.run_dir),
    }


def main(argv: list[str] | None = None) -> int:
    """The supervisor: run one prepared run to its recorded result."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("repo_test.detach: expects one job argument", file=sys.stderr)
        return exits.EXIT_USAGE
    try:
        finish_run(_prepared_of(args[0]))
    except RunRefused as exc:
        print(f"clagentic-loadout test: {exc}", file=sys.stderr)
        return exc.code
    return exits.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
