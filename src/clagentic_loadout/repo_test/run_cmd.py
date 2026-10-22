"""repo_test.run_cmd — ``clagentic-loadout test run``.

Runs the command the repository's profile declares for the requested mode,
records the run under the reports root, and returns the verb's exit code with the
JSON summary to print. Nothing here prints or exits; verb.py does.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clagentic_loadout.repo_test import exits, provenance, store
from clagentic_loadout.repo_test.junit import JunitError, JunitSummary, parse_junit
from clagentic_loadout.repo_test.runner import log_tail, run_command
from clagentic_loadout.repo_test.spec import (
    BASE_PLACEHOLDER,
    JUNIT_PLACEHOLDER,
    MODE_FILES,
    MODE_IMPACTED,
    MODE_TIER,
    TIER_NAME_RE,
    DeclaredTests,
)
from clagentic_loadout.review.atomic_io import ensure_private_dir, write_json_atomic
from clagentic_loadout.review.policy_source import (
    PROFILE_INVALID,
    PROFILE_MISSING,
    PROFILE_OK,
    PROFILE_PATH,
    read_repo_profile,
)
from clagentic_loadout.review.profile_verb import WorkingTreeReader

SCHEMA_VERSION = 1
_FAILURE_LOG_LINES = 40


class RunRefused(Exception):
    """The run cannot start. Carries the verb's exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RunOutcome:
    exit_code: int
    summary: dict[str, Any]
    failure_log_tail: str


def load_declared_tests(root: Path) -> DeclaredTests:
    """The ``test`` block of the profile in the working tree at *root*."""
    reader = WorkingTreeReader(root)
    profile, state, problem = read_repo_profile(reader, "working tree", set(reader.list_paths("")))
    if state == PROFILE_MISSING:
        raise RunRefused(
            f"{str(root)!r} has no repo profile at {PROFILE_PATH}; add one with a `test` block "
            "(see `loadout-profile init`)",
            exits.EXIT_PROFILE,
        )
    if state != PROFILE_OK or profile is None:
        detail = problem if state == PROFILE_INVALID else state
        raise RunRefused(f"the repo profile at {PROFILE_PATH} in {str(root)!r} is invalid: {detail}", exits.EXIT_PROFILE)
    if profile.test is None:
        raise RunRefused(
            f"the repo profile at {PROFILE_PATH} in {str(root)!r} declares no `test` block", exits.EXIT_NOT_DECLARED
        )
    return profile.test


def _resolve_files(root: Path, paths: Sequence[str]) -> list[str]:
    resolved_root = root.resolve()
    out: list[str] = []
    for raw in paths:
        if raw.startswith("-"):
            raise RunRefused(f"file argument {raw!r} looks like an option, not a path", exits.EXIT_USAGE)
        target = (root / raw).resolve()
        if not target.is_file():
            raise RunRefused(f"file {raw!r} is not a file under {str(resolved_root)!r}", exits.EXIT_USAGE)
        try:
            out.append(target.relative_to(resolved_root).as_posix())
        except ValueError:
            raise RunRefused(
                f"file {raw!r} resolves to {str(target)!r}, outside {str(resolved_root)!r}",
                exits.EXIT_USAGE,
            ) from None
    return out


def _result_counts(summary: JunitSummary | None) -> dict[str, int]:
    if summary is None:
        return {"collected": 0, "passed": 0, "failed": 0, "skipped": 0}
    return {
        "collected": summary.collected,
        "passed": summary.passed,
        "failed": summary.failed,
        "skipped": summary.skipped,
    }


def execute_run(
    *,
    mode: str,
    paths: Sequence[str],
    repo_root: str | Path,
    base: str | None,
    timeout_seconds: float | None,
    env: Mapping[str, str] | None = None,
    tier: str | None = None,
) -> RunOutcome:
    """Run the declared tests for *mode* and record the run. Mode ``tier`` runs
    the tier named by *tier*. Raises RunRefused for anything that stops the run
    before it starts."""
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise RunRefused(f"repo root {str(repo_root)!r} (resolved {str(root)!r}) is not a directory", exits.EXIT_USAGE)
    if (mode == MODE_TIER) != (tier is not None):
        raise RunRefused("`tier` needs a tier name, and no other mode takes one", exits.EXIT_USAGE)
    if tier is not None and TIER_NAME_RE.match(tier) is None:
        raise RunRefused(
            f"tier name {tier!r} is not valid: use a lowercase letter, then lowercase letters, "
            "digits, '-' or '_', at most 32 characters",
            exits.EXIT_USAGE,
        )
    if (mode == MODE_FILES) == (not paths):
        raise RunRefused(
            "`files` needs at least one path" if mode == MODE_FILES else f"`{mode}` takes no file paths",
            exits.EXIT_USAGE,
        )

    declared = load_declared_tests(root)
    if tier is not None:
        template = declared.tier_argv(tier)
        key_path = f"test.tiers.{tier}"
    else:
        template = declared.argv_for(mode)
        key_path = f"test.{mode}"
    if template is None:
        raise RunRefused(
            f"the repo profile in {str(root)!r} does not declare {key_path}; add it to run `{mode}`"
            + (f" {tier}" if tier is not None else ""),
            exits.EXIT_NOT_DECLARED,
        )

    base_ref = base if base is not None else (declared.base if mode == MODE_IMPACTED else None)
    base_sha: str | None = None
    if mode == MODE_IMPACTED and base_ref is None:
        raise RunRefused(
            f"`impacted` needs a base revision: pass --base, or set test.base in the profile in {str(root)!r}",
            exits.EXIT_NOT_DECLARED,
        )
    if base_ref is not None:
        base_sha = provenance.resolve_commit(root, base_ref)
        if base_sha is None:
            raise RunRefused(
                f"base revision {base_ref!r} does not name a commit in {str(root)!r}", exits.EXIT_USAGE
            )
    files = _resolve_files(root, paths) if mode == MODE_FILES else []

    reports = store.reports_root(env)
    run_id = store.new_run_id()
    run_dir = reports / run_id
    try:
        ensure_private_dir(reports)
        run_dir.mkdir(mode=0o700)
        write_json_atomic(run_dir / store.PROVENANCE_FILE, provenance.collect(root, base_sha))
    except OSError as exc:
        raise RunRefused(f"cannot record run {run_id} under {str(reports)!r}: {exc}", exits.EXIT_STORE_WRITE) from exc

    junit_path = run_dir / store.JUNIT_FILE
    log_path = run_dir / store.LOG_FILE
    argv = [
        part.replace(JUNIT_PLACEHOLDER, str(junit_path)).replace(BASE_PLACEHOLDER, base_sha or "")
        for part in template
    ] + files
    started = datetime.now(timezone.utc)
    outcome = run_command(argv, root, log_path, timeout_seconds)

    junit: JunitSummary | None = None
    problem = outcome.problem
    if problem is None:
        try:
            junit = parse_junit(junit_path)
        except JunitError as exc:
            problem = f"{exc} (the command exited {outcome.exit_code})"

    if junit is None:
        state, code = store.STATE_UNRUNNABLE, exits.EXIT_UNRUNNABLE
    else:
        state = store.STATE_COMPLETE
        failed = bool(junit.failing_ids) or outcome.exit_code != 0
        code = exits.EXIT_TESTS_FAILED if failed else exits.EXIT_OK
        if outcome.exit_code != 0 and not junit.failing_ids:
            problem = f"the command exited {outcome.exit_code} with no failing test in its report"

    record: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "run_id": run_id,
        "mode": mode,
        "tier": tier,
        "state": state,
        "problem": problem,
        "command": [*template, *files],
        "command_exit_code": outcome.exit_code,
        "counts": _result_counts(junit),
        "failing_ids": list(junit.failing_ids) if junit else [],
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        write_json_atomic(run_dir / store.RESULT_FILE, record)
    except OSError as exc:
        raise RunRefused(f"cannot record the result of run {run_id}: {exc}", exits.EXIT_STORE_WRITE) from exc

    printed = {key: value for key, value in record.items() if key != "failing_ids"}
    printed["failing_id_count"] = len(record["failing_ids"])
    tail = log_tail(log_path, _FAILURE_LOG_LINES) if state == store.STATE_UNRUNNABLE else ""
    return RunOutcome(exit_code=code, summary=printed, failure_log_tail=tail)


__all__ = ["RunOutcome", "RunRefused", "execute_run", "load_declared_tests"]
