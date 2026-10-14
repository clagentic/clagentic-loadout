"""review.dropped_overlap — a candidate is either a finding or dropped, never both.

A dropped candidate is one the reviewer examined and ruled out. The verdict is
computed from the findings list alone, so a candidate that is also still in that
list is scored and can block while the same comment lists it as dropped: the
posted comment would contradict itself and the gate would act on the half the
reviewer rejected. Whatever produced the two lists, the post path refuses the
overlap instead of choosing a side.

A finding's identity here is its location and rule, ``file:line:rule_id``, the
same id the rest of the review path uses. A dropped candidate carries exactly
those three fields, so the match needs nothing the caller does not already state.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def candidate_id(entry: Mapping[str, Any]) -> str:
    """The ``file:line:rule_id`` id of a finding or dropped candidate."""
    return f"{entry.get('file')}:{entry.get('line')}:{entry.get('rule_id')}"


def overlapping_ids(
    findings: Iterable[Mapping[str, Any]], dropped: Iterable[Mapping[str, Any]]
) -> list[str]:
    """Ids present in both *findings* and *dropped*, in finding order, each once.
    Entries that are not mappings are skipped; their own validation reports them."""
    dropped_ids = {candidate_id(entry) for entry in dropped if isinstance(entry, Mapping)}
    seen: set[str] = set()
    shared: list[str] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        found = candidate_id(finding)
        if found in dropped_ids and found not in seen:
            seen.add(found)
            shared.append(found)
    return shared


def overlap_message(shared: list[str]) -> str:
    """The refusal text for a non-empty overlap."""
    return (
        f"{len(shared)} candidate(s) are both in the findings and in the dropped list: "
        + ", ".join(shared)
        + ". A dropped candidate is not a finding and must not be scored or block. "
        "Remove each from the findings, or from the dropped list if it should be scored, "
        "then post again"
    )


__all__ = ["candidate_id", "overlap_message", "overlapping_ids"]
