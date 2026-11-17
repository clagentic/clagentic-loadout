"""repo_checks.run_cmd — run the entries of a profile's ``lint`` or ``build`` block.

Takes the block name, so the two verbs are one code path. The entries run in
declared order through ``push.verify_run.run_verify_entry``; the run stops at the
first entry that fails, times out or cannot start; one report is recorded per run.
Nothing here prints or exits: ``repo_checks.verb`` does.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clagentic_loadout.process_tree import raise_on_termination
from clagentic_loadout.push.verify_config import VerifyEntry
from clagentic_loadout.push.verify_run import VerifyResult, run_verify_entry
from clagentic_loadout.repo_checks import exits, store
from clagentic_loadout.repo_test import provenance
from clagentic_loadout.review.policy_source import (
    PROFILE_MISSING,
    PROFILE_OK,
    PROFILE_PATH,
    read_repo_profile,
)
from clagentic_loadout.review.profile_verb import WorkingTreeReader
from clagentic_loadout.verdict_record.record import utc_timestamp

SCHEMA_VERSION = 1

STATE_PASSED = "passed"
STATE_FAILED = "failed"
STATE_INTERRUPTED = "interrupted"


class RunRefused(Exception):
    """The run was refused before it started, or its report could not be
    recorded. Carries the verb's exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    state: str
    results: tuple[VerifyResult, ...]
    not_run: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        return exits.EXIT_OK if self.state == STATE_PASSED else exits.EXIT_CHECK_FAILED


def load_entries(root: Path, block: str) -> tuple[VerifyEntry, ...]:
    """The entries of *block* in the profile of the working tree at *root*.
    Raises RunRefused when the profile is invalid or declares none."""
    reader = WorkingTreeReader(root)
    profile, state, problem = read_repo_profile(reader, "working tree", set(reader.list_paths("")))
    if state == PROFILE_MISSING:
        raise RunRefused(
            f"{str(root)!r} has no repo profile at {PROFILE_PATH}, so no `{block}` block is declared "
            "(see `loadout-profile init`)",
            exits.EXIT_NOT_DECLARED,
        )
    if state != PROFILE_OK or profile is None:
        raise RunRefused(
            f"the repo profile at {PROFILE_PATH} in {str(root)!r} is invalid: {problem or state}",
            exits.EXIT_PROFILE_INVALID,
        )
    entries: tuple[VerifyEntry, ...] = getattr(profile, block)
    if not entries:
        raise RunRefused(
            f"the repo profile at {PROFILE_PATH} in {str(root)!r} declares no `{block}` entries",
            exits.EXIT_NOT_DECLARED,
        )
    return entries


def _select(entries: tuple[VerifyEntry, ...], block: str, only: str | None) -> tuple[VerifyEntry, ...]:
    if only is None:
        return entries
    chosen = tuple(entry for entry in entries if entry.name == only)
    if not chosen:
        raise RunRefused(
            f"--only {only!r} names no `{block}` entry; declared: {', '.join(e.name for e in entries)}",
            exits.EXIT_USAGE,
        )
    return chosen


def _entry_record(result: VerifyResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "argv": list(result.argv),
        "passed": result.passed,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "timeout_seconds": result.timeout_seconds,
        "start_error": result.start_error,
        "started": result.started,
        "finished": result.finished,
        "stdout_tail": result.stdout_tail,
        "stderr_tail": result.stderr_tail,
    }


def execute_run(
    *,
    block: str,
    repo_root: str | Path,
    only: str | None = None,
    env: Mapping[str, str] | None = None,
    on_start: Callable[[str, Mapping[str, Any]], None] | None = None,
    on_result: Callable[[VerifyResult], None] | None = None,
) -> RunOutcome:
    """Run the declared entries of *block* in *repo_root* and record the run.

    Raises RunRefused for a usage error, an invalid or empty profile block, or a
    run that cannot be recorded. A command-level failure is an outcome, not an
    exception."""
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise RunRefused(
            f"repo root {str(repo_root)!r} (resolved {str(root)!r}) is not a directory", exits.EXIT_USAGE
        )
    declared = load_entries(root, block)
    selected = _select(declared, block, only)
    recorded = provenance.collect(root)

    reports = store.reports_root(env)
    try:
        run_id, run_dir = store.create_run_dir(reports)
    except OSError as exc:
        raise RunRefused(f"cannot record a run under {str(reports)!r}: {exc}", exits.EXIT_STORE_WRITE) from exc
    if on_start is not None:
        on_start(run_id, recorded)

    started = utc_timestamp()
    results: list[VerifyResult] = []

    def record(state: str) -> RunOutcome:
        not_run = tuple(entry.name for entry in selected[len(results) :])
        report = {
            "schema": SCHEMA_VERSION,
            "run_id": run_id,
            "block": block,
            "state": state,
            "repo": recorded.get("repo"),
            "head_sha": recorded.get("head_sha"),
            "dirty": recorded.get("dirty"),
            "started_at": started,
            "finished_at": utc_timestamp(),
            "only": only,
            "declared": [entry.name for entry in declared],
            "entries": [_entry_record(result) for result in results],
            "not_run": list(not_run),
        }
        try:
            store.write_report(run_dir, report)
        except OSError as exc:
            raise RunRefused(
                f"run {run_id} finished ({state}) but its report could not be recorded in "
                f"{str(run_dir)!r}: {exc}",
                exits.EXIT_STORE_WRITE,
            ) from exc
        return RunOutcome(run_id, state, tuple(results), not_run)

    state = STATE_PASSED
    try:
        with raise_on_termination():
            for entry in selected:
                result = run_verify_entry(entry, root)
                results.append(result)
                if on_result is not None:
                    on_result(result)
                if not result.passed:
                    state = STATE_FAILED
                    break
    except (SystemExit, KeyboardInterrupt):
        # The entry's process tree is already ended; leave a report that says so,
        # and let the termination go on.
        with contextlib.suppress(RunRefused):
            record(STATE_INTERRUPTED)
        raise
    return record(state)


__all__ = [
    "STATE_FAILED",
    "STATE_INTERRUPTED",
    "STATE_PASSED",
    "RunOutcome",
    "RunRefused",
    "execute_run",
    "load_entries",
]
