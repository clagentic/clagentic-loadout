"""review.evaluate — turn a reviewer's findings into a computed verdict.

The reviewer states facts; this module decides. Given findings, the stakes of
the paths they touch, the standing dispositions, and the head the findings were
produced for, ``evaluate`` returns a Verdict whose outcome is one of:

  STALE     the head the findings were produced for is not the expected head;
  DEGRADED  a required input is missing (facts on a blocking finding, a
            required scanner's result, a policy file), so the result cannot be
            trusted to clear;
  BLOCKED   at least one open finding blocks;
  CLEAN     nothing blocks (advisory findings are listed).

STALE outranks DEGRADED outranks BLOCKED, because each earlier outcome means
the later ones may not describe the PR the reader is looking at.

Deterministic by construction: the same inputs always give the same verdict.
Nothing is ever dropped: a finding a disposition clears is annotated and kept
as advisory; findings that may be the same defect are annotated and kept.
Policy that can lower a verdict arrives already read from the trusted base
revision (review.policy_source); this module only applies it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from clagentic_loadout.review import dispositions as disp
from clagentic_loadout.review.facts import (
    FLOOR_CATEGORIES,
    KEY_FACTS,
    STATE_COMPLETE,
    ParsedFacts,
    parse_facts,
)
from clagentic_loadout.review.rubric import (
    DEFAULT_BLOCK_AT,
    SEVERITIES,
    Score,
    score,
    severity_rank,
)
from clagentic_loadout.review.rulings import is_ruling
from clagentic_loadout.review.stakes import INTERNAL, PathStakes, Stakes

#: Identifies the rules a verdict was computed under. Bump it with any change to
#: the rubric, the blocking rule, the merge rule or the disposition rules, so a
#: reader can tell two verdicts were not computed by the same logic. Version 2:
#: a praise label no longer exempts a finding from scoring.
EVALUATOR_VERSION = "2"

OUTCOME_CLEAN = "CLEAN"
OUTCOME_BLOCKED = "BLOCKED"
OUTCOME_DEGRADED = "DEGRADED"
OUTCOME_STALE = "STALE"

_SCANNER_RAN = "ran"
_SCANNER_NOT_APPLICABLE = "not_applicable"

_PRAISE = "praise"


@dataclass(frozen=True)
class EvaluatedFinding:
    index: int
    file: str
    line: int
    rule_id: str
    message: str
    severity: str
    blocking: bool
    advisory: bool
    floor_category: bool
    facts_state: str
    facts_problems: tuple[str, ...]
    stakes: PathStakes | None
    score: Score | None
    disposition: dict[str, Any] | None = None
    disposition_refused: str | None = None
    possible_duplicates: tuple[str, ...] = ()
    clear_with: str | None = None
    floor_forbids_clearing: bool = False
    praise: bool = False
    #: Position, in the findings the evaluator was given, of the copy this
    #: evaluation stands for (identical findings are merged into one).
    source_index: int = 0
    #: The refusal text names this finding and the kinds that can still clear
    #: it, so the general floor sentence is not repeated beside it. Not part of
    #: the serialised form.
    refusal_specific: bool = False

    @property
    def id(self) -> str:
        return f"{self.file}:{self.line}:{self.rule_id}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "id": self.id,
            "file": self.file,
            "line": self.line,
            "rule_id": self.rule_id,
            "message": self.message,
            "severity": self.severity,
            "blocking": self.blocking,
            "advisory": self.advisory,
            "floor_category": self.floor_category,
            "facts_state": self.facts_state,
            "facts_problems": list(self.facts_problems),
            "stakes": self.stakes.as_dict() if self.stakes else None,
            "score": self.score.as_dict() if self.score else None,
            "disposition": self.disposition,
            "disposition_refused": self.disposition_refused,
            "possible_duplicates": list(self.possible_duplicates),
            "clear_with": self.clear_with,
            "floor_forbids_clearing": self.floor_forbids_clearing,
            "praise": self.praise,
        }


@dataclass(frozen=True)
class Verdict:
    outcome: str
    head_sha: str
    expected_head: str | None
    block_at: str
    findings: tuple[EvaluatedFinding, ...]
    degraded: tuple[str, ...] = ()
    expired_dispositions: tuple[dict[str, Any], ...] = field(default=())
    #: Unexpired rulings for this pull request that cleared no finding.
    unapplied_rulings: tuple[dict[str, Any], ...] = field(default=())

    @property
    def blocking(self) -> tuple[EvaluatedFinding, ...]:
        return tuple(f for f in self.findings if f.blocking)

    @property
    def advisory(self) -> tuple[EvaluatedFinding, ...]:
        return tuple(f for f in self.findings if not f.blocking)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "outcome": self.outcome,
            "head_sha": self.head_sha,
            "expected_head": self.expected_head,
            "block_at": self.block_at,
            "blocking": [f.as_dict() for f in self.blocking],
            "advisory": [f.as_dict() for f in self.advisory],
            "degraded": list(self.degraded),
            "expired_dispositions": list(self.expired_dispositions),
        }
        # Absent rather than empty, so output without rulings is unchanged.
        if self.unapplied_rulings:
            out["unapplied_rulings"] = list(self.unapplied_rulings)
        return out


@dataclass(frozen=True)
class _Assessed:
    finding: Mapping[str, Any]
    parsed: ParsedFacts
    stakes: PathStakes
    score: Score
    floor_category: bool
    praise: bool
    source: int = 0


def _finding_id(finding: Mapping[str, Any]) -> str:
    return f"{finding['file']}:{finding['line']}:{finding['rule_id']}"


def _in_floor(parsed: ParsedFacts) -> bool:
    """Whether the facts put a finding in the security floor: a floor category,
    or no category stated at all (unknown counts at its worst case)."""
    category = parsed.facts.category
    return category is None or category in FLOOR_CATEGORIES


def is_floor_finding(finding: Mapping[str, Any]) -> bool:
    """Whether *finding* is a security-floor finding, by the one definition the
    evaluator itself scores with."""
    return _in_floor(parse_facts(finding.get(KEY_FACTS)))


def is_praise_note(finding: Mapping[str, Any]) -> bool:
    """Whether *finding* is shown as praise: it is labelled so AND its own facts
    are complete, outside the security floor, and state no impact at all.

    The label never changes a score. A finding that states anything else (no
    facts, invalid facts, a floor category, any impact) is scored from its facts
    like every other finding and the label is ignored. Impact none on every
    dimension scores none under any stakes, so this needs no policy to decide."""
    if finding.get("severity") != _PRAISE:
        return False
    parsed = parse_facts(finding.get(KEY_FACTS))
    return (
        parsed.state == STATE_COMPLETE
        and not _in_floor(parsed)
        and all(level == "none" for level in parsed.facts.impact)
    )


def _assess(finding: Mapping[str, Any], stakes: Stakes, source: int = 0) -> _Assessed:
    parsed = parse_facts(finding.get(KEY_FACTS))
    path_stakes = stakes.for_path(finding["file"])
    computed = score(parsed.facts, path_stakes)
    floor_category = _in_floor(parsed)
    return _Assessed(
        finding,
        parsed,
        path_stakes,
        computed,
        floor_category,
        is_praise_note(finding),
        source,
    )


def _blocks_before_dispositions(item: _Assessed, threshold: int) -> bool:
    """Whether *item* would block if no disposition applied. The one definition
    of blocking, shared by the merge below and the verdict loop. Scored from
    facts only: a praise label takes no part."""
    return severity_rank(item.score.severity) >= threshold or (
        item.floor_category and item.stakes.exposure != INTERNAL
    )


def _merge_identical(assessed: Iterable[_Assessed], threshold: int) -> list[_Assessed]:
    """Findings with the same (file, line, rule_id, message) are one finding.
    The surviving copy is the strongest: a non-praise copy over a praise one,
    then a blocking one over a non-blocking one, then the most severe. It sits
    in the first copy's position, so a merge never hides a blocking finding
    behind a milder or praise duplicate."""
    merged: list[_Assessed] = []
    position: dict[tuple[Any, ...], int] = {}

    def strength(item: _Assessed) -> tuple[bool, bool, int]:
        return (
            not item.praise,
            _blocks_before_dispositions(item, threshold),
            severity_rank(item.score.severity),
        )

    for item in assessed:
        f = item.finding
        key = (f["file"], f["line"], f["rule_id"], f["message"])
        if key not in position:
            position[key] = len(merged)
            merged.append(item)
        elif strength(item) > strength(merged[position[key]]):
            merged[position[key]] = item
    return merged


#: What a ruling that matched a finding came to: it cleared it, the security
#: floor refused it, or an earlier entry had already cleared the finding.
_USE_APPLIED = "applied"
_USE_REFUSED = "refused"
_USE_SUPERSEDED = "superseded"

_FLOOR_ROUTE = (
    "a security-floor finding clears only through a dispositions entry on the base "
    "branch, or by fixing it"
)


def _floor_refusal(entry: disp.Disposition, finding_id: str) -> tuple[str, bool]:
    """Why an entry did not clear a security-floor finding, and whether that text
    is specific to the finding. A ruling is never applied to a floor finding,
    whatever its kind, and its refusal names the finding and the one route that
    can clear it. A base-revision by_design disposition keeps its established
    wording, which the general floor sentence completes."""
    if not is_ruling(entry):
        return (
            f"a by_design disposition ({entry.path}, {entry.rule_id}) cannot clear a "
            "security-floor finding",
            False,
        )
    return (
        f"a {entry.kind} ruling by {entry.by} (requested by {entry.requested_by}) is not applied "
        f"to {finding_id}, which is a security-floor finding: {_FLOOR_ROUTE}",
        True,
    )


def _apply_dispositions(
    item: _Assessed,
    entries: Sequence[disp.Disposition],
    today: dt.date,
    used: dict[int, tuple[str, str]] | None = None,
) -> tuple[disp.Disposition | None, str | None, bool]:
    """The disposition that clears *item*, separately the reason a matching one
    was refused, and whether that reason is specific to the finding.

    A security-floor finding cannot be cleared by a by_design disposition, and
    cannot be cleared by a ruling of any kind. When *used* is given it records,
    by entry identity, what each matching ruling came to and for which finding."""
    f = item.finding
    finding_id = _finding_id(f)
    applied: disp.Disposition | None = None
    refused: str | None = None
    specific = False
    for entry in entries:
        if (
            entry.kind not in disp.KINDS
            or entry.is_expired(today)
            or not entry.matches(f["file"], f["rule_id"], f.get("fingerprint"))
        ):
            continue
        ruling = is_ruling(entry)
        if applied is not None:
            if ruling and used is not None:
                used.setdefault(id(entry), (_USE_SUPERSEDED, finding_id))
            continue
        if item.floor_category and (ruling or entry.kind == disp.BY_DESIGN):
            if refused is None or (ruling and not specific):
                refused, specific = _floor_refusal(entry, finding_id)
            if ruling and used is not None:
                used.setdefault(id(entry), (_USE_REFUSED, finding_id))
            continue
        applied = entry
        if ruling and used is not None:
            used[id(entry)] = (_USE_APPLIED, finding_id)
    return applied, refused, specific


def _unapplied_reason(status: str | None, finding_id: str | None) -> str:
    if status == _USE_REFUSED:
        return f"refused: {finding_id} is a security-floor finding; {_FLOOR_ROUTE}"
    if status == _USE_SUPERSEDED:
        return f"{finding_id} was already cleared by an earlier entry"
    return "matched no kept finding"


def _unapplied_rulings(
    entries: Sequence[disp.Disposition], today: dt.date, used: Mapping[int, tuple[str, str]]
) -> tuple[dict[str, Any], ...]:
    """The unexpired rulings that cleared nothing, each with why. An expired one
    is reported as expired instead."""
    out = []
    for entry in entries:
        if not is_ruling(entry) or entry.is_expired(today):
            continue
        status, finding_id = used.get(id(entry), (None, None))
        if status == _USE_APPLIED:
            continue
        out.append({**entry.as_dict(), "unapplied_reason": _unapplied_reason(status, finding_id)})
    return tuple(out)


def _scanner_satisfied(entry: Mapping[str, Any]) -> bool:
    """Allowlist: a scanner counts only when it ran, or declared itself not
    applicable and said why. Every other status, including one never heard of,
    is a scanner that gave no usable answer."""
    status = entry.get("status")
    if status == _SCANNER_RAN:
        return True
    reason = entry.get("reason")
    return status == _SCANNER_NOT_APPLICABLE and isinstance(reason, str) and bool(reason.strip())


def _scanner_problems(
    scanners_run: Sequence[Mapping[str, Any]] | None, required: Sequence[str]
) -> list[str]:
    reported: dict[str, list[Mapping[str, Any]]] = {}
    for entry in scanners_run or []:
        if isinstance(entry, Mapping):
            reported.setdefault(str(entry.get("scanner")), []).append(entry)
    problems = []
    for name in required:
        entries = reported.get(name)
        if not entries:
            problems.append(f"required scanner {name!r} did not report a result")
            continue
        # A scanner that reported twice is satisfied only if every report is.
        for entry in entries:
            if not _scanner_satisfied(entry):
                reason = entry.get("reason")
                detail = f": {reason}" if isinstance(reason, str) and reason.strip() else ""
                problems.append(f"required scanner {name!r} status {entry.get('status')!r}{detail}")
    return problems


def evaluate(
    findings: Sequence[Mapping[str, Any]],
    stakes: Stakes,
    dispositions: Sequence[disp.Disposition],
    head_sha: str,
    expected_head: str | None = None,
    *,
    block_at: str = DEFAULT_BLOCK_AT,
    today: dt.date | None = None,
    scanners_run: Sequence[Mapping[str, Any]] | None = None,
    required_scanners: Sequence[str] = (),
    degraded_reasons: Sequence[str] = (),
) -> Verdict:
    """Evaluate validated *findings* (file, line, rule_id, message, optional
    facts and fingerprint) and return the Verdict.

    ``block_at`` is the lowest severity that blocks. ``today`` dates the
    disposition expiry check (default: the current date). ``degraded_reasons``
    carries reasons found while reading policy, so they surface in the verdict.
    """
    if block_at not in SEVERITIES[1:]:
        raise ValueError(f"block_at must be one of {', '.join(SEVERITIES[1:])}, got {block_at!r}")
    day = today if today is not None else dt.date.today()
    threshold = severity_rank(block_at)

    merged = _merge_identical(
        (_assess(f, stakes, position) for position, f in enumerate(findings)), threshold
    )
    expired = tuple(e.as_dict() for e in dispositions if e.is_expired(day))
    by_file_rule: dict[tuple[str, str], list[int]] = {}
    for position, item in enumerate(merged, start=1):
        by_file_rule.setdefault((item.finding["file"], item.finding["rule_id"]), []).append(position)

    evaluated: list[EvaluatedFinding] = []
    degraded = list(degraded_reasons)
    used: dict[int, tuple[str, str]] = {}
    for position, item in enumerate(merged, start=1):
        f = item.finding
        blocking = _blocks_before_dispositions(item, threshold)
        applied: disp.Disposition | None = None
        refused: str | None = None
        refusal_specific = False
        if not item.praise:
            applied, refused, refusal_specific = _apply_dispositions(item, dispositions, day, used)
        if applied is not None:
            blocking = False
        siblings = by_file_rule[(f["file"], f["rule_id"])]
        duplicates = tuple(_finding_id(merged[p - 1].finding) for p in siblings if p != position)
        clear_kind = disp.ACCEPTED_RISK if item.floor_category else disp.BY_DESIGN
        if blocking and item.parsed.state != STATE_COMPLETE:
            problems = "; ".join(item.parsed.problems)
            degraded.append(
                f"facts {item.parsed.state} on blocking finding {_finding_id(f)}: {problems}"
            )
        evaluated.append(
            EvaluatedFinding(
                index=position,
                file=f["file"],
                line=f["line"],
                rule_id=f["rule_id"],
                message=f["message"],
                severity="none" if item.praise else item.score.severity,
                blocking=blocking,
                advisory=not blocking,
                floor_category=item.floor_category,
                facts_state=item.parsed.state,
                facts_problems=item.parsed.problems,
                stakes=None if item.praise else item.stakes,
                score=None if item.praise else item.score,
                disposition=applied.as_dict() if applied else None,
                disposition_refused=refused,
                refusal_specific=refusal_specific,
                possible_duplicates=duplicates,
                clear_with=(
                    disp.clearing_stanza(f["file"], f["rule_id"], clear_kind, day)
                    if blocking
                    else None
                ),
                floor_forbids_clearing=blocking and item.floor_category,
                praise=item.praise,
                source_index=item.source,
            )
        )

    degraded.extend(_scanner_problems(scanners_run, required_scanners))
    if expected_head is not None and head_sha != expected_head:
        outcome = OUTCOME_STALE
    elif degraded:
        outcome = OUTCOME_DEGRADED
    elif any(f.blocking for f in evaluated):
        outcome = OUTCOME_BLOCKED
    else:
        outcome = OUTCOME_CLEAN
    return Verdict(
        outcome=outcome,
        head_sha=head_sha,
        expected_head=expected_head,
        block_at=block_at,
        findings=tuple(evaluated),
        degraded=tuple(degraded),
        expired_dispositions=expired,
        unapplied_rulings=_unapplied_rulings(dispositions, day, used),
    )


__all__ = [
    "EVALUATOR_VERSION",
    "OUTCOME_BLOCKED",
    "OUTCOME_CLEAN",
    "OUTCOME_DEGRADED",
    "OUTCOME_STALE",
    "EvaluatedFinding",
    "Verdict",
    "evaluate",
    "is_floor_finding",
    "is_praise_note",
]
