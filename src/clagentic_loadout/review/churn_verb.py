"""review.churn_verb — the two ways the churn object is produced, sharing one
computation (review.churn): after a post, from the verdict just posted, and on
demand, from the reviewer's posted fences alone.

Both read the reviewer's own earlier fences through the host backend the caller
built, and both are advisory: a post never fails because the churn could not be
computed, and the on-demand read reports a ledger it cannot read as an error of
its own.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from clagentic_loadout.review import churn_report
from clagentic_loadout.review.churn import Key, Round, with_current
from clagentic_loadout.review.churn_brief import SpecQuote
from clagentic_loadout.review.churn_ledger import ChurnLedgerError, read_rounds


def churn_after_post(
    backend: Any,
    *,
    owner: str,
    repo: str,
    pr_number: int,
    login: str | None,
    head_sha: str,
    blocking: Iterable[Key],
    escalation_round: int,
    spec: SpecQuote | None,
) -> dict[str, Any]:
    """The churn object for a verdict just posted at *head_sha* holding *blocking*
    open, as *login*. The current round comes from the verdict itself, so it does
    not depend on the host already listing the new comment. Nothing is read when
    the breaker is off or nothing blocks, since no key can then churn."""
    keys = frozenset(blocking)
    current = Round(head_sha, keys)
    report = dict(owner=owner, repo=repo, backend=backend, spec=spec)
    if escalation_round == 0 or not keys:
        return churn_report.build([current], escalation_round, **report)
    if not login:
        return churn_report.unavailable(
            escalation_round, "the host did not name the author of the posted comment"
        )
    try:
        prior = read_rounds(backend, owner=owner, repo=repo, pr_number=pr_number, login=login)
    except ChurnLedgerError as exc:
        return churn_report.unavailable(escalation_round, str(exc))
    return churn_report.build(with_current(prior, current), escalation_round, **report)


def churn_on_demand(
    backend: Any,
    *,
    owner: str,
    repo: str,
    pr_number: int,
    login: str,
    escalation_round: int,
    spec: SpecQuote | None,
) -> tuple[dict[str, Any], str | None]:
    """``(churn object, head)`` for the reviewer's posted fences, the last of which
    is the current round; *head* is its head, None when there is none. Raises
    ChurnLedgerError when the comments cannot be read."""
    rounds = (
        read_rounds(backend, owner=owner, repo=repo, pr_number=pr_number, login=login)
        if escalation_round
        else []
    )
    head = rounds[-1].head_sha if rounds else None
    report = churn_report.build(
        rounds, escalation_round, backend=backend, owner=owner, repo=repo, spec=spec
    )
    return report, head


__all__ = ["churn_after_post", "churn_on_demand"]
