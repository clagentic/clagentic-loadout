"""review.rubric — severity computed from facts and stakes, by table.

The rubric is published in docs/review-evaluator.md and implemented here as
data, so the document and the code cannot drift apart silently (a test renders
the table from this module and compares it to the document).

Computation, for one finding:

 1. Impact points: each of confidentiality, integrity, availability scores
    none 0, partial 1, full 3, unknown 3 (worst case); the data class of the
    path then shifts the sum (public -1, internal 0, confidential +1,
    regulated +2), kept at 1 or more when any impact was stated, and at 0 when
    none was. The sum maps to an impact tier: 0 none, 1-2 limited, 3-5
    serious, 6 or more severe.
 2. Effective precondition rank: none 0, can_open_pr 1, can_merge 2,
    has_host_access 3, unknown 0 (worst case). Stakes then move it: an exposed
    path lowers the rank by one (floor 0), and a path whose merges are
    uncontrolled collapses can_merge to the can_open_pr rank, because anyone
    who can open a pull request can then land it.
 3. SEVERITY_TABLE[impact tier][effective precondition rank].
 4. Durability: ephemeral lowers the result one level (a result above none
    stays at least low).
 5. Reachability: ``no`` caps the result at low. ``yes`` and ``unknown`` change
    nothing, so an unknown never makes a finding milder.

Blocking is decided in review.evaluate: severity at or above the configured
threshold, or a security-floor category on an exposed path.
"""

from __future__ import annotations

from dataclasses import dataclass

from clagentic_loadout.review.facts import Facts
from clagentic_loadout.review.stakes import EXPOSED, UNCONTROLLED, PathStakes

SEVERITIES = ("none", "low", "medium", "high", "critical")
DEFAULT_BLOCK_AT = "high"

IMPACT_POINTS = {"none": 0, "partial": 1, "full": 3, "unknown": 3}
DATA_CLASS_SHIFT = {"public": -1, "internal": 0, "confidential": 1, "regulated": 2}
PRECONDITION_RANK = {"none": 0, "can_open_pr": 1, "can_merge": 2, "has_host_access": 3, "unknown": 0}

#: (lowest points, tier name), checked from the top.
IMPACT_TIERS = ((6, "severe"), (3, "serious"), (1, "limited"), (0, "none"))

#: Severity by impact tier, then by effective precondition rank 0..3.
SEVERITY_TABLE: dict[str, tuple[str, str, str, str]] = {
    "severe": ("critical", "high", "high", "medium"),
    "serious": ("high", "high", "medium", "low"),
    "limited": ("medium", "medium", "low", "low"),
    "none": ("none", "none", "none", "none"),
}

_CAN_OPEN_PR_RANK = PRECONDITION_RANK["can_open_pr"]
_CAN_MERGE_RANK = PRECONDITION_RANK["can_merge"]


def severity_rank(severity: str) -> int:
    return SEVERITIES.index(severity)


@dataclass(frozen=True)
class Score:
    severity: str
    impact_points: int
    impact_tier: str
    precondition_rank: int
    effective_precondition_rank: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "severity": self.severity,
            "impact_points": self.impact_points,
            "impact_tier": self.impact_tier,
            "precondition_rank": self.precondition_rank,
            "effective_precondition_rank": self.effective_precondition_rank,
        }


def impact_points(facts: Facts, stakes: PathStakes) -> int:
    base = sum(IMPACT_POINTS[level] for level in facts.impact)
    if base == 0:
        return 0
    return max(1, base + DATA_CLASS_SHIFT[stakes.data_class])


def impact_tier(points: int) -> str:
    for floor, name in IMPACT_TIERS:
        if points >= floor:
            return name
    return "none"


def effective_precondition_rank(facts: Facts, stakes: PathStakes) -> int:
    rank = PRECONDITION_RANK[facts.attacker_precondition]
    if stakes.exposure == EXPOSED:
        rank = max(0, rank - 1)
    if stakes.merge_control == UNCONTROLLED and rank == _CAN_MERGE_RANK:
        rank = _CAN_OPEN_PR_RANK
    return rank


def score(facts: Facts, stakes: PathStakes) -> Score:
    """The computed severity of a finding with *facts* on a path with *stakes*."""
    points = impact_points(facts, stakes)
    tier = impact_tier(points)
    raw_rank = PRECONDITION_RANK[facts.attacker_precondition]
    rank = effective_precondition_rank(facts, stakes)
    level = severity_rank(SEVERITY_TABLE[tier][rank])
    if facts.durability == "ephemeral" and level > 0:
        level = max(1, level - 1)
    if facts.reachable == "no":
        level = min(level, severity_rank("low"))
    return Score(SEVERITIES[level], points, tier, raw_rank, rank)


__all__ = [
    "DATA_CLASS_SHIFT",
    "DEFAULT_BLOCK_AT",
    "IMPACT_POINTS",
    "IMPACT_TIERS",
    "PRECONDITION_RANK",
    "SEVERITIES",
    "SEVERITY_TABLE",
    "Score",
    "effective_precondition_rank",
    "impact_points",
    "impact_tier",
    "score",
    "severity_rank",
]
