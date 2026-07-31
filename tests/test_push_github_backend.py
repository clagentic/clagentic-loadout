"""test_push_github_backend.py — tests for clagentic_loadout.push.github_backend
(lr-09ca, Wave B slice 3). All HTTP is mocked via an injected opener -- no
real network call anywhere in this file.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from clagentic_loadout.push.errors import PrOpenError
from clagentic_loadout.push.github_backend import GITHUB_API_BASE, create_pr, get_pr_body, update_pr


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


def _json_resp(status: int, payload) -> _FakeResponse:
    return _FakeResponse(status, json.dumps(payload).encode("utf-8"))


class TestCreatePr:
    def test_success_returns_pr_number(self):
        def opener(req, timeout=30):
            assert req.get_method() == "POST"
            assert req.full_url == f"{GITHUB_API_BASE}/repos/some-owner/some-repo/pulls"
            payload = json.loads(req.data.decode("utf-8"))
            assert payload == {"head": "feature", "base": "main", "title": "t", "body": "b"}
            return _json_resp(201, {"number": 7})

        pr_number = create_pr(
            "some-owner", "some-repo", token="tok", head="feature", base="main",
            title="t", body="b", opener=opener,
        )
        assert pr_number == 7

    def test_non_2xx_raises_with_server_message(self):
        def opener(req, timeout=30):
            raise urllib.error.HTTPError(
                req.full_url, 422, "Unprocessable", {},
                None,
            )

        with pytest.raises(PrOpenError):
            create_pr("o", "r", token="tok", head="h", base="main", title="t", body="b", opener=opener)

    def test_missing_number_raises(self):
        def opener(req, timeout=30):
            return _json_resp(201, {})

        with pytest.raises(PrOpenError):
            create_pr("o", "r", token="tok", head="h", base="main", title="t", body="b", opener=opener)

    def test_token_never_in_request_body_or_url(self):
        secret = "super-secret-gh-token"

        def opener(req, timeout=30):
            assert secret not in req.data.decode("utf-8")
            assert secret not in req.full_url
            return _json_resp(201, {"number": 1})

        create_pr("o", "r", token=secret, head="h", base="main", title="t", body="b", opener=opener)

    def test_redirect_response_never_followed(self):
        """A 3xx from the GitHub API must surface as a failure, never a
        silently-followed second request carrying the live token to a
        different host."""
        calls = []

        def opener(req, timeout=30):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": "http://evil.example.com"}, None)

        with pytest.raises(PrOpenError):
            create_pr("o", "r", token="tok", head="h", base="main", title="t", body="b", opener=opener)
        assert len(calls) == 1


class TestUpdatePr:
    def test_success_patches_title_and_body(self):
        def opener(req, timeout=30):
            assert req.get_method() == "PATCH"
            payload = json.loads(req.data.decode("utf-8"))
            assert payload == {"title": "new title", "body": "new body"}
            return _json_resp(200, {})

        update_pr("o", "r", 42, token="tok", title="new title", body="new body", opener=opener)

    def test_failure_raises_pr_open_error(self):
        def opener(req, timeout=30):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

        with pytest.raises(PrOpenError):
            update_pr("o", "r", 42, token="tok", title="t", opener=opener)


class TestGetPrBody:
    """lr-2500b7 append mode: get_pr_body is the GET half of the GET-then-
    PATCH append flow push.verb._run_update_pr composes."""

    def test_returns_existing_body(self):
        def opener(req, timeout=30):
            assert req.get_method() == "GET"
            assert req.full_url == f"{GITHUB_API_BASE}/repos/o/r/pulls/42"
            return _json_resp(200, {"number": 42, "body": "existing PR body text"})

        assert get_pr_body("o", "r", 42, token="tok", opener=opener) == "existing PR body text"

    def test_empty_body_returns_empty_string_not_none(self):
        def opener(req, timeout=30):
            return _json_resp(200, {"number": 42, "body": None})

        assert get_pr_body("o", "r", 42, token="tok", opener=opener) == ""

    def test_non_2xx_raises_pr_open_error(self):
        def opener(req, timeout=30):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

        with pytest.raises(PrOpenError):
            get_pr_body("o", "r", 42, token="tok", opener=opener)
