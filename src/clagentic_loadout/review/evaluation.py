"""review.evaluation — the evaluator wired to a review run.

`run` and `post` both turn a list of findings into a computed verdict, and
`loadout-review evaluate` must agree with them on the same inputs, so the
assembly of those inputs lives here once: the evaluator configuration (user
level), the policy read from the trusted base revision, and the repository
visibility as the host reported it.

Governing rule: anything that can lower a verdict must be positively
established from trusted input, and any value that is absent, unknown,
unrecognised or unverifiable resolves to the worst case. Here that means a
missing policy base revision, an unreadable policy file, and a visibility the
host record did not state all yield a DEGRADED verdict with the reason named,
and visibility then counts as public (every path exposed).

Nothing here removes a finding: `annotate_findings` returns the findings the
evaluator kept (identical copies are merged into the strongest) with the
computed fields written beside whatever the finding already carried.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clagentic_loadout.merge.fence_state import (
    KEY_EVALUATOR_VERSION,
    KEY_POLICY_BASE_SHA,
    KEY_VERDICT_OUTCOME,
    KEY_VERDICT_REASONS,
)
from clagentic_loadout.review.evaluate import (
    EVALUATOR_VERSION,
    OUTCOME_CLEAN,
    OUTCOME_STALE,
    Verdict,
    evaluate,
)
from clagentic_loadout.review.evaluate_verb import load_evaluator_config
from clagentic_loadout.review.facts import KEY_FACTS
from clagentic_loadout.review.finding_identity import KEY_FINGERPRINT, is_fingerprint
from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReader
from clagentic_loadout.review.policy_source import PolicySource, read_policy_from
from clagentic_loadout.review.profile_settings import ProfileSettings
from clagentic_loadout.review.ruling_provider import (
    RulingProvider,
    RulingRead,
    RulingScope,
    read_scope_rulings,
    resolve_ruling_provider,
)
from clagentic_loadout.review.rubric import DEFAULT_BLOCK_AT
from clagentic_loadout.review.stakes import VISIBILITIES, VISIBILITY_PUBLIC, Stakes

#: Recorded in a findings document only; the fence has no use for it.
KEY_REPOSITORY_VISIBILITY = "repository_visibility"

#: Fields `annotate_findings` writes on each finding. Removed before rewriting,
#: so a finding carried from an earlier run never keeps a stale computation.
ANNOTATION_KEYS = (
    "computed_severity",
    "blocking",
    "facts_state",
    "disposition",
    "disposition_refused",
    "possible_duplicates",
)

#: What the legacy `severity` field of a written finding says: blocking when the
#: computed verdict blocks on it, nit otherwise. Derived from the computation,
#: never from anything the reviewer asserted.
SEVERITY_BLOCKING = "blocking"
SEVERITY_NIT = "nit"

_MAX_REASON_CHARS = 300
_PRAISE = "praise"


@dataclass(frozen=True)
class EvaluationContext:
    """Everything besides the findings that a verdict depends on.

    ``policy_base_sha`` is the revision the pull request merges into, which is
    where policy is read from; None means it is not known. ``visibility`` is the
    repository visibility the host reported, None when it did not state one.

    Policy is read through ``policy_reader`` (normally the host API), or from
    the local repository at ``repo_path`` when that is named explicitly and no
    reader is given. With neither there is no policy source and the verdict is
    DEGRADED; the current directory is never assumed to be a repository."""

    block_at: str = DEFAULT_BLOCK_AT
    required_scanners: tuple[str, ...] = ()
    repo_path: Path | None = None
    policy_reader: PolicyReader | None = None
    policy_base_sha: str | None = None
    visibility: str | None = None
    #: Where per-PR rulings are read from, and the one repository and pull
    #: request they are for. With either missing no ruling is applied: a ruling
    #: that cannot be tied to this pull request must not clear anything.
    ruling_provider: RulingProvider | None = None
    ruling_scope: RulingScope | None = None
    #: The deployment's own repo profile settings (review.profile_settings). Used
    #: only by `run`, to state repo profile findings.
    profile_settings: ProfileSettings = field(default_factory=ProfileSettings)

    @property
    def effective_visibility(self) -> str:
        """The visibility the evaluation uses: the reported one when it is a
        recognised value, else public, the worst case."""
        return self.visibility if self.visibility in VISIBILITIES else VISIBILITY_PUBLIC


def context_from_config(
    config_root: str | Path | None, **fields: Any
) -> EvaluationContext:
    """An EvaluationContext whose evaluator settings come from the user-level
    configuration. Raises evaluate_verb.EvaluateError when it is invalid."""
    block_at, required = load_evaluator_config(config_root)
    if "ruling_provider" not in fields:
        fields["ruling_provider"] = resolve_ruling_provider(config_root)
    return EvaluationContext(block_at=block_at, required_scanners=tuple(required), **fields)


def read_context_policy(context: EvaluationContext) -> PolicySource:
    """The policy *context* describes, read once. Hand the result to
    ``evaluate_findings(policy=...)`` to evaluate under it without a second read."""
    visibility = context.effective_visibility
    if context.policy_base_sha is None:
        reason = "the policy base revision is not known, so no policy was read"
        return PolicySource((), Stakes(visibility=visibility), (reason,))
    reader = context.policy_reader
    if reader is None and context.repo_path is not None:
        reader = GitPolicyReader(context.repo_path)
    if reader is None:
        reason = "no policy source is available, so no policy was read"
        return PolicySource((), Stakes(visibility=visibility), (reason,))
    return read_policy_from(reader, context.policy_base_sha, visibility=visibility)


def _rulings(context: EvaluationContext) -> RulingRead:
    """The rulings on record for the context's pull request. None are applied
    when no registry is configured or the scope is not known."""
    return read_scope_rulings(context.ruling_provider, context.ruling_scope)


def evaluate_findings(
    findings: Sequence[Mapping[str, Any]],
    head_sha: str,
    context: EvaluationContext,
    *,
    expected_head: str | None = None,
    scanners_run: Sequence[Mapping[str, Any]] | None = None,
    today: dt.date | None = None,
    policy: PolicySource | None = None,
) -> Verdict:
    """The Verdict for *findings* produced for *head_sha*, under *context*.
    *policy*, when given, is the already-read policy for that context
    (read_context_policy); otherwise it is read here."""
    if policy is None:
        policy = read_context_policy(context)
    ruled = _rulings(context)
    degraded = [*policy.degraded, *ruled.degraded]
    if context.visibility not in VISIBILITIES:
        degraded.append(
            "the repository visibility was not established from the host record, "
            "so the repository is treated as public"
        )
    return evaluate(
        findings,
        policy.stakes,
        # Base-revision dispositions first, so a standing entry keeps its place;
        # a ruling then applies only where those leave a finding blocking.
        (*policy.dispositions, *ruled.rulings),
        head_sha,
        expected_head if expected_head is not None else head_sha,
        block_at=context.block_at,
        today=today,
        scanners_run=scanners_run,
        required_scanners=context.required_scanners,
        degraded_reasons=degraded,
    )


def _one_line(text: str) -> str:
    """*text* as a single bounded line without a backtick run, so a reason can
    sit in a verdict fence and a comment whatever the finding text held."""
    flat = " ".join(text.replace("`", "'").split())
    if len(flat) > _MAX_REASON_CHARS:
        return flat[: _MAX_REASON_CHARS - 3] + "..."
    return flat


def verdict_reasons(verdict: Verdict) -> list[str]:
    """Why the outcome is not CLEAN, in precedence order: a stale head, each
    degraded input, then each blocking finding. Empty for CLEAN."""
    if verdict.outcome == OUTCOME_CLEAN:
        return []
    reasons: list[str] = []
    if verdict.outcome == OUTCOME_STALE:
        reasons.append(
            f"the findings are for head {verdict.head_sha} but the expected head is "
            f"{verdict.expected_head}"
        )
    reasons.extend(verdict.degraded)
    reasons.extend(
        f"{finding.id} blocks (computed severity {finding.severity})" for finding in verdict.blocking
    )
    return [_one_line(reason) for reason in reasons]


def annotate_findings(
    findings: Sequence[Mapping[str, Any]], verdict: Verdict
) -> list[dict[str, Any]]:
    """The findings the evaluator kept, in evaluator order, each with the
    computed fields written beside what it already carried. Identical findings
    are merged into the strongest copy; nothing else is removed."""
    annotated: list[dict[str, Any]] = []
    for evaluated in verdict.findings:
        item = {k: v for k, v in findings[evaluated.source_index].items() if k not in ANNOTATION_KEYS}
        if not evaluated.praise:
            item["severity"] = SEVERITY_BLOCKING if evaluated.blocking else SEVERITY_NIT
        item.update(
            computed_severity=evaluated.severity,
            blocking=evaluated.blocking,
            facts_state=evaluated.facts_state,
            disposition=evaluated.disposition,
            disposition_refused=evaluated.disposition_refused,
            possible_duplicates=list(evaluated.possible_duplicates),
        )
        annotated.append(item)
    return annotated


def fence_evaluation_fields(verdict: Verdict, context: EvaluationContext) -> dict[str, Any]:
    """The evaluation fields a verdict fence carries. The base revision is left
    out when it was not known, rather than stated as null."""
    fields: dict[str, Any] = {
        KEY_VERDICT_OUTCOME: verdict.outcome,
        KEY_VERDICT_REASONS: verdict_reasons(verdict),
        KEY_EVALUATOR_VERSION: EVALUATOR_VERSION,
    }
    if context.policy_base_sha is not None:
        fields[KEY_POLICY_BASE_SHA] = context.policy_base_sha
    return fields


def evaluated_findings_for_fence(
    findings: Sequence[Mapping[str, Any]], *, with_fingerprint: bool = False
) -> list[dict[str, Any]]:
    """The findings *as given to the evaluator*, reduced to the fields the
    verdict depends on, for the fence to carry. The merge gate evaluates these
    again; carrying what the evaluator saw (not what was posted as prose) is
    what lets the two computations agree on identical policy.

    A finding's fingerprint is a link hint that is not posted, so it is not
    carried by default; the gate then cannot apply a disposition's fingerprint
    narrowing, and merge.gate_evaluation refuses to let such a disposition
    demote a block. With *with_fingerprint* (set only when a ruling registry is
    configured) it is carried when well formed, because a ruling is keyed to it
    and the gate must be able to match the same ruling the evaluator did."""
    carried: list[dict[str, Any]] = []
    for finding in findings:
        item: dict[str, Any] = {key: finding[key] for key in ("file", "line", "rule_id", "message")}
        if with_fingerprint and is_fingerprint(finding.get(KEY_FINGERPRINT)):
            item[KEY_FINGERPRINT] = finding[KEY_FINGERPRINT]
        if finding.get(KEY_FACTS) is not None:
            item[KEY_FACTS] = finding[KEY_FACTS]
        if finding.get("severity") == _PRAISE:
            item["severity"] = _PRAISE
        carried.append(item)
    return carried


def finding_state_id(file: str, line: int, rule_id: str) -> str:
    """The id a blocking finding holds in the fence's ``findings_open`` and a
    later fence names in ``cleared_claims``. Derived from the finding's
    location and rule, so it is the same wherever it is computed; a digest,
    because a path holds characters the id grammar does not allow."""
    digest = hashlib.sha256("\0".join((file, str(line), rule_id)).encode("utf-8"))
    return f"f-{digest.hexdigest()[:16]}"


def findings_open_from_verdict(verdict: Verdict, head_sha: str) -> list[dict[str, str]]:
    """The fence's ``findings_open`` for *verdict*: exactly the findings that
    block, after dispositions. A finding a base-revision disposition demoted is
    advisory and is not listed, so it cannot hold a later merge open."""
    # Two findings with different messages on one line and rule share an id;
    # the fence lists that id once, since it names a location and a rule.
    entries: dict[str, dict[str, str]] = {}
    for finding in verdict.blocking:
        state_id = finding_state_id(finding.file, finding.line, finding.rule_id)
        entries.setdefault(
            state_id, {"id": state_id, "rule_id": finding.rule_id, "head": head_sha}
        )
    return list(entries.values())


def verdict_document_fields(verdict: Verdict, context: EvaluationContext) -> dict[str, Any]:
    """The file-level fields a findings document records about its verdict."""
    return {
        **fence_evaluation_fields(verdict, context),
        KEY_POLICY_BASE_SHA: context.policy_base_sha,
        KEY_REPOSITORY_VISIBILITY: context.effective_visibility,
    }


def is_unsafe_status(status: str, verdict: Verdict) -> bool:
    """Whether a caller's *status* claims more than the computed outcome allows:
    clean when the outcome is anything but CLEAN."""
    return status == "clean" and verdict.outcome != OUTCOME_CLEAN


__all__ = [
    "ANNOTATION_KEYS",
    "KEY_EVALUATOR_VERSION",
    "KEY_POLICY_BASE_SHA",
    "KEY_REPOSITORY_VISIBILITY",
    "KEY_VERDICT_OUTCOME",
    "KEY_VERDICT_REASONS",
    "EvaluationContext",
    "annotate_findings",
    "context_from_config",
    "evaluate_findings",
    "evaluated_findings_for_fence",
    "fence_evaluation_fields",
    "finding_state_id",
    "findings_open_from_verdict",
    "is_unsafe_status",
    "read_context_policy",
    "verdict_document_fields",
    "verdict_reasons",
]
