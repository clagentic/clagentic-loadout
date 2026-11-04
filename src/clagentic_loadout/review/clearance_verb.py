"""review.clearance_verb — the reviewer's side of clearing its own earlier finding.

A reviewer that blocked a pull request can answer its own finding at the same
head, for example after the author supplied the record it asked for, without a
new head and without an operator ruling. Two read paths and one write path live
here:

  open_findings_of   the findings the role's own fences at a head leave open, with
                     the id each is cleared by, read from the pull request's
                     comments (the fences are the store the ids live in)
  clearance_claims   `post --clear ID REASON`: validate each request against that
                     open set and return the cleared_claims entries to carry in the
                     new fence
  `open`             the read-only verb that prints the open set

Only the role's own fences count (the platform login decides authorship), only at
the head being posted, and a finding this very review still holds open cannot be
cleared by it. What a clearance is worth to the merge gate is decided there
(merge.fence_clearance), not here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from clagentic_loadout.acquire.contract import CommentBackend
from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.merge.fence_clearance import open_findings
from clagentic_loadout.merge.fence_state import (
    KEY_FINGERPRINT,
    is_fingerprint,
    state_from_fence,
)
from clagentic_loadout.merge.verdict import ReviewerVerdict, find_all_verdict_blocks
from clagentic_loadout.review.evaluation import finding_state_id


class ClearanceError(Exception):
    """A clearance request or the open set could not be read; the message says why."""


def _author(comment: Mapping[str, Any]) -> str | None:
    user = comment.get("user")
    login = user.get("login") if isinstance(user, Mapping) else None
    return login if isinstance(login, str) else None


def _in_post_order(comments: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Comment ids rise with posting on both hosts, so they order the comments
    without trusting the listing's order."""
    if all(type(comment.get("id")) is int for comment in comments):
        return sorted(comments, key=lambda comment: comment["id"])
    return list(comments)


def _fence_of(comment: Mapping[str, Any], pr_number: int, head_sha: str) -> ReviewerVerdict | None:
    """The role's fence in *comment* when it is a single readable fence of this
    pull request at *head_sha*; anything else is not a fence at this head."""
    body = comment.get("body")
    if not isinstance(body, str):
        return None
    blocks = find_all_verdict_blocks(body)
    if len(blocks) != 1 or "_malformed" in blocks[0]:
        return None
    data = blocks[0]
    if data.get("pr_number") != pr_number or data.get("head_sha") != head_sha:
        return None
    status = data.get("review_status")
    if status not in ("clean", "blocking"):
        return None
    return ReviewerVerdict(
        reviewer=str(data.get("reviewer", "")),
        review_status=status,
        head_sha=head_sha,
        pr_number=pr_number,
        comment_id=comment.get("id", 0),
        comment_author_login=_author(comment) or "",
        state=state_from_fence(data),
    )


def open_findings_of(
    comments: Sequence[Mapping[str, Any]], *, login: str, pr_number: int, head_sha: str
) -> list[dict[str, Any]]:
    """The findings *login*'s fences at *head_sha* leave open, oldest first. Each
    entry names the id to clear it by, its rule, fingerprint when the fence
    stated one, the comment that holds it open, and (from that fence's evaluated
    findings) where it sits and what it claimed."""
    fences = [
        fence
        for comment in _in_post_order(
            [c for c in comments if isinstance(c, Mapping) and _author(c) == login]
        )
        if (fence := _fence_of(comment, pr_number, head_sha)) is not None
    ]
    found: list[dict[str, Any]] = []
    for fence, entry in open_findings(fences):
        item: dict[str, Any] = {
            "id": entry["id"],
            "rule_id": entry.get("rule_id"),
            "head": entry.get("head"),
            "comment_id": fence.comment_id,
        }
        if is_fingerprint(entry.get(KEY_FINGERPRINT)):
            item[KEY_FINGERPRINT] = entry[KEY_FINGERPRINT]
        for evaluated in fence.state.evaluated_findings or ():
            if evaluated.get("severity") == "praise":
                continue
            where = (evaluated.get("file"), evaluated.get("line"), evaluated.get("rule_id"))
            if finding_state_id(*where) == entry["id"]:
                item.update(file=evaluated["file"], line=evaluated["line"], message=evaluated["message"])
                break
        found.append(item)
    return found


def read_open_findings(
    backend: Any, *, owner: str, repo: str, pr_number: int, login: str, head_sha: str
) -> list[dict[str, Any]]:
    """open_findings_of over the comments the host serves for the pull request.
    Raises ClearanceError when they cannot be read in full."""
    if not isinstance(backend, CommentBackend):
        raise ClearanceError("the host backend cannot list comments")
    try:
        comments = backend.fetch_issue_comments(owner=owner, repo=repo, pr_number=pr_number)
    except AcquireFetchError as exc:
        raise ClearanceError(str(exc)) from exc
    return open_findings_of(comments, login=login, pr_number=pr_number, head_sha=head_sha)


def clearance_claims(
    requests: Sequence[tuple[str, str]],
    open_set: Sequence[Mapping[str, Any]],
    *,
    head_sha: str,
    still_open_ids: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """The cleared_claims entries for *requests*, each ``(finding id, reason)``.

    Every request is validated before any is returned: the id must be in
    *open_set* (the role's own open findings at this head), must not be one this
    review still holds open (*still_open_ids*), must be named once, and must give
    a reason. The claim carries the open finding's fingerprint when it had one, so
    the merge gate can match it to the exact finding."""
    by_id = {item["id"]: item for item in open_set}
    claims: list[dict[str, Any]] = []
    seen: set[str] = set()
    for finding_id, reason in requests:
        if not isinstance(reason, str) or not reason.strip():
            raise ClearanceError(
                f"--clear {finding_id}: a clearance needs a reason that states the evidence"
            )
        if finding_id in seen:
            raise ClearanceError(f"--clear names {finding_id} more than once")
        seen.add(finding_id)
        if finding_id in still_open_ids:
            raise ClearanceError(
                f"--clear {finding_id}: this review still holds that finding open, so it "
                "cannot also clear it"
            )
        target = by_id.get(finding_id)
        if target is None:
            known = ", ".join(sorted(by_id)) or "none"
            raise ClearanceError(
                f"--clear {finding_id}: this reviewer holds no such finding open at head "
                f"{head_sha} (open: {known}). Only a finding this reviewer raised at this head "
                "can be cleared; `loadout-review open` lists them."
            )
        claim: dict[str, Any] = {"id": finding_id, "head": head_sha, "evidence": reason.strip()}
        if is_fingerprint(target.get(KEY_FINGERPRINT)):
            claim[KEY_FINGERPRINT] = target[KEY_FINGERPRINT]
        claims.append(claim)
    return claims


def render_open(login: str, pr_number: int, head_sha: str, open_set: Sequence[Mapping[str, Any]]) -> str:
    """The JSON the `open` verb prints."""
    return json.dumps(
        {
            "result": "open_findings",
            "pr_number": pr_number,
            "reviewer_login": login,
            "head_sha": head_sha,
            "open": list(open_set),
        }
    )


__all__ = [
    "ClearanceError",
    "clearance_claims",
    "open_findings_of",
    "read_open_findings",
    "render_open",
]
