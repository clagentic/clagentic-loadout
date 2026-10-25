"""review.churn_ledger — the churn breaker's ledger: the posting role's own
earlier verdict fences on a pull request, read as review rounds.

Only comments whose author is the posting role's login count. Unlike the
docs-only rejection scan, where a forged comment can only make a review fuller,
an inflated round count escalates a builder for nothing, so the author is
checked. An undercount is the safe direction for advisory output, so every
ambiguity below reads as "not blocking" and ends the chain.

Fields read from a fence (nothing else is):

  pr_number, head_sha, review_status   which round it is, for which head
  findings_open[].id, .rule_id         the keys it held open
  evaluated_findings[], failure_sequences[]
                                       file, line and rule_id, from which
                                       findings_open ids are joined to a file
                                       (the id is review.evaluation.finding_state_id)

Rounds are distinct heads in post order; several fences at one head count once,
as the last of them. A comment without a fence, or whose fence is for another
pull request, is not a round at all. A fence that cannot be read (not one JSON
object, no usable head) is a round that holds nothing open.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from clagentic_loadout.acquire.contract import CommentBackend
from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.merge.fence_state import (
    KEY_EVALUATED_FINDINGS,
    KEY_FAILURE_SEQUENCES,
    KEY_FINDINGS_OPEN,
)
from clagentic_loadout.merge.verdict import find_all_verdict_blocks
from clagentic_loadout.review.churn import Key, Round
from clagentic_loadout.review.evaluation import finding_state_id
from clagentic_loadout.sha import FULL_SHA_RE

_STATUS_CLEAN = "clean"
_STATUS_BLOCKING = "blocking"


class ChurnLedgerError(Exception):
    """The pull request's comments could not be read; the message says why."""


def _author(comment: Mapping[str, Any]) -> str | None:
    user = comment.get("user")
    login = user.get("login") if isinstance(user, Mapping) else None
    return login if isinstance(login, str) else None


def _in_post_order(comments: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Comment ids rise with posting on both hosts, so they order the comments
    without trusting the listing's order; a listing without integer ids is kept
    as served."""
    if all(type(comment.get("id")) is int for comment in comments):
        return sorted(comments, key=lambda comment: comment["id"])
    return comments


def _files_by_state_id(data: Mapping[str, Any]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for source in (KEY_EVALUATED_FINDINGS, KEY_FAILURE_SEQUENCES):
        entries = data.get(source)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            file, line, rule_id = entry.get("file"), entry.get("line"), entry.get("rule_id")
            if isinstance(file, str) and type(line) is int and isinstance(rule_id, str):
                index.setdefault(finding_state_id(file, line, rule_id), set()).add(file)
    return index


def blocking_keys(data: Mapping[str, Any]) -> frozenset[Key] | None:
    """The (rule_id, file) keys a blocking fence holds open, or None when they
    cannot be read: findings_open is not a list, or an entry of it is malformed
    or cannot be joined to a file."""
    entries = data.get(KEY_FINDINGS_OPEN)
    if entries is None:
        return frozenset()
    if not isinstance(entries, list):
        return None
    index = _files_by_state_id(data)
    keys: set[Key] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            return None
        state_id, rule_id = entry.get("id"), entry.get("rule_id")
        files = index.get(state_id) if isinstance(state_id, str) else None
        if not files or not isinstance(rule_id, str):
            return None
        keys.update((rule_id, file) for file in files)
    return frozenset(keys)


def _round_of(body: Any, pr_number: int) -> Round | None:
    """The round a comment body is, or None when it is not a round of this PR."""
    if not isinstance(body, str):
        return None
    blocks = find_all_verdict_blocks(body)
    if not blocks:
        return None
    if len(blocks) > 1 or "_malformed" in blocks[0]:
        return Round(None)
    data = blocks[0]
    fence_pr = data.get("pr_number")
    if type(fence_pr) is int and fence_pr != pr_number:
        return None
    head = data.get("head_sha")
    if type(fence_pr) is not int or not isinstance(head, str) or not FULL_SHA_RE.match(head):
        return Round(None)
    status = data.get("review_status")
    if status == _STATUS_CLEAN:
        return Round(head)
    keys = blocking_keys(data) if status == _STATUS_BLOCKING else None
    return Round(head, keys if keys is not None else frozenset())


def rounds_from_comments(
    comments: Sequence[Mapping[str, Any]], *, login: str, pr_number: int
) -> list[Round]:
    """The review rounds *login* posted on pull request *pr_number*, oldest
    first, one per distinct head (the last fence at a head wins)."""
    own = [c for c in comments if isinstance(c, Mapping) and _author(c) == login]
    rounds: list[Round] = []
    for comment in _in_post_order(own):
        found = _round_of(comment.get("body"), pr_number)
        if found is None:
            continue
        if found.head_sha is not None:
            rounds = [item for item in rounds if item.head_sha != found.head_sha]
        rounds.append(found)
    return rounds


def read_rounds(
    backend: Any, *, owner: str, repo: str, pr_number: int, login: str
) -> list[Round]:
    """``rounds_from_comments`` over the comments the host serves for the pull
    request. Raises ChurnLedgerError when they cannot be read in full."""
    if not isinstance(backend, CommentBackend):
        raise ChurnLedgerError("the host backend cannot list comments")
    try:
        comments = backend.fetch_issue_comments(owner=owner, repo=repo, pr_number=pr_number)
    except AcquireFetchError as exc:
        raise ChurnLedgerError(str(exc)) from exc
    return rounds_from_comments(comments, login=login, pr_number=pr_number)


__all__ = [
    "ChurnLedgerError",
    "blocking_keys",
    "read_rounds",
    "rounds_from_comments",
]
