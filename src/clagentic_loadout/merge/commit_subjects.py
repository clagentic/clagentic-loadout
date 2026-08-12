"""merge.commit_subjects — merge-time backstop validating EACH branch commit
subject introduced by a PR (base..head) against the SAME Conventional
Commits grammar merge.title_gate already enforces on the PR title (lr-835c57).

ROOT CAUSE: on a merge_method='merge' (real, non-squash) repo, semantic-
release (Angular preset) parses the INDIVIDUAL BRANCH COMMIT SUBJECTS that
land in the merge, not the PR title. The title is promoted verbatim into the
merge COMMIT MESSAGE (merge.title_gate's own docstring), but that promoted
text is invisible to semantic-release on a real merge — it only ever reads
each parent commit's own subject line. A PR whose title passes
merge.title_gate but whose branch carries an 'lr-XXXX: <desc>' (ID-leading,
no type) commit subject therefore still yields next-release=none and
silently stops beta cuts, even though the title gate reported clean.

SQUASH IS A NO-OP HERE (by design, not an oversight): on a squash merge,
GitHub/Forgejo both rewrite the single resulting commit's subject FROM the PR
title — the exact string merge.title_gate already validated. Re-checking
branch commit subjects on a squash repo would just repeat that same title
check against text that never lands verbatim. This module's caller (merge.
verb) is expected to pass merge_method verbatim from the resolved backend/
repo config (the same "allow_squash vs merge_style='merge'" source the
backends already read for merge_pr's own merge_method parameter) and this
module no-ops for ANY value other than "merge".

BLOCK, NEVER REWRITE (hard constraint, mirrors TitleInvalidError's own
fail-closed posture): check_branch_commit_subjects raises
CommitSubjectInvalidError naming the offending SHA + subject + expected
grammar, then the caller bounces the merge. It NEVER rewrites, amends, or
otherwise normalizes a commit subject at merge time -- doing so would change
history a security/code reviewer already reviewed and reasoned about, and
would break the merge gate's own review-SHA invariant (merge.stale_sha
compares --expected-head-sha against the PR's CURRENT head; a merge-time
history rewrite would silently invalidate every reviewer verdict's SHA
stamp). Proactive normalization (so this backstop rarely fires at all)
belongs at authoring/push time instead -- this module is the DEFENSIVE last
resort, reached only when authoring- and push-time normalization both
failed.

GRAMMAR REUSE (hard constraint): this module never forks or re-implements the
grammar. is_conventional_title (merge.title_gate) is the SINGLE source of
truth, imported and called unchanged here — a subject conforms iff the exact
same CONVENTIONAL_COMMITS_RE the PR-title gate uses matches it.

BYPASS (mirrors --skip-title-check exactly): the caller passes skip=True
(--skip-commit-check at the CLI layer) for an automation PR whose commits
cannot be changed after the fact. Logging the bypass for audit is the
CALLER's job (same contract as merge.title_gate.check_pr_title's skip
parameter) -- this module only enforces or no-ops.

THIS IS A DECISION-LAYER MODULE, not a transport (mirrors merge.diff_scope /
merge.ci_status's own split): it takes an already-fetched sequence of (sha,
subject) pairs -- the git-host compare-API call that produces them is each
backend's own job (merge.forgejo_backend.fetch_branch_commit_subjects /
merge.github_backend.fetch_branch_commit_subjects, both reusing the SAME
base..head compare response the merge_pr 405-disambiguation path already
fetches wherever that information is already on hand, per this task's own
"no extra round-trip" constraint) -- so this module has no transport/
credential coupling and is trivially unit-testable against synthetic subject
lists.

merge_method IS THE GATE (hard constraint): this function no-ops for any
merge_method other than the literal string "merge" -- resolving what
merge_method actually applies to a given repo/PR (config's allow_squash /
merge_style, or a CLI override) is the caller's job, identical to how
merge.forgejo_backend / merge.github_backend already resolve their own
merge_pr merge_method today; this module never re-derives it.
"""

from __future__ import annotations

from clagentic_loadout.merge.errors import CommitSubjectInvalidError
from clagentic_loadout.merge.title_gate import is_conventional_title
from clagentic_loadout.task_id_guard import TaskIdGuardViolation, check_task_id_guard

#: The one merge_method value this backstop actually gates on. Any other
#: value (squash, rebase, or an integrator's own custom label) is a no-op --
#: see this module's docstring, "SQUASH IS A NO-OP HERE".
REAL_MERGE_METHOD = "merge"


def _format_commit_subject_error(
    sha: str, subject: str, *, pr_number: int, owner: str, repo: str
) -> str:
    """Build the CommitSubjectInvalidError message. Names the offending SHA
    and subject verbatim and reports the full expected grammar (CLI hygiene
    rule 4) -- never a collapsed guess."""
    return (
        f"Branch commit subject gate FAILED — PR #{pr_number} in "
        f"{owner}/{repo} introduces commit {sha} with a non-conformant "
        f"subject: {subject!r}\n"
        f"Expected Conventional Commits grammar:\n"
        f"  <type>(<scope>)!?: <description>\n"
        f"  type in feat|fix|docs|refactor|perf|test|build|ci|chore\n"
        f"  scope = optional parenthesised subsystem (e.g. '(auth)')\n"
        f"  !     = optional breaking-change marker before the colon\n"
        f"  description = at least one character after ': '\n"
        f"Example valid subjects:\n"
        f"  feat(auth): add PR title gate to the merge verb\n"
        f"  fix!: correct stale-SHA check order\n"
        f"WHY THIS MATTERS: on a merge_method='merge' repo, semantic-release "
        f"parses EACH branch commit's own subject, not the PR title -- a "
        f"non-conformant subject here yields next-release=none and silently "
        f"stops beta cuts even when the PR title itself is conformant.\n"
        f"RESOLUTION: this gate BLOCKS, it never rewrites history -- amend "
        f"the offending commit's subject on the branch (e.g. `git commit "
        f"--amend` / an interactive rebase) to match the grammar, force-push "
        f"the branch, then retry. To bypass this gate (e.g. an automation PR "
        f"whose commits cannot be changed), pass --skip-commit-check."
    )


def check_branch_commit_subjects(
    commit_subjects: list[tuple[str, str]],
    pr_number: int,
    owner: str,
    repo: str,
    *,
    merge_method: str,
    skip: bool = False,
    task_id_guard_pattern: str | None = None,
    task_id_guard_mode: str = "off",
) -> list[str]:
    """Assert every branch commit subject introduced by the PR (base..head)
    conforms to Conventional Commits grammar -- the SAME predicate
    merge.title_gate.is_conventional_title already enforces on the PR title.

    *commit_subjects* is an already-fetched ordered sequence of (sha,
    subject) pairs for the commits the PR introduces (base..head) -- fetch
    it via the resolved backend's fetch_branch_commit_subjects. *subject*
    here means the commit's FIRST LINE only (a multi-line commit message's
    body never enters the grammar check — matching the PR-title check, which
    only ever sees a single-line title).

    *merge_method* MUST be the RESOLVED merge method for this repo/PR (from
    repo/gate config -- allow_squash vs merge_style='merge' -- never guessed
    or re-derived here). This function is a no-op (returns cleanly) for any
    value other than REAL_MERGE_METHOD ("merge") — see this module's
    docstring, "SQUASH IS A NO-OP HERE": on any other merge method the
    resulting commit's subject is rewritten from the (already-gated) PR
    title, so re-checking branch subjects would be redundant.

    No-op when *skip* is True — the caller (--skip-commit-check at the CLI
    layer) is responsible for logging that the bypass was used, mirroring
    merge.title_gate.check_pr_title's skip contract exactly.

    Raises merge.errors.CommitSubjectInvalidError on the FIRST non-conformant
    subject encountered (in *commit_subjects* order), naming the offending
    SHA, the offending subject, and the required grammar. Never rewrites or
    normalizes any commit — see this module's docstring, "BLOCK, NEVER
    REWRITE".

    TASK-ID GUARD (lr-4005f5, clagentic_loadout.task_id_guard, reused not
    forked): after the Conventional Commits grammar check above passes for
    a given subject, ALSO check it against *task_id_guard_pattern* (the
    deployment's own configured `push.task_id_guard_pattern` -- read by the
    caller, merge.verb, from the SAME repo config every other gate key here
    reads; never re-derived in this module). A strict no-op when
    *task_id_guard_pattern* is None (see task_id_guard's own module
    docstring, "NO-OP BY DEFAULT"). Gated by the SAME `merge_method ==
    REAL_MERGE_METHOD` condition as the grammar check above -- a
    task-id-bearing subject only lands verbatim on a real (non-squash)
    merge; on any other merge method the resulting commit subject is
    rewritten from the PR title (already covered by merge.title_gate at the
    title surface).

    Raises task_id_guard.TaskIdGuardViolation when *task_id_guard_mode* ==
    "block" (the operator-pinned default once a pattern is configured) and a
    subject matches. Returns the list of formatted warning strings produced
    when *task_id_guard_mode* == "warn" instead (this module has no stderr
    of its own -- printing is the CALLER's job, mirroring every other
    skip-able gate in this package; see merge.verb's own wiring for where
    these are surfaced). Empty list when nothing warned (including the
    common case: no pattern configured at all).
    """
    if skip:
        return []
    if merge_method != REAL_MERGE_METHOD:
        return []
    warnings: list[str] = []
    for sha, subject in commit_subjects:
        if not is_conventional_title(subject):
            raise CommitSubjectInvalidError(
                _format_commit_subject_error(
                    sha, subject, pr_number=pr_number, owner=owner, repo=repo
                )
            )
        warning = check_task_id_guard(
            subject,
            field=f"branch commit subject ({sha})",
            pattern=task_id_guard_pattern,
            mode=task_id_guard_mode,
        )
        if warning:
            warnings.append(warning)
    return warnings


__all__ = ["REAL_MERGE_METHOD", "check_branch_commit_subjects"]
