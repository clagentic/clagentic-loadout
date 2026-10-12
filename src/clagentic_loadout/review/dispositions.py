"""review.dispositions — standing rulings on findings, read from the base revision.

A disposition says "findings of this rule on these paths are not blockers, and
here is why". It is policy that can LOWER a verdict, so the file is read only
from the trusted base revision (review.policy_source); an entry first added in
the diff being gated cannot clear that diff's own findings.

File format (``dispositions.yaml``)::

    dispositions:
      - path: "src/legacy/**"        # glob, ``**`` spans directories
        rule_id: some-rule           # exact rule id
        kind: accepted_risk          # by_design | false_positive | accepted_risk | mitigated
        rationale: "why this is acceptable"
        by: someone                  # who ruled
        at: 2026-09-01               # when
        expires: 2026-12-01          # optional; accepted_risk defaults to 30 days after ``at``
        fingerprint: "fp2:..."       # optional link hint, narrows a match, never required

A matched finding is annotated with the disposition and demoted to advisory,
never deleted. A security-floor finding cannot be cleared by ``by_design``. An
expired entry does not apply and is reported.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.review.path_glob import path_matches

BY_DESIGN = "by_design"
FALSE_POSITIVE = "false_positive"
ACCEPTED_RISK = "accepted_risk"
MITIGATED = "mitigated"
KINDS = (BY_DESIGN, FALSE_POSITIVE, ACCEPTED_RISK, MITIGATED)

DEFAULT_ACCEPTED_RISK_DAYS = 30

_ENTRY_KEYS = frozenset(
    {"path", "rule_id", "kind", "rationale", "by", "at", "expires", "fingerprint"}
)
_REQUIRED_KEYS = ("path", "rule_id", "kind", "rationale", "by", "at")


class DispositionsFormatError(ValueError):
    """``dispositions.yaml`` is not in the documented shape."""


@dataclass(frozen=True)
class Disposition:
    path: str
    rule_id: str
    kind: str
    rationale: str
    by: str
    at: dt.date
    expires: dt.date | None = None
    fingerprint: str | None = None

    @property
    def effective_expiry(self) -> dt.date | None:
        if self.expires is not None:
            return self.expires
        if self.kind == ACCEPTED_RISK:
            return self.at + dt.timedelta(days=DEFAULT_ACCEPTED_RISK_DAYS)
        return None

    def is_expired(self, today: dt.date) -> bool:
        expiry = self.effective_expiry
        return expiry is not None and today > expiry

    def matches(self, finding_file: str, rule_id: str, fingerprint: str | None) -> bool:
        """Path glob plus rule id. A fingerprint on the entry narrows the match
        only when the finding has one too: it is a link hint, never an identity."""
        if self.rule_id != rule_id or not path_matches(self.path, finding_file):
            return False
        if self.fingerprint is not None and fingerprint is not None:
            return self.fingerprint == fingerprint
        return True

    def as_dict(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "path": self.path,
            "rule_id": self.rule_id,
            "kind": self.kind,
            "rationale": self.rationale,
            "by": self.by,
            "at": self.at.isoformat(),
        }
        expiry = self.effective_expiry
        if expiry is not None:
            entry["expires"] = expiry.isoformat()
        if self.fingerprint is not None:
            entry["fingerprint"] = self.fingerprint
        return entry


def _date(value: Any, where: str) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise DispositionsFormatError(f"{where} must be a date like 2026-09-01, got {value!r}")


def _text(value: Any, where: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise DispositionsFormatError(f"{where} must be a non-empty string")


def parse_dispositions(doc: Any) -> tuple[Disposition, ...]:
    """The entries of a parsed ``dispositions.yaml``. Any malformed entry
    rejects the whole file (DispositionsFormatError): the policy can only lower
    verdicts, so a half-understood file applies nothing."""
    if doc is None:
        return ()
    if not isinstance(doc, dict) or set(doc) - {"dispositions"}:
        raise DispositionsFormatError("dispositions file must be a mapping with a 'dispositions' list")
    entries = doc.get("dispositions", [])
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise DispositionsFormatError("dispositions must be a list")
    parsed: list[Disposition] = []
    for index, entry in enumerate(entries, start=1):
        where = f"dispositions[{index}]"
        if not isinstance(entry, dict):
            raise DispositionsFormatError(f"{where} must be a mapping")
        unknown = sorted(str(k) for k in entry if k not in _ENTRY_KEYS)
        if unknown:
            raise DispositionsFormatError(f"{where} has unknown keys: {', '.join(unknown)}")
        missing = [k for k in _REQUIRED_KEYS if k not in entry]
        if missing:
            raise DispositionsFormatError(f"{where} is missing: {', '.join(missing)}")
        kind = entry["kind"]
        if kind not in KINDS:
            raise DispositionsFormatError(f"{where}.kind must be one of {', '.join(KINDS)}, got {kind!r}")
        expires = _date(entry["expires"], f"{where}.expires") if entry.get("expires") is not None else None
        at = _date(entry["at"], f"{where}.at")
        if expires is not None and expires < at:
            raise DispositionsFormatError(f"{where}.expires is before {where}.at")
        fingerprint = entry.get("fingerprint")
        if fingerprint is not None:
            fingerprint = _text(fingerprint, f"{where}.fingerprint")
        parsed.append(
            Disposition(
                path=_text(entry["path"], f"{where}.path"),
                rule_id=_text(entry["rule_id"], f"{where}.rule_id"),
                kind=kind,
                rationale=_text(entry["rationale"], f"{where}.rationale"),
                by=_text(entry["by"], f"{where}.by"),
                at=at,
                expires=expires,
                fingerprint=fingerprint,
            )
        )
    return tuple(parsed)


def clearing_stanza(file: str, rule_id: str, kind: str, today: dt.date) -> str:
    """The exact ``dispositions.yaml`` entry that would clear a finding, once it
    is present on the base revision. ``rationale`` and ``by`` are left for the
    person ruling to fill in; an accepted_risk entry names its expiry."""
    lines = [
        "dispositions:",
        f"  - path: {_quote(file)}",
        f"    rule_id: {_quote(rule_id)}",
        f"    kind: {kind}",
        '    rationale: "<why this finding does not block>"',
        '    by: "<who is ruling>"',
        f"    at: {today.isoformat()}",
    ]
    if kind == ACCEPTED_RISK:
        expiry = today + dt.timedelta(days=DEFAULT_ACCEPTED_RISK_DAYS)
        lines.append(f"    expires: {expiry.isoformat()}")
    return "\n".join(lines)


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


__all__ = [
    "ACCEPTED_RISK",
    "BY_DESIGN",
    "DEFAULT_ACCEPTED_RISK_DAYS",
    "FALSE_POSITIVE",
    "KINDS",
    "MITIGATED",
    "Disposition",
    "DispositionsFormatError",
    "clearing_stanza",
    "parse_dispositions",
]
