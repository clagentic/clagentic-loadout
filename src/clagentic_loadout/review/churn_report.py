"""review.churn_report — the ``churn`` object `loadout-review post` adds to its
result and `loadout-review churn` prints.

Shape (every key always present)::

    {"escalation_round": 2,
     "keys": [{"rule_id": "R1", "file": "src/a.py", "rounds": 2, "heads": ["<sha>", "<sha>"]}],
     "level": "none" | "brief" | "stop",
     "net_delta": {"from_head": "<sha>", "to_head": "<sha>",
                   "files": [{"path": "src/a.py", "added": 3, "removed": 1}]} | null,
     "brief": "<text>" | null,
     "options": ["simplify", "ship", "continue"] | []}

``net_delta_unavailable`` (a reason) is added when the level is brief or stop
and the delta is null; ``ledger_unavailable`` (a reason) replaces the computed
fields' meaning when the pull request's comments could not be read, and the
level is then none. It is advisory output: nothing here refuses anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from clagentic_loadout.review import churn_brief
from clagentic_loadout.review.churn import (
    LEVEL_NONE,
    LEVEL_STOP,
    STOP_OPTIONS,
    Round,
    assess,
)
from clagentic_loadout.review.churn_delta import read_net_delta
from clagentic_loadout.review.spec_review import SpecFacts


def spec_quote(facts: SpecFacts | None) -> churn_brief.SpecQuote | None:
    """What step 1 of the brief quotes from the review's sealed spec, or None
    when the review was given none."""
    if facts is None:
        return None
    return churn_brief.SpecQuote(facts.goal, facts.scope_in, facts.scope_out)


def unavailable(escalation_round: int, reason: str) -> dict[str, Any]:
    """The churn object when the ledger could not be read: no level is claimed,
    and the reason says why."""
    return {
        "escalation_round": escalation_round,
        "keys": [],
        "level": LEVEL_NONE,
        "net_delta": None,
        "brief": None,
        "options": [],
        "ledger_unavailable": reason,
    }


def build(
    rounds: Sequence[Round],
    escalation_round: int,
    *,
    backend: Any,
    owner: str,
    repo: str,
    spec: churn_brief.SpecQuote | None,
) -> dict[str, Any]:
    """The churn object for *rounds* (oldest first, the last being the current
    round) under *escalation_round*. The host is read only for a brief or stop
    level, and only for the net delta."""
    assessment = assess(rounds, escalation_round)
    report: dict[str, Any] = {
        "escalation_round": assessment.escalation_round,
        "keys": [
            {"rule_id": k.rule_id, "file": k.file, "rounds": k.rounds, "heads": list(k.heads)}
            for k in assessment.keys
        ],
        "level": assessment.level,
        "net_delta": None,
        "brief": None,
        "options": [],
    }
    deciding = assessment.deciding
    if assessment.level == LEVEL_NONE or deciding is None:
        return report
    delta, reason = read_net_delta(
        backend, owner=owner, repo=repo, from_head=deciding.heads[0], to_head=deciding.heads[-1]
    )
    if delta is not None:
        report["net_delta"] = delta.to_dict()
    else:
        report["net_delta_unavailable"] = reason
    report["brief"] = churn_brief.render(assessment.level, deciding, spec)
    if assessment.level == LEVEL_STOP:
        report["options"] = list(STOP_OPTIONS)
    return report


__all__ = ["build", "spec_quote", "unavailable"]
