"""merge.fence_union — a required role's verdict at a head is the union of every
fence the role posted at that head.

A reviewer role is posted by more than one path (the gate run, a later real
run, a re-post). Reading only the newest fence let the order of posting decide
the outcome: a CLEAN gate fence followed by a BLOCKED real-run post at one head
read as BLOCKED, and the reverse order read as CLEAN, although the two runs
disagreed about the same code.

The rule, in the merge gate's terms:

  - Every fence by the role at the PR head is a candidate, and the gate runs its
    full chain on each. A candidate that is refused refuses the role, whatever
    order the fences were posted in.
  - A finding one fence kept is never covered by another fence dropping it. The
    only thing that clears a kept finding is a disposition in the base branch,
    which the gate chain applies to each fence on its own.
  - With exactly one fence at the head the result is the single-fence result:
    this module reads the newest fence first, exactly as the reader always did.

A fence for another head is never a candidate and never ends the walk: freshness
is still decided by the head stamp, and a fence for an older head neither
authorizes this head nor hides a fence at it. A role whose NEWEST fence is for
another head is refused as a stale verdict, as before, because that first read
is not changed.

An older comment whose fence cannot be read for the head (unreadable, another
role's content, another head) is skipped here. The reader has already refused,
on the newest read, every older fence that is unreadable and may have held a
finding open; one that remains asserts nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from typing import Any

from clagentic_loadout.merge.errors import (
    VerdictMalformedError,
    VerdictMissingError,
    VerdictRoleMismatchError,
    VerdictStaleError,
)
from clagentic_loadout.merge.verdict import ReviewerVerdict

#: Reasons an older comment is not a fence at this head. Skipping it never
#: relaxes anything: the newest read, which stays strict, has already judged it.
_NOT_A_HEAD_FENCE = (
    VerdictMissingError,
    VerdictMalformedError,
    VerdictRoleMismatchError,
    VerdictStaleError,
)


def collect_head_fences(
    read: Callable[[Collection[Any]], ReviewerVerdict],
    fence_ids: Callable[[], Sequence[Any]],
) -> tuple[ReviewerVerdict, ...]:
    """Every fence by the role at the head, newest first.

    *read* takes the comment ids to pass over and returns the verdict selected
    among the rest; it raises as merge.verdict.read_reviewer_verdict does.
    *fence_ids* returns the ids of the role's fenced comments, newest first.

    The first read is the newest fence and raises exactly as before. Each older
    fenced comment is then read with every newer comment passed over, so its
    history is its own earlier posts only.
    """
    newest = read(())
    ids = list(fence_ids())
    if newest.comment_id not in ids:
        return (newest,)
    fences = [newest]
    newer = list(ids[: ids.index(newest.comment_id) + 1])
    for comment_id in ids[len(newer) :]:
        try:
            candidate = read(tuple(newer))
        except _NOT_A_HEAD_FENCE:
            candidate = None
        newer.append(comment_id)
        if candidate is not None and candidate.comment_id == comment_id:
            fences.append(candidate)
    return tuple(fences)


__all__ = ["collect_head_fences"]
