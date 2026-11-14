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
carry. Distinct candidates can share that id, so a dropped entry that carries a
message removes only the finding with the same message; one without a message
(an older producer) removes every finding at that id.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from typing import Any


def candidate_id(entry: Mapping[str, Any]) -> str:
    """The ``file:line:rule_id`` id of a finding or dropped candidate."""
    return f"{entry.get('file')}:{entry.get('line')}:{entry.get('rule_id')}"


def _is_dropped(finding: Mapping[str, Any], dropped_by_id: Mapping[str, set[str] | None]) -> bool:
    """Whether *finding* is a dropped candidate. Two candidates can share
    ``file:line:rule_id`` (and a fingerprint), so the message tells them apart
    when the dropped entry carries one. A dropped entry without a message, or a
    finding without one, can only be matched by location."""
    key = candidate_id(finding)
    if key not in dropped_by_id:
        return False
    messages = dropped_by_id[key]
    message = finding.get("message")
    if messages is None or not isinstance(message, str):
        return True
    return message in messages


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
    # Per location id: the messages of the dropped entries there, or None when
    # any of them carries no message and so can only match by location.
    dropped_by_id: dict[str, set[str] | None] = {}
    for entry in dropped:
        if not isinstance(entry, Mapping):
            continue
        key = candidate_id(entry)
        message = entry.get("message")
        messages = dropped_by_id.get(key, set())
        if not isinstance(message, str):
            dropped_by_id[key] = None
        elif messages is not None:
            messages.add(message)
            dropped_by_id[key] = messages
    if not dropped_by_id:
        return findings
    kept: list[dict[str, Any]] = []
    for finding in findings:
        if isinstance(finding, Mapping) and _is_dropped(finding, dropped_by_id):
            print(
                f"{prog}: notice: candidate {candidate_id(finding)} is in both --findings "
                "and --dropped; the drop is honoured",
                file=sys.stderr,
            )
            continue
        kept.append(finding)
    return kept


__all__ = ["candidate_id", "drop_overlapping"]
