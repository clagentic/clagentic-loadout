"""review.facts — the closed vocabulary of facts a reviewer states about a finding.

The reviewer supplies FACTS and nothing else: where the defect sits in the
rulebook, whether it is reachable, who the attacker must be, what it costs, and
whether the damage lasts. Severity and blocking are computed from these by
review.rubric and review.evaluate; the reviewer never names them.

Every value is drawn from a closed list (published as
``schemas/review-facts.json``, kept equal to the tuples below by a test). A fact
that is absent, outside its list, or the explicit ``unknown`` is resolved to the
worst case for its dimension by the rubric, so omitting a fact can never make a
finding look milder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Rulebook rule families. ``other`` is the one non-security catch-all.
CATEGORIES = (
    "injection",
    "authn_authz",
    "secrets_exposure",
    "crypto",
    "deserialization",
    "ssrf",
    "path_traversal",
    "input_validation",
    "supply_chain",
    "ci_workflow",
    "data_exposure",
    "resource_exhaustion",
    "concurrency",
    "error_handling",
    "correctness",
    "api_contract",
    "performance",
    "maintainability",
    "test_gap",
    "other",
)

#: Categories that block on an internet-exposed path whatever their computed
#: severity: the defect classes where "low impact as stated" is most often a
#: reviewer's underestimate.
FLOOR_CATEGORIES = frozenset(
    {
        "injection",
        "authn_authz",
        "secrets_exposure",
        "deserialization",
        "ssrf",
        "path_traversal",
    }
)

REACHABLE_VALUES = ("yes", "no", "unknown")
PRECONDITIONS = ("none", "can_open_pr", "can_merge", "has_host_access", "unknown")
IMPACT_LEVELS = ("none", "partial", "full", "unknown")
IMPACT_DIMENSIONS = ("confidentiality", "integrity", "availability")
DURABILITY_VALUES = ("durable", "ephemeral", "unknown")

MAX_EVIDENCE_CHARS = 300
CWE_PATTERN = r"^CWE-[0-9]{1,5}$"

KEY_FACTS = "facts"

STATE_COMPLETE = "complete"
STATE_ABSENT = "absent"
STATE_INVALID = "invalid"

_UNKNOWN = "unknown"
_TOP_LEVEL_KEYS = frozenset(
    {"category", "cwe", "reachable", "attacker_precondition", "impact", "durability", "evidence"}
)


@dataclass(frozen=True)
class Facts:
    """One finding's facts, every field already inside its vocabulary.

    ``category`` is None when the reviewer gave none; the evaluator treats an
    unclassified finding as a floor category (the worst case for that
    dimension). ``impact`` is ordered as IMPACT_DIMENSIONS."""

    category: str | None
    cwe: str | None
    reachable: str
    attacker_precondition: str
    impact: tuple[str, str, str]
    durability: str
    evidence: str | None


@dataclass(frozen=True)
class ParsedFacts:
    """Facts read from a finding, with how complete the reviewer's statement was.

    ``state`` is ``complete`` (every required fact present and valid; an
    explicit ``unknown`` counts as present), ``absent`` (no facts object) or
    ``invalid`` (something missing or outside its vocabulary, listed in
    ``problems``). In the last two states the offending dimensions already hold
    the worst-case value."""

    facts: Facts
    state: str
    problems: tuple[str, ...] = ()


WORST_CASE_FACTS = Facts(
    category=None,
    cwe=None,
    reachable=_UNKNOWN,
    attacker_precondition=_UNKNOWN,
    impact=(_UNKNOWN, _UNKNOWN, _UNKNOWN),
    durability=_UNKNOWN,
    evidence=None,
)


def _choice(raw: dict[str, Any], key: str, allowed: tuple[str, ...], problems: list[str]) -> str:
    if key not in raw:
        problems.append(f"{key} is missing")
        return _UNKNOWN
    value = raw[key]
    if isinstance(value, str) and value in allowed:
        return value
    problems.append(f"{key} must be one of {', '.join(allowed)}, got {value!r}")
    return _UNKNOWN


def _impact(raw: dict[str, Any], problems: list[str]) -> tuple[str, str, str]:
    if "impact" not in raw:
        problems.append("impact is missing")
        return WORST_CASE_FACTS.impact
    block = raw["impact"]
    if not isinstance(block, dict):
        problems.append(f"impact must be an object, got {block!r}")
        return WORST_CASE_FACTS.impact
    extra = sorted(str(k) for k in block if k not in IMPACT_DIMENSIONS)
    if extra:
        problems.append(f"impact has unknown keys: {', '.join(extra)}")
    values = []
    for dimension in IMPACT_DIMENSIONS:
        if dimension not in block:
            problems.append(f"impact.{dimension} is missing")
            values.append(_UNKNOWN)
        elif isinstance(block[dimension], str) and block[dimension] in IMPACT_LEVELS:
            values.append(block[dimension])
        else:
            problems.append(
                f"impact.{dimension} must be one of {', '.join(IMPACT_LEVELS)}, "
                f"got {block[dimension]!r}"
            )
            values.append(_UNKNOWN)
    return (values[0], values[1], values[2])


def _category(raw: dict[str, Any], problems: list[str]) -> str | None:
    if "category" not in raw:
        problems.append("category is missing")
        return None
    value = raw["category"]
    if isinstance(value, str) and value in CATEGORIES:
        return value
    problems.append(f"category must be one of {', '.join(CATEGORIES)}, got {value!r}")
    return None


def _cwe(raw: dict[str, Any], problems: list[str]) -> str | None:
    value = raw.get("cwe")
    if value is None:
        return None
    if isinstance(value, str) and re.match(CWE_PATTERN, value):
        return value
    problems.append(f"cwe must look like CWE-79, got {value!r}")
    return None


def _evidence(raw: dict[str, Any], problems: list[str]) -> str | None:
    value = raw.get("evidence")
    if value is None:
        return None
    if isinstance(value, str) and len(value) <= MAX_EVIDENCE_CHARS:
        return value
    problems.append(f"evidence must be a string of at most {MAX_EVIDENCE_CHARS} characters")
    return None


def parse_facts(raw: Any) -> ParsedFacts:
    """Read a finding's ``facts`` value. Never raises: whatever is wrong is
    reported in ``problems`` and replaced by the worst case for that dimension."""
    if raw is None:
        return ParsedFacts(WORST_CASE_FACTS, STATE_ABSENT, ("no facts were stated",))
    if not isinstance(raw, dict):
        return ParsedFacts(
            WORST_CASE_FACTS, STATE_INVALID, (f"facts must be an object, got {type(raw).__name__}",)
        )
    problems: list[str] = []
    extra = sorted(str(k) for k in raw if k not in _TOP_LEVEL_KEYS)
    if extra:
        problems.append(f"unknown fact keys: {', '.join(extra)}")
    facts = Facts(
        category=_category(raw, problems),
        cwe=_cwe(raw, problems),
        reachable=_choice(raw, "reachable", REACHABLE_VALUES, problems),
        attacker_precondition=_choice(raw, "attacker_precondition", PRECONDITIONS, problems),
        impact=_impact(raw, problems),
        durability=_choice(raw, "durability", DURABILITY_VALUES, problems),
        evidence=_evidence(raw, problems),
    )
    state = STATE_INVALID if problems else STATE_COMPLETE
    return ParsedFacts(facts, state, tuple(problems))


__all__ = [
    "CATEGORIES",
    "CWE_PATTERN",
    "DURABILITY_VALUES",
    "FLOOR_CATEGORIES",
    "IMPACT_DIMENSIONS",
    "IMPACT_LEVELS",
    "KEY_FACTS",
    "MAX_EVIDENCE_CHARS",
    "PRECONDITIONS",
    "REACHABLE_VALUES",
    "STATE_ABSENT",
    "STATE_COMPLETE",
    "STATE_INVALID",
    "WORST_CASE_FACTS",
    "Facts",
    "ParsedFacts",
    "parse_facts",
]
