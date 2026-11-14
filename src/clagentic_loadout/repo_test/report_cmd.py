"""repo_test.report_cmd — ``clagentic-loadout test report``.

Picks one recorded run by id, by ``latest``, or by exact commit, and answers with
its summary, its failing test ids, the failure records of its failed cases, or a
comparison with another run. It creates
and deletes nothing under the reports root; the one record it can write is the
``result.json`` of a detached run whose process is gone (state ``interrupted``,
see ``repo_test.store``).

Selecting a run:

  * ``<run-id>`` is untrusted input and must match the run id grammar.
  * ``latest`` is the newest finished run, or an exit 79 refusal naming the
    running run when the repository has only runs still in progress;
    ``--repo`` narrows it to one
    repository (a path, the repository's name, or its remote URL). Without
    ``--repo`` it is scoped to the git repository holding the current directory,
    or refused when the current directory is in none and the runs on record span
    more than one repository.
  * ``<run-id> --repo R`` asserts the run belongs to R and refuses when it does not.
  * ``--sha`` selects the newest clean run recorded for exactly that commit (the
    full SHA, or an unambiguous prefix of 7 or more hex characters). It never
    falls back to ``latest``: with no match it refuses and names what exists. A
    run on a dirty tree, or one that recorded no commit, is never selected.
  * A future-dated run (its id time or recorded start is later than now plus a
    few minutes) is never picked by ``latest`` or ``--sha``: a recorder with a
    skewed clock would otherwise hide every real run after it. Each one skipped
    is named once through *note*; when only future-dated runs match, the report
    is refused. An explicit run id still opens one, with the same note. No
    record is changed or removed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from clagentic_loadout.repo_test import exits, store
from clagentic_loadout.repo_test.failures import bound_failures
from clagentic_loadout.repo_test.go_test_json import GoStreamError
from clagentic_loadout.repo_test.go_test_json import parse_failed_cases as parse_go_failed_cases
from clagentic_loadout.repo_test.junit import ID_SCHEME, JunitError, parse_failed_cases
from clagentic_loadout.repo_test.provenance import toplevel
from clagentic_loadout.repo_test.repo_match import (
    SHA_ARG_RE,
    legacy_remote_text,
    recorded_remote,
    repo_label,
    repo_matches,
)
from clagentic_loadout.repo_test.spec import MODE_TIER, REPORT_FORMATS, REPORT_GO_TEST_JSON, REPORT_JUNIT
from clagentic_loadout.repo_test.store import RunRecord, StoreError

LATEST = "latest"
_LISTED_RUNS = 10

REPO_SAME = "same"
REPO_UNVERIFIED = "unverified"

ID_BASIS_COLLAPSED = "collapsed"


class ReportRefused(Exception):
    """The report cannot be produced. Carries the verb's exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _describe_runs(runs: Sequence[RunRecord]) -> str:
    shown = runs[-_LISTED_RUNS:]
    parts = []
    for run in shown:
        commit = run.head_sha[:12] if run.head_sha else "commit unknown"
        parts.append(f"{run.run_id} ({commit}{', dirty' if run.dirty else ''})")
    return ", ".join(parts) if parts else "no runs"


def _date_note(run: RunRecord, verb: str, now: datetime | None) -> str | None:
    """What to say about a run whose date makes it unfit for selection, if anything."""
    moment = store.dated_time(run)
    if moment is None:
        return f"{verb} run {run.run_id}: its date cannot be read"
    if store.is_future_dated(run, now):
        return f"{verb} run {run.run_id}: dated {moment.isoformat()}, later than now"
    return None


def _split_future(
    runs: list[RunRecord], now: datetime | None, note: Callable[[str], None] | None
) -> tuple[list[RunRecord], list[RunRecord], list[RunRecord]]:
    """*runs* without the future-dated and undatable ones, then those two groups.
    Each skipped run is named."""
    present: list[RunRecord] = []
    future: list[RunRecord] = []
    undatable: list[RunRecord] = []
    for run in runs:
        message = _date_note(run, "skipped", now)
        if message is None:
            present.append(run)
            continue
        (undatable if store.is_undatable(run) else future).append(run)
        if note is not None:
            note(message)
    return present, future, undatable


def _named(future: Sequence[RunRecord]) -> str:
    return ", ".join(run.run_id for run in future)


def _repo_key(run: RunRecord) -> str:
    return repo_label(run.repo) if run.repo else "unknown repository"


def _scoped_runs(
    runs: list[RunRecord],
    cwd: Path | None,
    note: Callable[[str], None] | None,
    subject: str = "`latest`",
    *,
    empty_ok: bool = False,
) -> tuple[list[RunRecord], str | None]:
    """Narrow *runs* when no ``--repo`` was given: to the repository holding *cwd*
    when it is inside a git work tree; otherwise refuse if the runs span more than
    one repository. Returns the runs and the repository they were narrowed to.
    With *empty_ok*, a git work tree with no run of its own yields no runs instead
    of a refusal, so the caller can look elsewhere before refusing."""
    top = toplevel(cwd) if cwd is not None else None
    if top is not None:
        scoped = [run for run in runs if run.repo.get("path") == top]
        if note is not None:
            note(f"{subject} is scoped to the repository at {top!r}; pass --repo to name another")
        if not scoped and empty_ok:
            return [], top
        if not scoped:
            raise ReportRefused(
                f"no finished test run for the repository at {top!r} under the reports root; "
                "pass --repo to name another repository",
                exits.EXIT_NO_RUNS,
            )
        return scoped, top
    found = sorted({_repo_key(run) for run in runs})
    if len(found) > 1:
        raise ReportRefused(
            f"the current directory is not in a git work tree and runs are recorded for {len(found)} "
            f"repositories ({'; '.join(found)}); pass --repo to name one",
            exits.EXIT_REPO_MISMATCH,
        )
    return runs, None


def _load_runs(
    root: Path,
    repo: str | None,
    cwd: Path | None,
    note: Callable[[str], None] | None,
    *,
    name_running: bool = True,
) -> list[RunRecord]:
    try:
        runs = store.list_runs(root, include_unfinished=True)
    except StoreError as exc:
        raise ReportRefused(str(exc), exits.EXIT_NO_RUNS) from exc
    # An interrupted run ended without a result, so it is never the answer to
    # `latest` or `--sha`; it is read by its run id.
    runs = [run for run in runs if run.state != store.STATE_INTERRUPTED]
    finished = [run for run in runs if run.state != store.STATE_RUNNING]
    running = [run for run in runs if run.state == store.STATE_RUNNING]
    top: str | None = None
    if repo is not None:
        finished = [run for run in finished if repo_matches(run.repo, repo)]
        running = [run for run in running if repo_matches(run.repo, repo)]
    elif runs:
        # Scoping, and its ambiguity check, sees finished runs only: a run still in
        # progress must not change which finished run answers.
        finished, top = _scoped_runs(finished, cwd, note, empty_ok=True)
    if not finished:
        # A run still in progress is not a missing one: say so, so the caller waits
        # instead of concluding there is nothing to read.
        if repo is None and running:
            running, _ = _scoped_runs(running, cwd, None, empty_ok=True)
        if running and name_running:
            newest = running[-1]
            started = newest.result.get("started_at") or "an unrecorded time"
            raise ReportRefused(
                f"no finished test run yet: run {newest.run_id} is still running (started {started}); "
                "read it again when it ends, or pass --wait",
                exits.EXIT_STILL_RUNNING,
            )
        if top is not None:
            raise ReportRefused(
                f"no finished test run for the repository at {top!r} under the reports root; "
                "pass --repo to name another repository",
                exits.EXIT_NO_RUNS,
            )
        scope = f" for repository {repo!r}" if repo is not None else ""
        raise ReportRefused(f"no finished test run{scope} under reports root {str(root)!r}", exits.EXIT_NO_RUNS)
    return finished


def select_run(
    root: Path,
    target: str,
    *,
    repo: str | None,
    sha: str | None,
    cwd: Path | None = None,
    note: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> RunRecord:
    """The run *target* names, subject to *repo* and *sha*. With an explicit run id,
    *repo* is an assertion about that run. For ``latest`` without *repo*, *cwd*
    scopes the choice (see ``_scoped_runs``) and *note* receives what it did. *now*
    is the reader's clock, injectable for tests."""
    if target != LATEST:
        if sha is not None:
            raise ReportRefused(
                "--sha selects among runs; it cannot be combined with an explicit run id",
                exits.EXIT_USAGE,
            )
        try:
            run = store.load_run(root, target)
        except StoreError as exc:
            raise ReportRefused(str(exc), exits.EXIT_BAD_RUN_ID) from exc
        if repo is not None and not repo_matches(run.repo, repo):
            raise ReportRefused(
                f"run {run.run_id} was recorded for {_repo_key(run)}, not the expected repository {repo!r}",
                exits.EXIT_REPO_MISMATCH,
            )
        message = _date_note(run, "opening", now)
        if note is not None and message is not None:
            note(message)
        return run
    all_runs = _load_runs(root, repo, cwd, note, name_running=sha is None)
    runs, future, undatable = _split_future(all_runs, now, note)
    if sha is None:
        if not runs:
            raise ReportRefused(
                "every finished test run was skipped, as dated later than now or with a date that "
                f"cannot be read; skipped: {_named([*future, *undatable])}",
                exits.EXIT_NO_RUNS,
            )
        return runs[-1]
    if SHA_ARG_RE.match(sha) is None:
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
        skipped = f"; skipped future-dated: {_named(future)}" if future else ""
        skipped += f"; skipped undatable: {_named(undatable)}" if undatable else ""
        raise ReportRefused(
            f"no clean test run recorded for commit {sha!r}; runs on record: {_describe_runs(runs)}{skipped}",
            exits.EXIT_SHA_NO_MATCH,
        )
    return matching[-1]


def summary_of(run: RunRecord) -> dict[str, Any]:
    result = run.result
    # bool is an int subclass; a hand-edited true must not read as scheme 1.
    id_scheme = result.get("id_scheme")
    summary: dict[str, Any] = {
        "run_id": run.run_id,
        "state": run.state,
        "mode": result.get("mode"),
        "tier": _tier_name(run),
        "problem": result.get("problem"),
        "command": result.get("command"),
        "command_exit_code": result.get("command_exit_code"),
        "counts": result.get("counts"),
        "id_scheme": id_scheme if isinstance(id_scheme, int) and not isinstance(id_scheme, bool) else None,
        "report_verification": result.get("report_verification"),
        "failing_id_count": len(_failing_ids(run)),
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
        "repo": run.provenance.get("repo"),
        "head_sha": run.head_sha,
        "dirty": run.dirty,
        "base_sha": run.provenance.get("base_sha"),
        "commit_known": run.head_sha is not None,
    }
    id_basis = result.get("id_basis")
    if isinstance(id_basis, str) and id_basis:
        summary["id_basis"] = id_basis
    if report_format_of(run) != REPORT_JUNIT:
        summary["report_format"] = report_format_of(run)
    note = _failing_id_note(run)
    if note is not None:
        summary["failing_id_note"] = note
    prepare = result.get("prepare")
    if isinstance(prepare, dict):
        summary["prepare"] = prepare
    tail = result.get("failure_log_tail")
    if run.state == store.STATE_UNRUNNABLE and isinstance(tail, str) and tail:
        summary["failure_log_tail"] = tail
    return summary


def date_mark(run: RunRecord, now: datetime | None = None) -> str | None:
    """Why a run's date makes it unfit for `latest`, or None when it is fit."""
    if store.is_undatable(run):
        return "undatable"
    if store.is_future_dated(run, now):
        return "future-dated"
    return None


def list_of(
    root: Path,
    *,
    repo: str | None,
    limit: int,
    cwd: Path | None = None,
    note: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """One row per recorded run of the scoped repository, newest first by run id,
    at most *limit*. Every state is listed, including those `latest` skips
    (running, interrupted, unrunnable, future-dated, undatable); a run whose date
    is unfit has ``date_mark`` set instead of being left out. Writes nothing."""
    try:
        runs = store.list_runs(root, include_unfinished=True)
    except StoreError as exc:
        raise ReportRefused(str(exc), exits.EXIT_NO_RUNS) from exc
    if repo is not None:
        runs = [run for run in runs if repo_matches(run.repo, repo)]
    elif runs:
        runs, _ = _scoped_runs(runs, cwd, note, subject="the run list")
    if not runs:
        scope = f" for repository {repo!r}" if repo is not None else ""
        raise ReportRefused(f"no test run{scope} under reports root {str(root)!r}", exits.EXIT_NO_RUNS)
    rows = []
    for run in reversed(runs[-limit:]):
        result = run.result
        rows.append(
            {
                "run_id": run.run_id,
                "state": run.state,
                "mode": result.get("mode"),
                "tier": _tier_name(run),
                "head_sha": run.head_sha,
                "dirty": run.dirty,
                "started_at": result.get("started_at"),
                "finished_at": result.get("finished_at"),
                "command_exit_code": result.get("command_exit_code"),
                "counts": result.get("counts"),
                "date_mark": date_mark(run, now),
            }
        )
    return rows


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


def failures_of(root: Path, run: RunRecord) -> list[dict[str, Any]]:
    """Bounded, redacted records of the failed cases of a complete run, read from
    the junit report recorded with it. A running run exits 79, an interrupted or
    unrunnable one is refused, and so is one whose report cannot be read."""
    _require_complete(run, "its failures")
    go_stream = report_format_of(run) == REPORT_GO_TEST_JSON
    report_path = root / run.run_id / (store.STREAM_FILE if go_stream else store.JUNIT_FILE)
    if report_path.is_symlink():
        raise ReportRefused(
            f"run {run.run_id}: {str(report_path)!r} is a symlink, refusing it", exits.EXIT_DIFF_REFUSED
        )
    try:
        cases = parse_go_failed_cases(report_path) if go_stream else parse_failed_cases(report_path)
    except (JunitError, GoStreamError) as exc:
        what = "go test event stream" if go_stream else "JUnit report"
        raise ReportRefused(
            f"run {run.run_id}: its recorded {what} cannot be read: {exc}", exits.EXIT_DIFF_REFUSED
        ) from exc
    return bound_failures(cases)


def report_format_of(run: RunRecord) -> str:
    """The report format a run was recorded with; a record without the key is junit."""
    value = run.result.get("report_format")
    return value if value in REPORT_FORMATS else REPORT_JUNIT


def is_terminal(run: RunRecord) -> bool:
    """Whether the run has ended, so reading it again will not change the answer."""
    return run.state in store.TERMINAL_STATES


def _require_complete(run: RunRecord, what: str) -> None:
    if run.state == store.STATE_RUNNING:
        raise ReportRefused(
            f"run {run.run_id} is still running: it has no test results yet, so {what} is not available",
            exits.EXIT_STILL_RUNNING,
        )
    if run.state != store.STATE_COMPLETE:
        raise ReportRefused(
            f"run {run.run_id} is {run.state or 'unreadable'}: it has no test results, so {what} "
            f"would be meaningless ({run.result.get('problem')})",
            exits.EXIT_DIFF_REFUSED,
        )


def _recorded(run: RunRecord, key: str) -> str | None:
    value = run.repo.get(key)
    return value if isinstance(value, str) and value else None


def _remote_shown(run: RunRecord) -> str:
    parts = recorded_remote(run.repo)
    return "/".join(parts) if parts else "an unparsed remote"


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
    head_legacy, base_legacy = legacy_remote_text(head.repo), legacy_remote_text(base.repo)
    if head_legacy is not None and base_legacy is not None:
        # Two records of the older form compare as they always did, on the text.
        head_remote, base_remote = head_legacy, base_legacy
    else:
        head_remote, base_remote = recorded_remote(head.repo), recorded_remote(base.repo)
    if head_remote is not None and base_remote is not None:
        if head_remote != base_remote:
            # Parsed parts only: legacy text may carry what the parts never do.
            raise _refuse_different(head, base, _remote_shown(head), _remote_shown(base))
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
    head_ids, base_ids = Counter(_failing_ids(head)), Counter(_failing_ids(base))
    # A run recorded before per-case entries holds one entry per name however many
    # cases failed under it, so its counts are not comparable with a new run's.
    # Compare presence only (every count becomes 1) and say so.
    collapsed = _has_distinct_ids(head) != _has_distinct_ids(base)
    if collapsed:
        head_ids, base_ids = Counter(set(head_ids)), Counter(set(base_ids))
    # Multiset difference: an id that failed more often at head than at base is
    # head_only once per extra failure, so a new failure cannot hide behind an
    # existing one that shares its id.
    diff: dict[str, Any] = {
        "head_run": head.run_id,
        "base_run": base.run_id,
        "head_only": sorted((head_ids - base_ids).elements()),
        "base_only": sorted((base_ids - head_ids).elements()),
        "shared_count": sum((head_ids & base_ids).values()),
        "repo_check": repo_check,
    }
    head_format, base_format = report_format_of(head), report_format_of(base)
    if head_format != base_format:
        # Ids of unlike formats are built differently, so a shared id is not
        # evidence of the same test; the diff is still printed.
        diff["report_format_note"] = (
            f"run {head.run_id} is {head_format} and run {base.run_id} is {base_format}: "
            "ids are not comparable across report formats"
        )
    if collapsed:
        diff["id_basis"] = ID_BASIS_COLLAPSED
        diff["id_basis_reason"] = (
            "one run was recorded before failing ids were kept per failed case, "
            "so failures that share an id are compared as one; a repeat failure of "
            "an id both runs list cannot be seen"
        )
    return diff


def _has_distinct_ids(run: RunRecord) -> bool:
    return run.result.get("id_scheme") == ID_SCHEME


def _failing_id_note(run: RunRecord) -> str | None:
    """Why a run's failed count and failing id count differ, or None when they
    do not. Only a run recorded before ids were made distinct can differ."""
    counts = run.result.get("counts")
    failed = counts.get("failed") if isinstance(counts, dict) else None
    if not isinstance(failed, int) or _has_distinct_ids(run) or failed == len(_failing_ids(run)):
        return None
    return (
        f"{failed - len(_failing_ids(run))} failed test case(s) share an id with another failed case "
        "and were recorded as one entry; this run predates per-case failing ids"
    )


def load_base_run(
    root: Path,
    base_id: str,
    *,
    note: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> RunRecord:
    if base_id == LATEST:
        raise ReportRefused("--diff takes an explicit run id as the base, not 'latest'", exits.EXIT_USAGE)
    try:
        base = store.load_run(root, base_id)
    except StoreError as exc:
        raise ReportRefused(str(exc), exits.EXIT_BAD_RUN_ID) from exc
    message = _date_note(base, "opening", now)
    if note is not None and message is not None:
        note(message)
    return base


__all__ = [
    "LATEST",
    "REPO_SAME",
    "REPO_UNVERIFIED",
    "ReportRefused",
    "date_mark",
    "diff_of",
    "failing_ids_of",
    "failures_of",
    "is_terminal",
    "list_of",
    "load_base_run",
    "select_run",
    "summary_of",
]
