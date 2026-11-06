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

Once the level is brief or stop the object also carries ``rounds``, one entry
per blocked round, oldest first::

    {"head_sha": "<sha>", "blocking_count": 2, "by": {"<group>": 1, "<group>": 1},
     "fix_delta": {"added": 3, "removed": 1} | null}

``by`` counts the round's blocking keys by the finding's ``defect_class`` when it
states one, else by ``rule_id``. ``fix_delta`` is the change from the previous
blocked head to this one, from the host's compare; it is null, with
``fix_delta_unavailable`` giving the reason, for the first round and for any
pair the host could not compare. ``stop_skipped`` is added (true) when the
caller is past the stop.

A key entry also carries ``defect_class`` (and ``class_fix``) when the reviewer
stated them for the key; the brief's step 5 quotes them.

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
    COUNT_KEY,
    COUNT_PR,
    LEVEL_NONE,
    LEVEL_STOP,
    STOP_OPTIONS,
    KeyChurn,
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


def _key_entry(key: KeyChurn) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "rule_id": key.rule_id, "file": key.file, "rounds": key.rounds, "heads": list(key.heads)
    }
    # Present only when the reviewer stated a class for the key, so a review
    # that states none yields the entry it always did.
    if key.defect_class:
        entry["defect_class"] = key.defect_class
        if key.class_fix:
            entry["class_fix"] = key.class_fix
    return entry


def _group(round_: Round, key: tuple[str, str]) -> str:
    defect_class = round_.classes.get(key, (None, None))[0]
    return defect_class or key[0]


def _round_entries(blocked: Sequence[Round], backend: Any, owner: str, repo: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    previous: str | None = None
    for item in blocked:
        by: dict[str, int] = {}
        for key in sorted(item.keys):
            group = _group(item, key)
            by[group] = by.get(group, 0) + 1
        entry: dict[str, Any] = {
            "head_sha": item.head_sha, "blocking_count": len(item.keys), "by": by,
            "fix_delta": None,
        }
        if previous is None:
            entry["fix_delta_unavailable"] = "the first blocked round has no earlier head"
        else:
            delta, reason = read_net_delta(
                backend, owner=owner, repo=repo, from_head=previous, to_head=item.head_sha
            )
            if delta is None:
                entry["fix_delta_unavailable"] = reason
            else:
                entry["fix_delta"] = {
                    "added": sum(f.added for f in delta.files),
                    "removed": sum(f.removed for f in delta.files),
                }
        previous = item.head_sha
        entries.append(entry)
    return entries


def build(
    rounds: Sequence[Round],
    escalation_round: int,
    *,
    backend: Any,
    owner: str,
    repo: str,
    spec: churn_brief.SpecQuote | None,
    churn_count: str = COUNT_KEY,
) -> dict[str, Any]:
    """The churn object for *rounds* (oldest first, the last being the current
    round) under *escalation_round*, counted as *churn_count*. The host is read
    only for a brief or stop level, for the net delta and the per-round fix
    deltas."""
    assessment = assess(rounds, escalation_round, churn_count)
    report: dict[str, Any] = {
        "escalation_round": assessment.escalation_round,
        "keys": [_key_entry(k) for k in assessment.keys],
        "level": assessment.level,
        "net_delta": None,
        "brief": None,
        "options": [],
    }
    deciding = assessment.deciding
    if assessment.level == LEVEL_NONE or (deciding is None and assessment.count != COUNT_PR):
        return report
    if assessment.count == COUNT_PR:
        heads = tuple(item.head_sha for item in assessment.blocked)
    else:
        heads = deciding.heads if deciding is not None else ()
    delta, reason = read_net_delta(
        backend, owner=owner, repo=repo, from_head=heads[0], to_head=heads[-1]
    )
    if delta is not None:
        report["net_delta"] = delta.to_dict()
    else:
        report["net_delta_unavailable"] = reason
    report["brief"] = churn_brief.render(
        assessment.level, deciding, spec, count=assessment.count,
        blocked_rounds=len(assessment.blocked), escalation_round=escalation_round,
        stop_skipped=assessment.stop_skipped,
    )
    report["rounds"] = _round_entries(assessment.blocked, backend, owner, repo)
    if assessment.stop_skipped:
        report["stop_skipped"] = True
    if assessment.level == LEVEL_STOP:
        report["options"] = list(STOP_OPTIONS)
    return report


__all__ = ["build", "spec_quote", "unavailable"]
