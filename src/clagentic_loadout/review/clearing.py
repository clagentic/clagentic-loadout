"""review.clearing — which earlier open findings a new verdict clears.

A reviewer's fence holds findings open (``findings_open``) and a later fence
clears one only by listing it in ``cleared_claims`` at its own head. This module
decides, from tool-recorded facts only, which earlier open findings the verdict
being posted may clear.

Governing rule: clearing lowers the verdict, so it needs a positive,
tool-recorded resolution. Silence never clears: a finding the new review simply
does not raise stays open. A prior open finding is cleared only when one of
these names it:

  1. a ``resolved`` entry the review run recorded (by the reviewer, or by the
     caller's resolved-findings file the run applied);
  2. a caller ruling given to ``post`` (``--resolved-findings``);
  3. a base-revision disposition that demotes the finding to advisory;
  4. a reviewer drop (``--dropped``) whose candidate maps to exactly one prior
     open finding.

A finding the new verdict raises again is never cleared. Everything here is
pure: the caller reads the host and the run directory and passes the facts in.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.merge.fence_state import FindingsState, unresolved_prior_findings
from clagentic_loadout.merge.verdict import EARLIER_FENCE_VALID, classify_earlier_fence
from clagentic_loadout.review.evaluation import finding_state_id
from clagentic_loadout.review.resolutions import Resolution

_SHORT = 12
_MAX_EVIDENCE_CHARS = 300

#: Maps (head the finding was raised at, file, line there) to the line at the
#: head being posted, or None when it cannot be placed.
LineMapper = Callable[[str, str, int], "int | None"]


@dataclass(frozen=True)
class PriorOpen:
    """One earlier open finding. The location is known only when the fence
    that raised it also carried the findings it was computed from."""

    id: str
    rule_id: str
    head: str
    file: str | None = None
    line: int | None = None
    message: str | None = None

    @property
    def located(self) -> bool:
        return self.file is not None and self.line is not None

    def as_finding(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "rule_id": self.rule_id, "message": self.message}


@dataclass(frozen=True)
class Clearing:
    """What the verdict may clear and what it leaves open."""

    claims: tuple[dict[str, str], ...]
    uncleared: tuple[PriorOpen, ...]
    #: Prior-only findings a caller ruling covered, so the posted body can list
    #: them under "Resolved by caller" like any other ruled finding.
    ruled: tuple[tuple[PriorOpen, Resolution], ...]
    notes: tuple[str, ...]


def one_line(text: str) -> str:
    flat = " ".join(str(text).replace("`", "'").split())
    if len(flat) > _MAX_EVIDENCE_CHARS:
        return flat[: _MAX_EVIDENCE_CHARS - 3] + "..."
    return flat


def prior_open_findings(
    comments: Iterable[Mapping[str, Any]], *, pr_number: int, reviewer: str, head_sha: str
) -> tuple[list[PriorOpen], list[str]]:
    """The reviewer's earlier open findings on the PR that no earlier fence has
    cleared: the same accounting the merge gate applies, over the same fences.
    An unreadable or foreign earlier comment asserts nothing here (the gate
    judges it on its own), and is named in the returned notes."""
    history: list[tuple[str, FindingsState]] = []
    located: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    for comment in comments:
        fence = classify_earlier_fence(dict(comment), pr_number=pr_number, reviewer_name=reviewer)
        if fence is None:
            continue
        if fence.kind != EARLIER_FENCE_VALID:
            notes.append(f"earlier comment #{fence.comment_id} read as {fence.kind}: {fence.reason}")
            continue
        history.append((fence.head, fence.state))
        evaluated = {
            finding_state_id(e["file"], e["line"], e["rule_id"]): e
            for e in (fence.state.evaluated_findings or ())
        }
        for entry in fence.state.findings_open:
            if entry["id"] in evaluated:
                located[entry["id"]] = evaluated[entry["id"]]
    unresolved = unresolved_prior_findings(history, FindingsState(), head_sha)
    result = []
    for entry in unresolved:
        where = located.get(entry["id"])
        result.append(
            PriorOpen(
                id=entry["id"],
                rule_id=entry["rule_id"],
                head=entry["head"],
                file=where["file"] if where else None,
                line=where["line"] if where else None,
                message=where["message"] if where else None,
            )
        )
    return result, notes


def _resolved_evidence(entry: Mapping[str, Any], head_sha: str) -> str:
    reason = entry.get("reason")
    if isinstance(reason, str) and reason.strip():
        return one_line(reason)
    return f"not raised at {head_sha[:_SHORT]} after re-review"


def _drop_matches(
    drop: Mapping[str, Any], prior: Sequence[PriorOpen], map_line: LineMapper
) -> list[PriorOpen]:
    """The prior open findings a dropped candidate stands for: same file and
    rule, and either the same line (exact id) or the line the prior one maps
    to at the head being posted. An exact match settles it."""
    same = [p for p in prior if p.located and p.file == drop["file"] and p.rule_id == drop["rule_id"]]
    exact = [p for p in same if p.line == drop["line"]]
    if exact:
        return exact
    return [p for p in same if map_line(p.head, p.file, p.line) == drop["line"]]


def derive_clearing(
    prior_open: Sequence[PriorOpen],
    *,
    head_sha: str,
    raised_ids: Collection[str],
    resolved_entries: Sequence[Mapping[str, Any]],
    caller_rulings: Sequence[Resolution],
    demoted: Mapping[str, str],
    drops: Sequence[Mapping[str, Any]],
    map_line: LineMapper,
    source_notes: Sequence[str] = (),
) -> Clearing:
    """Decide which of *prior_open* the verdict at *head_sha* clears.

    *raised_ids* are the finding ids the verdict itself holds open; those are
    never cleared. *resolved_entries* are the ``resolved`` entries from sources
    the caller already judged trustworthy. *demoted* maps a finding id to the
    evidence line for a base disposition that demotes it. *source_notes* are
    carried into the notes, naming sources that were missing."""
    candidates = [p for p in prior_open if p.id not in raised_ids]
    resolved_by_id: dict[str, Mapping[str, Any]] = {}
    for entry in resolved_entries:
        resolved_by_id.setdefault(finding_state_id(entry["file"], entry["line"], entry["rule_id"]), entry)

    drop_hits: dict[str, list[Mapping[str, Any]]] = {}
    for drop in drops:
        matched = _drop_matches(drop, candidates, map_line)
        if len(matched) == 1:
            drop_hits.setdefault(matched[0].id, []).append(drop)

    claims: list[dict[str, str]] = []
    uncleared: list[PriorOpen] = []
    ruled: list[tuple[PriorOpen, Resolution]] = []
    notes = list(source_notes)
    for prior in candidates:
        evidence: str | None = None
        if prior.id in resolved_by_id:
            evidence = _resolved_evidence(resolved_by_id[prior.id], head_sha)
        if evidence is None and prior.located:
            ruling = next((r for r in caller_rulings if r.matches(prior.as_finding())), None)
            if ruling is not None:
                evidence = one_line(ruling.reason)
                ruled.append((prior, ruling))
        if evidence is None and prior.id in demoted:
            evidence = demoted[prior.id]
        if evidence is None and len(drop_hits.get(prior.id, ())) == 1:
            reason = one_line(drop_hits[prior.id][0]["reason"])
            evidence = f"dropped by reviewer at {head_sha[:_SHORT]}: {reason}"
        if evidence is None:
            uncleared.append(prior)
            continue
        claims.append({"id": prior.id, "head": head_sha, "evidence": evidence})
    for prior in uncleared:
        ambiguous = len(drop_hits.get(prior.id, ())) > 1
        notes.append(
            f"prior finding {prior.id} (rule {prior.rule_id}, raised at {prior.head[:_SHORT]}) "
            "stays open: "
            + (
                "more than one dropped candidate maps to it"
                if ambiguous
                else "no recorded resolution, caller ruling, base disposition or unambiguous "
                "drop covers it"
            )
        )
    return Clearing(tuple(claims), tuple(uncleared), tuple(ruled), tuple(notes))

