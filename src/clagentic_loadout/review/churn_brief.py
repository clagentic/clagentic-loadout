"""review.churn_brief — the text the churn breaker hands a caller: an eight-step
brief for the next attempt, and at the stop level a request to stop first.

The text is fixed product text. It names no agent, tool or deployment, so it
reads the same wherever the verb runs; only step 1 varies, with the sealed spec
when the review was given one.
"""

from __future__ import annotations

from dataclasses import dataclass

from clagentic_loadout.merge.guidance_section import LABEL_LINE
from clagentic_loadout.review.churn import (
    LEVEL_BRIEF,
    LEVEL_STOP,
    OPTION_CONTINUE,
    OPTION_SHIP,
    OPTION_SIMPLIFY,
    COUNT_KEY,
    COUNT_PR,
    KeyChurn,
)

#: What the stop level tells the caller each option means.
_OPTION_MEANING = (
    (OPTION_SIMPLIFY, "cut the change back to the smallest design that meets the spec"),
    (OPTION_SHIP, "accept the remaining findings as known and merge what is there"),
    (OPTION_CONTINUE, "make one more attempt, following the brief below"),
)

_NO_SPEC_RESTATE = (
    "No sealed spec was given to this review. Restate the task description in your own "
    "words before changing anything."
)

STEPS = (
    "Restate the brief.",
    "Classify each finding as in-spec, fix-caused (introduced by an earlier fix in this "
    "change) or out-of-spec.",
    "Re-architect rather than patch: a finding that returns after a fix means the design "
    "is wrong at that point, so change the design instead of adding another guard.",
    "Add no new mechanism (flag, layer, retry or special case) without stating what "
    "requires it that the existing code cannot do.",
    "Fix the class once: find every site with the same defect shape and fix them together "
    "with one shared change.",
    "Send out-of-spec findings to the roadmap as follow-ups; do not fix them in this change.",
    "Write test fixtures as rows of one table, not as copies of a test body.",
    "Do not call a failure flaky without a reproduction; record the reproduction with the claim.",
)


#: Position of the "fix the class once" step in the rendered steps.
_CLASS_STEP_INDEX = 4


@dataclass(frozen=True)
class SpecQuote:
    """The parts of a sealed spec step 1 quotes."""

    goal: str
    scope_in: tuple[str, ...] = ()
    scope_out: tuple[str, ...] = ()


def _restate_step(spec: SpecQuote | None) -> str:
    if spec is None:
        return f"1. Restate the brief. {_NO_SPEC_RESTATE}"
    lines = ["1. Restate the brief, from the sealed spec this review is against.", f"   Goal: {spec.goal}"]
    for label, items in (("In scope", spec.scope_in), ("Out of scope", spec.scope_out)):
        lines.append(f"   {label}:" + ("" if items else " (none stated)"))
        lines.extend(f"   - {item}" for item in items)
    return "\n".join(lines)


def _stop_text(stop_skipped: bool, escalation_round: int) -> str:
    listed = "\n".join(f"  - {name}: {meaning}" for name, meaning in _OPTION_MEANING)
    text = (
        "STOP. Do not start another fix round yet. The project lead decides what happens "
        "next, and chooses one of:\n" + listed
    )
    if stop_skipped:
        text += (
            f"\nThe stop at round {escalation_round + 1} was not taken. This is the second "
            "instruction to stop."
        )
    return text


def _churn_line(deciding: KeyChurn | None, count: str, blocked_rounds: int) -> str:
    key_line = (
        f"Rule {deciding.rule_id} in {deciding.file} has blocked {deciding.rounds} "
        "consecutive review rounds."
        if deciding is not None
        else ""
    )
    if count == COUNT_PR:
        pr_line = (
            f"Churn: this pull request has been blocked in {blocked_rounds} review rounds, "
            "not necessarily on the same finding."
        )
        return " ".join(part for part in (pr_line, key_line) if part) + (
            " The brief for the next attempt:"
        )
    return f"Churn: {key_line[0].lower()}{key_line[1:]} The brief for the next attempt:"


def render(
    level: str,
    deciding: KeyChurn | None,
    spec: SpecQuote | None,
    *,
    count: str = COUNT_KEY,
    blocked_rounds: int = 0,
    escalation_round: int = 0,
    stop_skipped: bool = False,
) -> str:
    """The brief for *level* (brief or stop), led by the key that decided it, or,
    counting by pull request, by the number of blocked rounds."""
    if level not in (LEVEL_BRIEF, LEVEL_STOP):
        raise ValueError(f"no brief is rendered for level {level!r}")
    if deciding is None and count != COUNT_PR:
        raise ValueError("a key-counted brief needs the deciding key")
    head = [_stop_text(stop_skipped, escalation_round)] if level == LEVEL_STOP else []
    head.append(_churn_line(deciding, count, blocked_rounds))
    steps = [_restate_step(spec)]
    steps.extend(f"{number}. {step}" for number, step in enumerate(STEPS[1:], start=2))
    if deciding is not None and deciding.defect_class:
        # Step 5 is "fix the class once"; the deciding finding's own class
        # statement goes under it, labelled as the reviewer's, not as fact.
        quote = [f"   {LABEL_LINE}", f"   defect_class: {deciding.defect_class}"]
        if deciding.class_fix:
            quote.append(f"   class_fix: {deciding.class_fix}")
        steps[_CLASS_STEP_INDEX] += "\n" + "\n".join(quote)
    return "\n\n".join(head) + "\n\n" + "\n".join(steps)


__all__ = ["STEPS", "SpecQuote", "render"]
