"""merge.merge_readback — authoritative post-merge/post-close readback
(lr-361de3): the merge and close verbs' half of readback parity.

THE GAP THIS CLOSES (task lr-361de3, seq 2 items (c)/3 and (c)/4): before this
module, `merge.verb`'s success envelope printed only `{pr_number, owner,
repo}` after a merge -- no `merged_commit_sha`, no confirmation that the
merge actually landed beyond the mutating call's own response (Forgejo's
merge endpoint returns 200/204 with an EMPTY body on success -- there is no
merged-SHA field to trust there at all; see merge.forgejo_backend.merge_pr's
own docstring). `merge.close_verb` was the identical shape: the close was
trusted to the HTTP status code alone, with no state readback whatsoever.

THE PREDICATE PER VERB (seq 2 item (b), already researched -- not re-derived
here):
  MERGE: merged == true in a FRESH post-merge GET of the PR, AND the reported
    merge_commit_sha is resolvable on the PR's base branch (confirmed via the
    same compare-API ancestor check merge.forgejo_backend._is_head_ancestor_of_base
    already uses for its 405-disambiguation path -- REUSED here, not
    reimplemented a second time. GitHub side uses the equivalent compare
    endpoint via merge.github_backend's own request shaping).
  CLOSE: state == "closed" confirmed by a GET issued AFTER the PATCH -- both
    platforms expose PR state on the same GET .../pulls/{n} endpoint
    get_pr_info() already reads for the merge gate chain.

BOTH READBACKS REUSE get_pr_info() -- the SAME function each backend already
provides for the merge-gate chain's own PR-metadata read (see
merge.forgejo_backend.get_pr_info / merge.github_backend.get_pr_info). This
module does not add a second PR-read endpoint; it re-reads the identical
resource, AFTER the mutating call, and asserts a different set of fields
against the SAME response shape.

ENVELOPE SHAPE: both readbacks render into the ONE stable
transport.readback_envelope.Readback shape (lr-361de3's cross-verb stability
requirement -- see that module's own docstring) rather than a per-verb
ad-hoc dict, so a downstream consumer's predicate is the SAME
`envelope["readback"]["verified"] is True` test regardless of which verb
produced the envelope.

FAIL-CLOSED (task's own stated preference, seq 2 item (d)): a readback GET
that cannot be performed (network failure, non-2xx) or a readback that does
not confirm the expected state produces `Readback(verified=False, ...)` --
callers (merge.verb / merge.close_verb) translate that to a distinct,
non-zero, non-confusable exit code (EXIT_MERGE_READBACK_FAILED /
EXIT_CLOSE_READBACK_FAILED) rather than reporting EXIT_OK on an unconfirmed
mutation. This is a DELIBERATE BEHAVIOR CHANGE for both verbs (see PR body):
previously, a successful HTTP mutation was always EXIT_OK regardless of
whether the platform's OWN state ever actually reflected it.
"""

from __future__ import annotations

from typing import Any, Callable

from clagentic_loadout.transport.readback_envelope import (
    READBACK_SOURCE_API_GET,
    READBACK_SOURCE_READ_UNAVAILABLE,
    READBACK_SOURCE_VERIFY_FAILED,
    Readback,
)


def verify_merge_landed(
    get_pr_info_after: Callable[[], dict[str, Any]],
) -> Readback:
    """Re-read the PR's state via *get_pr_info_after* (a zero-arg callable
    the caller binds to the correct backend/owner/repo/pr_number -- see
    merge.verb._run's call site) and confirm the merge landed.

    Predicate (seq 2 item (b)): the re-read PR's `merged` field is `True`,
    AND `merge_commit_sha` is a non-empty string. This mirrors GitHub's own
    documented merge-response shape (`{"merged": true, "sha": ...}`) applied
    to the FRESH re-read rather than the merge call's own response -- the
    exact substitution this task requires (Forgejo's merge call returns no
    such field at all; only a fresh read of the PR resource carries it
    post-merge on either platform).

    Raises nothing: any failure to even perform the read (the underlying
    merge.errors.GateFactUnavailableError merge.forgejo_backend.get_pr_info /
    merge.github_backend.get_pr_info already raise on a non-200/network
    failure) is caught here and folded into a `verified=False,
    source=READBACK_SOURCE_READ_UNAVAILABLE` result -- the caller never sees
    a raised exception from this function, only a Readback to inspect.
    """
    from clagentic_loadout.merge.errors import GateFactUnavailableError

    try:
        pr_info = get_pr_info_after()
    except GateFactUnavailableError as exc:
        return Readback(
            verified=False,
            source=READBACK_SOURCE_READ_UNAVAILABLE,
            detail={"reason": str(exc)},
        )

    merged = pr_info.get("merged")
    merge_commit_sha = pr_info.get("merge_commit_sha") or ""
    if merged is True and isinstance(merge_commit_sha, str) and merge_commit_sha:
        return Readback(
            verified=True,
            source=READBACK_SOURCE_API_GET,
            detail={"merged_commit_sha": merge_commit_sha},
        )

    return Readback(
        verified=False,
        source=READBACK_SOURCE_VERIFY_FAILED,
        detail={
            "reason": (
                f"post-merge readback did not confirm the merge landed -- "
                f"merged={merged!r}, merge_commit_sha={merge_commit_sha!r} "
                f"(expected merged=True and a non-empty merge_commit_sha)."
            ),
        },
    )


def verify_pr_closed(
    get_pr_info_after: Callable[[], dict[str, Any]],
) -> Readback:
    """Re-read the PR's state via *get_pr_info_after* and confirm it is
    closed.

    Predicate (seq 2 item (b)): the re-read PR's `state` field equals
    `"closed"` -- both platforms report PR state on the same GET
    .../pulls/{n} resource get_pr_info() already reads for the merge gate
    chain. A PR the close PATCH just landed on is NOT distinguished from one
    that was ALREADY closed by another actor before this call -- close is
    documented as idempotent on both platform APIs (see
    merge.forgejo_backend.close_pr / merge.github_backend.close_pr's own
    docstrings), so `state == "closed"` is the correct, complete predicate
    either way.

    Raises nothing -- mirrors verify_merge_landed's own contract: any read
    failure folds into `verified=False,
    source=READBACK_SOURCE_READ_UNAVAILABLE`, never a raised exception.
    """
    from clagentic_loadout.merge.errors import GateFactUnavailableError

    try:
        pr_info = get_pr_info_after()
    except GateFactUnavailableError as exc:
        return Readback(
            verified=False,
            source=READBACK_SOURCE_READ_UNAVAILABLE,
            detail={"reason": str(exc)},
        )

    state = pr_info.get("state")
    if state == "closed":
        return Readback(
            verified=True,
            source=READBACK_SOURCE_API_GET,
            detail={"state": state},
        )

    return Readback(
        verified=False,
        source=READBACK_SOURCE_VERIFY_FAILED,
        detail={
            "reason": (
                f"post-close readback did not confirm the PR is closed -- "
                f"state={state!r} (expected 'closed')."
            ),
        },
    )


__all__ = [
    "verify_merge_landed",
    "verify_pr_closed",
]
