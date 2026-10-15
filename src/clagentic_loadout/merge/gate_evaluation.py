"""merge.gate_evaluation — the merge gate's own computation of a reviewer's verdict.

A reviewer's newest valid fence records the outcome the evaluator computed when
it was posted (``verdict_outcome``) and the findings that outcome was computed
from (``evaluated_findings``). The gate does not take that outcome on trust, and
it does not re-judge a finding either: it runs the same evaluator
(review.evaluate) over the same findings again, at merge time, with the policy
(dispositions and stakes) read at the PR's LIVE base revision and the PR's
current head as the expected head. Nothing in a fence or a findings file picks
the revision policy is read from; the ``policy_base_sha`` a fence records is
provenance only.

The governing rule: anything that can lower a verdict must be positively
established from trusted input, and an absent, unknown, unrecognised or
unverifiable value resolves to the worst case. The gate is a one-way barrier:
it adds refusals and never clears a block on its own authority. The single way
a recorded BLOCKED becomes a pass is a standing disposition that is now on the
trusted base and matches the blocking finding (the policy is read from the base
revision, so the PR under review cannot have written it); the gate names the
disposition it applied.

Decision table, for the fence outcome F and the gate's own outcome G:

  F STALE, or G STALE                  refuse, naming the heads
  F CLEAN, G anything else             refuse, naming the difference (an expired
                                       entry, a policy base that moved, a
                                       degraded input, a finding that now blocks)
  F BLOCKED, G CLEAN, F's findings_open  pass, naming each disposition applied; only
    non-empty and every entry matched    when EVERY recorded-blocking finding is
    by a disposition at the live base    individually matched, never on one match
                                         among several, and never on an empty list
  BLOCKED in F or G                    refuse, naming each open blocking finding
                                       and the entry that would clear it
  DEGRADED in F or G (no BLOCKED)      refuse unless merge.evaluator.allow_degraded
                                       is true for the role; then pass and record
  both CLEAN                           pass

A fence below fence_schema_version 3 has no computed verdict: with
merge.evaluator.require_evaluated false (the migration default) the status its
poster asserted applies, as before, with a notice; with it true the gate
refuses. An evaluated fence that does not carry its findings cannot be checked
and refuses.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clagentic_loadout.merge.errors import (
    VerdictBlockingError,
    VerdictDegradedError,
    VerdictGateDisagreementError,
    VerdictNotEvaluatedError,
    VerdictStaleError,
)
from clagentic_loadout.merge.gate_config import EvaluatorGate
from clagentic_loadout.merge.verdict import ReviewerVerdict
from clagentic_loadout.review.evaluate import (
    OUTCOME_BLOCKED,
    OUTCOME_CLEAN,
    OUTCOME_DEGRADED,
    OUTCOME_STALE,
    Verdict,
)
from clagentic_loadout.review.evaluate_render import (
    finding_notes,
    ruling_provenance,
    unapplied_ruling_line,
)
from clagentic_loadout.review.evaluate_verb import EvaluateError
from clagentic_loadout.review.evaluation import (
    context_from_config,
    evaluate_findings,
    finding_state_id,
)
from clagentic_loadout.review.policy_reader import PolicyReader
from clagentic_loadout.review.policy_source import DISPOSITIONS_PATH
from clagentic_loadout.review.ruling_provider import IGNORED_PREFIX, RulingScope
from clagentic_loadout.review.rulings import SOURCE_RULING


@dataclass(frozen=True)
class GateEvaluation:
    """What the gate concluded about one reviewer's newest fence."""

    reviewer: str
    #: False for a fence with no computed verdict, applied as asserted.
    evaluated: bool
    fence_outcome: str | None = None
    gate_outcome: str | None = None
    #: Dispositions at the live base that cleared a finding the fence had blocked.
    applied_dispositions: tuple[str, ...] = ()
    #: Per-PR rulings from the ruling registry that cleared a finding, and the
    #: unexpired ones for this PR that cleared nothing (each with why). Both are
    #: empty, and absent from the serialised form, when no registry is configured.
    applied_rulings: tuple[str, ...] = ()
    unapplied_rulings: tuple[str, ...] = ()
    #: Reasons carried when a DEGRADED verdict was allowed to pass.
    degraded_reasons: tuple[str, ...] = ()
    degraded_allowed: bool = False
    #: Lines for the operator on stderr.
    notices: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "reviewer": self.reviewer,
            "evaluated": self.evaluated,
            "fence_outcome": self.fence_outcome,
            "gate_outcome": self.gate_outcome,
            "applied_dispositions": list(self.applied_dispositions),
            "degraded_allowed": self.degraded_allowed,
            "degraded_reasons": list(self.degraded_reasons),
        }
        if self.applied_rulings:
            out["applied_rulings"] = list(self.applied_rulings)
        if self.unapplied_rulings:
            out["unapplied_rulings"] = list(self.unapplied_rulings)
        return out

    def attestation_line(self) -> str:
        """One line for the merge-completion attestation."""
        if not self.evaluated:
            return f"{self.reviewer}: not evaluated (status asserted by the reviewer)"
        text = f"{self.reviewer}: fence {self.fence_outcome}, gate {self.gate_outcome}"
        if self.applied_dispositions:
            text += f"; disposition applied: {'; '.join(self.applied_dispositions)}"
        if self.applied_rulings:
            text += f"; ruling applied: {'; '.join(self.applied_rulings)}"
        if self.unapplied_rulings:
            text += f"; ruling not applied: {'; '.join(self.unapplied_rulings)}"
        if self.degraded_allowed:
            text += f"; DEGRADED allowed by merge.evaluator.allow_degraded: {'; '.join(self.degraded_reasons)}"
        return text


def _describe_disposition(entry: Mapping[str, Any]) -> str:
    expiry = f", expires {entry['expires']}" if entry.get("expires") else ""
    return (
        f"{entry['path']} {entry['rule_id']} ({entry['kind']}, ruled by {entry['by']} "
        f"on {entry['at']}{expiry})"
    )


def _applied_dispositions(verdict: Verdict) -> tuple[str, ...]:
    return tuple(
        f"{finding.id}: {_describe_disposition(finding.disposition)}"
        for finding in verdict.findings
        if finding.disposition is not None and not _from_registry(finding.disposition)
    )


def _from_registry(entry: Mapping[str, Any]) -> bool:
    """Whether a serialised disposition is a per-PR ruling rather than a
    base-revision entry."""
    return entry.get("source") == SOURCE_RULING


def _describe_ruling(entry: Mapping[str, Any]) -> str:
    expiry = f", expires {entry['expires']}" if entry.get("expires") else ""
    return (
        f"{entry['kind']} ruling {ruling_provenance(entry)} at {entry['repo']}#{entry['pr']}"
        f"{expiry}: {entry['rationale']}"
    )


def _applied_rulings(verdict: Verdict) -> tuple[str, ...]:
    return tuple(
        f"{finding.id}: {_describe_ruling(finding.disposition)}"
        for finding in verdict.findings
        if finding.disposition is not None and _from_registry(finding.disposition)
    )


def _unapplied_rulings_text(verdict: Verdict) -> tuple[str, ...]:
    return tuple(unapplied_ruling_line(entry) for entry in verdict.unapplied_rulings)


def _expired_rulings_text(verdict: Verdict) -> list[str]:
    return [
        f"{entry['path']} {entry['rule_id']} ({entry['kind']}, expired {entry.get('expires')})"
        for entry in verdict.expired_dispositions
        if _from_registry(entry)
    ]


def _ruling_notices(
    label: str,
    where: str,
    computed: Verdict,
    applied_rulings: Sequence[str],
    unapplied_rulings: Sequence[str],
    *,
    applied_already_named: bool = False,
) -> tuple[str, ...]:
    """Operator lines naming the registry's effect on this verdict, whatever its
    outcome: rulings that cleared a finding, rulings that cleared nothing, and a
    registry that was not used. Empty when a registry has nothing to say, which
    is every verdict when none is configured."""
    notices: list[str] = []
    if applied_rulings and not applied_already_named:
        notices.append(
            f"{label} verdict ({where}): ruling(s) from the ruling registry cleared "
            f"finding(s): {'; '.join(applied_rulings)}"
        )
    if unapplied_rulings:
        notices.append(
            f"{label} verdict ({where}): ruling(s) for this PR that cleared nothing: "
            f"{'; '.join(unapplied_rulings)}"
        )
    expired = _expired_rulings_text(computed)
    if expired:
        notices.append(
            f"{label} verdict ({where}): expired ruling(s), no longer applied: {'; '.join(expired)}"
        )
    ignored = [reason for reason in computed.degraded if reason.startswith(IGNORED_PREFIX)]
    if ignored:
        notices.append(
            f"{label} verdict ({where}): the ruling registry was not used: {'; '.join(ignored)}"
        )
    return tuple(notices)


def _expired_text(verdict: Verdict) -> list[str]:
    return [
        f"{'ruling ' if _from_registry(entry) else ''}{entry['path']} {entry['rule_id']} "
        f"({entry['kind']}, expired {entry.get('expires')})"
        for entry in verdict.expired_dispositions
    ]


def _blocking_text(verdict: Verdict) -> str:
    """Each blocking finding with the entry that would clear it, from code."""
    parts = []
    for finding in verdict.blocking:
        notes = "\n".join(f"      {line}" for line in finding_notes(finding))
        parts.append(f"  - {finding.id} (computed severity {finding.severity})\n{notes}")
    return "\n".join(parts)


def unmatched_recorded_blocks(
    recorded: Sequence[Mapping[str, Any]], computed: Verdict
) -> list[tuple[Mapping[str, Any], str]]:
    """The recorded blocking entries no disposition at the live base matches,
    each with why. A recorded entry is matched when the gate's computation holds
    at least one finding with that id and EVERY such finding carries an applied
    disposition: ids are a digest of location and rule, so two findings with
    different messages can share one, and covering only one of them clears
    nothing. A disposition that narrows by fingerprint is applied on path and
    rule alone here because the fence does not carry fingerprints, so it cannot
    be shown to match and never counts. A per-PR ruling is the exception: it
    matches only a finding that carries the very fingerprint it names, so its
    match is shown, and it counts."""
    by_id: dict[str, list[Any]] = {}
    for finding in computed.findings:
        if finding.praise:
            # Praise is never scored or dispositioned, so it cannot be the
            # finding a recorded block names, whatever id it happens to share.
            continue
        by_id.setdefault(finding_state_id(finding.file, finding.line, finding.rule_id), []).append(
            finding
        )
    unmatched: list[tuple[Mapping[str, Any], str]] = []
    for entry in recorded:
        group = by_id.get(str(entry.get("id")), [])
        if not group:
            unmatched.append((entry, "the gate's computation holds no such finding"))
        elif any(f.disposition is None for f in group):
            unmatched.append((entry, "no disposition at the live base matches it"))
        elif any(
            f.disposition.get("fingerprint") and not _from_registry(f.disposition) for f in group
        ):
            unmatched.append(
                (
                    entry,
                    "the disposition that applies narrows by fingerprint, which the fence "
                    "does not carry, so it cannot be shown to match",
                )
            )
    return unmatched


def _unmatched_text(
    recorded: Sequence[Mapping[str, Any]], unmatched: Sequence[tuple[Mapping[str, Any], str]]
) -> str:
    if not recorded:
        return (
            "  - the fence lists no open finding (findings_open is empty or absent), so "
            "nothing can be shown to be cleared"
        )
    return "\n".join(
        f"  - {entry.get('id')} (rule {entry.get('rule_id')}): {why}" for entry, why in unmatched
    )


def _finding_dicts(state_findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for item in state_findings]


def _recompute(
    verdict: ReviewerVerdict,
    *,
    current_head_sha: str,
    live_base_sha: str,
    policy_reader: PolicyReader | None,
    visibility: str | None,
    config_root: str | Path | None,
    today: dt.date | None,
    ruling_scope: RulingScope | None = None,
) -> Verdict:
    """The gate's own evaluation of the fence's findings. Policy is read through
    *policy_reader* at *live_base_sha*, never at a revision a fence or findings
    file names; with no reader or no base revision it is not read and the
    verdict is DEGRADED. Per-PR rulings are read for *ruling_scope* from the
    registry, never from the fence."""
    state = verdict.state
    assert state.evaluated_findings is not None
    try:
        context = context_from_config(
            config_root,
            policy_reader=policy_reader,
            policy_base_sha=live_base_sha or None,
            visibility=visibility,
            ruling_scope=ruling_scope,
        )
    except EvaluateError as exc:
        raise VerdictNotEvaluatedError(
            f"cannot re-check the verdict in comment #{verdict.comment_id} on PR "
            f"#{verdict.pr_number}: the evaluator configuration is invalid ({exc})"
        ) from exc
    return evaluate_findings(
        _finding_dicts(state.evaluated_findings),
        verdict.head_sha,
        context,
        expected_head=current_head_sha,
        scanners_run=list(state.scanners_run),
        today=today,
    )


def _difference_text(verdict: ReviewerVerdict, computed: Verdict, live_base_sha: str) -> str:
    """Why the gate's outcome is worse than the fence's, in the terms an
    operator can act on."""
    lines = []
    recorded_base = verdict.state.policy_base_sha
    if recorded_base and live_base_sha and recorded_base != live_base_sha:
        lines.append(
            f"the policy base moved from {recorded_base} (when the review was posted) to "
            f"{live_base_sha} (now); policy changes merged to the base since the review apply"
        )
    expired = _expired_text(computed)
    if expired:
        lines.append("expired disposition(s), no longer applied: " + "; ".join(expired))
    unapplied = _unapplied_rulings_text(computed)
    if unapplied:
        lines.append("ruling(s) for this PR that cleared nothing: " + "; ".join(unapplied))
    lines.extend(computed.degraded)
    if computed.outcome == OUTCOME_BLOCKED:
        lines.append("finding(s) that now block:\n" + _blocking_text(computed))
    return "\n".join(f"  - {line}" for line in lines) if lines else "  (no further detail)"


def evaluate_at_gate(
    verdict: ReviewerVerdict,
    reviewer_name: str,
    *,
    current_head_sha: str,
    live_base_sha: str,
    policy_reader: PolicyReader | None,
    visibility: str | None,
    evaluator: EvaluatorGate,
    config_root: str | Path | None = None,
    today: dt.date | None = None,
    ruling_scope: RulingScope | None = None,
) -> GateEvaluation:
    """Apply the decision table in the module docstring to *verdict*, the
    reviewer's newest valid fence (read by merge.verdict.read_reviewer_verdict,
    which has already refused a fence for another head).

    Raises one of merge.errors' Verdict* refusals, each naming what it refused
    on: VerdictNotEvaluatedError, VerdictStaleError, VerdictBlockingError,
    VerdictGateDisagreementError, VerdictDegradedError.
    """
    state = verdict.state
    label = reviewer_name.upper()
    where = f"comment #{verdict.comment_id} on PR #{verdict.pr_number}"

    if not state.evaluated:
        if evaluator.require_evaluated:
            raise VerdictNotEvaluatedError(
                f"{label} verdict ({where}) carries no computed verdict (fence_schema_version "
                f"{state.fence_schema_version}, need 3), and merge.evaluator.require_evaluated is "
                f"true. The status in that fence was asserted by its poster. Have the reviewer "
                f"post again with a loadout-review that computes the verdict, then retry the "
                f"merge gate."
            )
        return GateEvaluation(
            reviewer=reviewer_name,
            evaluated=False,
            notices=(
                f"{label} verdict ({where}) was not evaluated: its fence is below "
                f"fence_schema_version 3, so review_status={verdict.review_status!r} is the "
                f"status its poster asserted and is applied as it always was "
                f"(merge.evaluator.require_evaluated is false)",
            ),
        )

    fence_outcome = state.verdict_outcome
    assert fence_outcome is not None
    fence_reasons = list(state.verdict_reasons)

    if fence_outcome == OUTCOME_STALE:
        raise VerdictStaleError(
            f"STALE GATE DATA — the {label} verdict ({where}) records outcome STALE: its "
            f"findings were produced for head {verdict.head_sha!r}, which was not the PR head "
            f"when it was posted (the merge head is {current_head_sha!r}). "
            + ("; ".join(fence_reasons) + ". " if fence_reasons else "")
            + "Re-run the reviewer at the current head, then retry the merge gate."
        )

    # A refusal that needs no recomputation is made from the fence alone, so a
    # fence that cannot be re-checked still refuses on what it records.
    if state.evaluated_findings is None:
        if fence_outcome == OUTCOME_BLOCKED:
            raise VerdictBlockingError(
                f"{label} BLOCKED merge — the verdict ({where}) records outcome BLOCKED: "
                + "; ".join(fence_reasons)
                + ". The fence does not carry its findings, so the clearing entries cannot be "
                "named here; run `loadout-review evaluate` on the findings."
            )
        if fence_outcome == OUTCOME_DEGRADED and reviewer_name not in evaluator.allow_degraded:
            raise VerdictDegradedError(
                f"{label} verdict ({where}) is DEGRADED: "
                + "; ".join(fence_reasons)
                + f". merge.evaluator.allow_degraded is not true for role {reviewer_name!r}."
            )
        raise VerdictNotEvaluatedError(
            f"{label} verdict ({where}) records outcome {fence_outcome} but does not carry the "
            f"findings it was computed from (evaluated_findings), so the gate cannot compute it "
            f"again at the live base revision. An outcome that cannot be re-checked is not "
            f"accepted. Have the reviewer post again, then retry the merge gate."
        )

    computed = _recompute(
        verdict,
        current_head_sha=current_head_sha,
        live_base_sha=live_base_sha,
        policy_reader=policy_reader,
        visibility=visibility,
        config_root=config_root,
        today=today,
        ruling_scope=ruling_scope,
    )
    gate_outcome = computed.outcome
    applied = _applied_dispositions(computed)
    applied_rulings = _applied_rulings(computed)
    unapplied_rulings = _unapplied_rulings_text(computed)
    ruling_fields = {"applied_rulings": applied_rulings, "unapplied_rulings": unapplied_rulings}

    if gate_outcome == OUTCOME_STALE:
        raise VerdictStaleError(
            f"STALE GATE DATA — the {label} findings ({where}) were produced for head "
            f"{verdict.head_sha!r} but the merge head is {current_head_sha!r}."
        )

    if fence_outcome == OUTCOME_CLEAN and gate_outcome != OUTCOME_CLEAN:
        raise VerdictGateDisagreementError(
            f"{label} verdict ({where}) records CLEAN, but the merge gate computed "
            f"{gate_outcome} from the same findings at the live base revision "
            f"{live_base_sha or '(unknown)'}:\n"
            + _difference_text(verdict, computed, live_base_sha)
            + "\nThe gate does not accept a clean verdict it cannot reproduce. Have the "
            "reviewer post again against the current base, then retry the merge gate."
        )

    # A recorded block passes only when every finding the fence recorded as
    # blocking is individually matched by a disposition applied at the live
    # base. A disposition on some other finding, or a finding that stopped
    # blocking for any other reason, clears nothing.
    unmatched = unmatched_recorded_blocks(state.findings_open, computed)
    cleared_by_disposition = (
        fence_outcome == OUTCOME_BLOCKED
        and gate_outcome == OUTCOME_CLEAN
        and bool(state.findings_open)
        and not unmatched
    )
    if cleared_by_disposition:
        notices: list[str] = []
        if applied:
            notices.append(
                f"{label} verdict ({where}) records BLOCKED, but the standing disposition(s) on "
                f"the live base revision {live_base_sha} now demote the finding(s) to advisory "
                f"and the gate computes CLEAN: {'; '.join(applied)}. A disposition merged to "
                f"the base after the review applies, because policy is read from the base "
                f"revision, which the PR under review cannot have written"
            )
        if applied_rulings:
            notices.append(
                f"{label} verdict ({where}) records BLOCKED, but the ruling(s) recorded for this "
                f"PR in the ruling registry demote the finding(s) to advisory and the gate "
                f"computes CLEAN: {'; '.join(applied_rulings)}. A ruling is read from the "
                f"registry, which the PR under review cannot write"
            )
        notices.extend(
            _ruling_notices(
                label, where, computed, applied_rulings, unapplied_rulings, applied_already_named=True
            )
        )
        return GateEvaluation(
            reviewer=reviewer_name,
            evaluated=True,
            fence_outcome=fence_outcome,
            gate_outcome=gate_outcome,
            applied_dispositions=applied,
            notices=tuple(notices),
            **ruling_fields,
        )

    if OUTCOME_BLOCKED in (fence_outcome, gate_outcome):
        if computed.blocking:
            listing = (
                "open blocking finding(s), each with the entry that would clear it:\n"
                + _blocking_text(computed)
            )
        else:
            listing = (
                "the fence records: " + "; ".join(fence_reasons)
                + f". The gate's own computation at the live base finds nothing blocking "
                f"(outcome {gate_outcome}), but a recorded block is cleared only by a "
                f"disposition that matches each finding it recorded:\n"
                + _unmatched_text(state.findings_open, unmatched)
            )
        if applied_rulings:
            listing += "\nruling(s) that cleared a finding:\n" + "\n".join(
                f"  - {line}" for line in applied_rulings
            )
        if unapplied_rulings:
            listing += "\nruling(s) for this PR that cleared nothing:\n" + "\n".join(
                f"  - {line}" for line in unapplied_rulings
            )
        expired_rulings = _expired_rulings_text(computed)
        if expired_rulings:
            listing += "\nexpired ruling(s), no longer applied:\n" + "\n".join(
                f"  - {line}" for line in expired_rulings
            )
        ignored = [r for r in computed.degraded if r.startswith(IGNORED_PREFIX)]
        if ignored:
            listing += "\nthe ruling registry was not used:\n" + "\n".join(
                f"  - {line}" for line in ignored
            )
        raise VerdictBlockingError(
            f"{label} BLOCKED merge — verdict ({where}) outcome {fence_outcome}, gate outcome "
            f"{gate_outcome}. {listing}\nAdd the entry to {DISPOSITIONS_PATH} on the base branch "
            f"(an entry present only in the change under review does not apply), or fix the "
            f"finding and re-run {label}, then retry the merge gate."
        )

    if OUTCOME_DEGRADED in (fence_outcome, gate_outcome):
        reasons = tuple(dict.fromkeys([*fence_reasons, *computed.degraded]))
        if reviewer_name not in evaluator.allow_degraded:
            raise VerdictDegradedError(
                f"{label} verdict ({where}) is DEGRADED (fence {fence_outcome}, gate "
                f"{gate_outcome}): a required input to it was missing, so it cannot clear the "
                f"change. Reasons:\n"
                + "\n".join(f"  - {reason}" for reason in reasons)
                + f"\nmerge.evaluator.allow_degraded is not true for role {reviewer_name!r}. "
                f"Fix the missing input and re-run {label}, or allow it for the role in the "
                f"tracked gate file, then retry the merge gate."
            )
        return GateEvaluation(
            reviewer=reviewer_name,
            evaluated=True,
            fence_outcome=fence_outcome,
            gate_outcome=gate_outcome,
            degraded_reasons=reasons,
            degraded_allowed=True,
            **ruling_fields,
            notices=(
                f"{label} verdict ({where}) is DEGRADED and merge.evaluator.allow_degraded is "
                f"true for role {reviewer_name!r}; proceeding. Reasons: {'; '.join(reasons)}",
                *_ruling_notices(label, where, computed, applied_rulings, unapplied_rulings),
            ),
        )

    return GateEvaluation(
        reviewer=reviewer_name,
        evaluated=True,
        fence_outcome=fence_outcome,
        gate_outcome=gate_outcome,
        notices=_ruling_notices(label, where, computed, applied_rulings, unapplied_rulings),
        **ruling_fields,
    )


__all__ = ["GateEvaluation", "evaluate_at_gate", "unmatched_recorded_blocks"]
