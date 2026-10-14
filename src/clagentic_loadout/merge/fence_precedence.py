"""merge.fence_precedence — a passed gate fence at a head is not superseded by a
later post by the same role at that head.

The verdict reader selects a role's newest fenced comment. A reviewer role is
posted by more than one path (the gate run, a later real run, a re-post), so a
later post at the SAME head used to replace a verdict the merge gate had
already accepted: a CLEAN gate fence followed by a BLOCKED real-run post at one
head read as BLOCKED, and the gate result flipped without the head changing.

The rule, in the merge gate's terms:

  - The newest fence at the head is checked first, exactly as before. If the
    gate passes it, nothing here changes anything.
  - If the gate REFUSES it, an older fence by the same role at the SAME head is
    checked in turn, newest first, with the full chain. The first one the gate
    passes is the role's verdict at that head, and each newer fence that was
    passed over is named so the operator sees what was set aside.
  - If no fence at the head passes, the refusal of the NEWEST fence is raised
    unchanged: with no passed gate fence at the head, behaviour is as before.

A fence at another head is never a candidate: freshness is still decided by the
head stamp, and a fence for an older head never authorizes a newer one. The
chain a candidate must pass is the caller's own, so this module adds no
acceptance rule of its own and cannot pass a fence the gate would refuse.

Scope: only a fence that parses and is for the head is passed over. A newer
comment whose fence cannot be read, or whose content belongs to another role,
is a defect in the newest post and refuses as before; it carries no head to
compare.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from clagentic_loadout.merge.errors import (
    VerdictMalformedError,
    VerdictMissingError,
    VerdictRoleMismatchError,
    VerdictStaleError,
)
from clagentic_loadout.merge.verdict import ReviewerVerdict

T = TypeVar("T")

#: Failures of reading a candidate. On a fallback read they mean "no older fence
#: at this head to try", never a reason to refuse differently from the newest.
_NO_FURTHER_CANDIDATE = (
    VerdictMissingError,
    VerdictMalformedError,
    VerdictRoleMismatchError,
    VerdictStaleError,
)


@dataclass(frozen=True)
class PassedFence(Generic[T]):
    """The fence the gate settled on, what the check returned for it, and the
    newer same-head fences passed over to reach it as (comment id, refusal)."""

    verdict: ReviewerVerdict
    result: T
    passed_over: tuple[tuple[Any, Exception], ...] = ()


def select_passed_fence(
    read: Callable[[Collection[Any]], ReviewerVerdict],
    check: Callable[[ReviewerVerdict], T],
    refusals: tuple[type[Exception], ...],
) -> PassedFence[T]:
    """Read the role's newest fence and run *check* on it, falling back to older
    fences at the same head only when *check* refuses with one of *refusals*.

    *read* takes the comment ids to pass over and returns the verdict selected
    among the rest; it raises as merge.verdict.read_reviewer_verdict does.
    *check* is the full gate chain for one verdict: it returns on a pass and
    raises one of *refusals* otherwise. Any other exception propagates at once.

    Raises the newest fence's own error when no fence at the head passes.
    """
    passed_over: list[tuple[Any, Exception]] = []
    excluded: list[Any] = []
    while True:
        try:
            verdict = read(tuple(excluded))
        except _NO_FURTHER_CANDIDATE:
            if not passed_over:
                raise
            raise passed_over[0][1] from None
        if verdict.comment_id in excluded:
            # The reader returned a comment it was told to pass over, so the
            # walk cannot make progress; the newest fence's refusal stands.
            raise passed_over[0][1]
        try:
            result = check(verdict)
        except refusals as exc:
            passed_over.append((verdict.comment_id, exc))
            excluded.append(verdict.comment_id)
            continue
        return PassedFence(verdict, result, tuple(passed_over))


__all__ = ["PassedFence", "select_passed_fence"]
