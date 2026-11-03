"""review.scanner_findings — turn scanner hits into findings, or into context.

Each hit is classified by this tool, against the lines the pull request adds at
the reviewed head (review.changed_lines). The classification is a function of
the hit's file and line and of the host's diff, and of nothing else: not a field
of the scanner file, not a finding, not a dropped candidate, not any earlier
output a reviewer could have edited.

  in range       the hit is on a line the change adds. It becomes a finding
                 whose facts this module states, id ``file:line:scanner.rule``,
                 and it blocks under the evaluator like any finding whose facts
                 say so, unless the reviewer lists it in ``--dropped`` with a
                 reason (review.dropped_overlap).
  outside range  the hit is on a line the change leaves alone, or in a file it
                 does not touch. It is never a finding and takes no part in the
                 verdict; it is kept as context.

Duplicates: a reviewer's finding with the same ``file:line:rule_id`` as a scanner
finding is replaced by the scanner finding, so the location is counted once and
the facts that stand are the tool's, never the milder ones a reviewer wrote. A
reviewer finding on the same line under a different rule id is a different
finding and is kept: a finding cannot cover a scanner hit by sitting next to it.

The facts are stated at their worst reading (reachable, no precondition, full
impact) because a scanner reports a pattern, not a reachability analysis: the
judgement that it is not exploitable belongs to the reviewer, and is made by
dropping the candidate with a reason. A hit from a scanner declared as a secret
scanner is in the security floor.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.review.changed_lines import added_lines
from clagentic_loadout.review.dropped_overlap import candidate_id
from clagentic_loadout.review.scanner_input import KIND_SECRET, ScannerHit, ScannerInput

KEY_SCANNER = "scanner"

_CATEGORY_SECRET = "secrets_exposure"
_CATEGORY_OTHER = "other"


@dataclass(frozen=True)
class Ingestion:
    """What a scanner input came to for one pull request."""

    #: Findings to add to the reviewer's own: the in-range hits.
    findings: tuple[dict[str, Any], ...]
    #: The outside-range hits, as the fence and the comment list them.
    outside_range: tuple[dict[str, Any], ...]
    #: Ids of reviewer findings a scanner finding replaced (same file:line:rule_id).
    replaced: tuple[str, ...]


def finding_rule_id(hit: ScannerHit) -> str:
    """The id form of a scanner finding: ``scanner.rule``."""
    return f"{hit.scanner}.{hit.rule_id}"


def _facts(hit: ScannerHit) -> dict[str, Any]:
    return {
        "category": _CATEGORY_SECRET if hit.kind == KIND_SECRET else _CATEGORY_OTHER,
        "reachable": "yes",
        "attacker_precondition": "none",
        "impact": {"confidentiality": "full", "integrity": "full", "availability": "full"},
        "durability": "durable",
        "evidence": (
            f"scanner {hit.scanner} reported rule {hit.rule_id} on a line this change adds"
        ),
    }


def finding_of(hit: ScannerHit) -> dict[str, Any]:
    """The finding an in-range *hit* becomes, in the shape a findings file holds."""
    return {
        "file": hit.file,
        "line": hit.line,
        "rule_id": finding_rule_id(hit),
        "message": hit.message,
        "severity": None,
        "facts": _facts(hit),
    }


def _context_of(hit: ScannerHit) -> dict[str, Any]:
    return {
        "scanner": hit.scanner,
        "file": hit.file,
        "line": hit.line,
        "rule_id": hit.rule_id,
        "message": hit.message,
    }


def ingest(
    scanner_input: ScannerInput,
    diff_text: str,
    reviewer_findings: Sequence[Mapping[str, Any]],
    *,
    prog: str = "loadout-review",
) -> Ingestion:
    """Classify every hit of *scanner_input* against *diff_text* and return the
    findings to add and the context to list. The diff is read only when there is
    a hit to classify; a diff that cannot be read then raises
    changed_lines.DiffUnreadableError, because no hit can be called outside the
    range of a diff nothing can be said about."""
    hits = scanner_input.hits
    if not hits:
        return Ingestion((), (), ())
    added = added_lines(diff_text)
    in_range: list[dict[str, Any]] = []
    outside: list[dict[str, Any]] = []
    for hit in hits:
        if hit.line in added.get(hit.file, frozenset()):
            in_range.append(finding_of(hit))
        else:
            outside.append(_context_of(hit))
    scanner_ids = {candidate_id(item) for item in in_range}
    replaced = tuple(
        candidate_id(item) for item in reviewer_findings if candidate_id(item) in scanner_ids
    )
    for finding_id in replaced:
        print(
            f"{prog}: notice: the reviewer finding {finding_id} is also a scanner finding; "
            "the scanner finding stands and the location is counted once",
            file=sys.stderr,
        )
    return Ingestion(tuple(in_range), tuple(outside), replaced)


def merge_into(
    reviewer_findings: Sequence[Mapping[str, Any]], ingestion: Ingestion
) -> list[dict[str, Any]]:
    """The reviewer's findings without those the scanner findings replace,
    followed by the scanner findings, each id once."""
    replaced = set(ingestion.replaced)
    kept = [dict(item) for item in reviewer_findings if candidate_id(item) not in replaced]
    seen: set[str] = set()
    for item in ingestion.findings:
        if candidate_id(item) not in seen:
            seen.add(candidate_id(item))
            kept.append(dict(item))
    return kept


__all__ = ["Ingestion", "finding_of", "finding_rule_id", "ingest", "merge_into"]
