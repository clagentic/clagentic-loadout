"""merge.merge_shape — surface a requested-vs-actual merge-shape mismatch
loudly instead of silently (lr-14f704, item 3).

THE DEFECT THIS CLOSES, ONE LAYER OVER FROM push.remote_readback (lr-4e8a43):
`merge.verb` reports a merge as having used *args.merge_method* (the flag the
caller requested), but until lr-14f704's other fix, that value was NEVER
actually forwarded to either backend's `merge_pr` — the merge ALWAYS executed
a real merge commit regardless of what was requested, and nothing in this
package's own substrate detected or reported the mismatch. The release-gate
role that reported the incident that opened lr-14f704 had to run `git
merge-base` BY HAND, out-of-band, to discover the tool had done something
other than what was asked. `push.remote_readback`'s own docstring names the
exact shape of this defect class: "a caller reporting a remote fact ...
structurally indistinguishable from a caller that skipped the [operation]
entirely." This module is the merge-side instance of that same fix, reusing
its vocabulary and pattern rather than inventing a second one (task
directive, lr-14f704): read the ACTUAL result back (never trust the
request), and hand the caller a structurally-tagged verdict object it must
itself act on, rather than raising unilaterally.

SCOPE: THIS IS A LOCAL GIT READBACK, NOT A REMOTE ONE — a deliberate
divergence from push.remote_readback's `git ls-remote`, named here so a
reader does not assume the two are interchangeable. Confirming a merge's
actual SHAPE (how many parents the landed commit has) requires reading that
commit's OWN parent list, which neither platform's merge-API response body
carries (GitHub returns a bare `sha`; Forgejo returns no body at all — see
merge.forgejo_backend.merge_pr's own docstring). The commit object itself,
however, is already guaranteed to be present in a --repo-path working tree
by the time this module's check runs: merge.verb._run only reaches this
check AFTER merge.tree_sync.advance_repo_to_merged_sha has already fetched
and checked out that exact commit (verified, per that module's own
contract). `git log -1 --format=%P <sha>` against that already-synced local
tree is therefore a genuine, already-fetched-object read, not a guess or a
second network round-trip — the "no extra round-trip if the info is already
fetched" constraint this package applies elsewhere (see merge.commit_subjects'
docstring for the same constraint applied to the compare-API fetch).

NO --repo-path, NO CHECK (documented, not silently expanded): a bare
API-only merge invocation (--no-post-merge-tree or --skip-post-merge, no
local tree at all) has no local object database to read a parent list from,
and this module does not add a platform-API round trip to cover that case —
doing so would be new scope beyond what the task asked for (a readback of
what tree_sync ALREADY fetched, not a new fetch of its own). merge.verb._run
only calls check_merge_shape when a --repo-path tree was actually synced;
see that module's own call site for the exact placement (immediately after
advance_repo_to_merged_sha, before any post_merge_steps run).

WARN-BY-DEFAULT, CONFIG-GATED STRICTNESS (the trade-off named explicitly per
the task's own non-negotiable constraint): making a detected mismatch a hard
failure by default would impose a brand-new failure mode on every existing
external consumer of `loadout-merge` — a caller who has never once passed
--merge-method (accepting the "merge" default) is entirely unaffected either
way, but a caller who DOES pass a non-default value and has, for whatever
reason, been silently getting a different shape than requested (exactly the
originating incident) would see a previously-"successful" invocation start
exiting non-zero the moment this ships, for a defect this SAME release also
fixes. Two things point at the same operator-facing severity call being
STRICT here without a way to opt back out:
  1. Prior to this PR, EVERY caller of --merge-method squash/rebase on this
     package was silently getting merge_method='merge' behavviour --
     hardening this without an escape hatch turns "the flag now finally
     works" into "the flag now finally works, or your merge starts
     refusing," in the very release that fixes the flag.
  2. A shipped tool with users beyond one crew (this repo's own CLAUDE.md,
     "Loadout is a SHIPPED TOOL with EXTERNAL USERS") must not unilaterally
     add a new refusal path a caller cannot see coming.
The default is therefore WARN (log to stderr, never touch the exit code) --
symmetrical to push.remote_readback's own "ADDITIVE HALF ONLY" posture for
the exact same reason. A repo that wants the stricter behavior opts in
per-repo via `merge: enforce_merge_shape: true` (merge.post_merge_config-
style repo-tier key, see merge.post_merge_config.resolve_enforce_merge_shape)
-- MergeShapeMismatchError is raised only when that key is explicitly set,
never by default. This mirrors this package's own established "additive
now, config-gated enforcement as a named follow-up" precedent (see
docs/integration.md, "Authoritative post-push remote state" -> "why this is
additive, not enforcement") rather than inventing a fresh trade-off shape for
this one case.

PARENT-COUNT EXPECTATION TABLE: a real "merge" commit ALWAYS has >= 2
parents (that is the definition of a merge commit); "squash" and "rebase"
(Forgejo's fast-forward-without-its-own-merge-commit variant, and GitHub's
rebase-merge, which both replay commits onto the base with new SHAs) always
land as a SINGLE-parent commit; Forgejo's "rebase-merge" (rebase WITH a
trailing merge commit) is expected to land like a real merge (>= 2 parents),
same as "merge". "manually-merged" (Forgejo-only, an out-of-band "someone
already merged this, just record it" marker) has no predictable shape at
all -- this module makes NO assertion for it, matching the honest "we cannot
know" answer rather than picking an arbitrary expected count.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

#: merge_method values expected to land as a commit with >= 2 parents (a
#: genuine merge commit). Mirrors merge.commit_subjects.REAL_MERGE_METHOD's
#: own "merge" token plus Forgejo's rebase-with-merge-commit variant, which
#: is the ONE Forgejo-only `Do` value that still produces a multi-parent
#: landed commit (see merge.forgejo_backend.VALID_DO_VALUES).
MULTI_PARENT_MERGE_METHODS = frozenset({"merge", "rebase-merge"})

#: merge_method values expected to land as a single-parent commit (the
#: original branch history is replayed/collapsed onto the base rather than
#: joined to it).
SINGLE_PARENT_MERGE_METHODS = frozenset({"squash", "rebase"})

#: merge_method values this module makes NO shape assertion for at all --
#: see module docstring, "PARENT-COUNT EXPECTATION TABLE".
UNVERIFIABLE_MERGE_METHODS = frozenset({"manually-merged"})


class MergeShapeCheckError(Exception):
    """Raised when the actual merge shape cannot even be READ (the `git log`
    call itself fails, or returns no parseable parent line) -- a
    check-EXECUTION failure, distinct from a successful read that simply
    disagrees with what was requested (see MergeShapeMismatchError). Never
    raised for an unrecognized/unverifiable merge_method -- see
    UNVERIFIABLE_MERGE_METHODS."""


class MergeShapeMismatchError(Exception):
    """Raised ONLY when a repo has opted into `merge: enforce_merge_shape:
    true` (see module docstring, "WARN-BY-DEFAULT, CONFIG-GATED
    STRICTNESS") and the actual landed commit's parent count disagrees with
    what *requested_merge_method* predicts. Never raised by default."""


@dataclass(frozen=True)
class MergeShapeCheck:
    """Result of comparing the ACTUAL parent count of a landed merge commit
    (read back locally, never assumed) against what *requested_merge_method*
    predicts.

    `verified=False` means the requested merge_method carries no shape
    prediction at all (see UNVERIFIABLE_MERGE_METHODS) -- this is a no-op
    pass, not a claim that the shape was checked, mirroring
    push.remote_readback.AuthorshipCheck's own `checked` field for the same
    "this dimension was never assessed" case.
    """

    verified: bool
    matches: bool
    requested_merge_method: str
    actual_parent_count: int
    sha: str


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def check_merge_shape(
    sha: str,
    requested_merge_method: str,
    repo_path: str | Path,
) -> MergeShapeCheck:
    """Read back the ACTUAL parent count of commit *sha* in the working tree
    at *repo_path* (already synced onto that exact commit by
    merge.tree_sync.advance_repo_to_merged_sha by the time merge.verb._run
    calls this -- see module docstring, "SCOPE") and compare it against what
    *requested_merge_method* predicts.

    Runs `git log -1 --format=%P <sha> --` against the ALREADY-FETCHED local
    object database -- no network call, no platform API call (see module
    docstring for why this must be a local, not remote, readback).

    Returns a MergeShapeCheck with `verified=False` (a no-op pass) when
    *requested_merge_method* is not in MULTI_PARENT_MERGE_METHODS or
    SINGLE_PARENT_MERGE_METHODS (an unrecognized value from a future/custom
    Forgejo `Do` extension, or a deliberately-unverifiable one like
    "manually-merged") -- this function never guesses an expectation for a
    method it does not have a documented shape for.

    Raises MergeShapeCheckError if the `git log` call itself fails (non-zero
    exit) or returns no parseable parent line -- a check-EXECUTION failure.
    Never falls back to trusting the request on a read failure -- exactly
    the "don't trust the local copy" posture push.remote_readback's own
    docstring establishes for the analogous remote-read case.
    """
    if (
        requested_merge_method not in MULTI_PARENT_MERGE_METHODS
        and requested_merge_method not in SINGLE_PARENT_MERGE_METHODS
    ):
        return MergeShapeCheck(
            verified=False,
            matches=True,
            requested_merge_method=requested_merge_method,
            actual_parent_count=-1,
            sha=sha,
        )

    repo_path = Path(repo_path)
    result = _run_git(["log", "-1", "--format=%P", sha, "--"], cwd=repo_path)
    if result.returncode != 0:
        raise MergeShapeCheckError(
            f"merge-shape readback FAILED -- `git log -1 --format=%P {sha}` "
            f"in {repo_path} exited {result.returncode}: "
            f"{result.stderr.strip()[:400]}. This is NOT a fallback to "
            f"trusting the requested merge_method -- the caller cannot "
            f"report a merge shape this process did not itself confirm."
        )

    parent_line = result.stdout.strip()
    # An empty %P (zero parents) is itself a valid, parseable readback -- a
    # root commit has no parents. Distinguish "git ran and told us zero
    # parents" (parent_line == "", returncode == 0) from "git could not tell
    # us anything at all" (already handled by the returncode check above).
    actual_parent_count = len(parent_line.split()) if parent_line else 0

    if requested_merge_method in MULTI_PARENT_MERGE_METHODS:
        matches = actual_parent_count >= 2
    else:
        matches = actual_parent_count == 1

    return MergeShapeCheck(
        verified=True,
        matches=matches,
        requested_merge_method=requested_merge_method,
        actual_parent_count=actual_parent_count,
        sha=sha,
    )


def format_mismatch_message(check: MergeShapeCheck, *, pr_number: int, owner: str, repo: str) -> str:
    """Render a MergeShapeCheck's mismatch as a human-readable message,
    naming the offending SHA, the requested method, and the actual parent
    count -- never a collapsed guess (CLI hygiene rule 4). Shared by the
    warn-log and the MergeShapeMismatchError message so both surfaces report
    identically."""
    expected = (
        ">= 2 parents (a real merge commit)"
        if check.requested_merge_method in MULTI_PARENT_MERGE_METHODS
        else "exactly 1 parent (squash/rebase)"
    )
    return (
        f"merge-shape MISMATCH -- PR #{pr_number} in {owner}/{repo} requested "
        f"--merge-method {check.requested_merge_method!r} (expected {expected}) "
        f"but the landed commit {check.sha} actually has "
        f"{check.actual_parent_count} parent(s). The merge already happened "
        f"server-side and cannot be undone by this check -- this reports the "
        f"disagreement automatically so a caller does not have to discover a "
        f"requested-vs-actual merge-method mismatch by manually inspecting "
        f"`git merge-base` after the fact."
    )


__all__ = [
    "MULTI_PARENT_MERGE_METHODS",
    "SINGLE_PARENT_MERGE_METHODS",
    "UNVERIFIABLE_MERGE_METHODS",
    "MergeShapeCheck",
    "MergeShapeCheckError",
    "MergeShapeMismatchError",
    "check_merge_shape",
    "format_mismatch_message",
]
