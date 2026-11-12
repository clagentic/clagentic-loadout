"""merge.scanner_coverage — the one definition of which scanners a review requires,
whether a scanner's report satisfies that requirement, and what did not run.

The evaluator (review.evaluate, through review.evaluation) and the merge gate
(merge.verdict.check_required_scanners) both judge a verdict's ``scanners_run``
against a required set. They go through this module for all three questions, so
they cannot disagree:

  required set   ``required_scanners_for``: the union of the repo gate's
                 ``merge.required_scanners`` entry for the reviewing role (read
                 at the base commit) and the user-level
                 ``review.evaluator.required_scanners``.
  satisfaction   ``classify_scanner``: ran, or not_applicable with a reason, is
                 SATISFIED; not_invoked is NOT_RUN_REPORTED (shown, never a
                 refusal); anything else, including no report at all, is
                 NOT_RUN_FAILING and is the only outcome that feeds a refusal.
  coverage       ``scanner_coverage``: what a reader of the posted result needs
                 to tell a complete scan from a partial one.

The scanner status literals are fence_state's (SCANNER_STATUSES); nothing here
restates them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from clagentic_loadout.merge.fence_state import (
    SCANNER_NOT_APPLICABLE,
    SCANNER_NOT_INVOKED,
    SCANNER_RAN,
)

SATISFIED = "satisfied"
NOT_RUN_REPORTED = "not_run_reported"
NOT_RUN_FAILING = "not_run_failing"

#: The status a required scanner carries in the coverage report when the
#: verdict recorded no result for it at all.
STATUS_MISSING = "missing"

_NO_REASON = "no reason given"
_MISSING_REASON = "no result was reported"


class ScannerDeclaration(Protocol):
    """What the repo gate offers the accessor: the scanners it requires of a role."""

    def scanners_for(self, reviewer_name: str) -> tuple[str, ...]: ...


def required_scanners_for(
    role: str | None,
    repo_gate: ScannerDeclaration | None,
    user_level: Sequence[str] = (),
) -> tuple[str, ...]:
    """The scanners a review by *role* must have run: the repo gate's entry for
    the role, then the user-level ones, each name once. *repo_gate* is None when
    no gate applies (none declared, or the caller ignores it)."""
    declared = repo_gate.scanners_for(role) if repo_gate is not None and role is not None else ()
    return tuple(dict.fromkeys((*declared, *user_level)))


def _usable_reason(entry: Mapping[str, Any]) -> str | None:
    reason = entry.get("reason")
    return reason if isinstance(reason, str) and reason.strip() else None


def classify_scanner(entry: Mapping[str, Any] | None) -> str:
    """How one scanner report stands against a requirement. *entry* is None for
    a required scanner the verdict records nothing for. Allowlist: a status never
    heard of, a blank reason on not_applicable and a missing report are all
    NOT_RUN_FAILING."""
    if entry is None:
        return NOT_RUN_FAILING
    status = entry.get("status")
    if status == SCANNER_RAN:
        return SATISFIED
    if status == SCANNER_NOT_INVOKED:
        return NOT_RUN_REPORTED
    if status == SCANNER_NOT_APPLICABLE and _usable_reason(entry) is not None:
        return SATISFIED
    return NOT_RUN_FAILING


def scanner_coverage(
    scanners_run: Sequence[Mapping[str, Any]] | None, required: Sequence[str] = ()
) -> dict[str, Any]:
    """``{complete, not_run: [{scanner, status, reason, required}]}``.

    ``not_run`` lists every report that is not SATISFIED, and every required
    scanner with no report (status ``missing``). ``complete`` is true exactly
    when ``not_run`` is empty. Reporting only: nothing here refuses."""
    required_names = set(required)
    not_run: list[dict[str, Any]] = []
    reported: set[str] = set()
    for entry in scanners_run or ():
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("scanner"))
        reported.add(name)
        if classify_scanner(entry) == SATISFIED:
            continue
        not_run.append(
            {
                "scanner": name,
                "status": str(entry.get("status")),
                "reason": _usable_reason(entry) or _NO_REASON,
                "required": name in required_names,
            }
        )
    for name in required:
        if name not in reported:
            not_run.append(
                {"scanner": name, "status": STATUS_MISSING, "reason": _MISSING_REASON, "required": True}
            )
    return {"complete": not not_run, "not_run": not_run}


def coverage_header_line(coverage: Mapping[str, Any]) -> str | None:
    """The one comment-header line naming each scanner that did not run, or None
    when all did. Names and statuses only: a reason is free text and stays in
    the structured result."""
    not_run = coverage["not_run"]
    if not not_run:
        return None
    named = ", ".join(
        f"{item['scanner']} ({item['status']}{', required' if item['required'] else ''})"
        for item in not_run
    )
    return f"scanners not run: {named}"


__all__ = [
    "NOT_RUN_FAILING",
    "NOT_RUN_REPORTED",
    "SATISFIED",
    "STATUS_MISSING",
    "ScannerDeclaration",
    "classify_scanner",
    "coverage_header_line",
    "required_scanners_for",
    "scanner_coverage",
]
