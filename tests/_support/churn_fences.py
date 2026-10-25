"""Comment listings for the churn tests, built from the blocking and clean verdict
comments captured from real hosts (tests/real_payloads). A captured comment keeps
its host-shaped envelope (author object, timestamps, urls) and its prose; the head,
pull request number, author, id and the finding it blocks on are re-stamped, since a
test needs several rounds of one pull request and a captured one is a single round."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from clagentic_loadout.review.evaluation import finding_state_id

PAYLOADS = Path(__file__).resolve().parent.parent / "real_payloads"
FENCE_OPEN = "```review-result\n"
FENCE_CLOSE = "\n```\n"

#: The captured blocking and clean verdict comments, by platform: the index of
#: each in the captured listing.
_CAPTURED = {"github": {"blocking": 0, "clean": 1}, "forgejo": {"blocking": 1, "clean": 0}}


def captured_comment(platform: str, kind: str) -> dict:
    """The captured verdict comment of *kind* ("blocking" or "clean"), as the host served it."""
    listing = json.loads((PAYLOADS / f"{platform}_comments.json").read_text(encoding="utf-8"))
    return copy.deepcopy(listing[_CAPTURED[platform][kind]])


def _split(body: str) -> tuple[str, dict]:
    prose, _, rest = body.partition(FENCE_OPEN)
    return prose, json.loads(rest.rsplit(FENCE_CLOSE, 1)[0])


def verdict_comment(
    platform: str,
    *,
    comment_id: int,
    login: str,
    head: str,
    pr_number: int,
    blocks_on: tuple[tuple[str, str, int], ...] = (),
    reviewer: str = "reviewer",
    created_at: str | None = None,
) -> dict:
    """A verdict comment by *login* for *head*. *blocks_on* is the findings it
    holds open, each ``(rule_id, file, line)``; none gives a clean verdict."""
    comment = captured_comment(platform, "blocking" if blocks_on else "clean")
    prose, fence = _split(comment["body"])
    fence.update(reviewer=reviewer, head_sha=head, pr_number=pr_number)
    fence["range"] = {**fence["range"], "head": head}
    if blocks_on:
        fence["findings_open"] = [
            {"id": finding_state_id(file, line, rule), "rule_id": rule, "head": head}
            for rule, file, line in blocks_on
        ]
        fence["failure_sequences"] = [
            {"file": file, "line": line, "rule_id": rule, "failure_sequence": "a -> b"}
            for rule, file, line in blocks_on
        ]
        fence["evaluated_findings"] = [
            {"file": file, "line": line, "rule_id": rule, "message": f"{rule} is wrong"}
            for rule, file, line in blocks_on
        ]
    comment["body"] = prose + FENCE_OPEN + json.dumps(fence) + FENCE_CLOSE
    comment["id"] = comment_id
    comment["user"] = {**comment["user"], "login": login}
    if created_at is not None:
        comment["created_at"] = created_at
    return comment


def replace_fence(comment: dict, **changes) -> dict:
    """*comment* with fields of its fence replaced (a key set to None is removed)."""
    out = copy.deepcopy(comment)
    prose, fence = _split(out["body"])
    for key, value in changes.items():
        if value is None:
            fence.pop(key, None)
        else:
            fence[key] = value
    out["body"] = prose + FENCE_OPEN + json.dumps(fence) + FENCE_CLOSE
    return out
