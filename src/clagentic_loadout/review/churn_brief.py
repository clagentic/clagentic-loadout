"""review.churn_brief — the text the churn breaker hands a caller: an eight-step
brief for the next attempt, and at the stop level a request to stop first.

The text is fixed product text. It names no agent, tool or deployment, so it
reads the same wherever the verb runs; only step 1 varies, with the sealed spec
when the review was given one.
"""

from __future__ import annotations

from dataclasses import dataclass

from clagentic_loadout.review.churn import (
    LEVEL_BRIEF,
    LEVEL_STOP,
    OPTION_CONTINUE,
    OPTION_SHIP,
    OPTION_SIMPLIFY,
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


def _stop_text() -> str:
    listed = "\n".join(f"  - {name}: {meaning}" for name, meaning in _OPTION_MEANING)
    return (
        "STOP. Do not start another fix round yet. The project lead decides what happens "
        "next, and chooses one of:\n" + listed
    )


def render(level: str, deciding: KeyChurn, spec: SpecQuote | None) -> str:
    """The brief for *level* (brief or stop), led by the key that decided it."""
    if level not in (LEVEL_BRIEF, LEVEL_STOP):
        raise ValueError(f"no brief is rendered for level {level!r}")
    head = [_stop_text()] if level == LEVEL_STOP else []
    head.append(
        f"Churn: rule {deciding.rule_id} in {deciding.file} has blocked {deciding.rounds} "
        "consecutive review rounds. The brief for the next attempt:"
    )
    steps = [_restate_step(spec)]
    steps.extend(f"{number}. {step}" for number, step in enumerate(STEPS[1:], start=2))
    return "\n\n".join(head) + "\n\n" + "\n".join(steps)


__all__ = ["STEPS", "SpecQuote", "render"]
