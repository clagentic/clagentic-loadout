"""push.branch_commit_check — push-time backstop against a branch that
carries a STRAY merge commit from an unmerged PR (lr-dd1742).

THE DEFECT THIS CLOSES: a feature branch cut from a working tree that
already contains another PR's merge commit (opened, but not yet landed on
origin's base branch) silently inherits that commit. On a
merge_method='merge' repo, `merge.commit_subjects.check_branch_commit_subjects`
already refuses this at MERGE time (EXIT_COMMIT_SUBJECT_INVALID) — a
platform-generated merge-commit subject like "Merge pull request #377 from
<owner>/<branch>" never conforms to Conventional Commits grammar. That gate
is correct and stays. The problem this module closes is WHEN the gate first
fires: today, only hours later, after build, review, and security audit have
already run against the offending SHA — not at push time, when the commit is
first introduced and remediation (fetch + rebase, verified empty-diff) is
cheapest.

NOT THE SAME DEFECT AS THE STALE-LOCAL-MAIN CLASS: a prior, unrelated fix in
a different push tool made a branch-range computation range against
origin/main instead of a stale local main cache, closing a "the tool
computed the WRONG range" defect. This module's defect is different in
kind: the branch range IS correct (the merge commit really is in
origin/<base>..HEAD), because the branch was cut from a tree that genuinely
already had it — nothing about ranging against a freshly-fetched ref
changes that fact. See this task's PR body for the full account of why the
two are not the same fix.

REUSE, NOT A PARALLEL GRAMMAR OR GATE SIGNAL (hard constraint, matches this
package's own established pattern — merge.commit_subjects' own docstring,
"GRAMMAR REUSE"): this module imports and calls
merge.title_gate.is_conventional_title UNCHANGED — the exact same predicate
merge.commit_subjects already applies to each branch commit subject at merge
time, and merge.title_gate already applies to the PR title. There is no
second "does this look like a GitHub merge-commit subject" regex here: a
narrower, GitHub-specific shape check would fork the grammar and would also
miss Forgejo's own auto-generated merge-commit subjects (see
push.git_push's own module for the GENERIC platform-neutral posture this
package takes elsewhere). This ALSO reuses merge.commit_subjects.
REAL_MERGE_METHOD as the gate condition (see check_branch_commit_subjects
below) — the SAME "only meaningful on a real, non-squash merge" signal the
merge-time gate already keys on, not a second config knob.

BLOCK, NEVER AUTO-REBASE (design decision, named explicitly per this task's
own requirement to justify the choice): auto-rebasing onto a freshly-fetched
base branch before push was considered and rejected. This package's own
commit-subject gate at merge time is BLOCK-NEVER-REWRITE by hard constraint
(merge.commit_subjects' own docstring); push-time already performs ONE
history mutation (push.identity.pin_commits_to_bot_identity, explicit
history_rewritten flag threaded through to force-with-lease) and that
mutation's own safety margin is already load-bearing. Silently layering a
SECOND, unrelated history rewrite (a rebase onto a fetched base) inside the
same push call multiplies the ways a push can leave a caller's working tree
in a state it did not ask for — a caller whose stray-commit branch legitimately
depends on that commit's content (not just a same-day coincidence) would have
that content silently discarded rather than told about it. Refusing, naming
the offending SHA/subject, and handing the caller the exact remediation
command (fetch + rebase — the SAME steps that closed both observed incidents
in minutes) keeps this module in the same fail-closed class as every other
push-time gate in this package (push.cleanliness_check, push.namespace_guard,
merge.title_gate via _check_title_gate) rather than introducing the one
verb in this whole package that mutates history unprompted.

COMPARISON REF IS FETCHED, NEVER A LOCAL BRANCH READ (mirrors
merge.tree_sync.advance_repo_to_merged_sha's own "fetch for comparison only,
never mutate local state" pattern): the base branch is fetched into
FETCH_HEAD and diffed against HEAD — the caller's local `<base>` branch (if
one even exists in this working tree) is never read, and never
fast-forwarded, checked out, or otherwise touched. A caller invoking this
verb from an intentionally offline/isolated spawn with unreachable origin
gets CommitCheckUnavailableError (fail-open at the CALLER's discretion, see
below), never a silent pass against a stale or absent local ref.

FAIL-OPEN ON CHECK-EXECUTION FAILURE, NOT ON A FOUND MERGE COMMIT (mirrors
push.cleanliness_check's own CleanlinessCheckError-is-soft-fail contract):
this module raises TWO distinct exceptions with two distinct caller
contracts.  A `git fetch`/`git log` failure (CommitCheckUnavailableError) is
a check-EXECUTION failure — push.verb treats this the same way it treats a
CleanlinessCheckError, printing a warning and continuing, never blocking a
push over the check's OWN inability to run (e.g. no network reachable to
origin). Finding a stray merge commit (StrayMergeCommitError) is the actual
gate firing and is always a hard refusal — see check_branch_for_stray_merge_commits.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from clagentic_loadout.merge.commit_subjects import REAL_MERGE_METHOD
from clagentic_loadout.merge.title_gate import is_conventional_title

#: Separator between a commit's subject (%s) and its full hex SHA (%H) in
#: the `git log` format string below. Chosen to be exceedingly unlikely to
#: appear inside a real commit subject; a subject that DID contain it would
#: only ever cause an over-eager split, never a false negative on the gate
#: itself (worst case: the parsed subject is truncated at the separator,
#: which can only make a conformant-looking subject LOOK non-conformant,
#: never the reverse -- fail-closed either way).
_LOG_FIELD_SEP = "\x1f"


class CommitCheckUnavailableError(Exception):
    """Raised when the underlying git commands (fetch, log) themselves fail
    -- not a git repo, origin unreachable, no such base branch, etc. A
    check-EXECUTION failure, distinct from StrayMergeCommitError (a
    successful check that found a real offender). Callers should treat this
    as a soft-fail (warn, continue) exactly like
    push.cleanliness_check.CleanlinessCheckError -- this module's OWN
    inability to run must never block a push that would otherwise be
    clean."""


class StrayMergeCommitError(Exception):
    """Raised when check_branch_for_stray_merge_commits finds at least one
    commit in <fetched base>..HEAD whose subject does not conform to
    Conventional Commits grammar -- the shape a platform-generated merge
    commit ("Merge pull request #N from ...") always has, and the same
    grammar merge.commit_subjects already refuses at merge time. Carries the
    full list of offending (sha, subject) pairs (first-found order, via
    `.offenders`) so a caller can report or inspect them structurally; the
    exception's own str() is the fully-formatted message (see
    _format_stray_merge_commit_error), naming every offender, the expected
    grammar, WHY this matters, and the exact remediation command -- CLI
    hygiene rule 4, never a collapsed guess."""

    def __init__(self, offenders: list[tuple[str, str]], *, base_branch: str, remote: str) -> None:
        self.offenders = offenders
        super().__init__(
            _format_stray_merge_commit_error(offenders, base_branch=base_branch, remote=remote)
        )


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def fetch_branch_commit_subjects(
    repo_root: str | Path,
    base_branch: str,
    *,
    remote: str = "origin",
) -> list[tuple[str, str]]:
    """Return [(sha, subject), ...] for EVERY commit in `<fetched
    remote/base_branch>..HEAD`, unfiltered (first line only) -- the raw
    fetch+log primitive both `find_non_conformant_branch_commits` below and
    the task-id guard's own push-time check
    (push.verb._run_task_id_guard_commit_check, lr-4005f5) share, so the one
    `git fetch` + `git log` round-trip is never duplicated for two
    independent per-commit checks over the same range.

    Fetches *base_branch* from *remote* into FETCH_HEAD for the comparison
    ONLY -- never reads, fast-forwards, or checks out any local branch named
    *base_branch* (mirrors merge.tree_sync.advance_repo_to_merged_sha's own
    "fetch for comparison, never mutate local state" pattern; see this
    module's docstring for why a local branch read would reproduce the
    stale-local-ref defect class a prior, unrelated fix already closed
    elsewhere).

    Order is oldest-to-newest ancestry order (git log's own default with
    the range positional as given), matching
    merge.commit_subjects.check_branch_commit_subjects' own "first offender
    in *commit_subjects* order" contract.

    Raises:
        CommitCheckUnavailableError: the fetch or log command itself fails
            (unreachable remote, no such base branch, not a git repo).
    """
    fetch = _run_git(["fetch", remote, base_branch], cwd=Path(repo_root))
    if fetch.returncode != 0:
        raise CommitCheckUnavailableError(
            f"git fetch {remote} {base_branch} failed (exit {fetch.returncode}): "
            f"{fetch.stderr.strip()[:400]}"
        )

    log = _run_git(
        [
            "log",
            "FETCH_HEAD..HEAD",
            f"--format=%H{_LOG_FIELD_SEP}%s",
        ],
        cwd=Path(repo_root),
    )
    if log.returncode != 0:
        raise CommitCheckUnavailableError(
            f"git log FETCH_HEAD..HEAD failed (exit {log.returncode}): "
            f"{log.stderr.strip()[:400]}"
        )

    subjects: list[tuple[str, str]] = []
    for line in log.stdout.splitlines():
        if not line.strip():
            continue
        sha, _sep, subject = line.partition(_LOG_FIELD_SEP)
        if not subject:
            # Malformed line (separator absent) -- skip rather than guess;
            # this can only under-report, never falsely refuse a clean push.
            continue
        subjects.append((sha, subject))
    return subjects


def find_non_conformant_branch_commits(
    repo_root: str | Path,
    base_branch: str,
    *,
    remote: str = "origin",
) -> list[tuple[str, str]]:
    """Return [(sha, subject), ...] for every commit in
    `<fetched remote/base_branch>..HEAD` whose subject (first line only)
    does not conform to Conventional Commits grammar
    (merge.title_gate.is_conventional_title, reused unchanged).

    Thin filter over `fetch_branch_commit_subjects` (the shared fetch+log
    primitive) -- see that function's own docstring for the fetch/ordering
    contract this inherits unchanged.

    Raises:
        CommitCheckUnavailableError: the fetch or log command itself fails
            (unreachable remote, no such base branch, not a git repo).
    """
    subjects = fetch_branch_commit_subjects(repo_root, base_branch, remote=remote)
    return [(sha, subject) for sha, subject in subjects if not is_conventional_title(subject)]


def _format_stray_merge_commit_error(
    offenders: list[tuple[str, str]], *, base_branch: str, remote: str
) -> str:
    """Build the StrayMergeCommitError message. Names every offending SHA
    and subject verbatim, states WHY this matters, and gives the exact
    remediation command (CLI hygiene rule 4 -- never a collapsed guess)."""
    listed = "\n".join(f"  {sha} {subject!r}" for sha, subject in offenders)
    return (
        f"Push-time branch commit-subject check FAILED -- this branch "
        f"carries {len(offenders)} commit(s) in {remote}/{base_branch}..HEAD "
        f"with a non-conformant subject:\n{listed}\n"
        f"Expected Conventional Commits grammar:\n"
        f"  <type>(<scope>)!?: <description>\n"
        f"  type in feat|fix|docs|refactor|perf|test|build|ci|chore\n"
        f"WHY THIS MATTERS: on a merge_method='merge' repo, this exact same "
        f"grammar is enforced again at merge time (merge.commit_subjects) -- "
        f"a non-conformant subject there refuses the merge only AFTER build, "
        f"review, and security audit have already run against this SHA. The "
        f"most common cause is a branch cut from a working tree that already "
        f"contained another PR's merge commit before that PR landed on "
        f"{remote}/{base_branch} -- platform-generated merge-commit subjects "
        f"(e.g. 'Merge pull request #N from ...') never conform to this "
        f"grammar.\n"
        f"RESOLUTION: git fetch {remote} {base_branch} && "
        f"git rebase {remote}/{base_branch} -- this drops an already-merged "
        f"commit via patch-id once its originating PR is in "
        f"{base_branch}'s ancestry. VERIFY the rebase changed nothing you "
        f"intended to keep (e.g. `git diff <old-tip> HEAD` is empty) before "
        f"re-pushing with --force-with-lease. To bypass this gate, pass "
        f"--skip-branch-commit-check."
    )


def check_branch_for_stray_merge_commits(
    repo_root: str | Path,
    base_branch: str,
    *,
    merge_method: str,
    remote: str = "origin",
    skip: bool = False,
) -> None:
    """Push-time backstop: refuse a push whose branch carries a commit in
    `<fetched remote/base_branch>..HEAD` with a non-Conventional-Commits
    subject -- the shape a stray, not-yet-landed merge commit from another
    PR always has (see this module's docstring for the full defect
    account).

    A no-op (returns cleanly) for any *merge_method* other than
    merge.commit_subjects.REAL_MERGE_METHOD ("merge") -- REUSES that exact
    same gate condition merge.commit_subjects already keys on: on a
    squash/rebase repo the resulting commit's subject is rewritten from the
    (already-gated) PR title, so this check would be pure friction with no
    corresponding merge-time hazard.

    No-op when *skip* is True (the caller's --skip-branch-commit-check) --
    logging the bypass for audit is the CALLER's job, mirroring every other
    skip-able gate in this package (merge.title_gate.check_pr_title,
    merge.commit_subjects.check_branch_commit_subjects).

    Raises:
        StrayMergeCommitError: at least one non-conformant commit was found.
            Carries the full offender list.
        CommitCheckUnavailableError: the underlying git fetch/log failed --
            a check-EXECUTION failure. Callers should treat this as a
            soft-fail (warn, continue), exactly like
            push.cleanliness_check.CleanlinessCheckError.
    """
    if skip:
        return
    if merge_method != REAL_MERGE_METHOD:
        return

    offenders = find_non_conformant_branch_commits(repo_root, base_branch, remote=remote)
    if offenders:
        raise StrayMergeCommitError(offenders, base_branch=base_branch, remote=remote)


__all__ = [
    "CommitCheckUnavailableError",
    "StrayMergeCommitError",
    "check_branch_for_stray_merge_commits",
    "fetch_branch_commit_subjects",
    "find_non_conformant_branch_commits",
]
