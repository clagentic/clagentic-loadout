"""merge.fence_clearance — a reviewer's clearance of its own earlier blocking fence.

A role's verdict at a head is every fence it posted at that head, and each must
pass (merge.fence_union). Before this module, nothing a later fence said could
lift an earlier blocking fence: a finding the reviewer itself considered answered
kept the gate refusing until an operator disposition landed on the base branch,
or the head moved.

A clearance lets the reviewer that raised a finding answer it at the same head.
It is one of two shapes in a LATER fence of the same role at the same head:

  cleared_claims  {id, head, evidence, fingerprint?}   a clearance stated as such
  dropped         {file, line, rule_id, message, reason}  a candidate the reviewer
                                       examined and dropped, which names the same
                                       location and rule as the finding it held
                                       open: the shape reviews posted before
                                       cleared_claims carried a fingerprint made

An earlier fence is superseded (and no longer needs to pass on its own) only when
ALL of the following hold; each is checked from trusted input, and a doubtful case
leaves the fence in force, so this adds a way to clear a block and never weakens
one the old way refused:

  - it is an evaluated BLOCKED fence that lists the findings it held open;
  - EVERY finding it lists is cleared, and the latest word on it in this role's
    later fences at the head is a clearance, not a re-raise;
  - each clearance carries evidence (a non-blank reason), is for this head, and,
    when the block states a fingerprint, states the same one;
  - each finding is process record by the base-branch policy
    (review.clearance_policy). Anything the policy does not classify is security
    content and still needs a disposition on the base branch;
  - the gate's own recomputation of the fence's findings at the live base finds
    nothing degraded and no blocking finding that the clearances do not cover.

A clearance never reaches another role's fence (fences are read per role, by the
platform's login), another head's, or a finding that is not listed as held open.
It is not a ``by_design`` ruling and creates no disposition.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.merge.errors import VerdictNotEvaluatedError
from clagentic_loadout.merge.fence_state import KEY_FINGERPRINT, is_fingerprint
from clagentic_loadout.merge.gate_evaluation import GateEvaluation, recompute_at_gate
from clagentic_loadout.merge.verdict import ReviewerVerdict
from clagentic_loadout.review.clearance_policy import ClearancePolicy
from clagentic_loadout.review.evaluate import OUTCOME_BLOCKED, OUTCOME_STALE, Verdict
from clagentic_loadout.review.evaluation import finding_state_id
from clagentic_loadout.review.policy_reader import PolicyReader
from clagentic_loadout.review.policy_source import read_policy_from
from clagentic_loadout.review.ruling_provider import RulingScope
from clagentic_loadout.sha import FULL_SHA_RE

SOURCE_CLAIM = "cleared_claim"
SOURCE_DROPPED = "dropped"


@dataclass(frozen=True)
class Clearance:
    """One clearance of a finding, as a later fence states it."""

    finding_id: str
    evidence: str
    comment_id: Any
    source: str
    fingerprint: str | None = None

    def matches(self, entry: Mapping[str, Any], head: str) -> bool:
        """Whether this clearance answers the open *entry* of a fence for *head*.
        The id decides; a fingerprint the entry states must be the one the
        clearance states, and a clearance that states none cannot answer it."""
        if entry.get("id") != self.finding_id or entry.get("head") != head:
            return False
        recorded = entry.get(KEY_FINGERPRINT)
        if is_fingerprint(recorded):
            return self.fingerprint == recorded
        return True


@dataclass(frozen=True)
class SupersededFence:
    """An earlier blocking fence that later clearances of the same role answer."""

    verdict: ReviewerVerdict
    clearances: tuple[Clearance, ...]

    def describe_clearances(self) -> list[str]:
        """One line per finding: its id, the comment that cleared it, how, and
        the evidence it gave."""
        return [
            f"{item.finding_id} cleared by comment #{item.comment_id} ({item.source}): "
            f"{' '.join(item.evidence.split())}"
            for item in self.clearances
        ]


def _nonblank(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def clearances_in(fence: ReviewerVerdict) -> tuple[Clearance, ...]:
    """The clearances *fence* states, evidence-bearing and for its own head only."""
    found: list[Clearance] = []
    for claim in fence.state.cleared_claims:
        if claim.get("head") != fence.head_sha or not _nonblank(claim.get("evidence")):
            continue
        if not _nonblank(claim.get("id")):
            continue
        fingerprint = claim.get(KEY_FINGERPRINT)
        found.append(
            Clearance(
                claim["id"],
                claim["evidence"],
                fence.comment_id,
                SOURCE_CLAIM,
                fingerprint if is_fingerprint(fingerprint) else None,
            )
        )
    for entry in fence.state.dropped:
        file, line, rule_id = entry.get("file"), entry.get("line"), entry.get("rule_id")
        if not (_nonblank(file) and _nonblank(rule_id) and type(line) is int):
            continue
        if not _nonblank(entry.get("reason")):
            continue
        found.append(
            Clearance(
                finding_state_id(file, line, rule_id),
                entry["reason"],
                fence.comment_id,
                SOURCE_DROPPED,
            )
        )
    return tuple(found)


def _later_word(
    entry: Mapping[str, Any], head: str, later: Sequence[ReviewerVerdict]
) -> Clearance | None:
    """The clearance that is the latest word on *entry* among *later* (oldest
    first), or None when a later fence re-raises it or none clears it."""
    word: Clearance | None = None
    for fence in later:
        if any(item.get("id") == entry.get("id") for item in fence.state.findings_open):
            word = None
            continue
        for clearance in clearances_in(fence):
            if clearance.matches(entry, head):
                word = clearance
    return word


def _stated_categories(fence: ReviewerVerdict, finding_id: str) -> list[str | None]:
    """The category each of *fence*'s evaluated findings under *finding_id* states."""
    categories: list[str | None] = []
    for item in fence.state.evaluated_findings or ():
        if item.get("severity") == "praise":
            continue
        if finding_state_id(item.get("file"), item.get("line"), item.get("rule_id")) != finding_id:
            continue
        facts = item.get("facts")
        category = facts.get("category") if isinstance(facts, Mapping) else None
        categories.append(category if isinstance(category, str) else None)
    return categories


def _eligible(fence: ReviewerVerdict) -> bool:
    state = fence.state
    return (
        fence.review_status == "blocking"
        and state.evaluated
        and state.verdict_outcome == OUTCOME_BLOCKED
        and state.evaluated_findings is not None
        and bool(state.findings_open)
    )


def apply_clearances(
    head_fences: Sequence[ReviewerVerdict],
    *,
    policy: Callable[[], ClearancePolicy],
    recompute: Callable[[ReviewerVerdict], Verdict],
) -> tuple[tuple[ReviewerVerdict, ...], tuple[SupersededFence, ...], tuple[str, ...]]:
    """Split a role's fences at the head (newest first, as
    merge.fence_union.collect_head_fences returns them) into the ones that must
    still pass and the ones a later clearance by the same role supersedes.

    *policy* returns the base-branch clearance policy; it is called at most once,
    and only when some fence has every finding cleared. *recompute* is the gate's
    own evaluation of a fence's findings at the live base
    (merge.gate_evaluation.recompute_at_gate). The newest fence is never
    superseded, since nothing is later than it.

    The third element holds one line for each fence whose findings the role
    cleared but which stays in force, and why, so the refusal that follows says
    what it is still waiting for."""
    chronological = list(reversed(head_fences))
    superseded: dict[int, SupersededFence] = {}
    declined: list[str] = []
    loaded: ClearancePolicy | None = None
    for index, fence in enumerate(chronological):
        if not _eligible(fence):
            continue
        later = chronological[index + 1 :]
        words = [_later_word(entry, fence.head_sha, later) for entry in fence.state.findings_open]
        if not later or any(word is None for word in words):
            continue
        if loaded is None:
            loaded = policy()
        security = [
            entry
            for entry in fence.state.findings_open
            if not loaded.is_process_record(
                entry.get("rule_id", ""), _stated_categories(fence, entry["id"])
            )
        ]
        if security:
            declined.append(
                f"comment #{fence.comment_id}: the reviewer cleared its finding(s) "
                f"{', '.join(str(e['id']) for e in fence.state.findings_open)}, but "
                f"{', '.join(str(e.get('rule_id')) for e in security)} is not a process-record "
                "finding in the repo profile on the base branch, so a clearance does not lift "
                "the block; it needs a disposition on the base branch"
            )
            continue
        try:
            computed = recompute(fence)
        except VerdictNotEvaluatedError:
            continue
        covered = {entry["id"] for entry in fence.state.findings_open}
        uncovered = [
            item
            for item in computed.blocking
            if finding_state_id(item.file, item.line, item.rule_id) not in covered
        ]
        if computed.outcome == OUTCOME_STALE or computed.degraded or uncovered:
            declined.append(
                f"comment #{fence.comment_id}: the reviewer cleared its finding(s), but the gate's "
                "own computation of that fence at the live base is degraded or finds a blocking "
                "finding the clearance does not cover, so the block stands"
            )
            continue
        superseded[index] = SupersededFence(fence, tuple(word for word in words if word is not None))
    kept = tuple(fence for index, fence in enumerate(chronological) if index not in superseded)
    return (
        tuple(reversed(kept)),
        tuple(superseded[i] for i in sorted(superseded)),
        tuple(declined),
    )


def open_findings(
    fences: Sequence[ReviewerVerdict],
) -> list[tuple[ReviewerVerdict, Mapping[str, Any]]]:
    """The findings *fences* (one role at one head, oldest first) leave open: each
    ``(fence that listed it, its findings_open entry)`` that no later fence of the
    role clears. A finding a later fence lists again is open under that later
    fence. This is the set a reviewer may clear, and the set a clearance is
    validated against."""
    held: dict[str, tuple[ReviewerVerdict, Mapping[str, Any]]] = {}
    for fence in fences:
        for clearance in clearances_in(fence):
            owner = held.get(clearance.finding_id)
            if owner is not None and clearance.matches(owner[1], fence.head_sha):
                del held[clearance.finding_id]
        for entry in fence.state.findings_open:
            held[entry["id"]] = (fence, entry)
    return list(held.values())


def read_clearance_policy(
    policy_reader: PolicyReader | None, live_base_sha: str, visibility: str | None
) -> ClearancePolicy:
    """The clearance policy of the repo profile at the live base revision. It is
    the empty policy (nothing is process record) when no reader or base revision
    is available or the profile is missing or invalid: the default fails closed."""
    if policy_reader is None or not FULL_SHA_RE.match(live_base_sha or ""):
        return ClearancePolicy()
    profile = read_policy_from(policy_reader, live_base_sha, visibility=visibility).repo_profile
    return profile.clearance if profile is not None else ClearancePolicy()


def supersede_at_gate(
    head_fences: Sequence[ReviewerVerdict],
    reviewer_name: str,
    *,
    current_head_sha: str,
    live_base_sha: str,
    policy_reader: PolicyReader | None,
    visibility: str | None,
    ruling_scope: RulingScope | None = None,
) -> tuple[tuple[ReviewerVerdict, ...], tuple[GateEvaluation, ...], tuple[str, ...]]:
    """apply_clearances with the policy and the recomputation read the way the gate
    reads them: at the live base revision, through *policy_reader*. Returns the
    fences that must still pass, one GateEvaluation for each superseded fence
    (for the merge output and the attestation), and the lines that say why a
    cleared fence stays in force."""
    kept, superseded, declined = apply_clearances(
        head_fences,
        policy=lambda: read_clearance_policy(policy_reader, live_base_sha, visibility),
        recompute=lambda fence: recompute_at_gate(
            fence,
            current_head_sha=current_head_sha,
            live_base_sha=live_base_sha,
            policy_reader=policy_reader,
            visibility=visibility,
            ruling_scope=ruling_scope,
        ),
    )
    records = tuple(
        GateEvaluation(
            reviewer=reviewer_name,
            evaluated=True,
            fence_outcome=item.verdict.state.verdict_outcome,
            applied_clearances=tuple(item.describe_clearances()),
        )
        for item in superseded
    )
    return kept, records, declined


__all__ = [
    "SOURCE_CLAIM",
    "SOURCE_DROPPED",
    "Clearance",
    "SupersededFence",
    "apply_clearances",
    "clearances_in",
    "open_findings",
    "read_clearance_policy",
    "supersede_at_gate",
]
