"""merge.tree_sync — advance a local working tree to the merged main SHA
after a server-side API merge (lr-7c5540), and (lr-d95cdb) land that tree
usefully for the NEXT dispatch once any post_merge_steps have run.

THE DEFECT THIS CLOSES: `merge.verb`'s post-merge step (step 10) previously
ran `merge.post_merge.run_post_merge_steps` against `--repo-path` EXACTLY as
the caller's working tree was already checked out. The merge itself (step 9,
`backend.merge_pr`) is a server-side API call — it never advances the LOCAL
working tree at `--repo-path` to the resulting merged commit. Consequence:
a post-merge step that packages/installs the repo (e.g. `npm pack`) packages
whatever ref the caller happened to leave checked out (typically the feature
branch HEAD that opened the PR), not the merged main commit that actually
landed. Content-equivalent for a clean, no-drift merge, but silently wrong
the moment main contains anything the branch does not (a concurrent merge, a
squash/rebase transform, base drift, a follow-up commit landed between gate
evaluation and merge execution).

THE FIX: before `merge.verb._run` calls `run_post_merge_steps`, it now calls
`advance_repo_to_merged_sha` below to put `--repo-path`'s working tree
literally ON the merged commit, via `git fetch` + `git checkout --detach`
(never a merge/rebase in the local tree — a post-merge step must see EXACTLY
the merged tree, not a local reconstruction of it that could itself diverge).

TRADE-OFF NAMED (per the task's explicit requirement, PR body carries this
too): the cleanest signal would be the merged commit SHA straight from the
merge API response. `merge.github_backend.merge_pr` already receives one
(GitHub's merge endpoint responds with `{"sha": ..., "merged": true}`) and
now returns it. `merge.forgejo_backend.merge_pr`, however, has NO documented
response field carrying the merged SHA at all — Forgejo's merge endpoint
responds 200/204 with an empty body on success (see that module's own
docstring); there is nothing to return. Rather than silently picking one
resolution strategy and leaving the other backend's SHA-precision gap
unaddressed, `advance_repo_to_merged_sha` accepts an OPTIONAL
`known_merged_sha` (wired from the GitHub path) and, when absent (the Forgejo
path, and a defensive fallback for GitHub too), independently resolves the
merged tip via `git fetch <remote> <base_branch>` + reading `FETCH_HEAD` —
after a merge lands server-side, the base branch's remote-tracking tip IS
the merged commit. `<remote>` is derived from the base branch's configured
tracking remote (`push.git_coords.tracking_remote`, falling back to
'origin' only when none is configured), never hardcoded (lr-ffede4) — a
tree whose sole remote is not named 'origin' must still resolve correctly. Both paths read back the resulting local HEAD SHA after
checkout, but only the known_merged_sha path VERIFIES it (comparing the
post-checkout HEAD against that expected value, so a resolution bug on that
path fails loud rather than silently landing on the wrong ref). The
base-branch-fallback path has no independent expected SHA to compare
against — it resolves and checks out the base branch's fetched tip and
trusts that tip IS the merged commit (true because the merge already landed
server-side before this function is ever called), without a second,
independent confirmation of that trust.

FAIL LOUD, NEVER A SILENT STALE-REF RUN: any git subprocess failure (fetch,
checkout, rev-parse) or a post-checkout HEAD mismatch raises TreeSyncError.
The caller (merge.verb._run) translates this to EXIT_POST_MERGE_FAILED via
the SAME exit-code path a failing post_merge_steps entry already uses —
never a fallback to running steps against the stale ref.

LANDING ON THE BASE BRANCH AFTER POST-MERGE STEPS (lr-d95cdb): the DETACHED
checkout above is deliberate and stays deliberate — a post_merge_steps entry
must see EXACTLY the merged tree, never a local reconstruction of it. But a
repo dispatched into again right after a merge previously had NO way to end
up anywhere useful: `advance_repo_to_merged_sha` only ever ran as a
prerequisite to post_merge_steps (so a repo with none configured got NO sync
at all, tree left wherever the caller last checked it out — typically the
feature branch HEAD that opened the PR), and even when it did run, it left
the tree DETACHED, not on an updated local base branch.

`land_on_base_branch` below closes both gaps WITHOUT touching the detach
above: `merge.verb._run` now calls `advance_repo_to_merged_sha` (detached,
unconditionally on any `--repo-path` at all — no longer gated on whether
post_merge_steps are configured), lets post_merge_steps run (or skips them,
exactly as before) against that detached, verified tree, and ONLY THEN calls
`land_on_base_branch` to move `--repo-path` off the detached HEAD onto
*base_branch*, fast-forwarded to point at the SAME already-landed SHA
`advance_repo_to_merged_sha` verified — never a second, independent
resolution, and never a local merge/rebase that could diverge from what the
server already decided. This is `git checkout -B <base_branch> <landed_sha>`
(create-or-reset the local branch ref to point exactly at *landed_sha*, then
check it out) — not `git merge`, not `git rebase`: the branch ref is simply
repointed at a commit that is already, by construction, the tip of the
remote's own base branch (verified moments earlier by
`advance_repo_to_merged_sha`), so this can never introduce local drift or a
three-way merge result the server-side merge did not itself produce.

A per-repo config key (`merge.sync_tree_after_merge`, see
`merge.post_merge_config.resolve_sync_tree_after_merge`) controls whether
`merge.verb._run` performs ANY of this (`advance_repo_to_merged_sha` +,
after steps, `land_on_base_branch`) at all when `--repo-path` is given;
defaulting to on. Flipping it off restores the pre-lr-d95cdb behavior of
leaving `--repo-path` exactly where the caller left it (no sync attempted).

NO CHECKOUT UNLESS SOMETHING WILL ACTUALLY READ THE TREE (lr-173768): the
detached-checkout + land-on-base-branch pair above mutates every file in
`--repo-path`'s working tree TWICE per merge -- and until this task, it did
so UNCONDITIONALLY, even when no `post_merge_steps` were configured or
`--skip-post-merge` meant none would run. On a host where multiple agents
share one on-disk checkout for build/dispatch work, that is a real-world
contention source: a merge yanks the files on disk out from under whatever
else is running in that same tree, with no signal to that other process.
`merge.verb._run` now calls
`fetch_merged_sha_object` (see below) -- NOT `advance_repo_to_merged_sha` --
whenever no `post_merge_steps` will actually run this invocation (the list
resolved to empty, or `--skip-post-merge` was passed): this still fetches
and verifies the merged commit is present in the local object database (so
`merge.merge_shape.check_merge_shape`'s `git log` readback keeps working
unconditionally, and the merge-completion attestation's SHA claim stays
independently confirmed), but performs NO `git checkout` and NO
`git checkout -B` at all -- the working tree, the index, and HEAD are left
byte-for-byte as the caller had them. `land_on_base_branch` is correspondingly
skipped in that case too: there is no detached HEAD to move off of, and
re-pointing the caller's branch out from under it would be exactly the same
class of surprise mutation this task removes elsewhere. Only when at least
one post_merge_steps entry will actually execute (steps non-empty AND
--skip-post-merge not given) does `merge.verb._run` still call
`advance_repo_to_merged_sha` (the checkout) followed by `land_on_base_branch`
after those steps run -- because a step that reads the filesystem (e.g.
`scripts/install.sh` reading `pyproject.toml`/package source off disk) has no
way to see "what merged" other than a real, populated checkout; there is no
index-free or bare-repo substitute for that specific need. See
`merge.verb`'s own module docstring, "WORKING-TREE SYNC BEFORE POST-MERGE
STEPS", for the exact call-site gating and the full PR-body enumeration of
what mutates and what does not.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from clagentic_loadout.push.git_coords import tracking_remote


class TreeSyncError(Exception):
    """Raised when the local working tree at --repo-path cannot be verified
    to be advanced to the merged main SHA. Always raised BEFORE any
    post_merge_steps entry runs — a caller must never fall back to running
    steps against whatever ref the tree happened to already be on."""


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def resolve_base_branch(pr_info: dict) -> str:
    """Extract the PR's base branch name (the ref the merge landed on) from
    a get_pr_info() response. Both backends' PR payloads use the same
    {"base": {"ref": ...}} shape (mirrors get_pr_head_sha's own cross-
    platform reuse in forgejo_backend/github_backend). Returns "" if absent
    -- the caller (merge.verb._run) treats an unresolvable base branch as a
    TreeSyncError, never a guess."""
    base = pr_info.get("base", {})
    if not isinstance(base, dict):
        return ""
    return base.get("ref", "") or ""


def advance_repo_to_merged_sha(
    repo_path: str | Path,
    *,
    base_branch: str,
    known_merged_sha: str | None = None,
) -> str:
    """Advance the working tree at *repo_path* to the merged main commit and
    return the resulting HEAD SHA.

    *known_merged_sha*: when the merge backend's own API response already
    carries the merged SHA (the GitHub path -- see module docstring), pass it
    here; this function fetches that exact commit and checks it out directly.
    When absent (the Forgejo path, which has no such response field), this
    function instead fetches *base_branch* from its resolved tracking remote
    (see below) and resolves `FETCH_HEAD` as the merged tip -- valid because
    the merge has ALREADY landed server-side by the time this is called
    (step 9 of merge.verb._run precedes this call), so the base branch's
    remote tip IS the merge result.

    After `git checkout --detach`, the resulting `git rev-parse HEAD` is
    always read back. VERIFICATION (comparing that readback against an
    expected SHA) only happens when *known_merged_sha* was supplied: the
    base-branch-fallback path (no *known_merged_sha*) has no independent
    expected value to compare against -- it trusts the fetched base branch's
    tip IS the merged commit (true because the merge already landed
    server-side before this function runs) without a second, independent
    confirmation. Any subprocess failure, or a known_merged_sha mismatch on
    the path that CAN check one, raises TreeSyncError -- this function never
    returns a SHA a subprocess failure prevented it from confirming.

    The remote fetched from is never assumed to be named 'origin': it is
    derived via `push.git_coords.tracking_remote` (reads
    `branch.<base_branch>.remote`, falling back to 'origin' only when no
    tracking remote is configured) -- the same zero-config-correct
    derivation the push layer already uses, reused here rather than
    duplicated (lr-ffede4). A tree whose sole remote is not named 'origin'
    (e.g. a fork/mirror tracking a remote named 'github') previously had its
    `git fetch origin` fail here, which silently skipped every
    post_merge_steps entry after an otherwise-successful merge.

    Raises TreeSyncError on: a missing/unresolvable *base_branch*, any
    non-zero `git fetch`/`git checkout`/`git rev-parse` exit, or a
    post-checkout HEAD that does not match *known_merged_sha* when one was
    supplied.
    """
    repo_path = Path(repo_path)
    if not base_branch:
        raise TreeSyncError(
            f"cannot advance working tree at {repo_path}: no base branch "
            f"could be resolved from the merged PR's metadata. Refusing to "
            f"guess which ref the merge landed on."
        )

    remote_name = tracking_remote(base_branch, repo_path)
    fetch_target = known_merged_sha if known_merged_sha else base_branch
    fetch_result = _run_git(["fetch", remote_name, fetch_target], cwd=repo_path)
    if fetch_result.returncode != 0:
        raise TreeSyncError(
            f"git fetch {remote_name} {fetch_target!r} failed (exit "
            f"{fetch_result.returncode}) in {repo_path}: "
            f"{fetch_result.stderr.strip()[:400]}"
        )

    checkout_target = known_merged_sha if known_merged_sha else "FETCH_HEAD"
    checkout_result = _run_git(
        ["checkout", "--detach", checkout_target], cwd=repo_path
    )
    if checkout_result.returncode != 0:
        raise TreeSyncError(
            f"git checkout --detach {checkout_target!r} failed (exit "
            f"{checkout_result.returncode}) in {repo_path}: "
            f"{checkout_result.stderr.strip()[:400]}"
        )

    rev_parse_result = _run_git(["rev-parse", "HEAD"], cwd=repo_path)
    if rev_parse_result.returncode != 0:
        raise TreeSyncError(
            f"git rev-parse HEAD failed (exit {rev_parse_result.returncode}) "
            f"in {repo_path} after checkout: "
            f"{rev_parse_result.stderr.strip()[:400]}"
        )
    landed_sha = rev_parse_result.stdout.strip()
    if not landed_sha:
        raise TreeSyncError(
            f"git rev-parse HEAD returned an empty SHA in {repo_path} after "
            f"checkout -- cannot verify the working tree landed on the "
            f"merged commit."
        )
    if known_merged_sha and landed_sha != known_merged_sha:
        raise TreeSyncError(
            f"working tree at {repo_path} landed on {landed_sha!r} after "
            f"checkout, but the merge API reported the merged SHA as "
            f"{known_merged_sha!r} -- refusing to run post-merge steps "
            f"against a tree that does not verifiably match what was merged."
        )
    return landed_sha


def fetch_merged_sha_object(
    repo_path: str | Path,
    *,
    base_branch: str,
    known_merged_sha: str | None = None,
) -> str:
    """Fetch the merged commit into *repo_path*'s local object database and
    return its SHA, WITHOUT checking anything out -- no `git checkout`, no
    change to the working tree, the index, or HEAD (lr-173768; see module
    docstring, "NO CHECKOUT UNLESS SOMETHING WILL ACTUALLY READ THE TREE").

    Mirrors `advance_repo_to_merged_sha`'s fetch + verification logic
    exactly (same remote derivation via `push.git_coords.tracking_remote`,
    same known_merged_sha-vs-resolved-tip verification contract) -- the ONLY
    difference is that this function stops after the fetch: it never runs
    `git checkout --detach`. Used by `merge.verb._run` whenever no
    `post_merge_steps` will actually execute this invocation (empty list, or
    `--skip-post-merge`), so `merge.merge_shape.check_merge_shape`'s local
    `git log` readback and the merge-completion attestation's SHA claim can
    still be independently confirmed against a real fetched object, without
    paying for a checkout nothing will read.

    VERIFICATION IS THIS FUNCTION'S OWN, NOT DELEGATED TO THE CALLER
    (security-review finding, folded into lr-173768): BOTH
    the known_merged_sha path and the base-branch-fallback path run a real
    local readback -- `git cat-file -e <sha>^{commit}` -- confirming the
    fetched object EXISTS locally and IS A COMMIT, not merely that some ref
    or literal SHA string resolves. Before this fix, the known_merged_sha
    path returned the caller-supplied value on a bare `git fetch` exit-code
    success alone, with no independent confirmation the object actually
    landed in this tree's object database or was even a commit -- that made
    this function's own claimed verification contract a property of its ONE
    caller (`merge.verb._run` happens to run `merge.merge_shape.
    check_merge_shape`'s own `git log` immediately afterward, which would
    have caught a missing/non-commit object) rather than a property of this
    function itself. A future call site that reorders, removes, or never
    adds that follow-up check would have silently lost the verification, and
    no test here would have caught it. `git cat-file -e ...^{commit}` is
    used (rather than another `git rev-parse`) specifically because
    `rev-parse` alone only confirms "some object with a name shaped like
    this string is known" without confirming its TYPE -- the `^{commit}`
    peel makes a tree/blob/tag-only object (which `git log`/`git checkout`
    would then fail on downstream anyway) a refusal HERE instead, at the
    earliest possible point.

    Raises TreeSyncError on the same conditions `advance_repo_to_merged_sha`
    does: a missing/unresolvable *base_branch*, a non-zero `git fetch`, a
    resolved SHA that does not match *known_merged_sha* when one was
    supplied, or (either path) a failed `git cat-file -e ...^{commit}`
    readback -- meaning the fetched object either does not exist locally or
    is not a commit. Since there is no checkout here, "resolved SHA" for the
    base-branch-fallback path (no *known_merged_sha*) is read via
    `git rev-parse FETCH_HEAD` rather than a post-checkout `git rev-parse
    HEAD` -- the object is the same either way; only the ref used to name it
    differs.
    """
    repo_path = Path(repo_path)
    if not base_branch:
        raise TreeSyncError(
            f"cannot fetch merged commit for {repo_path}: no base branch "
            f"could be resolved from the merged PR's metadata. Refusing to "
            f"guess which ref the merge landed on."
        )

    remote_name = tracking_remote(base_branch, repo_path)
    fetch_target = known_merged_sha if known_merged_sha else base_branch
    fetch_result = _run_git(["fetch", remote_name, fetch_target], cwd=repo_path)
    if fetch_result.returncode != 0:
        raise TreeSyncError(
            f"git fetch {remote_name} {fetch_target!r} failed (exit "
            f"{fetch_result.returncode}) in {repo_path}: "
            f"{fetch_result.stderr.strip()[:400]}"
        )

    if known_merged_sha:
        fetched_sha = known_merged_sha
    else:
        rev_parse_result = _run_git(["rev-parse", "FETCH_HEAD"], cwd=repo_path)
        if rev_parse_result.returncode != 0:
            raise TreeSyncError(
                f"git rev-parse FETCH_HEAD failed (exit "
                f"{rev_parse_result.returncode}) in {repo_path} after fetch: "
                f"{rev_parse_result.stderr.strip()[:400]}"
            )
        fetched_sha = rev_parse_result.stdout.strip()
        if not fetched_sha:
            raise TreeSyncError(
                f"git rev-parse FETCH_HEAD returned an empty SHA in "
                f"{repo_path} after fetch -- cannot verify the merged "
                f"commit was fetched."
            )

    # Independent local readback (see docstring above): both
    # paths converge here so NEITHER can skip verification -- confirms
    # *fetched_sha* both exists in this tree's object database and is a
    # commit object, never trusting a bare fetch exit code or a resolved-ref
    # name alone.
    cat_file_result = _run_git(
        ["cat-file", "-e", f"{fetched_sha}^{{commit}}"], cwd=repo_path
    )
    if cat_file_result.returncode != 0:
        raise TreeSyncError(
            f"git cat-file -e {fetched_sha}^{{commit}} failed (exit "
            f"{cat_file_result.returncode}) in {repo_path} after fetch -- "
            f"the fetched object does not exist locally or is not a commit; "
            f"refusing to report a SHA this function cannot independently "
            f"confirm landed: {cat_file_result.stderr.strip()[:400]}"
        )

    return fetched_sha


def land_on_base_branch(
    repo_path: str | Path,
    *,
    base_branch: str,
    landed_sha: str,
) -> str:
    """Move *repo_path* off a detached HEAD onto *base_branch*, pointed at
    *landed_sha* (lr-d95cdb — see module docstring, "LANDING ON THE BASE
    BRANCH AFTER POST-MERGE STEPS").

    Called by `merge.verb._run` AFTER `advance_repo_to_merged_sha` verified
    *landed_sha* and (if configured) `post_merge_steps` ran against that
    detached tree -- never before, and never as a substitute for the detach
    those steps depend on.

    Runs `git checkout -B <base_branch> <landed_sha>`: create-or-reset the
    local branch ref *base_branch* to point exactly at *landed_sha*, then
    check it out. This is a ref repoint, never a merge/rebase -- *landed_sha*
    is already, by construction, the exact commit the caller already
    verified is the server-side merge result, so there is no local
    reconstruction that could diverge from it. A pre-existing local
    *base_branch* (e.g. a stale prior clone of it) is reset to *landed_sha*
    outright (`-B`, not `-b`) -- this is deliberate: the whole point is that
    the NEXT dispatch must land on the tip that just merged, not on whatever
    local state pointed a branch of that name at before.

    Raises TreeSyncError on any non-zero `git checkout` exit or a
    post-checkout `git rev-parse HEAD` that does not equal *landed_sha* --
    fail loud, mirroring `advance_repo_to_merged_sha`'s own contract, rather
    than silently leaving the tree detached (the pre-lr-d95cdb state) if this
    final step cannot be verified.
    """
    repo_path = Path(repo_path)
    # Trailing `--` (lr-e1e2, security-review finding, defense-in-depth):
    # terminates option scanning so a *base_branch*/*landed_sha* value
    # beginning with '-' can never be parsed as a git flag. Verified this is
    # the CORRECT placement for `git checkout -B <branch> <start-point>`:
    # a LEADING `--` before the start-point is wrong here -- for this
    # subcommand shape it reinterprets everything after it as a pathspec,
    # which breaks resolving a real start-point entirely (confirmed:
    # `git checkout -B x -- <sha>` fails with "is not a commit and a branch
    # ... cannot be created from it"); a TRAILING `--` (after the
    # start-point, with nothing following it) terminates option parsing
    # without reinterpreting the already-consumed start-point as anything
    # else, and is a no-op for a well-formed branch/sha exactly like this
    # one. Both base_branch and landed_sha come from trusted sources today
    # (the PR API's base.ref, and a git rev-parse readback this module
    # already verified) -- this is not a live vector, just defense-in-depth
    # that costs nothing and changes no observable behavior for a
    # well-formed ref/sha.
    checkout_result = _run_git(
        ["checkout", "-B", base_branch, landed_sha, "--"], cwd=repo_path
    )
    if checkout_result.returncode != 0:
        raise TreeSyncError(
            f"git checkout -B {base_branch!r} {landed_sha!r} failed (exit "
            f"{checkout_result.returncode}) in {repo_path}: "
            f"{checkout_result.stderr.strip()[:400]}"
        )

    # `git rev-parse HEAD` -- no `--` here: `HEAD` is a fixed literal, never
    # a caller-influenced value, so there is nothing for a `--` separator to
    # protect; verified adding one changes the OUTPUT SHAPE for this
    # subcommand (`git rev-parse HEAD --` prints the resolved SHA on one
    # line and a literal '--' on a second line, which would corrupt the
    # `.strip()` parsing below) rather than being a no-op the way it is for
    # `checkout -B ... --`.
    rev_parse_result = _run_git(["rev-parse", "HEAD"], cwd=repo_path)
    if rev_parse_result.returncode != 0:
        raise TreeSyncError(
            f"git rev-parse HEAD failed (exit {rev_parse_result.returncode}) "
            f"in {repo_path} after landing on {base_branch!r}: "
            f"{rev_parse_result.stderr.strip()[:400]}"
        )
    final_sha = rev_parse_result.stdout.strip()
    if final_sha != landed_sha:
        raise TreeSyncError(
            f"working tree at {repo_path} landed on {final_sha!r} after "
            f"checking out {base_branch!r}, but expected {landed_sha!r} -- "
            f"refusing to leave the tree in an unverified state."
        )
    return final_sha


__all__ = [
    "TreeSyncError",
    "advance_repo_to_merged_sha",
    "fetch_merged_sha_object",
    "land_on_base_branch",
    "resolve_base_branch",
]
