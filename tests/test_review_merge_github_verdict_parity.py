"""test_review_merge_github_verdict_parity.py — cross-backend regression
test for lr-71f467 (P1): GitHub verdict-endpoint mismatch between
review.github_backend (write side) and merge.github_backend (read side).

DEFECT THIS GUARDS AGAINST: before this fix, review.github_backend posted a
reviewer's verdict as a GitHub NATIVE PR REVIEW (POST /pulls/{pr}/reviews),
while merge.github_backend.fetch_comments (the merge gate's read side) only
ever read issue comments (GET /issues/{pr}/comments). A clean verdict posted
via review-post was therefore INVISIBLE to the merge gate: loadout-merge
refused every GitHub merge requiring a reviewer verdict with exit 24 ("No PR
comment from reviewer login ... found"), even though the reviewer had
genuinely posted a clean verdict moments before.

THE LOOP THIS TEST EXERCISES (VERIFY-DONE criterion from lr-71f467): post a
verdict via review.github_backend.post_and_verify_review (the exact function
review.verb's --verdict-review-status / --verdict-findings routes call),
then read it back via merge.github_backend.fetch_comments + merge.verdict.
read_reviewer_verdict (the exact functions the merge gate itself calls) --
against a SHARED in-memory GitHub server, so this test fails exactly the way
the real gatekeeper PR #17 incident did if the two backends ever diverge on
the verdict transport again.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from clagentic_loadout.merge import github_backend as merge_github_backend
from clagentic_loadout.merge.verdict import (
    build_verdict_block,
    read_reviewer_verdict,
)
from clagentic_loadout.review import github_backend as review_github_backend

_OWNER = "some-owner"
_REPO = "some-repo"
_PR_NUMBER = 42
_HEAD_SHA = "a" * 40
_REVIEWER_LOGIN = "reviewer-app[bot]"
_REVIEWER_NAME = "reviewer"


class _FakeResponse:
    def __init__(self, status: int, body: bytes, content_type: str = "application/json"):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json_response(status: int, payload) -> _FakeResponse:
    return _FakeResponse(status, json.dumps(payload).encode("utf-8"))


class _SharedGithubServer:
    """In-memory stand-in for the GitHub REST API's issues/{pr}/comments
    endpoint, shared by BOTH backends under test -- a POST issued through
    review_github_backend's opener actually appends to the SAME list a GET
    issued through merge_github_backend's opener reads. This is what makes
    the test a genuine cross-backend regression check rather than two
    independently-mocked unit tests that could each pass while still
    disagreeing on the endpoint shape in production.
    """

    def __init__(self) -> None:
        self.comments: list[dict] = []
        self._next_id = 1000

    def review_opener(self, req, timeout=15):
        """Opener injected into review.github_backend calls -- also serves
        GET /user for identity resolution (PAT/OAuth-shaped: 200 directly,
        no app-slug config needed)."""
        url = req.full_url
        if url.endswith("/user"):
            return _json_response(200, {"login": _REVIEWER_LOGIN})
        if req.get_method() == "GET" and url.endswith(f"/issues/{_PR_NUMBER}/comments"):
            return _json_response(200, list(self.comments))
        if req.get_method() == "POST" and url.endswith(f"/issues/{_PR_NUMBER}/comments"):
            payload = json.loads(req.data.decode("utf-8"))
            comment = {
                "id": self._next_id,
                "user": {"login": _REVIEWER_LOGIN},
                "body": payload["body"],
                "html_url": f"http://example.invalid/comments/{self._next_id}",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._next_id += 1
            self.comments.append(comment)
            return _json_response(200, {"id": comment["id"], "html_url": comment["html_url"]})
        raise AssertionError(f"unexpected review-side request: {req.get_method()} {url}")

    def merge_opener(self, req, timeout=30):
        """Opener injected into merge.github_backend calls -- read-only GET
        against the SAME comments list the review-side opener writes to."""
        url = req.full_url
        if req.get_method() == "GET" and url.endswith(f"/issues/{_PR_NUMBER}/comments"):
            return _json_response(200, list(self.comments))
        raise AssertionError(f"unexpected merge-side request: {req.get_method()} {url}")


class TestGithubReviewPostVisibleToMergeGate:
    """The cross-backend loop lr-71f467 fixes: post via review.github_backend,
    read via merge.github_backend -- on a SHARED server."""

    def test_verdict_posted_via_review_backend_is_found_by_merge_gate(self):
        server = _SharedGithubServer()

        # WRITE SIDE: the exact call review.verb's verdict routes make --
        # a fenced ```review-result``` block appended to the comment body via
        # merge.verdict.build_verdict_block, posted through
        # review.github_backend.post_and_verify_review.
        fence = build_verdict_block(_REVIEWER_NAME, "clean", _HEAD_SHA, _PR_NUMBER)
        body = f"No issues found.\n{fence}"
        verified = review_github_backend.post_and_verify_review(
            _OWNER, _REPO, _PR_NUMBER, body, "tok", opener=server.review_opener,
        )
        assert verified.login == _REVIEWER_LOGIN

        # READ SIDE: the exact calls the merge gate makes -- fetch_comments
        # (GET issues/{pr}/comments), then read_reviewer_verdict (the SAME
        # parser/authorship-binding the gate enforces).
        comments = merge_github_backend.fetch_comments(
            _OWNER, _REPO, _PR_NUMBER, token="tok", opener=server.merge_opener,
        )
        verdict = read_reviewer_verdict(
            comments, _REVIEWER_LOGIN, _HEAD_SHA, _PR_NUMBER, _OWNER, _REPO,
            expected_reviewer_name=_REVIEWER_NAME,
        )

        assert verdict.review_status == "clean"
        assert verdict.head_sha == _HEAD_SHA
        assert verdict.reviewer == _REVIEWER_NAME
        assert verdict.comment_author_login == _REVIEWER_LOGIN

    def test_blocking_verdict_posted_via_review_backend_is_found_by_merge_gate(self):
        """Same loop, 'blocking' status -- the merge gate must see a
        BLOCKING verdict too (not just a clean one), so a genuinely
        blocking review is never invisible to the gate either."""
        server = _SharedGithubServer()

        fence = build_verdict_block(_REVIEWER_NAME, "blocking", _HEAD_SHA, _PR_NUMBER)
        body = f"Found an issue.\n{fence}"
        review_github_backend.post_and_verify_review(
            _OWNER, _REPO, _PR_NUMBER, body, "tok", opener=server.review_opener,
        )

        comments = merge_github_backend.fetch_comments(
            _OWNER, _REPO, _PR_NUMBER, token="tok", opener=server.merge_opener,
        )
        verdict = read_reviewer_verdict(
            comments, _REVIEWER_LOGIN, _HEAD_SHA, _PR_NUMBER, _OWNER, _REPO,
            expected_reviewer_name=_REVIEWER_NAME,
        )
        assert verdict.review_status == "blocking"

    def test_verdict_at_stale_sha_is_refused_by_merge_gate_not_silently_accepted(self):
        """The cross-backend fix must not weaken the existing SHA-freshness
        gate: a verdict posted for an OLDER head_sha must still be refused
        when the merge gate re-reads it against a NEWER current head SHA."""
        from clagentic_loadout.merge.errors import VerdictStaleError

        server = _SharedGithubServer()
        stale_sha = "b" * 40
        fence = build_verdict_block(_REVIEWER_NAME, "clean", stale_sha, _PR_NUMBER)
        body = f"No issues found.\n{fence}"
        review_github_backend.post_and_verify_review(
            _OWNER, _REPO, _PR_NUMBER, body, "tok", opener=server.review_opener,
        )

        comments = merge_github_backend.fetch_comments(
            _OWNER, _REPO, _PR_NUMBER, token="tok", opener=server.merge_opener,
        )
        with pytest.raises(VerdictStaleError):
            read_reviewer_verdict(
                comments, _REVIEWER_LOGIN, _HEAD_SHA, _PR_NUMBER, _OWNER, _REPO,
                expected_reviewer_name=_REVIEWER_NAME,
            )

    def test_no_verdict_posted_is_refused_by_merge_gate(self):
        """Sanity check on the OTHER side of the loop: an empty comments list
        (nothing posted yet) must still refuse -- this test file must not
        accidentally make the gate permissive while fixing the transport
        mismatch."""
        from clagentic_loadout.merge.errors import VerdictMissingError

        server = _SharedGithubServer()
        comments = merge_github_backend.fetch_comments(
            _OWNER, _REPO, _PR_NUMBER, token="tok", opener=server.merge_opener,
        )
        with pytest.raises(VerdictMissingError):
            read_reviewer_verdict(
                comments, _REVIEWER_LOGIN, _HEAD_SHA, _PR_NUMBER, _OWNER, _REPO,
                expected_reviewer_name=_REVIEWER_NAME,
            )
