"""review.docs_only_review — the lighter docs-only review, and when it applies.

A review may take a lighter path for a change that is documentation only. The
path is opt-in and errs toward more review: it applies only when ALL of these
hold, and otherwise the change gets the full review, however plainly it is
documentation.

  1. The classification (review.docs_only_policy) is docs-only at the base.
  2. The post names a sealed spec whose ``docs_only_justification`` is non-empty.
     That text is the requesting agent's own statement of why less review is
     enough; it is audited, not trusted.
  3. The reviewer does not reject the justification, and no verdict on this pull
     request has ever rejected it: one rejection, by any reviewer at any head,
     means full review for every later round, whatever the spec says.

On the lighter path the review is still a real reviewer verdict. The findings it
states are posted as advisory and never enter the open findings, so the computed
verdict cannot block on them. A finding that is, or might be, a secret is the
exception and is judged exactly as in a full review: only a finding whose stated
category is something other than secrets exposure is advisory, so a finding with
no category counts as a possible secret. The scanner outcomes the deployment
requires are still required (the post still evaluates them), so the secrets scan
always runs.

``decide`` is pure. The object it builds is the ``docs_only`` evidence the fence
and the verdict record carry; ``loadout-merge`` prints an accepted one as a
waiver line, which is the only place the waiver is shown.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.acquire.contract import CommentBackend
from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.merge.fence_state import (
    CLASSIFICATION_CODE,
    CLASSIFICATION_DOCS_ONLY,
    KEY_DOCS_ONLY,
    normalize_docs_only,
)
from clagentic_loadout.merge.fence_syntax import find_fence_syntax
from clagentic_loadout.merge.verdict import find_all_verdict_blocks
from clagentic_loadout.review.docs_only_policy import (
    STATE_ABSENT,
    STATE_OK,
    AllowlistSource,
    ChangedPath,
    Classification,
    DocsOnlyPolicy,
    classify_changeset,
)
from clagentic_loadout.review.facts import KEY_FACTS, parse_facts
from clagentic_loadout.review.policy_source import PROFILE_PATH, PolicySource
from clagentic_loadout.review.repo_profile import RepoProfile

#: The category whose findings stay blocking on the lighter path.
SECRETS_CATEGORY = "secrets_exposure"

#: Note rendered under an advisory finding of a docs-only pass.
ADVISORY_NOTE = "docs-only pass: advisory, not evaluated for blocking"

MAX_REASON_CHARS = 300

#: How many of the paths that made a change code a `run` result lists.
MAX_REPORTED_CODE_PATHS = 20


class DocsOnlyError(ValueError):
    """A docs-only input cannot be recorded as given; the message says which."""


def allowlist_of(
    profile: RepoProfile | None, profile_state: str, base_sha: str | None
) -> tuple[DocsOnlyPolicy | None, AllowlistSource]:
    """The docs allowlist in *profile* (read at *base_sha*, ending in
    *profile_state*) and where it came from. None for the allowlist when the
    profile gave none: it is missing, invalid, unread or declares no
    ``review.docs_only``."""
    if profile is not None and profile.docs_only is not None:
        return profile.docs_only, AllowlistSource(base_sha, PROFILE_PATH, STATE_OK)
    state = STATE_ABSENT if profile is not None else profile_state
    return None, AllowlistSource(base_sha, PROFILE_PATH, state)


def classify_with_profile(
    changed: Sequence[ChangedPath],
    profile: RepoProfile | None,
    profile_state: str,
    base_sha: str | None,
) -> Classification:
    """The classification of *changed* by the allowlist in the profile read at
    *base_sha*. The one entry point `run`, `post` and the verb share."""
    allowlist, source = allowlist_of(profile, profile_state, base_sha)
    return classify_changeset(changed, allowlist, source)


def classify_with_policy(
    changed: Sequence[ChangedPath], policy: PolicySource, base_sha: str | None
) -> Classification:
    """``classify_with_profile`` over the profile a read of the policy source
    already holds."""
    return classify_with_profile(changed, policy.repo_profile, policy.profile_state, base_sha)


def check_reason(reason: str) -> str:
    """*reason* as a rejection reason: one non-empty line of bounded length with
    no fence syntax. Raises DocsOnlyError."""
    text = reason.strip()
    if not text:
        raise DocsOnlyError("the rejection reason must not be empty")
    if "\n" in text or "\r" in text or len(text) > MAX_REASON_CHARS:
        raise DocsOnlyError(
            f"the rejection reason must be one line of at most {MAX_REASON_CHARS} characters"
        )
    offending = find_fence_syntax(text)
    if offending is not None:
        raise DocsOnlyError(
            f"the rejection reason contains the fence-delimiter sequence {offending!r}"
        )
    return text


def prior_rejection(comments: Sequence[Mapping[str, Any]], pr_number: int) -> str | None:
    """A description of the first comment on the pull request whose verdict
    fence records a rejected docs-only justification, or None when none does.
    Any comment counts, from any author at any head: a forged rejection can only
    make a review fuller, so the author is not checked."""
    for comment in comments:
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        for block in find_all_verdict_blocks(body):
            docs_only = block.get(KEY_DOCS_ONLY)
            if (
                isinstance(docs_only, dict)
                and docs_only.get("accepted") is False
                and block.get("pr_number") == pr_number
            ):
                return f"comment #{comment.get('id')}"
    return None


def earlier_verdicts_reader(backend: Any, *, owner: str, repo: str, pr_number: int):
    """A zero-argument reader of the pull request's earlier rejection: returns
    its description or None, and raises DocsOnlyError when the pull request's
    comments cannot be read. Nothing is read until it is called."""

    def read() -> str | None:
        if not isinstance(backend, CommentBackend):
            raise DocsOnlyError("the host backend cannot list comments")
        try:
            comments = backend.fetch_issue_comments(owner=owner, repo=repo, pr_number=pr_number)
        except AcquireFetchError as exc:
            raise DocsOnlyError(str(exc)) from exc
        return prior_rejection(comments, pr_number)

    return read


@dataclass(frozen=True)
class ChangesetInput:
    """What `run` knows of the whole pull request, taken before any delta
    narrowing: every changed path, and how to read an earlier rejection."""

    changed: tuple[ChangedPath, ...]
    earlier_rejection: Callable[[], str | None]


def summarize_changeset(
    changeset: ChangesetInput, policy: PolicySource, base_sha: str | None
) -> dict[str, Any]:
    """The classification block `run` records in its result: the classification
    and the allowlist it came from, the paths that made it code, and whether the
    lighter path is open to a post that names a sealed spec with a justification
    (it is not once a verdict on the pull request has rejected one)."""
    classification = classify_with_policy(changeset.changed, policy, base_sha)
    block: dict[str, Any] = {
        "classification": classification.kind,
        "allowlist_source": classification.source.to_dict(),
        "path_count": len(classification.files),
        "code_paths": [
            {"path": item.path, "reason": item.reason}
            for item in classification.code_files()[:MAX_REPORTED_CODE_PATHS]
        ],
    }
    if not classification.is_docs_only:
        block.update(lighter_review_available=False, note="full review: the change is classified as code")
        return block
    try:
        earlier = changeset.earlier_rejection()
    except DocsOnlyError as exc:
        block.update(
            lighter_review_available=False,
            note=f"full review: the pull request's earlier verdicts could not be read ({exc})",
        )
        return block
    if earlier is not None:
        # The paths are documentation, but a reviewer has rejected the claim that
        # less review is enough: from then on the pull request is code, for this
        # round and every later one.
        block.update(
            classification=CLASSIFICATION_CODE,
            path_classification=CLASSIFICATION_DOCS_ONLY,
            lighter_review_available=False,
            note=f"full review: a verdict on this pull request rejected the justification ({earlier})",
        )
        return block
    block.update(
        lighter_review_available=True,
        note="lighter review is open to a post that names a sealed spec with a docs-only justification",
    )
    return block


@dataclass(frozen=True)
class DocsOnlyDecision:
    """Whether the lighter path applies, the evidence object to record (None when
    there is nothing to record), and a line saying which way it went and why."""

    lighter: bool
    evidence: dict[str, Any] | None
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {"lighter_review": self.lighter, "note": self.note}


def _evidence(
    classification: Classification,
    spec_hash: str,
    justification: str,
    *,
    accepted: bool,
    reason: str | None,
) -> dict[str, Any]:
    return {
        "classification": classification.kind,
        "allowlist_source": {
            "base_sha": classification.source.base_sha,
            "path": classification.source.path,
        },
        "justification": justification,
        "spec_hash": spec_hash,
        "accepted": accepted,
        "rejection_reason": reason,
    }


def decide(
    classification: Classification,
    *,
    spec_hash: str | None,
    justification: str | None,
    reject_reason: str | None = None,
    earlier_rejection: str | None = None,
    earlier_unreadable: str | None = None,
) -> DocsOnlyDecision:
    """The docs-only decision for one post. *reject_reason* is the reviewer's
    rejection of this justification (already checked by ``check_reason``);
    *earlier_rejection* describes a rejection already on the pull request, and
    *earlier_unreadable* says why the pull request's earlier verdicts could not
    be read (which counts against the lighter path, like a rejection).

    Raises DocsOnlyError when a rejection cannot be recorded."""
    has_justification = bool(spec_hash) and bool(justification)
    if reject_reason is not None:
        if not has_justification:
            raise DocsOnlyError(
                "there is no docs-only justification to reject: name a sealed spec "
                "whose docs_only_justification is set"
            )
        evidence = _evidence(
            classification, spec_hash, justification, accepted=False, reason=reject_reason
        )
        try:
            normalize_docs_only(evidence)
        except ValueError as exc:
            raise DocsOnlyError(f"the rejection cannot be recorded: {exc}") from exc
        return DocsOnlyDecision(
            False, evidence, f"full review: the docs-only justification was rejected: {reject_reason}"
        )
    if classification.kind != CLASSIFICATION_DOCS_ONLY:
        return DocsOnlyDecision(False, None, "full review: the change is classified as code")
    if not has_justification:
        return DocsOnlyDecision(
            False, None, "full review: no sealed spec with a docs-only justification was named"
        )
    if earlier_rejection is not None:
        return DocsOnlyDecision(
            False,
            None,
            f"full review: a verdict on this pull request rejected the justification "
            f"({earlier_rejection})",
        )
    if earlier_unreadable is not None:
        return DocsOnlyDecision(
            False,
            None,
            f"full review: the pull request's earlier verdicts could not be read "
            f"({earlier_unreadable})",
        )
    evidence = _evidence(classification, spec_hash, justification, accepted=True, reason=None)
    try:
        normalize_docs_only(evidence)
    except ValueError as exc:
        return DocsOnlyDecision(
            False, None, f"full review: the justification cannot be recorded in the fence ({exc})"
        )
    return DocsOnlyDecision(True, evidence, "lighter review: the docs-only justification is accepted")


def is_possible_secret(finding: Mapping[str, Any]) -> bool:
    """Whether *finding* is, or might be, a secret: its stated category is secrets
    exposure, or it states none (or a category outside the vocabulary)."""
    return parse_facts(finding.get(KEY_FACTS)).facts.category in (None, SECRETS_CATEGORY)


def split_findings(
    findings: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(held, advisory)``: the findings still judged as in a full review, and
    the ones the lighter path posts as advisory. Order is kept within each."""
    held: list[dict[str, Any]] = []
    advisory: list[dict[str, Any]] = []
    for finding in findings:
        (held if is_possible_secret(finding) else advisory).append(finding)
    return held, advisory


__all__ = [
    "ADVISORY_NOTE",
    "ChangesetInput",
    "DocsOnlyDecision",
    "DocsOnlyError",
    "SECRETS_CATEGORY",
    "allowlist_of",
    "check_reason",
    "classify_with_policy",
    "classify_with_profile",
    "decide",
    "earlier_verdicts_reader",
    "is_possible_secret",
    "prior_rejection",
    "split_findings",
    "summarize_changeset",
]
