"""review.churn — the churn breaker's computation: how many consecutive blocked
reviews have named the same (rule, file), and what that asks of the caller.

Two ways to count (``churn_count``): ``key`` counts per key, as described next;
``pr`` counts every distinct blocked head the role posted on the pull request as a
round, whichever keys it blocked on, so a pull request that blocks round after
round on DIFFERENT findings (fix-of-fix churn) is caught too. Under ``pr`` the
level comes from that count, and the key chains are still listed for context.

A round is one distinct head the reviewer posted a verdict for. A key
``(rule_id, file)`` is blocking in a round when that round's verdict holds it
open. A key churns when it is blocking in consecutive rounds that END in the
current one: a round in which it is absent (clean, or blocking on other keys
only) ends its chain, and so does a round whose blocking keys could not be read.

The levels are advisory output and never a refusal:

  none   the breaker is off (escalation_round 0), or no key has reached the round
  brief  the highest-round key has exactly ``escalation_round`` rounds: the caller
         is handed the net delta since that chain began and the builder brief
  stop   the highest-round key has more: the same, plus a request to stop and the
         three options a lead chooses from

Past the stop (more than ``escalation_round + 1`` rounds) the stop was not taken
and ``stop_skipped`` is set, so the text can say it is the second instruction.

Everything here is pure: the rounds come from the caller (review.churn_ledger
reads them from the posted fences), the host is never touched.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

LEVEL_NONE = "none"
LEVEL_BRIEF = "brief"
LEVEL_STOP = "stop"

OPTION_SIMPLIFY = "simplify"
OPTION_SHIP = "ship"
OPTION_CONTINUE = "continue"
#: What the caller is offered at the stop level; the choice is the lead's.
STOP_OPTIONS = (OPTION_SIMPLIFY, OPTION_SHIP, OPTION_CONTINUE)

COUNT_PR = "pr"
COUNT_KEY = "key"
#: How rounds are counted toward the level.
CHURN_COUNTS = (COUNT_PR, COUNT_KEY)

#: A repeat needs two rounds, so a key is reported as churning from its second
#: blocked round at the latest, whatever the escalation round.
_MIN_REPORTED_ROUNDS = 2

Key = tuple[str, str]


@dataclass(frozen=True)
class Round:
    """One review round: the head it was stamped for and the keys it held open.
    *head_sha* is None for a fence that could not be read, which holds nothing
    open and so ends every chain."""

    head_sha: str | None
    keys: frozenset[Key] = frozenset()
    #: The reviewer's class statement for a key, as (defect_class, class_fix),
    #: where the round's findings state one. Advisory text: it takes no part in
    #: which keys churn, so it is left out of equality.
    classes: Mapping[Key, tuple[str, str | None]] = field(default_factory=dict, compare=False)
    #: The round's findings as its fence carried them (file, line, rule_id and
    #: any class keys), for the guidance object. Left out of equality for the
    #: same reason.
    evaluated: tuple[Mapping[str, Any], ...] = field(default=(), compare=False)


@dataclass(frozen=True)
class KeyChurn:
    """A key blocking in *rounds* consecutive rounds, *heads* oldest first."""

    rule_id: str
    file: str
    rounds: int
    heads: tuple[str, ...]
    defect_class: str | None = None
    class_fix: str | None = None


@dataclass(frozen=True)
class Assessment:
    escalation_round: int
    keys: tuple[KeyChurn, ...]
    level: str
    count: str = COUNT_KEY
    #: Every round that held something open, oldest first, whatever its keys.
    blocked: tuple[Round, ...] = ()
    stop_skipped: bool = False

    @property
    def deciding_rounds(self) -> int:
        """The round count the level was taken from."""
        if self.count == COUNT_PR:
            return len(self.blocked)
        return self.keys[0].rounds if self.keys else 0

    @property
    def deciding(self) -> KeyChurn | None:
        """The key with the most rounds (the first listed on a tie)."""
        return self.keys[0] if self.keys else None


def _chain(rounds: Sequence[Round], key: Key) -> tuple[str, ...]:
    heads: list[str] = []
    for item in reversed(rounds):
        if key not in item.keys or item.head_sha is None:
            break
        heads.append(item.head_sha)
    return tuple(reversed(heads))


def with_current(prior: Sequence[Round], current: Round) -> list[Round]:
    """*prior* rounds followed by *current*. A prior round at the current head is
    dropped: the current round replaces it, as the last fence at a head does."""
    return [
        item for item in prior if item.head_sha is None or item.head_sha != current.head_sha
    ] + [current]


def level_for(rounds: int, escalation_round: int) -> str:
    """The level a key with *rounds* consecutive blocked rounds reaches."""
    if escalation_round <= 0 or rounds < escalation_round:
        return LEVEL_NONE
    return LEVEL_BRIEF if rounds == escalation_round else LEVEL_STOP


def assess(
    rounds: Sequence[Round], escalation_round: int, count: str = COUNT_KEY
) -> Assessment:
    """The churn of *rounds* (oldest first, the last being the current round)
    under *escalation_round*, counted as *count*. Keys are listed most rounds
    first, then by rule and file, so the output is stable."""
    if count not in CHURN_COUNTS:
        raise ValueError(f"unknown churn count {count!r}")
    if not rounds:
        return Assessment(escalation_round, (), LEVEL_NONE, count)
    listed_from = min(escalation_round, _MIN_REPORTED_ROUNDS) if escalation_round > 0 else 0
    churning: list[KeyChurn] = []
    for rule_id, file in rounds[-1].keys:
        heads = _chain(rounds, (rule_id, file))
        if listed_from and len(heads) >= listed_from:
            defect_class, class_fix = rounds[-1].classes.get((rule_id, file), (None, None))
            churning.append(KeyChurn(rule_id, file, len(heads), heads, defect_class, class_fix))
    churning.sort(key=lambda item: (-item.rounds, item.rule_id, item.file))
    blocked = tuple(item for item in rounds if item.head_sha is not None and item.keys)
    if count == COUNT_PR:
        # A round that could not be read holds nothing open, so it is not counted:
        # undercounting is the safe direction. The current round must itself block.
        current_blocks = bool(rounds[-1].head_sha is not None and rounds[-1].keys)
        level = level_for(len(blocked), escalation_round) if current_blocks else LEVEL_NONE
    else:
        level = level_for(churning[0].rounds, escalation_round) if churning else LEVEL_NONE
    assessment = Assessment(escalation_round, tuple(churning), level, count, blocked)
    skipped = level == LEVEL_STOP and assessment.deciding_rounds > escalation_round + 1
    return replace(assessment, stop_skipped=skipped)


__all__ = [
    "CHURN_COUNTS",
    "COUNT_KEY",
    "COUNT_PR",
    "LEVEL_BRIEF",
    "LEVEL_NONE",
    "LEVEL_STOP",
    "OPTION_CONTINUE",
    "OPTION_SHIP",
    "OPTION_SIMPLIFY",
    "STOP_OPTIONS",
    "Assessment",
    "Key",
    "KeyChurn",
    "Round",
    "assess",
    "level_for",
    "with_current",
]
