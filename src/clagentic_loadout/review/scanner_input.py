"""review.scanner_input — read and validate the scanner results a review is given.

The file reports what each scanner did and what it found (see
``schemas/scanner-input.schema.json``). It is the scanner adapter's output, not
the reviewer's judgement, and it is read strictly: an unknown key, an unknown
status, a malformed hit, a hit listed under a scanner that did not complete, or
a file stamped for another head is refused with the reason, never repaired.

Nothing here decides whether a hit is part of the change under review, or what
category of defect a scanner looks for: that is review.scanner_findings, from the
diff and from the repository's policy at the base revision. The file is
reviewer-writable, so it carries neither.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clagentic_loadout.merge.fence_syntax import find_fence_syntax
from clagentic_loadout.merge.fence_state import (
    SCANNER_FAILED,
    SCANNER_NOT_APPLICABLE,
    SCANNER_NOT_INVOKED,
    SCANNER_RAN,
    SCANNER_STATUSES,
)
from clagentic_loadout.sha import FULL_SHA_RE

#: Recorded when a scanner that gave no usable answer says nothing about why.
#: It is only ever attached to a status that is unsatisfied regardless.
DEFAULT_REASON = "the scanner reported no reason"

_SCANNER_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
_RULE_RE = re.compile(r"\A[^\s`~]{1,200}\Z")
_MAX_MESSAGE_CHARS = 300
_TOP_KEYS = ("head_sha", "scanners")
_SCANNER_KEYS = ("scanner", "status", "reason", "hits")
_HIT_KEYS = ("file", "line", "rule_id", "message")


class ScannerInputError(ValueError):
    """The scanner input is unreadable, malformed, or for another head."""


@dataclass(frozen=True)
class ScannerHit:
    scanner: str
    file: str
    line: int
    rule_id: str
    message: str


@dataclass(frozen=True)
class ScannerResult:
    scanner: str
    status: str
    reason: str | None
    hits: tuple[ScannerHit, ...]


@dataclass(frozen=True)
class ScannerInput:
    head_sha: str
    scanners: tuple[ScannerResult, ...]

    def scanners_run(self) -> list[dict[str, str]]:
        """The ``scanners_run`` entries of the verdict fence, one per scanner."""
        entries = []
        for result in self.scanners:
            entry = {"scanner": result.scanner, "status": result.status}
            if result.reason is not None:
                entry["reason"] = result.reason
            entries.append(entry)
        return entries

    @property
    def hits(self) -> tuple[ScannerHit, ...]:
        return tuple(hit for result in self.scanners for hit in result.hits)


def _one_line(text: str) -> str:
    """*text* as one bounded line that cannot open a fenced block."""
    flat = " ".join(text.replace("`", "'").split())
    flat = re.sub(r"~{3,}", "~", flat)
    if len(flat) > _MAX_MESSAGE_CHARS:
        flat = flat[: _MAX_MESSAGE_CHARS - 3] + "..."
    return flat


def _object(value: Any, where: str, allowed: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScannerInputError(f"{where} must be a JSON object, got {type(value).__name__}")
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ScannerInputError(f"{where} has unknown field(s) {unknown}; allowed: {list(allowed)}")
    return value


def _hit(raw: Any, where: str, scanner: str) -> ScannerHit:
    entry = _object(raw, where, _HIT_KEYS)
    file_value, line = entry.get("file"), entry.get("line")
    rule_id, message = entry.get("rule_id"), entry.get("message")
    if not isinstance(file_value, str) or not file_value.strip():
        raise ScannerInputError(f"{where}.file must be a non-empty string")
    path = file_value[2:] if file_value.startswith("./") else file_value
    if path.startswith("/") or "\\" in path or ".." in path.split("/") or not path:
        raise ScannerInputError(
            f"{where}.file must be a repository-relative path, got {file_value!r}"
        )
    if find_fence_syntax(path) is not None:
        raise ScannerInputError(f"{where}.file must not contain fence syntax, got {file_value!r}")
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        raise ScannerInputError(f"{where}.line must be an integer of 1 or greater, got {line!r}")
    if not isinstance(rule_id, str) or not _RULE_RE.match(rule_id):
        raise ScannerInputError(
            f"{where}.rule_id must be 1-200 characters without whitespace, backtick or tilde, "
            f"got {rule_id!r}"
        )
    if not isinstance(message, str) or not _one_line(message):
        raise ScannerInputError(f"{where}.message must be a non-empty string")
    return ScannerHit(scanner, path, line, rule_id, _one_line(message))


def _result(raw: Any, where: str) -> ScannerResult:
    entry = _object(raw, where, _SCANNER_KEYS)
    name, status = entry.get("scanner"), entry.get("status")
    if not isinstance(name, str) or not _SCANNER_NAME_RE.match(name):
        raise ScannerInputError(
            f"{where}.scanner must be 1-64 characters of letters, digits, '.', '_', ':' or '-', "
            f"starting with a letter or digit, got {name!r}"
        )
    if status not in SCANNER_STATUSES:
        raise ScannerInputError(
            f"{where}.status must be one of {list(SCANNER_STATUSES)}, got {status!r}"
        )
    reason = entry.get("reason")
    if reason is not None:
        if not isinstance(reason, str) or not _one_line(reason):
            raise ScannerInputError(f"{where}.reason must be a non-empty string")
        reason = _one_line(reason)
    if status == SCANNER_NOT_APPLICABLE and reason is None:
        raise ScannerInputError(f"{where}: status {status!r} needs a reason saying why")
    if status in (SCANNER_NOT_INVOKED, SCANNER_FAILED) and reason is None:
        reason = DEFAULT_REASON
    raw_hits = entry.get("hits", [])
    if not isinstance(raw_hits, list):
        raise ScannerInputError(f"{where}.hits must be a JSON array")
    if raw_hits and status != SCANNER_RAN:
        raise ScannerInputError(
            f"{where}: scanner {name!r} has status {status!r} but lists hits; only a scanner "
            "that ran completely reports a result"
        )
    hits = tuple(_hit(item, f"{where}.hits[{i}]", name) for i, item in enumerate(raw_hits))
    return ScannerResult(name, status, reason, hits)


def parse_scanner_input(data: Any, *, reviewed_head: str, source: str = "scanner input") -> ScannerInput:
    """Validate *data* and return it, refusing a file stamped for a head other
    than *reviewed_head*."""
    document = _object(data, source, _TOP_KEYS)
    head = document.get("head_sha")
    if not isinstance(head, str) or not FULL_SHA_RE.match(head):
        raise ScannerInputError(f"{source}: head_sha must be 40 lowercase hex characters, got {head!r}")
    if head != reviewed_head:
        raise ScannerInputError(
            f"{source} is stamped for head {head}, but the reviewed head is {reviewed_head}; "
            "run the scanners at the reviewed head"
        )
    raw_scanners = document.get("scanners")
    if not isinstance(raw_scanners, list):
        raise ScannerInputError(f"{source}: scanners must be a JSON array")
    results = tuple(_result(item, f"{source}: scanners[{i}]") for i, item in enumerate(raw_scanners))
    seen: set[str] = set()
    for result in results:
        if result.scanner in seen:
            raise ScannerInputError(f"{source}: scanner {result.scanner!r} is listed more than once")
        seen.add(result.scanner)
    return ScannerInput(head, results)


def load_scanner_input(path: str | Path, *, reviewed_head: str) -> ScannerInput:
    """Read the scanner input file at *path* for the reviewed head."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScannerInputError(f"cannot read scanner input {str(path)!r}: {exc}") from exc
    return parse_scanner_input(data, reviewed_head=reviewed_head, source=f"scanner input {str(path)!r}")


__all__ = [
    "DEFAULT_REASON",
    "ScannerHit",
    "ScannerInput",
    "ScannerInputError",
    "ScannerResult",
    "load_scanner_input",
    "parse_scanner_input",
]
