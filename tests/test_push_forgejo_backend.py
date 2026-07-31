"""test_push_forgejo_backend.py — tests for clagentic_loadout.push.forgejo_backend
(lr-09ca, Wave B slice 3). All HTTP is mocked via an injected opener -- no
real network call anywhere in this file.
"""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.push.errors import PrOpenError
from clagentic_loadout.push.forgejo_backend import create_pr, get_pr_body, update_pr


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
        def opener(req, timeout=15):
            assert req.get_method() == "POST"
            assert req.full_url.endswith("/api/v1/repos/some-owner/some-repo/pulls")
            payload = json.loads(req.data.decode("utf-8"))
            assert payload == {"head": "feature", "base": "main", "title": "t", "body": "b"}
            return _json_resp(201, {"number": 42})

        pr_number = create_pr(
            "https://git-host.example.com", "some-owner", "some-repo",
            token="tok", head="feature", base="main", title="t", body="b",
            opener=opener,
        )
        assert pr_number == 42

    def test_missing_number_field_raises(self):
        def opener(req, timeout=15):
            return _json_resp(201, {})

        with pytest.raises(PrOpenError):
            create_pr(
                "https://git-host.example.com", "o", "r",
                token="tok", head="feature", base="main", title="t", body="b",
                opener=opener,
            )

    def test_token_never_in_request_body(self):
        secret = "super-secret-token-value"

        def opener(req, timeout=15):
            assert secret not in req.data.decode("utf-8")
            assert secret not in req.full_url
            return _json_resp(201, {"number": 1})

        create_pr(
            "https://git-host.example.com", "o", "r",
            token=secret, head="feature", base="main", title="t", body="b",
            opener=opener,
        )


class TestUpdatePr:
    def test_success_patches_title_and_body(self):
        def opener(req, timeout=15):
            assert req.get_method() == "PATCH"
            payload = json.loads(req.data.decode("utf-8"))
            assert payload == {"title": "new title", "body": "new body"}
            return _json_resp(200, {})

        update_pr(
            "https://git-host.example.com", "o", "r", 42,
            token="tok", title="new title", body="new body", opener=opener,
        )

    def test_partial_update_only_supplied_fields(self):
        def opener(req, timeout=15):
            payload = json.loads(req.data.decode("utf-8"))
            assert payload == {"title": "new title only"}
            return _json_resp(200, {})

        update_pr("https://git-host.example.com", "o", "r", 42, token="tok", title="new title only", opener=opener)

    def test_failure_raises_pr_open_error(self):
        def opener(req, timeout=15):
            import urllib.error

            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

        with pytest.raises(PrOpenError):
            update_pr("https://git-host.example.com", "o", "r", 42, token="tok", title="t", opener=opener)


class TestGetPrBody:
    """lr-2500b7 append mode: get_pr_body is the GET half of the GET-then-
    PATCH append flow push.verb._run_update_pr composes."""

    def test_returns_existing_body(self):
        def opener(req, timeout=15):
            assert req.get_method() == "GET"
            assert req.full_url.endswith("/api/v1/repos/o/r/pulls/42")
            return _json_resp(200, {"number": 42, "body": "existing PR body text"})

        assert get_pr_body(
            "https://git-host.example.com", "o", "r", 42, token="tok", opener=opener,
        ) == "existing PR body text"

    def test_empty_body_returns_empty_string_not_none(self):
        def opener(req, timeout=15):
            return _json_resp(200, {"number": 42, "body": None})

        result = get_pr_body("https://git-host.example.com", "o", "r", 42, token="tok", opener=opener)
        assert result == ""

    def test_missing_body_field_returns_empty_string(self):
        def opener(req, timeout=15):
            return _json_resp(200, {"number": 42})

        result = get_pr_body("https://git-host.example.com", "o", "r", 42, token="tok", opener=opener)
        assert result == ""

    def test_non_2xx_raises_pr_open_error(self):
        """GET is a read method: git_host_api.request() returns the raw
        non-2xx status rather than raising (see that module's own
        docstring, "GET/HEAD callers receive the response verbatim") --
        get_pr_body's own status check is what must fail closed here, not
        a caught GitHostApiError."""
        def opener(req, timeout=15):
            return _json_resp(404, {"message": "Not Found"})

        with pytest.raises(PrOpenError):
            get_pr_body("https://git-host.example.com", "o", "r", 42, token="tok", opener=opener)

    def test_network_failure_raises_pr_open_error(self):
        def opener(req, timeout=15):
            import urllib.error

            raise urllib.error.URLError("connection refused")

        with pytest.raises(PrOpenError):
            get_pr_body("https://git-host.example.com", "o", "r", 42, token="tok", opener=opener)
