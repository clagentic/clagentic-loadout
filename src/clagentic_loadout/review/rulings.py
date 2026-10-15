"""review.rulings — per-PR rulings on kept findings, recorded by an authorized role.

A ruling covers a kept finding the way a base-revision disposition entry does
(review.dispositions): the same kinds (by_design, false_positive, accepted_risk,
mitigated), the same expiry rules (an accepted_risk ruling expires 30 days after
it was made unless it names a date) and the same demotion to advisory. Nothing
is deleted; a cleared finding is annotated with the ruling.

One thing is stricter than a base-revision entry: a ruling NEVER clears a
security-floor finding, whatever its kind. A floor finding clears only through a
base-revision disposition entry (a reviewed change to the base branch) or by
being fixed. The evaluator enforces this when it reads the registry, and the
write verb refuses a floor finding when it records one.

What differs from a disposition is scope and source:

  * A ruling belongs to ONE repository and ONE pull request, and matches a
    finding by its fingerprint, with rule id and path as checks. It never
    clears a different PR or a different finding.
  * It is read from a ruling registry (review.ruling_provider) that the pull
    request under review can never write: not the PR branch, not the PR's own
    comments.
  * It may record a deferral: an opaque work-item reference plus a mandatory
    expiry. A deferral covers the finding at this PR only.

Registry file format (``rulings.yaml``)::

    rulings:
      - repo: some-owner/some-repo      # compared case-insensitively
        pr: 33
        fingerprint: "fp2:..."          # the finding's link hint, required
        rule_id: some-rule              # exact
        path: src/handler.py            # exact file path of the finding
        kind: accepted_risk             # by_design | false_positive | accepted_risk | mitigated
        rationale: "why this is acceptable"
        by: some-role                   # the attested role that recorded it
        requested_by: some-caller       # who asked for it, as the deployment names callers
        source_ref: "design note 4"     # opaque reference to where the decision was made
        at: 2026-09-01
        expires: 2026-10-01             # optional; accepted_risk defaults to 30 days after ``at``
        reference: ABC-123              # optional deferral; requires ``expires``

A registry that is not in this shape is rejected as a whole and applies
nothing, as for dispositions: rulings can only lower a verdict.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.review.dispositions import (
    KINDS,
    Disposition,
    DispositionsFormatError,
    date_field,
    text_field,
)

SOURCE_RULING = "ruling"

#: Used when the deployment configures no reference pattern: one token with no
#: whitespace. The reference is opaque; loadout assumes no particular tracker.
DEFAULT_REFERENCE_PATTERN = r"\A[A-Za-z0-9][A-Za-z0-9._:/#-]{0,127}\Z"

_ENTRY_KEYS = frozenset(
    {
        "repo",
        "pr",
        "fingerprint",
        "rule_id",
        "path",
        "kind",
        "rationale",
        "by",
        "requested_by",
        "source_ref",
        "at",
        "expires",
        "reference",
    }
)
_REQUIRED_KEYS = (
    "repo",
    "pr",
    "fingerprint",
    "rule_id",
    "path",
    "kind",
    "rationale",
    "by",
    "requested_by",
    "source_ref",
    "at",
)


class RulingFormatError(DispositionsFormatError):
    """The ruling registry is not in the documented shape."""


@dataclass(frozen=True)
class Ruling(Disposition):
    """A Disposition scoped to one repository and pull request.

    ``path`` is the finding's exact file path (not a glob) and ``fingerprint``
    is required, so the inherited fields keep their meaning and the evaluator
    treats a ruling like any other disposition."""

    repo: str = ""
    pr: int = 0
    #: Who asked for the ruling, as the deployment names callers; ``by`` is the
    #: attested role that wrote it.
    requested_by: str = ""
    #: Opaque pointer to where the decision was made, for the audit trail.
    source_ref: str = ""
    reference: str | None = None
    source: str = SOURCE_RULING

    def matches(self, finding_file: str, rule_id: str, fingerprint: str | None) -> bool:
        """The fingerprint decides; rule id and path are checks on it. A finding
        without a fingerprint can never be ruled on, because nothing then ties
        the ruling to that defect rather than to the file."""
        normalized = finding_file[2:] if finding_file.startswith("./") else finding_file
        return (
            fingerprint is not None
            and self.fingerprint == fingerprint
            and self.rule_id == rule_id
            and self.path == normalized
        )

    def as_entry(self) -> dict[str, Any]:
        """This ruling as a registry entry (the stored form, without ``source``)."""
        entry = super().as_dict()
        entry.update(
            repo=self.repo,
            pr=self.pr,
            requested_by=self.requested_by,
            source_ref=self.source_ref,
        )
        if self.reference is not None:
            entry["reference"] = self.reference
        return entry

    def as_dict(self) -> dict[str, Any]:
        return {**self.as_entry(), "source": self.source}


def is_ruling(entry: Any) -> bool:
    return getattr(entry, "source", None) == SOURCE_RULING


def same_repo(left: str, right: str) -> bool:
    """Repository slugs compare case-insensitively, as both hosts resolve them."""
    return left.strip().lower() == right.strip().lower()


def compile_reference_pattern(pattern: str | None) -> re.Pattern[str]:
    try:
        return re.compile(pattern or DEFAULT_REFERENCE_PATTERN)
    except re.error as exc:
        raise RulingFormatError(f"rulings reference_pattern is not a valid regex: {exc}") from exc


def check_reference(reference: str, pattern: re.Pattern[str], where: str) -> str:
    text = text_field(reference, where)
    if pattern.search(text) is None:
        raise RulingFormatError(
            f"{where} {text!r} does not match the configured work-item reference pattern "
            f"{pattern.pattern!r}"
        )
    return text


def build_ruling(
    *,
    repo: str,
    pr: Any,
    fingerprint: str,
    rule_id: str,
    path: str,
    kind: str,
    rationale: str,
    by: str,
    requested_by: str,
    source_ref: str,
    at: Any,
    expires: Any = None,
    reference: str | None = None,
    reference_pattern: re.Pattern[str] | None = None,
    where: str = "ruling",
) -> Ruling:
    """Validate the pieces of one ruling and return it. The one definition of a
    valid ruling, shared by the registry reader and the writer."""
    if kind not in KINDS:
        raise RulingFormatError(f"{where}.kind must be one of {', '.join(KINDS)}, got {kind!r}")
    if isinstance(pr, bool) or not isinstance(pr, int) or pr < 1:
        raise RulingFormatError(f"{where}.pr must be a positive pull request number, got {pr!r}")
    made = date_field(at, f"{where}.at")
    expiry = date_field(expires, f"{where}.expires") if expires is not None else None
    if expiry is not None and expiry < made:
        raise RulingFormatError(f"{where}.expires is before {where}.at")
    ref = None
    if reference is not None:
        if expiry is None:
            raise RulingFormatError(
                f"{where}.reference records a deferral, which needs {where}.expires"
            )
        ref = check_reference(
            reference, reference_pattern or compile_reference_pattern(None), f"{where}.reference"
        )
    return Ruling(
        path=text_field(path, f"{where}.path"),
        rule_id=text_field(rule_id, f"{where}.rule_id"),
        kind=kind,
        rationale=text_field(rationale, f"{where}.rationale"),
        by=text_field(by, f"{where}.by"),
        at=made,
        expires=expiry,
        fingerprint=text_field(fingerprint, f"{where}.fingerprint"),
        repo=text_field(repo, f"{where}.repo"),
        pr=pr,
        requested_by=text_field(requested_by, f"{where}.requested_by"),
        source_ref=text_field(source_ref, f"{where}.source_ref"),
        reference=ref,
    )


def parse_rulings(
    doc: Any, *, reference_pattern: re.Pattern[str] | None = None
) -> tuple[Ruling, ...]:
    """The entries of a parsed ``rulings.yaml``. A malformed entry rejects the
    whole file (RulingFormatError)."""
    if doc is None:
        return ()
    if not isinstance(doc, dict):
        raise RulingFormatError("rulings file must be a mapping with a 'rulings' list")
    stray = sorted(str(k) for k in doc if k != "rulings")
    if stray:
        raise RulingFormatError(f"unknown or misplaced key: {', '.join(stray)}")
    entries = doc.get("rulings", [])
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise RulingFormatError("rulings must be a list")
    parsed: list[Ruling] = []
    for index, entry in enumerate(entries, start=1):
        where = f"rulings[{index}]"
        if not isinstance(entry, dict):
            raise RulingFormatError(f"{where} must be a mapping")
        unknown = sorted(str(k) for k in entry if k not in _ENTRY_KEYS)
        if unknown:
            keys = ", ".join(f"{where}.{key}" for key in unknown)
            raise RulingFormatError(f"unknown or misplaced key: {keys}")
        missing = [k for k in _REQUIRED_KEYS if k not in entry]
        if missing:
            raise RulingFormatError(f"{where} is missing: {', '.join(missing)}")
        try:
            parsed.append(
                build_ruling(
                    repo=entry["repo"],
                    pr=entry["pr"],
                    fingerprint=entry["fingerprint"],
                    rule_id=entry["rule_id"],
                    path=entry["path"],
                    kind=entry["kind"],
                    rationale=entry["rationale"],
                    by=entry["by"],
                    requested_by=entry["requested_by"],
                    source_ref=entry["source_ref"],
                    at=entry["at"],
                    expires=entry.get("expires"),
                    reference=entry.get("reference"),
                    reference_pattern=reference_pattern,
                    where=where,
                )
            )
        except RulingFormatError:
            raise
        except DispositionsFormatError as exc:
            raise RulingFormatError(str(exc)) from exc
    return tuple(parsed)


def rulings_for_scope(rulings: Iterable[Ruling], repo: str, pr: int) -> tuple[Ruling, ...]:
    """The rulings that belong to *repo* and pull request *pr*, in file order."""
    return tuple(r for r in rulings if r.pr == pr and same_repo(r.repo, repo))


def rulings_document(rulings: Iterable[Ruling]) -> dict[str, Any]:
    """The registry document for *rulings*."""
    return {"rulings": [r.as_entry() for r in rulings]}


__all__ = [
    "DEFAULT_REFERENCE_PATTERN",
    "SOURCE_RULING",
    "Ruling",
    "RulingFormatError",
    "build_ruling",
    "check_reference",
    "compile_reference_pattern",
    "is_ruling",
    "parse_rulings",
    "rulings_document",
    "rulings_for_scope",
    "same_repo",
]
