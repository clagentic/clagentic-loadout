"""push.identity — bot-attributed commit re-authoring + HEAD-author gate.

Wave B slice 3 (lr-09ca, tome #688). Ported from the reference push
transport's identity-pinning path (pin_commits_to_builder_identity /
_reauthor_commits / _verify_head_author, lr-66c2/lr-2422/lr-9f50). This is
LOAD-BEARING BEHAVIOR the task requires preserved, not identity to strip: a
push authored under the wrong identity is unrecoverable once merged, so the
caller's own bot identity is verified on HEAD before every push.

WHAT MOVED / WHAT DIDN'T:
  - THE RE-AUTHORING MECHANISM IS NOT UNCHANGED (lr-ac7bb0 fix): a prior
    revision of this module used `git filter-branch --env-filter` over the
    caller's own commits, excluded against a resolved exclusion ref, ported
    verbatim from the reference implementation. `git filter-branch`
    materializes its rewritten tree onto the working tree by design (its
    terminal step checks out the new HEAD), which is unnecessary risk on a
    push path that runs against a caller's live working tree. This module
    now rebuilds the same commit range with `git commit-tree` instead — a
    primitive that only ever writes new git objects, never the working tree
    or the index — and moves the branch ref to the rebuilt tip with a
    single `git update-ref` call. See reauthor_commits' own docstring,
    "PRIMITIVE", for the full account. The rewrite-floor semantics
    (`HEAD ^<exclusion_ref>`, resolve_exclusion_ref below) are unchanged by
    this fix — only the mechanism that performs the rewrite changed.
  - THE EXCLUSION-REF COMPUTATION IS NOT UNCHANGED (lr-501695 fix, ported
    defect closed): a prior revision of this module carried this claim
    verbatim from the ported reference, and resolve_exclusion_ref's own
    "prefer origin/<base> over local <base>" logic was ALSO ported
    unchanged — including a one-directional defect it inherited rather
    than fixed (see resolve_exclusion_ref's own docstring for the full
    account: preferring the remote-tracking ref only guards against a
    LAGGING LOCAL base ref; when the remote-tracking ref is the stale one
    instead, the old logic let the rewrite range over-extend past the true
    merge base and re-stamp already-landed commits). This module now
    computes each resolvable candidate ref's OWN merge base with HEAD and
    takes the more-advanced of the two (see resolve_exclusion_ref's own
    docstring for why a merge-base against a single, unconditionally-
    preferred candidate does not fully close this on its own) instead of
    returning a branch ref directly — correct regardless of which side is
    stale. Worth stating plainly: a defect surviving a
    "ported, not fixed" transition into a stated-unchanged mechanism is
    exactly the failure mode a prior closed-as-moot task (a stale-local-
    base fix on a since-deleted predecessor script) already illustrated —
    this fix does not repeat it by leaving a second "unchanged" claim
    standing over logic that no longer is.
  - The IDENTITY SOURCE is no longer read from a reference-implementation-
    specific per-agent config section or credential-provider config file.
    Both are caller inputs to this module: `bot_name` / `bot_email` are
    plain parameters. Config-file lookup (if any) is the caller's concern
    (e.g. a loadout config schema in a later slice), never baked in here.
  - There is no reference-implementation gatekeeper "org_default" fallback
    tier — that was GitHub-App-install-token-specific plumbing this module
    does not own. A caller integrating a credential-minting provider that
    also exposes a bot identity resolves it itself and passes the result in.
  - DIVERGED-BASE-BRANCH FAIL-CLOSED (lr-1cd30b, follow-up gap the
    lr-501695 security review named non-blocking): resolve_exclusion_ref's
    "more advanced of the two merge-bases" comparison assumed the two
    candidates' merge-base points were always ancestor-comparable. When a
    base branch has genuinely diverged (its local and remote-tracking refs
    share only an older common ancestor — the realistic path being an
    upstream force-push), that assumption can fail; the prior code
    silently kept whichever candidate resolved first (always
    remote-tracking), an iteration-order artifact that a constructed case
    shows is NOT reliably the safer floor. resolve_exclusion_ref now raises
    AmbiguousExclusionRefError in that case instead — see its own
    docstring, "DIVERGED CANDIDATES", for the full geometry, the
    constructed evidence, and this deployment's reachability assessment.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from clagentic_loadout.push.errors import AuthorMismatchError, DirtyWorkTreeError


def _run(
    cmd: list[str],
    *,
    check: bool = False,
    cwd: Path | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd[0], result.stdout, result.stderr)
    return result


def get_head_author_email(git_cwd: Path | None = None) -> str:
    """Return the author email of the current HEAD commit, or "" on any
    failure (callers treat empty as unverifiable/mismatch)."""
    r = _run(["git", "log", "-1", "--format=%ae", "HEAD"], cwd=git_cwd)
    if r.returncode != 0:
        return ""
    return r.stdout.strip()


def verify_head_author(expected_email: str, git_cwd: Path | None = None) -> bool:
    """True iff HEAD's author email matches *expected_email* exactly."""
    actual = get_head_author_email(git_cwd)
    if not actual:
        return False
    return actual == expected_email


def check_clean_work_tree(git_cwd: Path | None = None) -> None:
    """Pre-flight check (lr-4cd7ac, diagnosis lr-60781e; rationale updated
    lr-ac7bb0). ORIGINAL RATIONALE, now historical: this pre-flight existed
    to pre-empt `git filter-branch`'s own refusal precondition
    ("You have unstaged changes", git-sh-setup's own
    require_clean_work_tree) with a more specific, LOCAL/RECOVERABLE
    message before that refusal surfaced as a bare filter-branch failure
    inside AuthorMismatchError's mis-attribution framing.

    reauthor_commits no longer calls `git filter-branch` (lr-ac7bb0: it
    rebuilds the rewrite range with `git commit-tree`, which reads and
    writes git objects only, never the working tree or the index) -- so
    that specific precondition no longer exists to pre-empt, and an
    unstaged change in a TRACKED file can no longer cause the rewrite
    itself to fail. This check is kept as a residual safety signal: an
    unstaged edit present at push time most often means a caller is mid-
    edit or the tree is in some other locally-inconsistent state a push
    should not run against silently, even though the underlying
    tree-clobbering risk this pre-flight was originally written to prevent
    is gone. Deliberately still `git diff --name-only` (tracked files with
    unstaged working-tree changes), not `git status --porcelain` (which
    would also flag untracked files) -- widening this check's scope is not
    required for correctness now that the rewrite mechanism never touches
    the working tree at all, and would only make this a stricter,
    unrelated cleanliness gate (see push.cleanliness_check for that
    concern's own, purpose-built home).

    Raises:
        DirtyWorkTreeError: one or more tracked files have unstaged
            changes. Message enumerates the file count and (up to a small
            cap) their paths.

    A no-op (returns None) when the check itself cannot run (not a git
    repo, git missing) or when the tree is clean.
    """
    result = _run(["git", "diff", "--name-only"], cwd=git_cwd)
    if result.returncode != 0:
        return
    dirty_files = [line for line in result.stdout.splitlines() if line.strip()]
    if not dirty_files:
        return
    listed = ", ".join(repr(f) for f in dirty_files[:10])
    more = f" (+{len(dirty_files) - 10} more)" if len(dirty_files) > 10 else ""
    raise DirtyWorkTreeError(
        f"working tree has unstaged changes in {len(dirty_files)} tracked "
        f"file(s): {listed}{more}. This is a LOCAL, RECOVERABLE condition, "
        f"not an identity/mis-attribution problem -- commit or stash the "
        f"changes and retry."
    )


def _merge_base_with_head(ref: str, git_cwd: Path | None) -> str | None:
    """`git merge-base <ref> HEAD`, or None if the ref doesn't resolve
    against HEAD (unrelated histories, or *ref* itself absent)."""
    mb = _run(["git", "merge-base", ref, "HEAD"], cwd=git_cwd)
    if mb.returncode != 0 or not mb.stdout.strip():
        return None
    return mb.stdout.strip()


def _is_ancestor(candidate_sha: str, of_sha: str, git_cwd: Path | None) -> bool:
    """True iff *candidate_sha* is an ancestor of (or equal to) *of_sha*."""
    if candidate_sha == of_sha:
        return True
    r = _run(["git", "merge-base", "--is-ancestor", candidate_sha, of_sha], cwd=git_cwd)
    return r.returncode == 0


class AmbiguousExclusionRefError(Exception):
    """Raised by resolve_exclusion_ref when the remote-tracking and local
    candidate refs' own merge-bases with HEAD are mutually non-comparable
    (neither is an ancestor of the other) -- see resolve_exclusion_ref's own
    docstring, "DIVERGED CANDIDATES" section, for the full geometry and why
    this is a fail-closed refusal rather than a silent pick."""


def resolve_exclusion_ref(
    base_branch: str,
    git_cwd: Path | None = None,
) -> tuple[str, str] | tuple[None, None]:
    """Resolve the exclusion ref reauthor_commits uses as the rewrite floor.

    THE FLOOR IS THE MORE-ADVANCED MERGE BASE, NEVER A SINGLE BRANCH REF
    DIRECTLY (fix direction (2), lr-501695): a prior revision of this
    function preferred `origin/<base_branch>` (remote tracking ref) over
    the local `<base_branch>` ref, on the reasoning that a LAGGING LOCAL
    base ref would place already-landed commits inside the rewrite range
    and corrupt their SHAs. That reasoning was correct as far as it went,
    but the failure is SYMMETRIC and a straight "always prefer one ref
    over the other" choice only ever guards one direction: when the
    REMOTE-TRACKING ref is the stale one instead (local `<base_branch>`
    merged or fast-forwarded ahead of `origin/<base_branch>` without a
    subsequent fetch — routine on a repo that merges non-squash, since a
    landed merge commit advances local `<base_branch>` immediately but the
    remote-tracking ref only catches up on the next fetch), unconditionally
    preferring `origin/<base_branch>` makes it the STALE, BEHIND-BUT-STILL-
    AN-ANCESTOR-OF-HEAD ref — `HEAD ^origin/<base_branch>` then over-
    extends past the true merge base and includes already-landed commits
    that are already reachable from the MORE CURRENT of the two refs (see
    push.reauthor_commits' caller — identity re-authoring is unrecoverable
    once pushed). Taking `merge-base(origin/<base_branch>, HEAD)` alone
    does not fix this specific geometry either: when origin/<base_branch>
    is a plain ancestor of HEAD (the diagnosed shape — local main was
    fast-forwarded/merged ahead of a stale origin/main, so origin/main is
    STILL reachable from HEAD, just further back), its own merge-base with
    HEAD is itself — unchanged from the unfixed behavior.

    THE FIX: resolve BOTH candidates that exist (remote-tracking AND
    local), compute each one's OWN merge-base with HEAD, and return
    whichever of the two merge-base points is the more advanced (i.e. the
    one the OTHER is an ancestor of) — the floor is always the LATEST
    point both HEAD and at least one resolvable base-branch ref agree is
    already-landed history, never the earliest. This is correct regardless
    of which single ref (local or remote-tracking) is the stale one: the
    stale ref's own merge-base-with-HEAD is necessarily an ancestor of (or
    equal to) the current ref's, so the more-advanced comparison always
    picks the current one's floor without needing to know in advance which
    side was stale. When only one candidate ref resolves at all (e.g. no
    remote configured), its own merge-base with HEAD is used directly —
    unchanged behavior for that case.

    DIVERGED CANDIDATES (lr-1cd30b, follow-up gap named non-blocking by the
    lr-501695 security review): the "more advanced of the two" comparison
    above presumes the two candidates' merge-base points are
    ancestor-comparable — one reachable from the other. That presumption
    can fail: if HEAD's own history contains a merge commit joining two
    independently-evolved lines (e.g. an upstream force-push on
    `origin/<base_branch>` diverges it from local `<base_branch>`, and HEAD
    later merges history reachable only via each ref's own pre-divergence
    side), `merge-base(origin/<base_branch>, HEAD)` and
    `merge-base(<base_branch>, HEAD)` can be two DISTINCT commits, NEITHER
    an ancestor of the other. Constructed and verified directly (lr-1cd30b
    investigation, not by reasoning alone): in that geometry, the prior
    unguarded comparison loop silently returned the remote-tracking
    candidate's merge-base every time (an artifact of candidate iteration
    order — remote-tracking is always resolved first — never a reasoned
    safety choice), and that pick is NOT reliably the more-advanced of the
    two: a constructed case exists where it is the LESS advanced point,
    placing already-landed commits reachable only via the local ref's line
    (including a merge commit) inside the rewrite range — the same
    already-landed-commits-get-re-stamped class lr-501695 fixed for the
    single-stale-ref case. There is no general topological law forcing
    either candidate to be the safer pick when they are incomparable, so
    THIS FUNCTION NOW RAISES AmbiguousExclusionRefError instead of guessing
    — rewriting history on an undefined floor is worse than refusing.
    REACHABILITY: this verb's push-time call site does not fetch
    `origin/<base_branch>` immediately before re-authoring runs (see
    push.verb._run_create_pr, where pin_commits_to_bot_identity is called
    before the branch-commit-check's own fetch and before resolve_lease's
    conditional pre-lease fetch) — an upstream force-push on the base
    branch landing between a caller's last fetch and this push, combined
    with independent local history advancing via a merge, is realistic
    operational geometry here, not purely theoretical.

    Returns (floor_sha, label) on success, (None, None) if neither
    candidate ref resolves, or if merge-base fails against every
    resolved candidate (e.g. genuinely unrelated histories).

    Raises:
        AmbiguousExclusionRefError: both candidates resolved, but their
            merge-base points with HEAD are mutually non-comparable (see
            "DIVERGED CANDIDATES" above).
    """
    candidates: list[tuple[str, str]] = []

    remote_ref = f"origin/{base_branch}"
    if _run(["git", "rev-parse", "--verify", remote_ref], cwd=git_cwd).returncode == 0:
        candidates.append((remote_ref, f"remote tracking ref {remote_ref!r}"))

    if _run(["git", "rev-parse", "--verify", base_branch], cwd=git_cwd).returncode == 0:
        candidates.append((base_branch, f"local ref {base_branch!r}"))

    if not candidates:
        return None, None

    resolved: list[tuple[str, str]] = []
    for ref, ref_label in candidates:
        merge_base_sha = _merge_base_with_head(ref, git_cwd)
        if merge_base_sha is not None:
            resolved.append((merge_base_sha, ref_label))

    if not resolved:
        return None, None

    # Pick the most-advanced merge-base point: the one every OTHER
    # resolved candidate's merge-base is an ancestor of (or equal to).
    # With at most two candidates (remote-tracking, local), this is
    # well-defined ONLY when the two merge-base points are themselves
    # ancestor-comparable -- see "DIVERGED CANDIDATES" above for the
    # constructed geometry where they are not, and why that case raises
    # rather than silently keeping the first-resolved candidate.
    best_sha, best_label = resolved[0]
    for sha, label in resolved[1:]:
        if sha == best_sha:
            continue
        if _is_ancestor(best_sha, sha, git_cwd):
            best_sha, best_label = sha, label
        elif not _is_ancestor(sha, best_sha, git_cwd):
            raise AmbiguousExclusionRefError(
                f"base branch {base_branch!r} has diverged: the "
                f"remote-tracking and local candidate refs' merge-bases "
                f"with HEAD are two different commits "
                f"({best_sha} via {best_label}, {sha} via {label}) and "
                f"neither is an ancestor of the other. Refusing to guess "
                f"which is the safe rewrite floor -- rewriting history on "
                f"an undefined floor risks re-stamping already-landed "
                f"commits. Fetch the base branch and resolve the "
                f"divergence (e.g. rebase or reset the local ref onto the "
                f"current upstream) before retrying."
            )

    return best_sha, f"merge-base({best_label}, HEAD) = {best_sha}"


def _commit_tree_sha(git_cwd: Path | None, sha: str) -> str | None:
    r = _run(["git", "rev-parse", f"{sha}^{{tree}}"], cwd=git_cwd)
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def _commit_parents(git_cwd: Path | None, sha: str) -> list[str] | None:
    r = _run(["git", "rev-parse", f"{sha}^@"], cwd=git_cwd)
    if r.returncode != 0:
        return None
    return [line for line in r.stdout.splitlines() if line.strip()]


def _commit_field(git_cwd: Path | None, sha: str, fmt: str) -> str | None:
    r = _run(["git", "log", "-1", f"--format={fmt}", sha], cwd=git_cwd)
    if r.returncode != 0:
        return None
    return r.stdout


def _rebuild_commit(
    git_cwd: Path | None,
    original_sha: str,
    new_parents: list[str],
    bot_name: str,
    bot_email: str,
) -> str | None:
    """Create a new commit object carrying *original_sha*'s tree and
    message, *new_parents* as its parent list, *bot_name*/*bot_email* as
    both author and committer, and the ORIGINAL author/committer dates
    (never "now" -- `git commit-tree` defaults to the current time when no
    date env var is supplied, and silently re-dating every rewritten commit
    to the moment of the push would be its own, unrelated behavior change
    from what `git filter-branch --env-filter` did, which only ever
    overrode name/email). Returns the new commit SHA, or None on any
    failure (a caller-visible cause is not reconstructed here -- the
    top-level `reauthor_commits` cause message covers the whole rewrite as
    a unit, matching filter-branch's own single-cause failure surface).
    """
    tree = _commit_tree_sha(git_cwd, original_sha)
    if tree is None:
        return None
    message = _commit_field(git_cwd, original_sha, "%B")
    if message is None:
        return None
    # Strict ISO 8601 (%aI/%cI), never the locale-dependent default date
    # format -- GIT_AUTHOR_DATE/GIT_COMMITTER_DATE must parse unambiguously
    # regardless of the runtime's locale or git's own `date.format` config.
    author_date = _commit_field(git_cwd, original_sha, "%aI")
    committer_date = _commit_field(git_cwd, original_sha, "%cI")
    if author_date is None or committer_date is None:
        return None

    commit_tree_env = os.environ.copy()
    commit_tree_env["GIT_AUTHOR_NAME"] = bot_name
    commit_tree_env["GIT_AUTHOR_EMAIL"] = bot_email
    commit_tree_env["GIT_AUTHOR_DATE"] = author_date.strip()
    commit_tree_env["GIT_COMMITTER_NAME"] = bot_name
    commit_tree_env["GIT_COMMITTER_EMAIL"] = bot_email
    commit_tree_env["GIT_COMMITTER_DATE"] = committer_date.strip()

    cmd = ["git", "commit-tree", tree]
    for parent in new_parents:
        cmd += ["-p", parent]
    cmd += ["-F", "-"]

    result = subprocess.run(
        cmd,
        input=message,
        capture_output=True,
        text=True,
        env=commit_tree_env,
        cwd=str(git_cwd) if git_cwd is not None else None,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def reauthor_commits(
    base_branch: str,
    bot_name: str,
    bot_email: str,
    git_cwd: Path | None = None,
) -> tuple[bool, str]:
    """Rewrite every commit on the current branch (not reachable from the
    resolved exclusion ref) to *bot_name*/*bot_email* as both author and
    committer.

    Returns (True, "") on success (including the no-op case where there is
    nothing to rewrite). Returns (False, cause) on any failure — callers
    must fail closed on False; *cause* is a short, non-empty diagnostic
    string suitable for embedding directly in a caller's own error message
    (lr-4cd7ac, diagnosis lr-60781e: the underlying rewrite's stderr/cause
    was previously captured and then discarded, collapsing every
    precondition failure -- a dirty tree, an unresolvable ref, anything
    else -- into one indistinguishable message with no cause at all). Also
    returns (False, cause) when resolve_exclusion_ref raises
    AmbiguousExclusionRefError (lr-1cd30b: a diverged base branch with no
    ancestor-comparable floor) — an undefined rewrite floor is refused
    here, before any rewrite runs, rather than proceeding on a guess.

    PRIMITIVE (lr-ac7bb0, replaces a prior `git filter-branch --env-filter`
    call): this function no longer uses `git filter-branch`. filter-branch
    materializes its rewritten tree onto the working tree by design (its
    terminal step is `git read-tree -u -m HEAD`, a working-tree-touching
    two-way merge against the newly rewritten HEAD) -- unnecessary risk on
    a push path that runs against a caller's live working tree, and on at
    least one deployment topology (a shared, non-worktree-isolated
    checkout) that read-tree lands on content other in-flight work shares.
    Investigation (lr-ac7bb0 task thread) found the guard-width framing
    this task was originally filed under unsupported -- filter-branch's
    own precondition
    (git-sh-setup's require_clean_work_tree) already refuses on staged
    changes too, and the terminal read-tree is a two-way merge that refuses
    to clobber untracked files rather than deleting them -- but the
    tree-touching primitive itself remains an avoidable risk regardless of
    whether it has yet destroyed anything, so it is replaced here with
    `git commit-tree`: for each commit in the rewrite range (oldest first),
    a new commit object is built from the ORIGINAL commit's tree and
    message, the already-rebuilt SHAs of any in-range parent (an
    excluded/already-landed parent keeps its original SHA unchanged -- the
    same rewrite-floor semantics `HEAD ^<exclusion_ref>` already expresses,
    unchanged by this fix), and *bot_name*/*bot_email* as both author and
    committer. `git commit-tree` never reads or writes the working tree or
    the index -- it only writes new git objects -- so no working-tree
    content, tracked or untracked, staged or not, can ever be touched by
    this rewrite. The branch ref is moved to the last rebuilt commit via a
    single `git update-ref` call at the end; nothing is checked out.
    """
    quick_check = _run(["git", "rev-list", "--count", f"{base_branch}..HEAD"], cwd=git_cwd)
    if quick_check.returncode != 0 or quick_check.stdout.strip() == "0":
        return True, ""

    try:
        exclusion_ref, label = resolve_exclusion_ref(base_branch, git_cwd)
    except AmbiguousExclusionRefError as exc:
        return False, str(exc)
    if exclusion_ref is None:
        return False, (
            f"could not resolve an exclusion ref for base branch "
            f"{base_branch!r} (neither a remote-tracking nor a local ref "
            f"resolved against HEAD)"
        )

    branch_ref = _run(["git", "symbolic-ref", "-q", "HEAD"], cwd=git_cwd)
    if branch_ref.returncode != 0 or not branch_ref.stdout.strip():
        return False, (
            "HEAD is not a branch (detached HEAD) -- re-authoring updates a "
            "branch ref and refuses on a detached checkout rather than "
            "guessing which ref to move"
        )
    head_ref_name = branch_ref.stdout.strip()

    head_before = _run(["git", "rev-parse", "HEAD"], cwd=git_cwd)
    if head_before.returncode != 0 or not head_before.stdout.strip():
        return False, "could not resolve HEAD to a commit SHA"
    original_head_sha = head_before.stdout.strip()

    rewrite_set = _run(
        ["git", "rev-list", "--reverse", "--topo-order", "HEAD", f"^{exclusion_ref}"],
        cwd=git_cwd,
    )
    if rewrite_set.returncode != 0:
        return False, (
            f"could not enumerate the rewrite range (HEAD ^{exclusion_ref})"
        )
    original_shas = [line for line in rewrite_set.stdout.splitlines() if line.strip()]
    if not original_shas:
        return True, ""

    # Fix direction (3), lr-501695: log the rewrite set (count + SHAs)
    # BEFORE the rewrite runs, so a future regression in the
    # exclusion-ref/merge-base computation above is VISIBLE to an operator
    # (or CI log) instead of silently re-stamping already-landed history.
    # Best-effort only — a failure to enumerate subjects never blocks the
    # rewrite itself; the set is diagnostic, not a gate.
    subjects = _run(
        ["git", "log", "--format=%H %s", "HEAD", f"^{exclusion_ref}"],
        cwd=git_cwd,
    )
    if subjects.returncode == 0:
        rewrite_lines = [line for line in subjects.stdout.splitlines() if line.strip()]
        print(
            f"push: re-authoring {len(rewrite_lines)} commit(s) to bot identity "
            f"(exclusion ref: {label}):",
            file=sys.stderr,
        )
        for line in rewrite_lines:
            print(f"push:   {line}", file=sys.stderr)

    sha_map: dict[str, str] = {}
    for original_sha in original_shas:
        original_parents = _commit_parents(git_cwd, original_sha)
        if original_parents is None:
            return False, f"could not resolve parents of commit {original_sha}"
        # A parent already rebuilt (inside this rewrite range) is remapped
        # to its new SHA; a parent outside the range (already-landed,
        # reachable from exclusion_ref) keeps its original SHA -- this is
        # exactly the rewrite-floor semantics `HEAD ^<exclusion_ref>`
        # already expresses, just applied per-parent instead of via a
        # single filter-branch range argument.
        new_parents = [sha_map.get(p, p) for p in original_parents]
        new_sha = _rebuild_commit(git_cwd, original_sha, new_parents, bot_name, bot_email)
        if new_sha is None:
            return False, f"failed to rebuild commit {original_sha} via git commit-tree"
        sha_map[original_sha] = new_sha

    new_head_sha = sha_map[original_shas[-1]]
    update = _run(
        ["git", "update-ref", head_ref_name, new_head_sha, original_head_sha],
        cwd=git_cwd,
    )
    if update.returncode != 0:
        return False, (
            f"rebuilt {len(original_shas)} commit(s) but failed to move "
            f"{head_ref_name} to the new tip {new_head_sha} "
            f"({_first_nonempty_stderr_line(update.stderr)})"
        )

    return True, ""


def _first_nonempty_stderr_line(stderr: str) -> str:
    """Return the first non-blank line of *stderr*, or a fixed fallback
    string when *stderr* is empty/whitespace-only -- a git subprocess's own
    decisive diagnostic is generally its FIRST line, with any subsequent
    lines being secondary detail that would only dilute the embedded
    cause."""
    for line in stderr.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return "git exited non-zero with no stderr output"


def pin_commits_to_bot_identity(
    bot_name: str | None,
    bot_email: str | None,
    base_branch: str,
    git_cwd: Path | None = None,
    *,
    fail_closed_on_missing: bool = False,
) -> bool:
    """Re-author branch commits to (*bot_name*, *bot_email*), then verify
    HEAD carries that identity.

    Identity resolution is the CALLER's responsibility — this function takes
    the resolved name/email directly, never reads a config file itself. When
    either is missing:
      - fail_closed_on_missing=True: raise AuthorMismatchError (a caller
        that requires bot attribution, e.g. a namespace-restricted push,
        must not silently skip it).
      - fail_closed_on_missing=False: return False (re-authoring skipped;
        the caller decides whether that is acceptable for its deployment).

    Returns True when re-authoring was performed (the caller must then pass
    force_with_lease=True to the subsequent push, since history changed).

    Raises AuthorMismatchError when re-authoring fails, or when HEAD's
    author does not match the expected identity afterward — a
    mis-attributed push is unrecoverable, so this never returns a silent
    partial success. The raised message embeds the real cause reported by
    the underlying rewrite (lr-4cd7ac, diagnosis lr-60781e) — never a
    generic "re-authoring failed" with no diagnostic content.

    A dirty tracked work tree is NOT surfaced here — see
    check_clean_work_tree, a separate pre-flight callers should run before
    this function: an unstaged-changes precondition failure is a LOCAL,
    RECOVERABLE condition unrelated to commit authorship, and raising it
    as an AuthorMismatchError would misleadingly frame it as an
    unrecoverable mis-attribution risk. This function does not run that
    pre-flight itself so a caller that already ran it is never charged a
    second `git diff` round-trip.
    """
    if not bot_name or not bot_email:
        if fail_closed_on_missing:
            raise AuthorMismatchError(
                "bot identity (name + email) is required for this push but "
                "was not supplied. Refusing to push — a mis-attributed push "
                "is unrecoverable. Resolve the bot identity (config or "
                "credential-provider-derived) and retry."
            )
        return False

    ok, cause = reauthor_commits(base_branch, bot_name, bot_email, git_cwd)
    if not ok:
        raise AuthorMismatchError(
            f"commit re-authoring failed — cannot pin commits to bot identity "
            f"{bot_email!r}. Cause: {cause}. A mis-attributed push is "
            f"unrecoverable; fix the underlying failure and retry."
        )

    if not verify_head_author(bot_email, git_cwd):
        actual = get_head_author_email(git_cwd)
        raise AuthorMismatchError(
            f"HEAD commit author email {actual!r} does not match expected "
            f"bot identity {bot_email!r} after re-authoring. A mis-"
            f"attributed push is unrecoverable; refusing to push."
        )

    return True


__all__ = [
    "AmbiguousExclusionRefError",
    "check_clean_work_tree",
    "get_head_author_email",
    "pin_commits_to_bot_identity",
    "reauthor_commits",
    "resolve_exclusion_ref",
    "verify_head_author",
]
