"""review.dropped_overlap — a candidate is either a finding or dropped, never both.

A dropped candidate is one the reviewer examined and ruled out. The verdict is
computed from the findings list alone, so a candidate that is also still in that
list would be scored and could block while the same comment lists it as dropped:
the posted comment would contradict itself and the gate would act on the half
the reviewer rejected. The drop wins: every post path removes such a candidate
from the findings before anything is evaluated, says so on stderr, and leaves it
listed under dropped with its reason. Refusing instead would turn a review whose
producer emits overlapping lists into no post at all.

A candidate is identified by its location and rule, ``file:line:rule_id``, the id
the rest of the review path uses and which a finding and a dropped candidate both
carry.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from typing import Any


def candidate_id(entry: Mapping[str, Any]) -> str:
    """The ``file:line:rule_id`` id of a finding or dropped candidate."""
    return f"{entry.get('file')}:{entry.get('line')}:{entry.get('rule_id')}"


def drop_overlapping(
    findings: list[dict[str, Any]],
    dropped: Iterable[Any],
    *,
    prog: str = "loadout-review",
) -> list[dict[str, Any]]:
    """*findings* without any candidate that is also in *dropped*, order kept.
    One stderr notice per removed candidate. Entries that are not mappings are
    left alone; their own validation reports them. With nothing to remove the
    same list is returned, untouched."""
    dropped_ids = {candidate_id(entry) for entry in dropped if isinstance(entry, Mapping)}
    if not dropped_ids:
        return findings
    kept: list[dict[str, Any]] = []
    for finding in findings:
        if isinstance(finding, Mapping) and candidate_id(finding) in dropped_ids:
            print(
                f"{prog}: notice: candidate {candidate_id(finding)} is in both --findings "
                "and --dropped; the drop is honoured",
                file=sys.stderr,
            )
            continue
        kept.append(finding)
    return kept


__all__ = ["candidate_id", "drop_overlapping"]
