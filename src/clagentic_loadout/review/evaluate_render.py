"""review.evaluate_render — the human rendering of a Verdict, built from code.

Every sentence here is derived from the computed Verdict; nothing is composed by
a model. A BLOCKED rendering lists each open blocking finding with the exact
dispositions entry that would clear it, or says the security floor forbids
clearing it by design.
"""

from __future__ import annotations

from clagentic_loadout.review.evaluate import (
    OUTCOME_BLOCKED,
    OUTCOME_CLEAN,
    OUTCOME_DEGRADED,
    OUTCOME_STALE,
    EvaluatedFinding,
    Verdict,
)
from clagentic_loadout.review.policy_source import DISPOSITIONS_PATH

_HEADLINES = {
    OUTCOME_STALE: "STALE: the findings do not describe the expected head",
    OUTCOME_DEGRADED: "DEGRADED: a required input is missing, so this cannot clear the change",
    OUTCOME_BLOCKED: "BLOCKED: at least one finding blocks",
    OUTCOME_CLEAN: "CLEAN: nothing blocks",
}


def _short(sha: str | None) -> str:
    return sha[:12] if sha else "(none)"


def _stakes_line(finding: EvaluatedFinding) -> str:
    if finding.stakes is None:
        return ""
    s = finding.stakes
    return (
        f"stakes: exposure={s.exposure} ({s.exposure_source}), data_class={s.data_class} "
        f"({s.data_class_source}), merge_control={s.merge_control} ({s.merge_control_source})"
    )


def _finding_lines(finding: EvaluatedFinding, *, detail: bool) -> list[str]:
    head = f"  {finding.index}. {finding.id}  severity={finding.severity}"
    if finding.praise:
        head += "  (praise)"
    lines = [head, f"     {finding.message}"]
    if detail and finding.stakes is not None:
        lines.append(f"     {_stakes_line(finding)}")
    if finding.facts_state != "complete" and not finding.praise:
        lines.append(f"     facts {finding.facts_state}: {'; '.join(finding.facts_problems)}")
    if finding.disposition is not None:
        d = finding.disposition
        lines.append(
            f"     cleared by {d['kind']} disposition ({d['by']}, {d['at']}): {d['rationale']}"
        )
    if finding.disposition_refused:
        lines.append(f"     disposition refused: {finding.disposition_refused}")
    if finding.possible_duplicates:
        lines.append(f"     possible duplicate of: {', '.join(finding.possible_duplicates)}")
    return lines


def _blocking_lines(finding: EvaluatedFinding) -> list[str]:
    lines = _finding_lines(finding, detail=True)
    if finding.floor_forbids_clearing:
        lines.append(
            "     The security floor forbids clearing this finding as by_design. It can be "
            "ruled accepted_risk (expires, default 30 days), false_positive or mitigated."
        )
    if finding.clear_with:
        lines.append(
            f"     To clear it, add this entry to {DISPOSITIONS_PATH} on the base branch "
            "(an entry present only in the change under review does not apply):"
        )
        lines.extend(f"       {row}" for row in finding.clear_with.splitlines())
    return lines


def render_text(verdict: Verdict) -> str:
    """The human rendering of *verdict*."""
    lines = [
        _HEADLINES[verdict.outcome],
        f"head: {_short(verdict.head_sha)}  expected head: {_short(verdict.expected_head)}  "
        f"blocks at: {verdict.block_at}",
    ]
    if verdict.outcome == OUTCOME_STALE:
        lines.append(
            f"The findings were produced for head {verdict.head_sha} but the expected head is "
            f"{verdict.expected_head}. Review the current head again."
        )
    if verdict.degraded:
        lines.append(f"Degraded inputs ({len(verdict.degraded)}):")
        lines.extend(f"  - {reason}" for reason in verdict.degraded)
    if verdict.blocking:
        lines.append(f"Blocking findings ({len(verdict.blocking)}):")
        for finding in verdict.blocking:
            lines.extend(_blocking_lines(finding))
    if verdict.advisory:
        lines.append(f"Advisory findings ({len(verdict.advisory)}):")
        for finding in verdict.advisory:
            lines.extend(_finding_lines(finding, detail=False))
    if verdict.expired_dispositions:
        lines.append(f"Expired dispositions, not applied ({len(verdict.expired_dispositions)}):")
        for entry in verdict.expired_dispositions:
            lines.append(
                f"  - {entry['path']} {entry['rule_id']} ({entry['kind']}, expired {entry['expires']})"
            )
    return "\n".join(lines)


__all__ = ["render_text"]
