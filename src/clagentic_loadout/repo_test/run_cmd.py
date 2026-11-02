"""repo_test.run_cmd — ``clagentic-loadout test run``.

Runs the command the repository's profile declares for the requested mode,
records the run under the reports root, and returns the verb's exit code with the
JSON summary to print. Nothing here prints or exits; verb.py does.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clagentic_loadout.repo_test import exits, provenance, store
from clagentic_loadout.repo_test.junit import ID_SCHEME, JunitError, JunitSummary, parse_junit
from clagentic_loadout.repo_test.repo_match import SHA_ARG_RE, repo_label, repo_matches
from clagentic_loadout.repo_test.report_scratch import (
    REFUSED,
    ReportUntrusted,
    collect_report,
    create_scratch,
    remove_scratch,
)
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
    """The run was refused, before it started or, when its result cannot be recorded,
    after it finished. Carries the verb's exit code."""

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


def check_expectation(recorded: Mapping[str, Any], expect_repo: str | None, expect_head: str | None) -> None:
    """Refuse when the provenance *recorded* for the tree about to be tested is not
    the repository or commit the caller expected. Runs before anything is created."""
    if expect_head is not None and SHA_ARG_RE.match(expect_head) is None:
        raise RunRefused(
            f"--expect-head must be 7 to 40 hex characters (a full SHA or an unambiguous prefix), got {expect_head!r}",
            exits.EXIT_USAGE,
        )
    repo = recorded.get("repo")
    repo = repo if isinstance(repo, dict) else {}
    if expect_repo is not None and not repo_matches(repo, expect_repo):
        raise RunRefused(
            f"expected repository {expect_repo!r} but this run would test {repo_label(repo)}; "
            "nothing was run or recorded",
            exits.EXIT_REPO_MISMATCH,
        )
    if expect_head is not None:
        head = recorded.get("head_sha")
        if not isinstance(head, str):
            raise RunRefused(
                f"expected head {expect_head!r} but the head of {repo_label(repo)} is unknown "
                "(not a git work tree, or git could not read it); nothing was run or recorded",
                exits.EXIT_REPO_MISMATCH,
            )
        if not head.startswith(expect_head.lower()):
            raise RunRefused(
                f"expected head {expect_head!r} but {repo_label(repo)} is at {head}; "
                "nothing was run or recorded",
                exits.EXIT_REPO_MISMATCH,
            )


def execute_run(
    *,
    mode: str,
    paths: Sequence[str],
    repo_root: str | Path,
    base: str | None,
    timeout_seconds: float | None,
    env: Mapping[str, str] | None = None,
    tier: str | None = None,
    expect_repo: str | None = None,
    expect_head: str | None = None,
    on_start: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> RunOutcome:
    """Run the declared tests for *mode* and record the run. Mode ``tier`` runs
    the tier named by *tier*. *expect_repo* and *expect_head* refuse the run when the
    tree is not that repository or commit. *on_start* is called with the run id and
    its provenance once the run is recorded, just before the command starts. Raises
    RunRefused before the run starts (usage, profile, expectation mismatch, store setup)
    and also after it has started when its result cannot be recorded; the caller still
    reports the run as finished in that case."""
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

    recorded = provenance.collect(root, base_sha)
    check_expectation(recorded, expect_repo, expect_head)

    reports = store.reports_root(env)
    run_id = store.new_run_id()
    run_dir = reports / run_id
    try:
        ensure_private_dir(reports)
        run_dir.mkdir(mode=0o700)
        write_json_atomic(run_dir / store.PROVENANCE_FILE, recorded)
    except OSError as exc:
        raise RunRefused(f"cannot record run {run_id} under {str(reports)!r}: {exc}", exits.EXIT_STORE_WRITE) from exc

    junit_path = run_dir / store.JUNIT_FILE
    log_path = run_dir / store.LOG_FILE
    try:
        scratch = create_scratch()
    except OSError as exc:
        raise RunRefused(f"cannot make a report directory for run {run_id}: {exc}", exits.EXIT_STORE_WRITE) from exc
    argv = [
        part.replace(JUNIT_PLACEHOLDER, str(scratch.junit_path)).replace(BASE_PLACEHOLDER, base_sha or "")
        for part in template
    ] + files
    started = datetime.now(timezone.utc)
    if on_start is not None:
        on_start(run_id, recorded)
    junit: JunitSummary | None = None
    report_verification: dict[str, Any] | None = None
    cleanup_problem: str | None = None
    try:
        outcome = run_command(argv, root, log_path, timeout_seconds)
        problem = outcome.problem
        if problem is None:
            # The caller's own ids are always the command's: a command that keeps
            # them never switched user.
            allowed = outcome.process_uids | {os.getuid(), os.geteuid()}
            try:
                report_verification = collect_report(scratch, junit_path, allowed)
                junit = parse_junit(junit_path)
            except ReportUntrusted as exc:
                report_verification = {"status": REFUSED, "reason": str(exc)}
                problem = f"{exc} (the command exited {outcome.exit_code})"
            except JunitError as exc:
                problem = f"{exc} (the command exited {outcome.exit_code})"
    finally:
        cleanup_problem = remove_scratch(scratch)
    if cleanup_problem is not None:
        problem = f"{problem}; {cleanup_problem}" if problem else cleanup_problem

    if junit is None:
        state, code = store.STATE_UNRUNNABLE, exits.EXIT_UNRUNNABLE
    else:
        state = store.STATE_COMPLETE
        failed = bool(junit.failing_ids) or outcome.exit_code != 0
        code = exits.EXIT_TESTS_FAILED if failed else exits.EXIT_OK
        if outcome.exit_code != 0 and not junit.failing_ids:
            exited = f"the command exited {outcome.exit_code} with no failing test in its report"
            problem = f"{exited}; {cleanup_problem}" if cleanup_problem else exited

    record: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "run_id": run_id,
        "mode": mode,
        "tier": tier,
        "state": state,
        "problem": problem,
        "command": [*template, *files],
        "command_exit_code": outcome.exit_code,
        "report_verification": report_verification,
        "counts": _result_counts(junit),
        "id_scheme": ID_SCHEME,
        "failing_ids": list(junit.failing_ids) if junit else [],
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        write_json_atomic(run_dir / store.RESULT_FILE, record)
    except OSError as exc:
        raise RunRefused(f"cannot record the result of run {run_id}: {exc}", exits.EXIT_STORE_WRITE) from exc

    printed = {key: value for key, value in record.items() if key != "failing_ids"}
    printed.update(repo=recorded.get("repo"), head_sha=recorded.get("head_sha"), dirty=recorded.get("dirty"))
    printed["failing_id_count"] = len(record["failing_ids"])
    tail = log_tail(log_path, _FAILURE_LOG_LINES) if state == store.STATE_UNRUNNABLE else ""
    return RunOutcome(exit_code=code, summary=printed, failure_log_tail=tail)


__all__ = ["RunOutcome", "RunRefused", "execute_run", "load_declared_tests"]
