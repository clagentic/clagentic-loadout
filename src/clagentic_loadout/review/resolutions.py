"""review.resolutions — findings a caller has ruled resolved or refuted.

A caller (an orchestrator, a person) can know that a prior finding no longer
applies for reasons the reviewer cannot see. It says so with a JSON file passed
to `loadout-review` (`--resolved-findings`): a list of entries, each naming a
finding by id or by fingerprint, with an optional reason. The file is opaque
caller input; this module learns nothing about who wrote it.

An entry is one of:

  "<ref>"                                    shorthand for {"id": "<ref>"}
  {"id": "<ref>", "reason": "<one line>"}    matches a finding's id or fingerprint
  {"fingerprint": "<fp>", "reason": "..."}   matches a finding's fingerprint only

A finding's id is its locator, ``file:line:rule_id``, in the numbering of the
head the finding was reported against. Its fingerprint is the optional link
hint of review.finding_identity. Matching is exact string equality. A
fingerprint is a hint, not an identity, so one entry resolves every finding
that shares it. An entry that matches no finding is reported by the caller of
`split_resolved` as unknown, never silently ignored and never an error.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clagentic_loadout.merge.fence_syntax import find_fence_syntax
from clagentic_loadout.review.finding_identity import KEY_FINGERPRINT, is_fingerprint

KEY_ID = "id"
KEY_REASON = "reason"

#: Values of the `by` field of a resolved entry.
BY_REVIEWER = "reviewer"
BY_CALLER = "caller"

#: Reason recorded when the caller supplies none.
DEFAULT_REASON = "resolved by the caller"


class ResolutionsError(ValueError):
    """The resolved-findings input is malformed."""


@dataclass(frozen=True)
class Resolution:
    """One caller ruling: *ref* identifies the finding(s), *reason* is shown
    beside them in the posted body."""

    ref: str
    fingerprint_only: bool
    reason: str

    def matches(self, finding: Mapping[str, Any]) -> bool:
        if finding.get(KEY_FINGERPRINT) == self.ref:
            return True
        return not self.fingerprint_only and locator(finding) == self.ref


def locator(finding: Mapping[str, Any]) -> str:
    """The id of a finding: ``file:line:rule_id``."""
    return f"{finding['file']}:{finding['line']}:{finding['rule_id']}"


def _one_line(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResolutionsError(f"{where} must be a non-empty string, got {value!r}")
    if "\n" in value or "\r" in value:
        raise ResolutionsError(f"{where} must be a single line")
    offending = find_fence_syntax(value)
    if offending is not None:
        raise ResolutionsError(f"{where} contains the fence-delimiter sequence {offending!r}")
    return value.strip()


def _entry(item: Any, position: int) -> Resolution:
    where = f"entry {position}"
    if isinstance(item, str):
        return Resolution(_one_line(item, where), False, DEFAULT_REASON)
    if not isinstance(item, dict):
        raise ResolutionsError(f"{where} must be a string or an object, got {type(item).__name__}")
    unknown = sorted(set(item) - {KEY_ID, KEY_FINGERPRINT, KEY_REASON})
    if unknown:
        raise ResolutionsError(
            f"{where} has unknown field(s) {unknown}; allowed: "
            f"{[KEY_ID, KEY_FINGERPRINT, KEY_REASON]}"
        )
    if (KEY_ID in item) == (KEY_FINGERPRINT in item):
        raise ResolutionsError(f"{where} must carry exactly one of {KEY_ID!r} and {KEY_FINGERPRINT!r}")
    fingerprint_only = KEY_FINGERPRINT in item
    ref = _one_line(item[KEY_FINGERPRINT if fingerprint_only else KEY_ID], f"{where}.{KEY_ID}")
    if fingerprint_only and not is_fingerprint(ref):
        raise ResolutionsError(f"{where}.{KEY_FINGERPRINT} is not a well-formed fingerprint: {ref!r}")
    reason = _one_line(item[KEY_REASON], f"{where}.{KEY_REASON}") if KEY_REASON in item else DEFAULT_REASON
    return Resolution(ref, fingerprint_only, reason)


def parse_resolutions(data: Any) -> list[Resolution]:
    """The rulings in an already-parsed resolved-findings document."""
    if not isinstance(data, list):
        raise ResolutionsError(f"must be a JSON array of entries, got {type(data).__name__}")
    return [_entry(item, position) for position, item in enumerate(data, start=1)]


def load_resolutions(path: str) -> list[Resolution]:
    """Read and validate a resolved-findings file."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResolutionsError(f"cannot read resolved-findings file {path!r}: {exc}") from exc
    try:
        return parse_resolutions(data)
    except ResolutionsError as exc:
        raise ResolutionsError(f"resolved-findings file {path!r}: {exc}") from exc


def split_resolved(
    findings: Iterable[dict[str, Any]], resolutions: Sequence[Resolution]
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], Resolution]], list[str]]:
    """Partition *findings* into (not ruled on, ruled on with the ruling that
    matched first, refs of rulings that matched nothing)."""
    remaining: list[dict[str, Any]] = []
    ruled: list[tuple[dict[str, Any], Resolution]] = []
    used: set[int] = set()
    for finding in findings:
        matching = [index for index, r in enumerate(resolutions) if r.matches(finding)]
        # Every entry that names the finding is used, so a second entry for an
        # already-ruled finding is not reported unknown; the first supplies the reason.
        used.update(matching)
        if matching:
            ruled.append((finding, resolutions[matching[0]]))
        else:
            remaining.append(finding)
    unknown = [r.ref for index, r in enumerate(resolutions) if index not in used]
    return remaining, ruled, unknown


def resolved_entry(finding: Mapping[str, Any], by: str, reason: str | None = None) -> dict[str, Any]:
    """A finding as the `resolved` list records it: its prior location and
    message, who resolved it, and the caller's reason when there is one."""
    entry: dict[str, Any] = {
        "file": finding["file"],
        "line": finding["line"],
        "rule_id": finding["rule_id"],
        "message": finding["message"],
        "by": by,
    }
    if reason is not None:
        entry[KEY_REASON] = reason
    if finding.get(KEY_FINGERPRINT):
        entry[KEY_FINGERPRINT] = finding[KEY_FINGERPRINT]
    return entry
