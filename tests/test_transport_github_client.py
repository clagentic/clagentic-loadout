"""test_transport_github_client.py — tests for
clagentic_loadout.transport.github_client (post-Wave-B extraction, lr-e1f9).

This is the shared, redirect-hardened GitHub REST request primitive
review/push/merge's github_backend.py modules all build on (see that
module's own docstring for what stayed local per-verb and why). Each verb's
own test suite (test_review_github_backend.py, test_push_github_backend.py,
test_merge_github_backend.py) proves that THEIR call sites still behave
identically through this shared layer -- these tests instead exercise
request_json() itself, directly, as the shared primitive:
  - Request shaping: method, URL, Authorization/Accept/X-GitHub-Api-Version
    headers, Content-Type only when a payload is given, extra_headers merged
    in (e.g. review's User-Agent).
  - parse_mode="strict" (push/merge): a non-empty body is always JSON-
    parsed; malformed JSON propagates uncaught.
  - parse_mode="content_type" (review): a non-empty body is JSON-parsed only
    when Content-Type contains "json"; otherwise returned as decoded text.
  - Empty body returns {} under both modes.
  - A non-2xx response is returned, never raised, with best-effort body
    parsing (malformed/empty error body tolerated as {}).
  - Redirect hardening: the DEFAULT opener (no `opener` injected) is built
    via `opener_factory` (default: transport.redirect_guard.
    no_redirect_opener) -- never bare urlopen -- and a 3xx surfaces as the
    bare status code with an empty body, never a second request.
  - `opener_factory` injection: a caller-supplied factory is used in place
    of the default when `opener` itself is not supplied, proving the seam
    each verb's own github_backend.py module relies on to keep its own
    `no_redirect_opener` name monkeypatchable at ITS module path.
  - Network-level failures (urllib.error.URLError) propagate uncaught --
    request_json() never swallows or translates them; each verb backend's
    own call site does that translation.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from clagentic_loadout.transport.github_client import (
    GITHUB_API_BASE,
    GITHUB_API_VERSION,
    request_json,
)


class _FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict | None = None):
        self.status = status
        self._body = body
        self.headers = headers if headers is not None else {}

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json_resp(status: int, payload, headers: dict | None = None) -> _FakeResponse:
    return _FakeResponse(status, json.dumps(payload).encode("utf-8"), headers=headers)


def _http_error(url: str, code: int, payload: dict | None = None) -> urllib.error.HTTPError:
    import io

    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(body))


class TestGithubApiBase:
    def test_is_the_real_public_github_host(self):
        assert GITHUB_API_BASE == "https://api.github.com"

    def test_api_version_matches_documented_value(self):
        assert GITHUB_API_VERSION == "2022-11-28"


class TestRequestShaping:
    def test_get_request_carries_auth_accept_and_version_headers(self):
        captured = {}

        def opener(req, timeout=30):
            captured["req"] = req
            return _json_resp(200, {"login": "some-user"})

        status, body = request_json("GET", f"{GITHUB_API_BASE}/user", "tok", opener=opener)
        req = captured["req"]
        assert req.get_method() == "GET"
        assert req.get_header("Authorization") == "token tok"
        assert req.get_header("Accept") == "application/vnd.github+json"
        assert req.get_header("X-github-api-version") == GITHUB_API_VERSION
        assert status == 200
        assert body == {"login": "some-user"}

    def test_payload_none_sends_no_body_and_no_content_type(self):
        captured = {}

        def opener(req, timeout=30):
            captured["req"] = req
            return _json_resp(200, {})

        request_json("GET", f"{GITHUB_API_BASE}/user", "tok", opener=opener)
        req = captured["req"]
        assert req.data is None
        assert req.get_header("Content-type") is None

    def test_payload_present_sets_json_content_type_and_body(self):
        captured = {}

        def opener(req, timeout=30):
            captured["req"] = req
            return _json_resp(201, {"number": 1})

        request_json(
            "POST", f"{GITHUB_API_BASE}/repos/o/r/pulls", "tok",
            {"title": "t"}, opener=opener,
        )
        req = captured["req"]
        assert req.get_header("Content-type") == "application/json"
        assert json.loads(req.data.decode("utf-8")) == {"title": "t"}

    def test_extra_headers_are_merged_in(self):
        captured = {}

        def opener(req, timeout=30):
            captured["req"] = req
            return _json_resp(200, {})

        request_json(
            "GET", f"{GITHUB_API_BASE}/user", "tok", opener=opener,
            extra_headers={"User-Agent": "clagentic-loadout-review/1.0"},
        )
        assert captured["req"].get_header("User-agent") == "clagentic-loadout-review/1.0"

    def test_custom_accept_header_is_honored(self):
        captured = {}

        def opener(req, timeout=30):
            captured["req"] = req
            return _json_resp(200, {})

        request_json(
            "GET", f"{GITHUB_API_BASE}/user", "tok", opener=opener,
            accept="application/vnd.github.raw",
        )
        assert captured["req"].get_header("Accept") == "application/vnd.github.raw"

    def test_token_never_in_url_or_body(self):
        secret = "super-secret-token-value"

        def opener(req, timeout=30):
            assert secret not in req.full_url
            if req.data:
                assert secret not in req.data.decode("utf-8")
            return _json_resp(201, {"number": 1})

        request_json(
            "POST", f"{GITHUB_API_BASE}/repos/o/r/pulls", secret,
            {"title": "t"}, opener=opener,
        )


class TestParseModeStrict:
    """push/merge's parse rule: a non-empty body is always JSON-parsed."""

    def test_non_empty_body_always_json_parsed(self):
        def opener(req, timeout=30):
            return _json_resp(200, [{"filename": "a.py"}])

        status, body = request_json(
            "GET", f"{GITHUB_API_BASE}/repos/o/r/pulls/1/files", "tok",
            opener=opener, parse_mode="strict",
        )
        assert status == 200
        assert body == [{"filename": "a.py"}]

    def test_empty_body_returns_empty_dict(self):
        def opener(req, timeout=30):
            return _FakeResponse(200, b"")

        status, body = request_json(
            "GET", f"{GITHUB_API_BASE}/user", "tok", opener=opener, parse_mode="strict",
        )
        assert status == 200
        assert body == {}

    def test_malformed_json_propagates_uncaught(self):
        def opener(req, timeout=30):
            return _FakeResponse(200, b"not json")

        with pytest.raises(json.JSONDecodeError):
            request_json(
                "GET", f"{GITHUB_API_BASE}/user", "tok", opener=opener, parse_mode="strict",
            )

    def test_content_type_is_ignored_in_strict_mode(self):
        # No Content-Type header at all (matching push/merge's original
        # fake-response shape, which never set .headers) still parses.
        def opener(req, timeout=30):
            return _FakeResponse(200, b'{"merged": true}')

        status, body = request_json(
            "PUT", f"{GITHUB_API_BASE}/repos/o/r/pulls/1/merge", "tok",
            {"merge_method": "merge"}, opener=opener, parse_mode="strict",
        )
        assert body == {"merged": True}


class TestParseModeContentType:
    """review's parse rule: JSON-parse only when Content-Type contains
    'json'; otherwise return decoded text."""

    def test_json_content_type_is_parsed(self):
        def opener(req, timeout=30):
            return _json_resp(
                200, {"login": "some-user"}, headers={"Content-Type": "application/json"}
            )

        status, body = request_json(
            "GET", f"{GITHUB_API_BASE}/user", "tok", opener=opener, parse_mode="content_type",
        )
        assert body == {"login": "some-user"}

    def test_non_json_content_type_returns_decoded_text(self):
        def opener(req, timeout=30):
            return _FakeResponse(200, b"plain text body", headers={"Content-Type": "text/plain"})

        status, body = request_json(
            "GET", f"{GITHUB_API_BASE}/user", "tok", opener=opener, parse_mode="content_type",
        )
        assert body == "plain text body"

    def test_missing_headers_attr_is_treated_as_non_json(self):
        class _NoHeadersResponse:
            def read(self):
                return b'{"login": "x"}'

            def getcode(self):
                return 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(req, timeout=30):
            return _NoHeadersResponse()

        status, body = request_json(
            "GET", f"{GITHUB_API_BASE}/user", "tok", opener=opener, parse_mode="content_type",
        )
        assert body == '{"login": "x"}'

    def test_empty_body_returns_empty_dict_regardless_of_content_type(self):
        def opener(req, timeout=30):
            return _FakeResponse(200, b"", headers={"Content-Type": "application/json"})

        status, body = request_json(
            "GET", f"{GITHUB_API_BASE}/user", "tok", opener=opener, parse_mode="content_type",
        )
        assert body == {}


class TestNon2xxResponses:
    def test_http_error_returns_status_and_parsed_body_never_raises(self):
        def opener(req, timeout=30):
            raise _http_error(req.full_url, 422, {"message": "Unprocessable"})

        status, body = request_json("POST", f"{GITHUB_API_BASE}/repos/o/r/pulls", "tok", {}, opener=opener)
        assert status == 422
        assert body == {"message": "Unprocessable"}

    def test_http_error_with_empty_body_returns_empty_dict(self):
        def opener(req, timeout=30):
            raise _http_error(req.full_url, 404)

        status, body = request_json("GET", f"{GITHUB_API_BASE}/user", "tok", opener=opener)
        assert status == 404
        assert body == {}

    def test_http_error_with_malformed_body_returns_empty_dict(self):
        import io

        def opener(req, timeout=30):
            raise urllib.error.HTTPError(
                req.full_url, 500, "err", {}, io.BytesIO(b"not json")
            )

        status, body = request_json("GET", f"{GITHUB_API_BASE}/user", "tok", opener=opener)
        assert status == 500
        assert body == {}


class TestRedirectHardening:
    def test_default_opener_is_built_via_opener_factory(self):
        captured = {}

        class _FakeOpenerDirector:
            def open(self, req, timeout=30):
                captured["req"] = req
                return _json_resp(200, {"login": "x"})

        def fake_factory():
            captured["factory_called"] = True
            return _FakeOpenerDirector()

        status, body = request_json(
            "GET", f"{GITHUB_API_BASE}/user", "tok", opener_factory=fake_factory,
        )
        assert status == 200
        assert captured.get("factory_called") is True

    def test_real_default_factory_is_no_redirect_opener(self):
        """No opener_factory override, no `opener` injected -- proves the
        module-level default IS transport.redirect_guard.no_redirect_opener
        itself (the same hardened builder every pre-extraction GitHub
        backend used), by identity, not just behavior."""
        import inspect

        from clagentic_loadout.transport.redirect_guard import no_redirect_opener

        default = inspect.signature(request_json).parameters["opener_factory"].default
        assert default is no_redirect_opener

    def test_3xx_surfaces_as_status_with_empty_body_never_raises(self):
        request_log = []

        def opener(req, timeout=30):
            request_log.append(req.full_url)
            raise urllib.error.HTTPError(
                req.full_url, 302, "Found",
                {"Location": "http://attacker.example.net/collect"}, None,
            )

        status, body = request_json("GET", f"{GITHUB_API_BASE}/user", "tok", opener=opener)
        assert status == 302
        assert body == {}
        assert len(request_log) == 1  # never a second request to the redirect target


class TestNetworkErrorsPropagate:
    def test_urlerror_propagates_uncaught(self):
        def opener(req, timeout=30):
            raise urllib.error.URLError("connection refused")

        with pytest.raises(urllib.error.URLError):
            request_json("GET", f"{GITHUB_API_BASE}/user", "tok", opener=opener)

    def test_timeout_error_propagates_uncaught(self):
        def opener(req, timeout=30):
            raise TimeoutError("timed out")

        with pytest.raises(TimeoutError):
            request_json("GET", f"{GITHUB_API_BASE}/user", "tok", opener=opener)
