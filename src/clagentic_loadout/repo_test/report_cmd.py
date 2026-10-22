"""repo_test.report_cmd — ``clagentic-loadout test report``.

Read-only: picks one recorded run by id, by ``latest``, or by exact commit, and
answers with its summary, its failing test ids, or a comparison with another
run. It never writes, creates or deletes anything under the reports root.

Selecting a run:

  * ``<run-id>`` is untrusted input and must match the run id grammar.
  * ``latest`` is the newest finished run; ``--repo`` narrows it to one
    repository (a path, the repository's name, or its remote URL).
  * ``--sha`` selects the newest clean run recorded for exactly that commit (the
    full SHA, or an unambiguous prefix of 7 or more hex characters). It never
    falls back to ``latest``: with no match it refuses and names what exists. A
    run on a dirty tree, or one that recorded no commit, is never selected.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from clagentic_loadout.repo_test import exits, store
from clagentic_loadout.repo_test.spec import MODE_TIER
from clagentic_loadout.repo_test.store import RunRecord, StoreError

LATEST = "latest"
_SHA_ARG_RE = re.compile(r"\A[0-9a-fA-F]{7,40}\Z")
_LISTED_RUNS = 10

REPO_SAME = "same"
REPO_UNVERIFIED = "unverified"


class ReportRefused(Exception):
    """The report cannot be produced. Carries the verb's exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _repo_matches(run: RunRecord, wanted: str) -> bool:
    repo = run.repo
    identities = {repo.get("path"), repo.get("name"), repo.get("remote_url")}
    remote = repo.get("remote_url")
    if isinstance(remote, str):
        identities.add(remote.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1])
    if wanted in identities:
        return True
    try:
        return str(Path(wanted).resolve()) == repo.get("path")
    except OSError:
        return False


def _describe_runs(runs: Sequence[RunRecord]) -> str:
    shown = runs[-_LISTED_RUNS:]
    parts = []
    for run in shown:
        commit = run.head_sha[:12] if run.head_sha else "commit unknown"
        parts.append(f"{run.run_id} ({commit}{', dirty' if run.dirty else ''})")
    return ", ".join(parts) if parts else "no runs"


def _load_runs(root: Path, repo: str | None) -> list[RunRecord]:
    try:
        runs = store.list_runs(root)
    except StoreError as exc:
        raise ReportRefused(str(exc), exits.EXIT_NO_RUNS) from exc
    if repo is not None:
        runs = [run for run in runs if _repo_matches(run, repo)]
    if not runs:
        scope = f" for repository {repo!r}" if repo is not None else ""
        raise ReportRefused(f"no finished test run{scope} under reports root {str(root)!r}", exits.EXIT_NO_RUNS)
    return runs


def select_run(root: Path, target: str, *, repo: str | None, sha: str | None) -> RunRecord:
    """The run *target* names, subject to *repo* and *sha*."""
    if target != LATEST:
        if sha is not None or repo is not None:
            raise ReportRefused(
                "--repo and --sha select among runs; they cannot be combined with an explicit run id",
                exits.EXIT_USAGE,
            )
        try:
            return store.load_run(root, target)
        except StoreError as exc:
            raise ReportRefused(str(exc), exits.EXIT_BAD_RUN_ID) from exc
    runs = _load_runs(root, repo)
    if sha is None:
        return runs[-1]
    if _SHA_ARG_RE.match(sha) is None:
        raise ReportRefused(
            f"--sha must be 7 to 40 hex characters (a full SHA or an unambiguous prefix), got {sha!r}",
            exits.EXIT_USAGE,
        )
    wanted = sha.lower()
    matching = [
        run for run in runs if run.head_sha is not None and run.head_sha.startswith(wanted) and run.dirty is False
    ]
    commits = {run.head_sha for run in matching}
    if len(commits) > 1:
        raise ReportRefused(
            f"--sha {sha!r} is ambiguous, it matches {len(commits)} commits; use a longer prefix",
            exits.EXIT_SHA_NO_MATCH,
        )
    if not matching:
        raise ReportRefused(
            f"no clean test run recorded for commit {sha!r}; runs on record: {_describe_runs(runs)}",
            exits.EXIT_SHA_NO_MATCH,
        )
    return matching[-1]


def summary_of(run: RunRecord) -> dict[str, Any]:
    result = run.result
    return {
        "run_id": run.run_id,
        "state": run.state,
        "mode": result.get("mode"),
        "tier": _tier_name(run),
        "problem": result.get("problem"),
        "command": result.get("command"),
        "command_exit_code": result.get("command_exit_code"),
        "counts": result.get("counts"),
        "failing_id_count": len(_failing_ids(run)),
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
        "repo": run.provenance.get("repo"),
        "head_sha": run.head_sha,
        "dirty": run.dirty,
        "base_sha": run.provenance.get("base_sha"),
        "commit_known": run.head_sha is not None,
    }


def _tier_name(run: RunRecord) -> str | None:
    """The tier a run executed, or None for a non-tier run or a record written
    before tiers existed."""
    value = run.result.get("tier")
    return value if isinstance(value, str) and value else None


def _is_tier_run(run: RunRecord) -> bool:
    return run.result.get("mode") == MODE_TIER


def _require_like_tiers(head: RunRecord, base: RunRecord) -> None:
    """Refuse to compare a tier run with a different kind of run, or with a run
    of another tier: their failing ids answer different questions."""
    if _is_tier_run(head) != _is_tier_run(base):
        tier_run, other = (head, base) if _is_tier_run(head) else (base, head)
        raise ReportRefused(
            f"run {tier_run.run_id} is a run of tier {_tier_name(tier_run) or 'unknown'!r} but run "
            f"{other.run_id} is not a tier run; refusing to compare unlike runs",
            exits.EXIT_DIFF_REFUSED,
        )
    if _is_tier_run(head) and _tier_name(head) != _tier_name(base):
        raise ReportRefused(
            f"runs {head.run_id} and {base.run_id} are of different tiers "
            f"({_tier_name(head) or 'unknown'!r} and {_tier_name(base) or 'unknown'!r}); "
            "refusing to compare them",
            exits.EXIT_DIFF_REFUSED,
        )


def _failing_ids(run: RunRecord) -> list[str]:
    ids = run.result.get("failing_ids")
    return [item for item in ids if isinstance(item, str)] if isinstance(ids, list) else []


def failing_ids_of(run: RunRecord) -> list[str]:
    """The sorted failing ids of a complete run. An unrunnable run has no result
    to read ids from, so it is refused rather than reported as an empty set."""
    _require_complete(run, "its failing ids")
    return sorted(_failing_ids(run))


def _require_complete(run: RunRecord, what: str) -> None:
    if run.state != store.STATE_COMPLETE:
        raise ReportRefused(
            f"run {run.run_id} is {run.state or 'unreadable'}: it has no test results, so {what} "
            f"would be meaningless ({run.result.get('problem')})",
            exits.EXIT_DIFF_REFUSED,
        )


def _recorded(run: RunRecord, key: str) -> str | None:
    value = run.repo.get(key)
    return value if isinstance(value, str) and value else None


def _refuse_different(head: RunRecord, base: RunRecord, head_id: str, base_id: str) -> ReportRefused:
    return ReportRefused(
        f"runs {head.run_id} and {base.run_id} are from different repositories "
        f"({head_id} and {base_id}); refusing to compare them",
        exits.EXIT_DIFF_REFUSED,
    )


def _repo_check(head: RunRecord, base: RunRecord) -> str:
    """Whether two runs are the same repository; refuse only on proof they differ.

    Remotes are compared only when both runs recorded one. Otherwise resolved
    paths are compared: equal paths are the same repository, and differing paths
    prove a difference only when neither run has a remote (a remote on one side
    means the other may just be a clone elsewhere), so that case is unverified,
    as is any run with no path recorded.
    """
    head_remote, base_remote = _recorded(head, "remote_url"), _recorded(base, "remote_url")
    if head_remote is not None and base_remote is not None:
        if head_remote != base_remote:
            raise _refuse_different(head, base, head_remote, base_remote)
        return REPO_SAME
    head_path, base_path = _recorded(head, "path"), _recorded(base, "path")
    if head_path is None or base_path is None:
        return REPO_UNVERIFIED
    if head_path == base_path:
        return REPO_SAME
    if head_remote is not None or base_remote is not None:
        return REPO_UNVERIFIED
    raise _refuse_different(head, base, head_path, base_path)


def diff_of(head: RunRecord, base: RunRecord) -> dict[str, Any]:
    """Failing ids only in *head*, only in *base*, and how many are in both."""
    _require_complete(head, "a comparison")
    _require_complete(base, "a comparison")
    _require_like_tiers(head, base)
    repo_check = _repo_check(head, base)
    head_ids, base_ids = set(_failing_ids(head)), set(_failing_ids(base))
    return {
        "head_run": head.run_id,
        "base_run": base.run_id,
        "head_only": sorted(head_ids - base_ids),
        "base_only": sorted(base_ids - head_ids),
        "shared_count": len(head_ids & base_ids),
        "repo_check": repo_check,
    }


def load_base_run(root: Path, base_id: str) -> RunRecord:
    if base_id == LATEST:
        raise ReportRefused("--diff takes an explicit run id as the base, not 'latest'", exits.EXIT_USAGE)
    try:
        return store.load_run(root, base_id)
    except StoreError as exc:
        raise ReportRefused(str(exc), exits.EXIT_BAD_RUN_ID) from exc


__all__ = [
    "LATEST",
    "REPO_SAME",
    "REPO_UNVERIFIED",
    "ReportRefused",
    "diff_of",
    "failing_ids_of",
    "load_base_run",
    "select_run",
    "summary_of",
]
