"""spec.removal_ledger -- matching a removal report (spec.removals) against the
removal ledger of a sealed spec, and rendering the result.

The ledger holds one line per removed item: ``{item, kind, disposition}``, the
disposition null or one of ``{kind: replaced_by, replacement}``, ``{kind: moved_to,
repo, task}`` or ``{kind: retired_by, decision}``. A spec sealed before the section
existed has no ledger, so every removed item is undisposed. Reporting only: this
judges nothing about whether a disposition is true.

A ledger is passed around as the mapping ``ledger_of`` returns: (item, kind) to its
disposition. An empty mapping is no spec, or a spec with no ledger.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

Ledger = Mapping[tuple[str, str], Mapping[str, Any] | None]


def describe(disposition: Mapping[str, Any] | None) -> str:
    """The ledger line as one phrase, or ``undisposed`` when there is none."""
    if not disposition:
        return "undisposed"
    kind = disposition.get("kind")
    if kind == "replaced_by":
        return f"replaced by {disposition.get('replacement')}"
    if kind == "moved_to":
        return f"moved to {disposition.get('repo')} ({disposition.get('task')})"
    if kind == "retired_by":
        return f"retired by {disposition.get('decision')}"
    return "undisposed"


def ledger_of(doc: Mapping[str, Any] | None) -> dict[tuple[str, str], Mapping[str, Any] | None]:
    """The ledger of the sealed spec *doc* keyed by (item, kind); empty for no spec
    or a spec with no ledger section."""
    section = (doc or {}).get("removal_ledger") or {}
    return {(line["item"], line["kind"]): line.get("disposition") for line in section.get("items", [])}


def match(report: Mapping[str, Any], ledger: Ledger) -> list[dict[str, Any]]:
    """Each removed item of *report* with its ledger line: ``{item, kind,
    disposition}``, the disposition null when the ledger holds none."""
    return [
        {"item": entry["item"], "kind": entry["kind"], "disposition": ledger.get((entry["item"], entry["kind"]))}
        for entry in report.get("items", [])
    ]


def undisposed(matched: Sequence[Mapping[str, Any]]) -> list[str]:
    """The keys of the matched items that have no ledger line."""
    return [entry["item"] for entry in matched if not entry["disposition"]]


def result_block(report: Mapping[str, Any], ledger: Ledger) -> dict[str, Any]:
    """The ``removed_behaviour`` object of a review result: the report matched
    against the spec's ledger."""
    matched = match(report, ledger)
    return {
        "status": report["status"],
        "base_sha": report["base_sha"],
        "head_sha": report["head_sha"],
        "items": matched,
        "undisposed": undisposed(matched),
        "not_computed": list(report.get("not_computed", [])),
        "note": report["note"],
    }


__all__ = ["Ledger", "describe", "ledger_of", "match", "result_block", "undisposed"]
