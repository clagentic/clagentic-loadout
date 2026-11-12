"""spec.remedy — the remedy entries of a spec draft, read from review records.

A review record (verdict_record.review_record, read by comment id through its one
reader, ``load_review_record``) lists the findings the review left open and,
separately, every finding it evaluated with its location and the reviewer's class
statements. An open entry names a finding by id (a digest of file, line and rule),
so each is joined to the evaluated finding with the same id to recover the
location and the class text. Nothing here parses a record file: a record is what
the reader returned.

A comment id with no record, or whose record is not valid, is a warning that names
the id; the entries of the other ids are still returned.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clagentic_loadout.artifact_store import StoreError
from clagentic_loadout.merge.fence_state import KEY_CLASS_FIX, KEY_DEFECT_CLASS
from clagentic_loadout.review.evaluation import finding_state_id
from clagentic_loadout.review.finding_identity import KEY_FINGERPRINT, is_fingerprint
from clagentic_loadout.spec import model
from clagentic_loadout.verdict_record.review_record import load_review_record

SOURCE_PREFIX = "review records"


@dataclass(frozen=True)
class RemedyRead:
    """What the records named by the author held: the entries (one per open
    blocking finding, first record wins for a repeated id), a warning per comment
    id that could not be used, and the comment ids that were read."""

    entries: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    read_ids: tuple[str, ...]


def _evaluated_by_id(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for item in record["findings"]["evaluated"]:
        if not all(key in item for key in ("file", "line", "rule_id")):
            continue
        found.setdefault(finding_state_id(item["file"], item["line"], item["rule_id"]), item)
    return found


def entries_of_record(record: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """``(entries, unmatched)``: a remedy entry per open finding of *record*, and
    the ids of open findings no evaluated finding of the record matches (they
    cannot be given a location)."""
    evaluated = _evaluated_by_id(record)
    entries: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for opened in record["findings"]["open"]:
        finding = evaluated.get(opened.get("id"))
        if finding is None:
            unmatched.append(str(opened.get("id")))
            continue
        fingerprint = opened.get(KEY_FINGERPRINT)
        if not is_fingerprint(fingerprint):
            fingerprint = finding.get(KEY_FINGERPRINT)
        entries.append(
            model.remedy_entry(
                finding_id=opened["id"],
                rule=finding["rule_id"],
                file=finding["file"],
                line=finding["line"],
                fingerprint=fingerprint if is_fingerprint(fingerprint) else None,
                comment_id=record["comment_id"],
                defect_class=finding.get(KEY_DEFECT_CLASS),
                class_fix=finding.get(KEY_CLASS_FIX),
            )
        )
    return entries, unmatched


def read_remedy(
    root: Path, comment_ids: Sequence[str], *, untrusted_roots: Sequence[Path] = ()
) -> RemedyRead:
    """The remedy entries of the review records *comment_ids* name, read under
    the review records *root*."""
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    warnings: list[str] = []
    read_ids: list[str] = []
    for comment_id in comment_ids:
        try:
            record = load_review_record(root, comment_id, untrusted_roots=untrusted_roots)
        except StoreError as exc:
            warnings.append(f"comment {comment_id}: no usable review record: {exc}")
            continue
        read_ids.append(comment_id)
        found, unmatched = entries_of_record(record)
        for entry in found:
            key = entry["fingerprint"] or entry["id"]
            if key not in seen:
                seen.add(key)
                entries.append(entry)
        for finding_id in unmatched:
            warnings.append(
                f"comment {comment_id}: open finding {finding_id} matches no evaluated finding "
                "of its record, so it has no location and is not listed"
            )
    return RemedyRead(tuple(entries), tuple(warnings), tuple(read_ids))


def remedy_note(comment_ids: Sequence[str], read: RemedyRead) -> str:
    """Why the remedy section is empty, for a draft with no entries."""
    if not comment_ids:
        return "no remedy entries: no --from-review was given"
    if not read.read_ids:
        return f"no remedy entries: none of the review records {list(comment_ids)} could be read"
    return f"no remedy entries: the review records {list(read.read_ids)} hold no open blocking finding"


def remedy_source(read: RemedyRead) -> str | None:
    return f"{SOURCE_PREFIX}: {', '.join(read.read_ids)}" if read.read_ids else None


__all__ = ["RemedyRead", "entries_of_record", "read_remedy", "remedy_note", "remedy_source"]
