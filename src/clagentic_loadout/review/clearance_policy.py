"""review.clearance_policy — which blocking findings a reviewer may clear itself.

A reviewer can clear its own earlier blocking finding at the same head (see
merge.fence_clearance). Whether that clearance is enough for the merge gate
depends on what the finding protects, and that is policy, read from the repo
profile at the pull request's base revision like the docs allowlist and the
scanner categories, so a pull request cannot widen the list it is judged by:

    review:
      clearance:
        process_record:
          rule_ids: ["*.path-choice.5"]   # fnmatch patterns over a finding's rule id
          categories: []                  # finding categories (review.facts.CATEGORIES)

A finding in the ``process_record`` class guards a record a person can supply (for
example a compliance record in the pull request body); an evidence-bearing
clearance from the reviewer that raised it is enough. Every other finding is
security content: it keeps needing a disposition on the base branch. Nothing is
named in code. An absent policy, an invalid profile, or a finding the policy does
not classify is security content, so the default fails closed.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.review.facts import CATEGORIES

KEY_CLEARANCE = "clearance"
KEY_PROCESS_RECORD = "process_record"
KEY_RULE_IDS = "rule_ids"
KEY_CATEGORIES = "categories"

_MAX_ENTRIES = 200
_MAX_ENTRY_CHARS = 200


class ClearancePolicyError(ValueError):
    """``review.clearance`` is not in the documented shape; the message names the key path."""


@dataclass(frozen=True)
class ClearancePolicy:
    """The finding classes a reviewer's own clearance is enough for. The empty
    policy classifies nothing as process record."""

    rule_ids: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()

    def is_process_record(self, rule_id: str, categories: Collection[str | None]) -> bool:
        """Whether a finding raised under *rule_id* is process record. *categories*
        holds the category each of the block's findings states (None for a
        finding that states none). A rule pattern decides on its own; a category
        decides only when there is at least one finding and every one states a
        listed category, so an unclassified finding is never process record."""
        if any(fnmatch.fnmatchcase(rule_id, pattern) for pattern in self.rule_ids):
            return True
        return bool(self.categories) and bool(categories) and all(
            category in self.categories for category in categories
        )


def _entries(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ClearancePolicyError(f"{where} must be a list of strings")
    if len(value) > _MAX_ENTRIES:
        raise ClearancePolicyError(f"{where} may hold at most {_MAX_ENTRIES} entries")
    out: list[str] = []
    for position, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ClearancePolicyError(f"{where}[{position}] must be a non-empty string")
        text = item.strip()
        if len(text) > _MAX_ENTRY_CHARS or "\n" in text or "\r" in text:
            raise ClearancePolicyError(
                f"{where}[{position}] must be one line of at most {_MAX_ENTRY_CHARS} characters"
            )
        out.append(text)
    return tuple(dict.fromkeys(out))


def parse_clearance_policy(block: Any, where: str = "review.clearance") -> ClearancePolicy:
    """The policy in a profile's ``review.clearance`` value; the empty policy when
    absent. Strict, so a half-understood list is never half-applied."""
    if block is None:
        return ClearancePolicy()
    if not isinstance(block, dict):
        raise ClearancePolicyError(f"{where} must be a mapping")
    unknown = sorted(str(k) for k in block if k != KEY_PROCESS_RECORD)
    if unknown:
        raise ClearancePolicyError(
            "unknown or misplaced key: " + ", ".join(f"{where}.{key}" for key in unknown)
        )
    record = block.get(KEY_PROCESS_RECORD)
    if record is None:
        return ClearancePolicy()
    here = f"{where}.{KEY_PROCESS_RECORD}"
    if not isinstance(record, dict):
        raise ClearancePolicyError(f"{here} must be a mapping")
    unknown = sorted(str(k) for k in record if k not in (KEY_RULE_IDS, KEY_CATEGORIES))
    if unknown:
        raise ClearancePolicyError(
            "unknown or misplaced key: " + ", ".join(f"{here}.{key}" for key in unknown)
        )
    rule_ids = (
        _entries(record[KEY_RULE_IDS], f"{here}.{KEY_RULE_IDS}")
        if record.get(KEY_RULE_IDS) is not None
        else ()
    )
    categories = (
        _entries(record[KEY_CATEGORIES], f"{here}.{KEY_CATEGORIES}")
        if record.get(KEY_CATEGORIES) is not None
        else ()
    )
    for position, category in enumerate(categories):
        if category not in CATEGORIES:
            raise ClearancePolicyError(
                f"{here}.{KEY_CATEGORIES}[{position}] must be one of {', '.join(CATEGORIES)}, "
                f"got {category!r}"
            )
    return ClearancePolicy(rule_ids=rule_ids, categories=categories)


__all__ = [
    "KEY_CLEARANCE",
    "ClearancePolicy",
    "ClearancePolicyError",
    "parse_clearance_policy",
]
