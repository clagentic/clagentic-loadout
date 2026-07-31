"""test_review_forgejo_backend.py — tests for
clagentic_loadout.review.forgejo_backend (lr-412f, Wave B slice 2).

Coverage:
  - post_and_verify_comment reuses transport.git_host_api's building blocks
    (request/resolve_bot_login/verify_comment_on_pr/check_pr_sha) rather
    than duplicating HTTP/readback logic -- verified by driving the SAME
    injected-opener pattern git_host_api's own test suite uses, with no real
    network call anywhere in this file.
  - Success path returns a VerifiedReview sourced from the readback.
  - FAILURE modes translate onto the shared review.errors vocabulary:
      * POST-phase git_host_api errors (write-method non-2xx, stale PR SHA,
        owner/repo not found, token fetch failure) -> ReviewPostError.
      * verify-phase git_host_api errors (no matching comment on readback)
        -> ReviewVerifyError.
  - No datetime dependence: the freshness anchor is exercised entirely
    through git_host_api's own not_before/created_at plumbing via the injected
    opener, never asserted against real wall-clock time here.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from clagentic_loadout.review.errors import (
    DeleteOwnCommentRefusedError,
    ReviewPostError,
    ReviewVerifyError,
)
from clagentic_loadout.review.forgejo_backend import (
    ForgejoReviewBackend,
    delete_own_comment,
    post_and_verify_comment,
)


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _make_success_opener(*, bot_login="git-host-bot", comment_id=7, pr_number=42):
    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "POST" and url.endswith(f"/issues/{pr_number}/comments"):
            return _FakeResponse(200, b'{"id": 7}')
        if url.endswith("/api/v1/user"):
            return _FakeResponse(200, json.dumps({"login": bot_login}).encode())
        if url.endswith(f"/issues/{pr_number}/comments"):
            return _FakeResponse(
                200,
                json.dumps(
                    [
                        {
                            "id": comment_id,
                            "user": {"login": bot_login},
                            "body": "review body text",
                            "created_at": "2099-01-01T00:00:10Z",
                            "html_url": "http://git-host.example.com/comment/7",
                        }
                    ]
                ).encode(),
            )
        raise AssertionError(f"unexpected request: {method} {url}")

    return opener


class TestPostAndVerifyCommentSuccess:
    def test_success_returns_verified_review_from_readback(self):
        opener = _make_success_opener()
        verified = post_and_verify_comment(
            "http://git-host.example.com",
            "tok",
            "some-owner",
            "some-repo",
            42,
            "review body text",
            opener=opener,
        )
        assert verified.id == 7
        assert verified.url == "http://git-host.example.com/comment/7"
        assert verified.login == "git-host-bot"

    def test_pr_sha_check_runs_before_post_when_supplied(self):
        calls = []

        def opener(req, timeout=15):
            calls.append((req.get_method(), req.full_url))
            if req.full_url.endswith("/pulls/42"):
                return _FakeResponse(200, json.dumps({"head": {"sha": "abc123"}}).encode())
            return _make_success_opener()(req, timeout=timeout)

        verified = post_and_verify_comment(
            "http://git-host.example.com",
            "tok",
            "o",
            "r",
            42,
            "review body text",
            expected_pr_sha="abc123",
            opener=opener,
        )
        assert verified.id == 7
        # The PR-SHA GET happens before the comments POST.
        methods_urls = [c for c in calls if "pulls/42" in c[1] or "comments" in c[1]]
        assert methods_urls[0] == ("GET", "http://git-host.example.com/api/v1/repos/o/r/pulls/42")


class TestPostAndVerifyCommentPostFailures:
    def test_write_method_non_2xx_raises_review_post_error(self):
        def opener(req, timeout=15):
            raise urllib.error.HTTPError(
                req.full_url, 422, "Unprocessable", {}, io.BytesIO(b'{"message":"bad"}')
            )

        with pytest.raises(ReviewPostError):
            post_and_verify_comment(
                "http://git-host.example.com", "tok", "o", "r", 1, "body", opener=opener
            )

    def test_stale_pr_sha_raises_review_post_error(self):
        def opener(req, timeout=15):
            if req.full_url.endswith("/pulls/1"):
                return _FakeResponse(200, json.dumps({"head": {"sha": "current-sha"}}).encode())
            raise AssertionError("should not reach POST after stale-SHA refusal")

        with pytest.raises(ReviewPostError, match="MISMATCH"):
            post_and_verify_comment(
                "http://git-host.example.com",
                "tok",
                "o",
                "r",
                1,
                "body",
                expected_pr_sha="stale-sha",
                opener=opener,
            )


class TestPostAndVerifyCommentVerifyFailures:
    def test_no_matching_comment_on_readback_raises_review_verify_error(self):
        def opener(req, timeout=15):
            url = req.full_url
            method = req.get_method()
            if method == "POST" and url.endswith("/issues/1/comments"):
                return _FakeResponse(200, b'{"id": 7}')
            if url.endswith("/api/v1/user"):
                return _FakeResponse(200, b'{"login": "git-host-bot"}')
            if url.endswith("/issues/1/comments"):
                return _FakeResponse(200, b"[]")  # nothing landed
            raise AssertionError(f"unexpected request: {method} {url}")

        with pytest.raises(ReviewVerifyError, match="MISMATCH"):
            post_and_verify_comment(
                "http://git-host.example.com", "tok", "o", "r", 1, "body", opener=opener
            )

    def test_wrong_login_on_readback_raises_review_verify_error(self):
        def opener(req, timeout=15):
            url = req.full_url
            method = req.get_method()
            if method == "POST" and url.endswith("/issues/1/comments"):
                return _FakeResponse(200, b'{"id": 7}')
            if url.endswith("/api/v1/user"):
                return _FakeResponse(200, b'{"login": "git-host-bot"}')
            if url.endswith("/issues/1/comments"):
                return _FakeResponse(
                    200,
                    json.dumps(
                        [
                            {
                                "id": 7,
                                "user": {"login": "someone-else"},
                                "body": "body",
                                "created_at": "2099-01-01T00:00:10Z",
                            }
                        ]
                    ).encode(),
                )
            raise AssertionError(f"unexpected request: {method} {url}")

        with pytest.raises(ReviewVerifyError, match="MISMATCH"):
            post_and_verify_comment(
                "http://git-host.example.com", "tok", "o", "r", 1, "body", opener=opener
            )


class TestForgejoReviewBackend:
    def test_post_and_verify_delegates_to_module_function(self):
        opener = _make_success_opener()
        backend = ForgejoReviewBackend(
            "tok", git_host_base="http://git-host.example.com", opener=opener
        )
        verified = backend.post_and_verify(
            owner="o", repo="r", pr_number=42, body="review body text"
        )
        assert verified.id == 7
        assert verified.login == "git-host-bot"


def _make_delete_opener(*, comment_id=42, own_login="git-host-bot", author_login=None, body="stub"):
    """Mirrors github_backend's own delete-fixture shape: GET /api/v1/user
    resolves own_login, GET issues/comments/<id> returns the comment's
    live author/body, DELETE succeeds. *author_login* defaults to
    *own_login* (the ordinary self-authored case) -- pass a different value
    to exercise the cross-author refusal."""
    if author_login is None:
        author_login = own_login

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if url.endswith("/api/v1/user"):
            return _FakeResponse(200, json.dumps({"login": own_login}).encode())
        if method == "GET" and url.endswith(f"/issues/comments/{comment_id}"):
            return _FakeResponse(
                200,
                json.dumps({"id": comment_id, "user": {"login": author_login}, "body": body}).encode(),
            )
        if method == "DELETE" and url.endswith(f"/issues/comments/{comment_id}"):
            return _FakeResponse(200, b"{}")
        raise AssertionError(f"unexpected request: {method} {url}")

    return opener


class TestDeleteOwnComment:
    """lr-f43c4b: this module's delete_own_comment adapter delegates to
    transport.git_host_api.delete_own_comment verbatim (no duplicated
    GET/assert/DELETE logic) and translates GitHostApiError onto this
    package's shared review.errors vocabulary -- mirroring
    post_and_verify_comment's own translation shape for the post path."""

    def test_success_deletes_own_comment(self):
        opener = _make_delete_opener()
        delete_own_comment(
            "http://git-host.example.com", "tok", "o", "r", 42, opener=opener
        )  # no exception raised == success

    def test_cross_author_refused(self):
        opener = _make_delete_opener(own_login="git-host-bot", author_login="someone-else")
        with pytest.raises(DeleteOwnCommentRefusedError, match="not the caller's own"):
            delete_own_comment(
                "http://git-host.example.com", "tok", "o", "r", 42, opener=opener
            )

    def test_verdict_fence_refused_even_for_own_comment(self):
        from clagentic_loadout.merge.verdict import build_verdict_block

        fenced_body = build_verdict_block("reviewer", "clean", "a" * 40, 42)
        opener = _make_delete_opener(body=fenced_body)
        with pytest.raises(DeleteOwnCommentRefusedError, match="verdict"):
            delete_own_comment(
                "http://git-host.example.com", "tok", "o", "r", 42, opener=opener
            )

    def test_delete_call_failure_raises_review_post_error(self):
        def opener(req, timeout=15):
            url = req.full_url
            method = req.get_method()
            if url.endswith("/api/v1/user"):
                return _FakeResponse(200, b'{"login": "git-host-bot"}')
            if method == "GET" and url.endswith("/issues/comments/42"):
                return _FakeResponse(
                    200,
                    json.dumps({"id": 42, "user": {"login": "git-host-bot"}, "body": "stub"}).encode(),
                )
            if method == "DELETE":
                raise urllib.error.HTTPError(
                    url, 500, "Internal Server Error", {}, io.BytesIO(b"{}")
                )
            raise AssertionError(f"unexpected request: {method} {url}")

        with pytest.raises(ReviewPostError):
            delete_own_comment(
                "http://git-host.example.com", "tok", "o", "r", 42, opener=opener
            )

    def test_forgejo_review_backend_delegates_to_module_function(self):
        opener = _make_delete_opener()
        backend = ForgejoReviewBackend(
            "tok", git_host_base="http://git-host.example.com", opener=opener
        )
        backend.delete_own_comment(owner="o", repo="r", comment_id=42)  # no exception == success

    def test_forgejo_review_backend_propagates_refusal(self):
        opener = _make_delete_opener(own_login="git-host-bot", author_login="someone-else")
        backend = ForgejoReviewBackend(
            "tok", git_host_base="http://git-host.example.com", opener=opener
        )
        with pytest.raises(DeleteOwnCommentRefusedError):
            backend.delete_own_comment(owner="o", repo="r", comment_id=42)
