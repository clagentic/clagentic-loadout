"""review.churn — the churn breaker's computation: how many consecutive blocked
reviews have named the same (rule, file), and what that asks of the caller.

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

Everything here is pure: the rounds come from the caller (review.churn_ledger
reads them from the posted fences), the host is never touched.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

LEVEL_NONE = "none"
LEVEL_BRIEF = "brief"
LEVEL_STOP = "stop"

OPTION_SIMPLIFY = "simplify"
OPTION_SHIP = "ship"
OPTION_CONTINUE = "continue"
#: What the caller is offered at the stop level; the choice is the lead's.
STOP_OPTIONS = (OPTION_SIMPLIFY, OPTION_SHIP, OPTION_CONTINUE)

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


@dataclass(frozen=True)
class KeyChurn:
    """A key blocking in *rounds* consecutive rounds, *heads* oldest first."""

    rule_id: str
    file: str
    rounds: int
    heads: tuple[str, ...]


@dataclass(frozen=True)
class Assessment:
    escalation_round: int
    keys: tuple[KeyChurn, ...]
    level: str

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


def assess(rounds: Sequence[Round], escalation_round: int) -> Assessment:
    """The churn of *rounds* (oldest first, the last being the current round)
    under *escalation_round*. Keys are listed most rounds first, then by rule and
    file, so the output is stable."""
    if not rounds:
        return Assessment(escalation_round, (), LEVEL_NONE)
    listed_from = min(escalation_round, _MIN_REPORTED_ROUNDS) if escalation_round > 0 else 0
    churning: list[KeyChurn] = []
    for rule_id, file in rounds[-1].keys:
        heads = _chain(rounds, (rule_id, file))
        if listed_from and len(heads) >= listed_from:
            churning.append(KeyChurn(rule_id, file, len(heads), heads))
    churning.sort(key=lambda item: (-item.rounds, item.rule_id, item.file))
    level = level_for(churning[0].rounds, escalation_round) if churning else LEVEL_NONE
    return Assessment(escalation_round, tuple(churning), level)


__all__ = [
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
