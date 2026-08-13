"""test_transport_git_host_api.py — tests for
clagentic_loadout.transport.git_host_api (lr-3ba8, Wave B slice 1; module
renamed from transport.forge_api, lr-9ade folded into lr-39f8; internal
identifiers completed lr-9fdbed).

Coverage:
  - validate_body_stdin_content: empty / non-JSON / non-object / missing-body
    all raise GitHostApiError(EXIT_BODY_STDIN_EMPTY); a well-formed body passes.
  - build_request / request: authenticated GET and write-with-body, via an
    injected opener -- NO real network call anywhere in this file.
    Content-Type is defaulted for a JSON body and left alone when the
    caller already supplied one.
  - --verify-comment gate: mandatory on a comments POST (missing flag ->
    EXIT_VERIFY_COMMENT_REQUIRED); success path (fresh, matching, own-bot
    comment); failure paths (wrong PR / wrong SHA / no matching comment /
    stale-but-matching comment).
  - credential-provider seam: an injected mock TokenProvider is used; an
    inherited env var has no effect on the resolved token.
  - Exit-code coverage for every EXIT_* constant reachable via main().
  - No wall-clock dependence: verify_comment_on_pr is driven by explicit
    not_before / created_at values, never datetime.now() inside the
    assertions themselves (main()'s own not_before capture is exercised
    only through the injected opener, never asserted against real time).
  - Redirect hardening (bobbie.sast.7): request() must never follow a 3xx
    -- the live git-host bearer token lives in the Authorization header, and
    urllib's default redirect handler would replay it to whatever host a
    Location names. Covers: exactly one request is ever made (no second
    request to the redirect target), the Authorization header is asserted
    absent from any redirect-target request, and a redirect surfaces as
    EXIT_CURL_FAILED rather than a silent success.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from clagentic_loadout.transport import git_host_api
from clagentic_loadout.transport import provider_config
from clagentic_loadout.transport.credential_provider import CredentialProviderError


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    """Belt-and-suspenders isolation (lr-396f, same pattern as lr-a7c2's
    fixture in test_transport_provider_config.py): _resolve_git_host_base's
    config-file tier reads through provider_config.load_user_config_section,
    which falls back to provider_config.DEFAULT_USER_CONFIG_ROOT --
    the REAL ~/.config/clagentic/loadout/ directory -- for any call that
    omits config_root. On a host with a real deployment config.yaml (e.g.
    one provisioned with a `git_host: base_url` key, lr-e570), that read
    leaks a live value into tests asserting the localhost placeholder or an
    env-only precedence outcome (confirmed: TestResolveGitHostBase's
    test_localhost_placeholder_when_everything_unset and
    test_configured_alias_name_ignores_default_alias_var both fail when a
    real config.yaml is present, verified via lore task lr-396f / PRs
    #41-#43). Every call site in this file that cares about the config-file
    tier already pins config_root=tmp_path explicitly; this autouse fixture
    is a conformance backstop (CLAUDE.md rule 6a) so a future test that
    forgets to pin it still resolves to an empty, per-test tmp directory
    rather than real host state, on top of (not instead of) the explicit
    pins."""
    isolated_root = tmp_path / "isolated-user-config-root"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", isolated_root)


# ---------------------------------------------------------------------------
# parse_json_body (post-Wave-B extraction, lr-e1f9) — the shared tolerant
# raw-bytes-to-dict parse push.forgejo_backend and merge.forgejo_backend's
# write-path callers need, previously duplicated locally in both modules.
# ---------------------------------------------------------------------------


class TestParseJsonBody:
    def test_well_formed_object_parses(self):
        assert git_host_api.parse_json_body(b'{"number": 42}') == {"number": 42}

    def test_empty_bytes_returns_empty_dict(self):
        assert git_host_api.parse_json_body(b"") == {}

    def test_invalid_json_returns_empty_dict_not_raise(self):
        assert git_host_api.parse_json_body(b"not json") == {}

    def test_non_utf8_bytes_returns_empty_dict_not_raise(self):
        assert git_host_api.parse_json_body(b"\xff\xfe\x00\x01") == {}

    def test_json_list_returns_empty_dict(self):
        # Every caller of this helper expects a dict-shaped response; a
        # list-shaped body (a different endpoint's shape) is never silently
        # treated as a usable dict.
        assert git_host_api.parse_json_body(b"[1, 2, 3]") == {}

    def test_json_scalar_returns_empty_dict(self):
        assert git_host_api.parse_json_body(b'"just a string"') == {}


# ---------------------------------------------------------------------------
# validate_body_stdin_content
# ---------------------------------------------------------------------------


class TestValidateBodyStdinContent:
    def test_well_formed_body_passes(self):
        git_host_api.validate_body_stdin_content(b'{"body": "hello"}')  # no raise

    def test_empty_bytes_rejected(self):
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.validate_body_stdin_content(b"")
        assert exc_info.value.code == git_host_api.EXIT_BODY_STDIN_EMPTY

    def test_non_json_rejected(self):
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.validate_body_stdin_content(b"not json at all")
        assert exc_info.value.code == git_host_api.EXIT_BODY_STDIN_EMPTY

    def test_non_object_json_rejected(self):
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.validate_body_stdin_content(b"[1, 2, 3]")
        assert exc_info.value.code == git_host_api.EXIT_BODY_STDIN_EMPTY

    def test_missing_body_key_rejected(self):
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.validate_body_stdin_content(b'{"not_body": "x"}')
        assert exc_info.value.code == git_host_api.EXIT_BODY_STDIN_EMPTY

    def test_empty_body_string_rejected(self):
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.validate_body_stdin_content(b'{"body": "   "}')
        assert exc_info.value.code == git_host_api.EXIT_BODY_STDIN_EMPTY

    def test_non_string_body_rejected(self):
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.validate_body_stdin_content(b'{"body": 123}')
        assert exc_info.value.code == git_host_api.EXIT_BODY_STDIN_EMPTY


# ---------------------------------------------------------------------------
# _SAFE_CALLER_RE anchoring (lr-3e3318)
# ---------------------------------------------------------------------------


class TestSafeCallerRegexTrailingNewlineAnchoring:
    """lr-3e3318 (BOBBIE, PR #133 audit): _SAFE_CALLER_RE previously
    anchored with a bare '$', which in Python (without re.MULTILINE) matches
    at end-of-string OR just before a trailing newline -- so 'caller\\n'
    passed validation. Re-anchored with \\A...\\Z; this locks the REJECT
    behavior directly against the compiled pattern, the same seam
    push/review/merge's --caller ultimately flows through."""

    def test_trailing_newline_rejected(self):
        assert git_host_api._SAFE_CALLER_RE.match("some-role\n") is None

    def test_leading_newline_rejected(self):
        assert git_host_api._SAFE_CALLER_RE.match("\nsome-role") is None

    def test_plain_valid_caller_still_accepted(self):
        assert git_host_api._SAFE_CALLER_RE.match("some-role") is not None


# ---------------------------------------------------------------------------
# build_request / request -- no real network, opener injected
# ---------------------------------------------------------------------------


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


class TestBuildRequest:
    def test_content_type_defaulted_for_json_body(self):
        req = git_host_api.build_request(
            "http://git-host.example.com", "POST", "/api/v1/x", "tok",
            body_bytes=b'{"body":"hi"}',
        )
        assert req.get_header("Content-type") == "application/json"
        assert req.get_header("Authorization") == "token tok"

    def test_content_type_not_overridden_when_caller_supplied(self):
        req = git_host_api.build_request(
            "http://git-host.example.com", "POST", "/api/v1/x", "tok",
            body_bytes=b"<xml/>",
            extra_headers={"Content-Type": "application/xml"},
        )
        assert req.get_header("Content-type") == "application/xml"

    def test_no_content_type_for_get(self):
        req = git_host_api.build_request(
            "http://git-host.example.com", "GET", "/api/v1/x", "tok",
        )
        assert req.get_header("Content-type") is None

    def test_token_never_in_url(self):
        req = git_host_api.build_request(
            "http://git-host.example.com", "GET", "/api/v1/x", "super-secret-tok",
        )
        assert "super-secret-tok" not in req.full_url


class TestRequest:
    def test_authenticated_get(self):
        captured = {}

        def fake_opener(req, timeout=15):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            return _FakeResponse(200, b'{"login": "some-bot"}')

        status, body = git_host_api.request(
            "http://git-host.example.com", "GET", "/api/v1/user", "tok-1",
            opener=fake_opener,
        )
        assert status == 200
        assert json.loads(body) == {"login": "some-bot"}
        assert captured["url"] == "http://git-host.example.com/api/v1/user"
        assert captured["headers"]["Authorization"] == "token tok-1"

    def test_write_with_stdin_body(self):
        captured = {}

        def fake_opener(req, timeout=15):
            captured["data"] = req.data
            captured["method"] = req.get_method()
            return _FakeResponse(200, b'{"id": 5}')

        status, body = git_host_api.request(
            "http://git-host.example.com",
            "POST",
            "/api/v1/repos/some-owner/some-repo/issues/1/comments",
            "tok-1",
            body_bytes=b'{"body":"hello"}',
            opener=fake_opener,
        )
        assert status == 200
        assert captured["data"] == b'{"body":"hello"}'
        assert captured["method"] == "POST"

    def test_write_method_non_2xx_raises_curl_failed(self):
        def fake_opener(req, timeout=15):
            raise urllib.error.HTTPError(
                req.full_url, 422, "Unprocessable", {}, io.BytesIO(b'{"message":"[Body]: Required"}')
            )

        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.request(
                "http://git-host.example.com", "POST", "/api/v1/repos/o/r/issues/1/comments",
                "tok-1", body_bytes=b'{"body":"x"}', opener=fake_opener,
            )
        assert exc_info.value.code == git_host_api.EXIT_CURL_FAILED

    def test_get_method_non_2xx_does_not_raise(self):
        """GET/HEAD callers get the response verbatim (incl. non-200) so they
        can parse diffs/files themselves -- only write methods fail closed."""

        def fake_opener(req, timeout=15):
            raise urllib.error.HTTPError(
                req.full_url, 404, "Not Found", {}, io.BytesIO(b"{}")
            )

        status, body = git_host_api.request(
            "http://git-host.example.com", "GET", "/api/v1/repos/o/r", "tok-1",
            opener=fake_opener,
        )
        assert status == 404

    def test_network_error_raises_curl_failed(self):
        def fake_opener(req, timeout=15):
            raise urllib.error.URLError("connection refused")

        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.request(
                "http://git-host.example.com", "GET", "/api/v1/user", "tok-1",
                opener=fake_opener,
            )
        assert exc_info.value.code == git_host_api.EXIT_CURL_FAILED


class TestRedirectHardening:
    """bobbie.sast.7: request() carries the live git-host bearer token in its
    Authorization header on every call. A 3xx must never be followed --
    urllib's default redirect handler would replay that header to whatever
    host the Location points at."""

    def test_default_opener_never_follows_redirects(self, monkeypatch):
        """No opener injected: request() must build its own no-redirect
        opener internally rather than falling back to
        urllib.request.urlopen's default redirect-following behavior.
        Verified by asserting the opener actually constructed uses
        _NoRedirectHandler, without performing any real network call."""
        captured = {}

        class _FakeOpenerDirector:
            def open(self, req, timeout=15):
                captured["req"] = req
                return _FakeResponse(200, b'{"ok": true}')

        def fake_build_opener(*handlers):
            captured["handlers"] = handlers
            return _FakeOpenerDirector()

        monkeypatch.setattr(git_host_api.urllib.request, "build_opener", fake_build_opener)

        status, _body = git_host_api.request(
            "http://git-host.example.com", "GET", "/api/v1/user", "tok-1",
        )
        assert status == 200
        assert git_host_api._NoRedirectHandler in captured["handlers"]

    def test_redirect_response_fails_closed_write_method(self):
        """A 3xx from a write-method call surfaces as EXIT_CURL_FAILED, not
        a silent success -- and no second request is ever made to the
        redirect target."""
        request_log = []

        def fake_opener(req, timeout=15):
            # Models a no-redirect opener's contract directly: it raises
            # HTTPError for the 3xx itself rather than following it, so this
            # fake never dispatches a second request to Location.
            request_log.append((req.full_url, dict(req.header_items())))
            raise urllib.error.HTTPError(
                req.full_url, 302, "Found",
                {"Location": "http://attacker.example.net/collect"},
                io.BytesIO(b""),
            )

        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.request(
                "http://git-host.example.com",
                "POST",
                "/api/v1/repos/o/r/issues/1/comments",
                "super-secret-tok",
                body_bytes=b'{"body":"x"}',
                opener=fake_opener,
            )
        assert exc_info.value.code == git_host_api.EXIT_CURL_FAILED
        # Exactly one request was made, and only to the originally
        # configured host -- the Location target was never contacted.
        assert len(request_log) == 1
        assert request_log[0][0] == "http://git-host.example.com/api/v1/repos/o/r/issues/1/comments"

    def test_redirect_response_fails_closed_get_method(self):
        """A 3xx from a GET call must ALSO fail closed -- GET's normal
        pass-non-2xx-through-to-the-caller contract does not extend to a
        redirect, which must never be treated as a completed call whose
        body a caller goes on to parse."""

        def fake_opener(req, timeout=15):
            raise urllib.error.HTTPError(
                req.full_url, 301, "Moved Permanently",
                {"Location": "http://attacker.example.net/collect"},
                io.BytesIO(b""),
            )

        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.request(
                "http://git-host.example.com", "GET", "/api/v1/repos/o/r", "tok-1",
                opener=fake_opener,
            )
        assert exc_info.value.code == git_host_api.EXIT_CURL_FAILED

    def test_authorization_header_never_sent_to_redirect_target(self):
        """Simulates what a REAL urllib redirect-following opener would do
        (re-issue the request against Location, headers intact) and asserts
        that path is never exercised: the fake opener here stands in for
        _NoRedirectHandler's refusal, so only one request -- to the
        ORIGINAL host -- is ever observed, and that request's own
        Authorization header is checked to make sure it's the real token
        only reaching the intended host, never a second request carrying it
        elsewhere."""
        seen_hosts = []

        def fake_opener(req, timeout=15):
            seen_hosts.append(urllib.parse.urlsplit(req.full_url).hostname)
            assert req.get_header("Authorization") == "token super-secret-tok"
            raise urllib.error.HTTPError(
                req.full_url, 302, "Found",
                {"Location": "http://attacker.example.net/collect"},
                io.BytesIO(b""),
            )

        with pytest.raises(git_host_api.GitHostApiError):
            git_host_api.request(
                "http://git-host.example.com", "GET", "/api/v1/user",
                "super-secret-tok", opener=fake_opener,
            )
        assert seen_hosts == ["git-host.example.com"]
        assert "attacker.example.net" not in seen_hosts

    def test_no_redirect_handler_refuses_via_redirect_request(self):
        """Direct unit test of _NoRedirectHandler.redirect_request: it must
        return None (refuse to follow) for every call, regardless of the
        3xx code or Location."""
        handler = git_host_api._NoRedirectHandler()
        result = handler.redirect_request(
            None, None, 302, "Found", {}, "http://attacker.example.net/collect"
        )
        assert result is None


# ---------------------------------------------------------------------------
# verify_comment_on_pr -- freshness-anchored readback
# ---------------------------------------------------------------------------


class TestVerifyCommentOnPr:
    def _opener_with_comments(self, comments):
        def fake_opener(req, timeout=15):
            return _FakeResponse(200, json.dumps(comments).encode("utf-8"))

        return fake_opener

    def test_fresh_matching_comment_confirmed(self):
        not_before = datetime(2026, 1, 1, tzinfo=timezone.utc)
        created = (not_before + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        comments = [
            {"id": 42, "html_url": "http://x/42", "user": {"login": "some-bot"},
             "body": "PEACHES verdict: clean", "created_at": created},
        ]
        result = git_host_api.verify_comment_on_pr(
            "http://git-host.example.com", "tok", "o", "r", "7",
            "PEACHES verdict: clean", "some-bot",
            not_before=not_before, opener=self._opener_with_comments(comments),
        )
        assert result["id"] == 42
        assert result["login"] == "some-bot"

    def test_stale_comment_fails_verify(self):
        not_before = datetime(2026, 1, 1, tzinfo=timezone.utc)
        stale_created = (not_before - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        comments = [
            {"id": 41, "html_url": "http://x/41", "user": {"login": "some-bot"},
             "body": "PEACHES verdict: clean", "created_at": stale_created},
        ]
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.verify_comment_on_pr(
                "http://git-host.example.com", "tok", "o", "r", "7",
                "PEACHES verdict: clean", "some-bot",
                not_before=not_before, opener=self._opener_with_comments(comments),
            )
        assert exc_info.value.code == git_host_api.EXIT_VERIFY_FAILED
        assert "freshness anchor" in str(exc_info.value)

    def test_wrong_login_fails_verify(self):
        not_before = datetime(2026, 1, 1, tzinfo=timezone.utc)
        created = (not_before + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        comments = [
            {"id": 43, "html_url": "http://x/43", "user": {"login": "someone-else"},
             "body": "PEACHES verdict: clean", "created_at": created},
        ]
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.verify_comment_on_pr(
                "http://git-host.example.com", "tok", "o", "r", "7",
                "PEACHES verdict: clean", "some-bot",
                not_before=not_before, opener=self._opener_with_comments(comments),
            )
        assert exc_info.value.code == git_host_api.EXIT_VERIFY_FAILED

    def test_no_matching_body_fails_verify(self):
        not_before = datetime(2026, 1, 1, tzinfo=timezone.utc)
        created = (not_before + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        comments = [
            {"id": 44, "html_url": "http://x/44", "user": {"login": "some-bot"},
             "body": "unrelated comment", "created_at": created},
        ]
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.verify_comment_on_pr(
                "http://git-host.example.com", "tok", "o", "r", "7",
                "PEACHES verdict: clean", "some-bot",
                not_before=not_before, opener=self._opener_with_comments(comments),
            )
        assert exc_info.value.code == git_host_api.EXIT_VERIFY_FAILED

    def test_wrong_pr_readback_empty_list_fails_verify(self):
        """Simulates the comment having landed on a different PR: the
        readback against the EXPECTED pr_number finds nothing."""
        not_before = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.verify_comment_on_pr(
                "http://git-host.example.com", "tok", "o", "r", "7",
                "PEACHES verdict: clean", "some-bot",
                not_before=not_before, opener=self._opener_with_comments([]),
            )
        assert exc_info.value.code == git_host_api.EXIT_VERIFY_FAILED

    def test_clock_skew_tolerance_absorbs_small_drift(self):
        """A comment created a couple seconds BEFORE not_before, within the
        tolerance window, still counts as fresh."""
        not_before = datetime(2026, 1, 1, tzinfo=timezone.utc)
        created = (not_before - timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
        comments = [
            {"id": 45, "html_url": "http://x/45", "user": {"login": "some-bot"},
             "body": "PEACHES verdict: clean", "created_at": created},
        ]
        result = git_host_api.verify_comment_on_pr(
            "http://git-host.example.com", "tok", "o", "r", "7",
            "PEACHES verdict: clean", "some-bot",
            not_before=not_before, opener=self._opener_with_comments(comments),
        )
        assert result["id"] == 45


# ---------------------------------------------------------------------------
# check_pr_sha
# ---------------------------------------------------------------------------


class TestCheckPrSha:
    def test_matching_sha_passes(self, capsys):
        def fake_opener(req, timeout=15):
            return _FakeResponse(200, json.dumps({"head": {"sha": "abc123"}}).encode("utf-8"))

        git_host_api.check_pr_sha(
            "http://git-host.example.com", "tok", "o", "r", "7", "abc123", opener=fake_opener,
        )  # no raise

    def test_mismatched_sha_fails_stale(self):
        def fake_opener(req, timeout=15):
            return _FakeResponse(200, json.dumps({"head": {"sha": "def456"}}).encode("utf-8"))

        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.check_pr_sha(
                "http://git-host.example.com", "tok", "o", "r", "7", "abc123", opener=fake_opener,
            )
        assert exc_info.value.code == git_host_api.EXIT_STALE_PR

    def test_pr_not_found_but_repo_exists_is_stale(self):
        calls = {"n": 0}

        def fake_opener(req, timeout=15):
            calls["n"] += 1
            if "/pulls/" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))
            return _FakeResponse(200, b"{}")

        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.check_pr_sha(
                "http://git-host.example.com", "tok", "o", "r", "7", "abc123", opener=fake_opener,
            )
        assert exc_info.value.code == git_host_api.EXIT_STALE_PR

    def test_repo_not_found_is_owner_repo_not_found(self):
        def fake_opener(req, timeout=15):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))

        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.check_pr_sha(
                "http://git-host.example.com", "tok", "o", "r", "7", "abc123", opener=fake_opener,
            )
        assert exc_info.value.code == git_host_api.EXIT_OWNER_REPO_NOT_FOUND

    def test_known_bad_owner_fast_rejects_before_network(self):
        called = {"n": False}

        def fake_opener(req, timeout=15):
            called["n"] = True
            return _FakeResponse(200, b"{}")

        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.check_pr_sha(
                "http://git-host.example.com", "tok", "bad-owner", "r", "7", "abc123",
                known_bad_owners=frozenset({"bad-owner"}), opener=fake_opener,
            )
        assert exc_info.value.code == git_host_api.EXIT_OWNER_REPO_NOT_FOUND
        assert called["n"] is False


# ---------------------------------------------------------------------------
# main() CLI wiring -- credential provider seam + exit codes
# ---------------------------------------------------------------------------


class _RecordingProvider:
    def __init__(self, token="tok-injected"):
        self.token = token
        self.calls = []

    def resolve_token(self, role: str) -> str:
        self.calls.append(role)
        return self.token


class TestMainGet:
    def test_get_uses_injected_provider_not_env(self, monkeypatch, capsys):
        monkeypatch.setenv("CLAGENTIC_LOADOUT_GIT_HOST_TOKEN", "ambient-should-be-ignored")
        provider = _RecordingProvider("tok-injected")

        def fake_opener(req, timeout=15):
            assert req.get_header("Authorization") == "token tok-injected"
            return _FakeResponse(200, b'{"ok": true}')

        rc = git_host_api.main(
            ["--caller", "some-role", "/api/v1/repos/o/r/pulls/1.diff"],
            token_provider=provider, opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert provider.calls == ["some-role"]
        out = capsys.readouterr().out
        assert '"ok": true' in out

    def test_help_exits_ok_before_parsing(self, capsys):
        rc = git_host_api.main(["--help"])
        assert rc == git_host_api.EXIT_OK
        # lr-396f fold-in: help text's program name tracks the lr-e570
        # rename (loadout-git-host-api), not the pre-rename "forge-api" (an
        # earlier internal log/error-prefix string, itself since renamed to
        # "git-host-api" in lr-9fdbed -- this test only asserts the BINARY
        # name, unaffected either way).
        assert "loadout-git-host-api" in capsys.readouterr().out

    def test_version_reports_current_program_name_not_stale_rename(self, capsys):
        """lr-17d9: the --version program-name string was missed in the
        forge -> git-host rename sweep (lr-e570) and still printed the stale
        'forge-api' binary name. --version must report the CURRENT resolved
        binary name (loadout-git-host-api, see pyproject.toml's
        [project.scripts] entry) -- CLAUDE.md rule 4: "error messages that
        report resolved values (never stale guesses)" applies equally to
        --version's own program name."""
        rc = git_host_api.main(["--version"])
        assert rc == git_host_api.EXIT_OK
        out = capsys.readouterr().out
        assert "loadout-git-host-api" in out
        assert "forge-api" not in out

    def test_missing_path_is_usage_error(self):
        """argparse itself refuses a missing required positional with its own
        usage-error exit code (2) -- main() propagates that verbatim rather
        than translating it to EXIT_USAGE, since argparse never raises into
        the GitHostApiError path for this case."""
        rc = git_host_api.main([])
        assert rc == 2

    def test_credential_provider_failure_is_token_fetch_failed(self):
        class _FailingProvider:
            def resolve_token(self, role: str) -> str:
                raise CredentialProviderError("broker unreachable")

        rc = git_host_api.main(
            ["/api/v1/repos/o/r/pulls/1.diff"], token_provider=_FailingProvider(),
        )
        assert rc == git_host_api.EXIT_TOKEN_FETCH_FAILED

    def test_network_failure_is_curl_failed(self):
        def fake_opener(req, timeout=15):
            raise urllib.error.URLError("connection refused")

        rc = git_host_api.main(
            ["/api/v1/repos/o/r/pulls/1.diff"],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_CURL_FAILED


class TestMainCommentsPostRequiresVerify:
    def test_comments_post_without_verify_comment_is_required_error(self):
        rc = git_host_api.main(
            ["POST", "/api/v1/repos/o/r/issues/5/comments"],
            token_provider=_RecordingProvider(),
        )
        assert rc == git_host_api.EXIT_VERIFY_COMMENT_REQUIRED

    def test_comments_post_without_verify_never_touches_network(self):
        called = {"n": False}

        def fake_opener(req, timeout=15):
            called["n"] = True
            return _FakeResponse(200, b"{}")

        git_host_api.main(
            ["POST", "/api/v1/repos/o/r/issues/5/comments"],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert called["n"] is False


class TestMainVerifyCommentEndToEnd:
    def _make_opener(self, *, own_login="some-bot", comment_created_at=None, comment_login=None, comment_body=None):
        """Build a fake opener that answers POST comments, GET /user, and
        GET comments (readback) with a fresh matching comment by default."""
        state = {"posted": False}

        def fake_opener(req, timeout=15):
            url = req.full_url
            if req.get_method() == "POST" and url.endswith("/comments"):
                state["posted"] = True
                return _FakeResponse(200, b'{"id": 999}')
            if url.endswith("/api/v1/user"):
                return _FakeResponse(200, json.dumps({"login": own_login}).encode("utf-8"))
            if url.endswith("/comments"):
                created = comment_created_at or (
                    datetime.now(timezone.utc) + timedelta(seconds=1)
                ).isoformat().replace("+00:00", "Z")
                login = comment_login if comment_login is not None else own_login
                body = comment_body if comment_body is not None else "verified comment body"
                return _FakeResponse(200, json.dumps([
                    {"id": 999, "html_url": "http://x/999", "user": {"login": login},
                     "body": body, "created_at": created},
                ]).encode("utf-8"))
            raise AssertionError(f"unexpected request to {url}")

        return fake_opener

    def _stdin(self, monkeypatch, body: bytes):
        monkeypatch.setattr(
            "sys.stdin", type("S", (), {"buffer": io.BytesIO(body)})()
        )

    def test_verify_comment_success_emits_verified_id(self, monkeypatch, capsys):
        self._stdin(monkeypatch, b'{"body":"verified comment body"}')
        opener = self._make_opener()

        rc = git_host_api.main(
            [
                "--body-stdin", "--verify-comment",
                "POST", "/api/v1/repos/o/r/issues/5/comments",
            ],
            token_provider=_RecordingProvider(), opener=opener,
        )
        assert rc == git_host_api.EXIT_OK
        out = capsys.readouterr().out
        payload = json.loads(out.strip().splitlines()[-1])
        assert payload["verified_comment_id"] == 999
        assert payload["verified_by_login"] == "some-bot"
        assert payload["pr_number"] == 5

    def test_verify_comment_wrong_login_fails(self, monkeypatch):
        self._stdin(monkeypatch, b'{"body":"verified comment body"}')
        opener = self._make_opener(comment_login="different-bot")

        rc = git_host_api.main(
            [
                "--body-stdin", "--verify-comment",
                "POST", "/api/v1/repos/o/r/issues/5/comments",
            ],
            token_provider=_RecordingProvider(), opener=opener,
        )
        assert rc == git_host_api.EXIT_VERIFY_FAILED

    def test_verify_comment_wrong_sha_fails_before_post(self, monkeypatch):
        self._stdin(monkeypatch, b'{"body":"verified comment body"}')

        def fake_opener(req, timeout=15):
            if "/pulls/" in req.full_url:
                return _FakeResponse(200, json.dumps({"head": {"sha": "current-sha"}}).encode("utf-8"))
            raise AssertionError(f"POST/readback should never fire on stale SHA: {req.full_url}")

        rc = git_host_api.main(
            [
                "--body-stdin", "--verify-comment", "--pr-sha", "stale-sha",
                "POST", "/api/v1/repos/o/r/issues/5/comments",
            ],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_STALE_PR

    def test_verify_comment_missing_readback_match_fails(self, monkeypatch):
        self._stdin(monkeypatch, b'{"body":"verified comment body"}')
        opener = self._make_opener(comment_body="totally different text")

        rc = git_host_api.main(
            [
                "--body-stdin", "--verify-comment",
                "POST", "/api/v1/repos/o/r/issues/5/comments",
            ],
            token_provider=_RecordingProvider(), opener=opener,
        )
        assert rc == git_host_api.EXIT_VERIFY_FAILED

    def test_empty_stdin_body_rejected_before_post(self, monkeypatch):
        self._stdin(monkeypatch, b"")
        called = {"n": False}

        def fake_opener(req, timeout=15):
            called["n"] = True
            return _FakeResponse(200, b"{}")

        rc = git_host_api.main(
            [
                "--body-stdin", "--verify-comment",
                "POST", "/api/v1/repos/o/r/issues/5/comments",
            ],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_BODY_STDIN_EMPTY
        assert called["n"] is False


# ---------------------------------------------------------------------------
# repo context (lr-ea28) -- derived from the API path via _REPOS_PATH_RE
# ---------------------------------------------------------------------------


class _RepoRecordingProvider:
    """TokenProvider recording (role, repo) so a test can assert git_host_api
    derives repo from the request path (lr-ea28)."""

    def __init__(self, token: str = "tok-injected"):
        self.token = token
        self.calls: list[tuple] = []

    def resolve_token(self, role: str, *, repo: str | None = None) -> str:
        self.calls.append((role, repo))
        return self.token


class TestMainRepoContext:
    def test_repo_scoped_path_derives_owner_repo(self, capsys):
        provider = _RepoRecordingProvider()

        def fake_opener(req, timeout=15):
            return _FakeResponse(200, b'{"ok": true}')

        rc = git_host_api.main(
            ["--caller", "some-role", "/api/v1/repos/some-owner/some-repo/pulls/1.diff"],
            token_provider=provider, opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert provider.calls == [("some-role", "some-owner/some-repo")]

    def test_non_repo_scoped_path_passes_none(self, capsys):
        provider = _RepoRecordingProvider()

        def fake_opener(req, timeout=15):
            return _FakeResponse(200, b'{"login": "some-bot"}')

        rc = git_host_api.main(
            ["--caller", "some-role", "/api/v1/user"],
            token_provider=provider, opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert provider.calls == [("some-role", None)]

    def test_repo_scoped_write_path_derives_owner_repo(self, monkeypatch):
        provider = _RepoRecordingProvider()
        monkeypatch.setattr(
            "sys.stdin", type("S", (), {"buffer": io.BytesIO(b'{"body":"x"}')})()
        )

        def fake_opener(req, timeout=15):
            url = req.full_url
            if req.get_method() == "POST" and url.endswith("/comments"):
                return _FakeResponse(200, b'{"id": 1}')
            if url.endswith("/api/v1/user"):
                return _FakeResponse(200, b'{"login": "some-bot"}')
            return _FakeResponse(
                200,
                json.dumps([
                    {"id": 1, "html_url": "http://x/1", "user": {"login": "some-bot"},
                     "body": "x", "created_at": "2099-01-01T00:00:01Z"},
                ]).encode("utf-8"),
            )

        rc = git_host_api.main(
            [
                "--body-stdin", "--verify-comment",
                "POST", "/api/v1/repos/some-owner/some-repo/issues/5/comments",
            ],
            token_provider=provider, opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert provider.calls == [(git_host_api.DEFAULT_ROLE, "some-owner/some-repo")]


# ---------------------------------------------------------------------------
# _resolve_git_host_base -- branded var / compat-alias / config-file /
# localhost placeholder precedence (lr-87bb, config-file tier added
# lr-e570; the deprecated pre-rename FORGE_BASE_URL env-var fallback tier
# removed lr-9fdbed -- scorched earth). explicit
# --git-host-base-url always wins; the branded
# CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL var is PRIMARY once no explicit flag
# is given; the compat-alias env var (name itself configurable, default
# FORGEJO_BASE_URL) is a fallback consulted ONLY when the branded var is
# unset; the user-level config.yaml `git_host: base_url` key is consulted next; the
# localhost placeholder is the final fallback when nothing else is set.
# review.verb and merge.verb both import this same function rather than
# forking their own copy -- see those modules' own test files for their
# call-site coverage.
# ---------------------------------------------------------------------------


class TestResolveGitHostBase:
    def test_explicit_flag_wins_over_everything(self):
        env = {
            git_host_api.GIT_HOST_BASE_URL_ENV_VAR: "http://branded.example:3000",
            git_host_api.DEFAULT_GIT_HOST_BASE_URL_COMPAT_ALIAS: "http://alias.example:3000",
        }
        result = git_host_api._resolve_git_host_base("http://explicit.example:3000", env=env)
        assert result == "http://explicit.example:3000"

    def test_explicit_flag_strips_trailing_slash(self):
        result = git_host_api._resolve_git_host_base("http://explicit.example:3000/", env={})
        assert result == "http://explicit.example:3000"

    def test_branded_var_wins_over_alias(self):
        env = {
            git_host_api.GIT_HOST_BASE_URL_ENV_VAR: "http://branded.example:3000",
            git_host_api.DEFAULT_GIT_HOST_BASE_URL_COMPAT_ALIAS: "http://alias.example:3000",
        }
        result = git_host_api._resolve_git_host_base(None, env=env)
        assert result == "http://branded.example:3000"

    def test_alias_used_when_branded_unset(self):
        env = {git_host_api.DEFAULT_GIT_HOST_BASE_URL_COMPAT_ALIAS: "http://alias.example:3000"}
        result = git_host_api._resolve_git_host_base(None, env=env)
        assert result == "http://alias.example:3000"

    def test_alias_used_when_branded_empty_string(self):
        # An explicitly-empty branded var is treated the same as unset --
        # empty string is falsy, so the alias tier still fires.
        env = {
            git_host_api.GIT_HOST_BASE_URL_ENV_VAR: "",
            git_host_api.DEFAULT_GIT_HOST_BASE_URL_COMPAT_ALIAS: "http://alias.example:3000",
        }
        result = git_host_api._resolve_git_host_base(None, env=env)
        assert result == "http://alias.example:3000"

    def test_config_file_used_when_all_env_tiers_unset(self, tmp_path):
        config_root = tmp_path / "config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "forgejo:\n  base_url: http://config-file.example:3000\n"
        )
        result = git_host_api._resolve_git_host_base(None, env={}, config_root=config_root)
        assert result == "http://config-file.example:3000"

    def test_config_file_absent_falls_through_to_placeholder(self, tmp_path):
        config_root = tmp_path / "config-root"
        config_root.mkdir()
        result = git_host_api._resolve_git_host_base(None, env={}, config_root=config_root)
        assert result == git_host_api.DEFAULT_GIT_HOST_BASE_URL

    def test_env_alias_wins_over_config_file(self, tmp_path):
        config_root = tmp_path / "config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "forgejo:\n  base_url: http://config-file.example:3000\n"
        )
        env = {git_host_api.DEFAULT_GIT_HOST_BASE_URL_COMPAT_ALIAS: "http://alias.example:3000"}
        result = git_host_api._resolve_git_host_base(None, env=env, config_root=config_root)
        assert result == "http://alias.example:3000"

    def test_config_file_value_strips_trailing_slash(self, tmp_path):
        config_root = tmp_path / "config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "forgejo:\n  base_url: http://config-file.example:3000/\n"
        )
        result = git_host_api._resolve_git_host_base(None, env={}, config_root=config_root)
        assert result == "http://config-file.example:3000"

    def test_legacy_git_host_section_used_when_forgejo_section_absent(self, tmp_path):
        """Compat shim (lr-08b451): an existing install that has only ever
        seeded the pre-rename `git_host:` section keeps resolving from it
        when the new `forgejo:` section carries no value."""
        config_root = tmp_path / "config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "git_host:\n  base_url: http://legacy-config.example:3000\n"
        )
        result = git_host_api._resolve_git_host_base(None, env={}, config_root=config_root)
        assert result == "http://legacy-config.example:3000"

    def test_new_forgejo_section_wins_over_legacy_git_host_section(self, tmp_path):
        """When both section names carry a value, the NEW name wins."""
        config_root = tmp_path / "config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "forgejo:\n  base_url: http://new-config.example:3000\n"
            "git_host:\n  base_url: http://legacy-config.example:3000\n"
        )
        result = git_host_api._resolve_git_host_base(None, env={}, config_root=config_root)
        assert result == "http://new-config.example:3000"

    def test_localhost_placeholder_when_everything_unset(self, tmp_path):
        config_root = tmp_path / "config-root"
        config_root.mkdir()
        result = git_host_api._resolve_git_host_base(None, env={}, config_root=config_root)
        assert result == git_host_api.DEFAULT_GIT_HOST_BASE_URL

    def test_alias_name_is_configurable(self):
        # A deployment can repoint the alias NAME itself via
        # GIT_HOST_BASE_URL_COMPAT_ALIAS_NAME_ENV_VAR -- the default alias
        # name (FORGEJO_BASE_URL) is not the only possibility.
        env = {
            git_host_api.GIT_HOST_BASE_URL_COMPAT_ALIAS_NAME_ENV_VAR: "SOME_OTHER_LEGACY_VAR",
            "SOME_OTHER_LEGACY_VAR": "http://legacy.example:3000",
        }
        result = git_host_api._resolve_git_host_base(None, env=env)
        assert result == "http://legacy.example:3000"

    def test_configured_alias_name_ignores_default_alias_var(self, tmp_path):
        # When the alias NAME is overridden, the default alias var name
        # (FORGEJO_BASE_URL) is no longer consulted -- only the configured
        # name is read.
        config_root = tmp_path / "config-root"
        config_root.mkdir()
        env = {
            git_host_api.GIT_HOST_BASE_URL_COMPAT_ALIAS_NAME_ENV_VAR: "SOME_OTHER_LEGACY_VAR",
            git_host_api.DEFAULT_GIT_HOST_BASE_URL_COMPAT_ALIAS: "http://should-not-be-used.example:3000",
        }
        result = git_host_api._resolve_git_host_base(None, env=env, config_root=config_root)
        assert result == git_host_api.DEFAULT_GIT_HOST_BASE_URL

    def test_alias_value_strips_trailing_slash(self):
        env = {git_host_api.DEFAULT_GIT_HOST_BASE_URL_COMPAT_ALIAS: "http://alias.example:3000/"}
        result = git_host_api._resolve_git_host_base(None, env=env)
        assert result == "http://alias.example:3000"

    def test_default_alias_name_is_forgejo_base_url(self):
        # Locks in the specific default name MILLER's diagnosis named as the
        # pre-existing back-compat tool's var (scripts/forgejo-curl) -- a
        # regression here would silently break the lr-87bb fix's whole
        # purpose even though the mechanism still "worked" generically.
        assert git_host_api.DEFAULT_GIT_HOST_BASE_URL_COMPAT_ALIAS == "FORGEJO_BASE_URL"


# ---------------------------------------------------------------------------
# GitHub-target routing (lr-104a) -- the shared read transport is
# git-host-aware: an absolute GitHub URL PATH uses the GitHub reader token
# and is issued with NO git-host base prepended; a relative Forgejo path
# keeps the pre-existing base-prepend behavior unchanged.
# ---------------------------------------------------------------------------


class TestIsGithubTarget:
    def test_absolute_github_url_is_github_target(self):
        assert git_host_api._is_github_target(
            "https://api.github.com/repos/some-owner/some-repo/pulls/318/reviews"
        )

    def test_absolute_http_github_url_is_github_target(self):
        # scheme is http, not https -- still an absolute URL whose host
        # matches, so it is still routed as a GitHub target.
        assert git_host_api._is_github_target("http://api.github.com/repos/o/r/pulls/1")

    def test_relative_forgejo_path_is_not_github_target(self):
        assert not git_host_api._is_github_target("/api/v1/repos/some-owner/some-repo/pulls/42.diff")

    def test_absolute_forgejo_url_is_not_github_target(self):
        # An absolute URL pointed at a non-GitHub host (e.g. a caller who
        # passed a full git-host URL by habit) is not a GitHub target either
        # -- only the configured GitHub hostname routes differently.
        assert not git_host_api._is_github_target("http://git-host.example.com:3000/api/v1/repos/o/r")

    def test_github_hostname_substring_in_path_is_not_a_match(self):
        # The literal text "github.com" appearing somewhere in a RELATIVE
        # path's query string/etc must never be mistaken for a host match --
        # only the URL's own host component is inspected (urlsplit, not a
        # raw substring test).
        assert not git_host_api._is_github_target(
            "/api/v1/repos/o/r/issues?q=mentions-github.com-in-title"
        )

    def test_custom_github_hostname_override(self):
        # A GitHub Enterprise Server deployment names a different hostname;
        # the default 'github.com' host is then NOT a GitHub target under
        # that override.
        assert git_host_api._is_github_target(
            "https://api.ghe.example.com/repos/o/r/pulls/1",
            github_hostname="ghe.example.com",
        )
        assert not git_host_api._is_github_target(
            "https://api.github.com/repos/o/r/pulls/1",
            github_hostname="ghe.example.com",
        )

    def test_non_http_scheme_is_not_github_target(self):
        assert not git_host_api._is_github_target("ftp://api.github.com/repos/o/r")


class TestAbsoluteUrlHostMatchesGitHostBase:
    """_absolute_url_host_matches_git_host_base (lr-69af67): the host-anchor
    check on the Forgejo absolute-URL branch, mirroring _is_github_target's
    role for the GitHub branch."""

    def test_same_host_and_port_matches(self):
        assert git_host_api._absolute_url_host_matches_git_host_base(
            "http://forgejo.example.com:3000/api/v1/repos/o/r/pulls/1",
            "http://forgejo.example.com:3000",
        )

    def test_different_host_does_not_match(self):
        assert not git_host_api._absolute_url_host_matches_git_host_base(
            "http://attacker.example.net:3000/api/v1/repos/o/r/pulls/1",
            "http://forgejo.example.com:3000",
        )

    def test_same_host_different_port_does_not_match(self):
        # A host-only match is not enough -- a same-hostname, differing-port
        # absolute URL is exactly the shape a misconfigured reverse proxy or
        # copy/paste error would produce.
        assert not git_host_api._absolute_url_host_matches_git_host_base(
            "http://forgejo.example.com:9999/api/v1/repos/o/r/pulls/1",
            "http://forgejo.example.com:3000",
        )

    def test_case_insensitive_host_match(self):
        assert git_host_api._absolute_url_host_matches_git_host_base(
            "http://FORGEJO.EXAMPLE.COM:3000/api/v1/repos/o/r/pulls/1",
            "http://forgejo.example.com:3000",
        )

    def test_loopback_base_matches_itself(self):
        assert git_host_api._absolute_url_host_matches_git_host_base(
            "http://127.0.0.1:3000/api/v1/repos/o/r/pulls/1",
            "http://127.0.0.1:3000",
        )


class _PlatformRecordingProvider:
    """TokenProvider recording which token it minted, so a test can assert
    which token flows into the Authorization header for a given target."""

    def __init__(self, token: str):
        self.token = token
        self.calls: list[str] = []

    def resolve_token(self, role: str, *, repo: str | None = None) -> str:
        self.calls.append(role)
        return self.token


class TestMainGithubTargetRouting:
    def test_github_absolute_url_no_base_prepended(self, capsys):
        """The lr-104a repro: git-host-api GET <absolute GitHub URL> must issue
        the request against that URL VERBATIM -- never with the git-host
        base prepended in front of it (the malformed
        'http://<git-host-base><https://api.github.com/...>' defect)."""
        captured = {}

        def fake_opener(req, timeout=15):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            return _FakeResponse(200, b'{"reviews": []}')

        rc = git_host_api.main(
            [
                "--caller", "holden",
                "GET", "https://api.github.com/repos/some-owner/some-repo/pulls/318/reviews",
            ],
            token_provider=_PlatformRecordingProvider("gh-reader-tok"),
            opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert captured["url"] == (
            "https://api.github.com/repos/some-owner/some-repo/pulls/318/reviews"
        )
        assert "127.0.0.1" not in captured["url"]
        assert captured["headers"]["Authorization"] == "token gh-reader-tok"

    def test_github_target_ignores_explicit_git_host_base_flag(self, capsys):
        """Even an explicit --git-host-base-url must never be prepended to
        an absolute GitHub target -- the base flag is a Forgejo-path-only
        concern."""
        captured = {}

        def fake_opener(req, timeout=15):
            captured["url"] = req.full_url
            return _FakeResponse(200, b"{}")

        rc = git_host_api.main(
            [
                "--git-host-base-url", "http://forgejo.example.com:3000",
                "GET", "https://api.github.com/repos/o/r/pulls/1/reviews",
            ],
            token_provider=_PlatformRecordingProvider("gh-reader-tok"),
            opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert captured["url"] == "https://api.github.com/repos/o/r/pulls/1/reviews"

    def test_forgejo_relative_path_still_gets_base_prepended(self, capsys):
        """Regression guard: a relative Forgejo path's existing behavior
        (base prepended in front of the path) must be byte-identical after
        this fix."""
        captured = {}

        def fake_opener(req, timeout=15):
            captured["url"] = req.full_url
            return _FakeResponse(200, b"{}")

        rc = git_host_api.main(
            [
                "--git-host-base-url", "http://forgejo.example.com:3000",
                "GET", "/api/v1/repos/some-owner/some-repo/pulls/42.diff",
            ],
            token_provider=_PlatformRecordingProvider("forgejo-tok"),
            opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert captured["url"] == (
            "http://forgejo.example.com:3000/api/v1/repos/some-owner/some-repo/pulls/42.diff"
        )

    def test_absolute_forgejo_url_is_not_double_prepended(self, capsys):
        """lr-8f7d4e regression: a documented caller pre-check contract can
        pass an ABSOLUTE Forgejo URL as PATH (not just a relative one).
        _is_github_target correctly says "not a GitHub target" for this
        (see test_absolute_forgejo_url_is_not_github_target), but that alone
        left target_platform=PLATFORM_FORGEJO, and git_host_base was then
        prepended in front of the ALREADY-ABSOLUTE path -- producing a
        malformed double-authority URL urllib cannot resolve
        ('http://127.0.0.1:3000http://forgejo.example.com:3000/...' ->
        "Name or service not known"). The built URL must be the absolute
        PATH verbatim, single scheme/authority, never the base prepended in
        front of it."""
        captured = {}

        def fake_opener(req, timeout=15):
            captured["url"] = req.full_url
            return _FakeResponse(200, b"{}")

        rc = git_host_api.main(
            [
                "--git-host-base-url", "http://forgejo.example.com:3000",
                "GET", "http://forgejo.example.com:3000/api/v1/repos/some-owner/some-repo/pulls/42",
            ],
            token_provider=_PlatformRecordingProvider("forgejo-tok"),
            opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert captured["url"] == (
            "http://forgejo.example.com:3000/api/v1/repos/some-owner/some-repo/pulls/42"
        )
        # The double-prepend defect's tell: the base's own scheme/host
        # would appear TWICE in the built URL if this regressed.
        assert captured["url"].count("http://forgejo.example.com:3000") == 1
        parsed = urllib.parse.urlsplit(captured["url"])
        assert parsed.scheme == "http"
        assert parsed.netloc == "forgejo.example.com:3000"

    def test_absolute_forgejo_url_host_mismatch_is_not_silently_trusted(self, capsys):
        """lr-69af67: closing a gap flagged non-blocking on lr-8f7d4e/#77 by
        both review passes. The double-prepend fix above (see
        test_absolute_forgejo_url_is_not_double_prepended) emptied
        git_host_base for ANY absolute http(s) URL routed to the Forgejo
        branch, with no host-anchor check equivalent to
        _is_github_target's hostname-suffix match -- so the live Forgejo
        bearer token would be attached and sent to WHATEVER host path_arg
        named, not only the configured --git-host-base-url. An absolute URL
        whose host does NOT match the resolved git-host base must be
        refused BEFORE any I/O -- never issued with the git-host token
        attached, and never silently normalized/prepended either."""
        opener_called = False

        def fake_opener(req, timeout=15):
            nonlocal opener_called
            opener_called = True
            return _FakeResponse(200, b"{}")

        provider = _PlatformRecordingProvider("forgejo-tok")
        rc = git_host_api.main(
            [
                "--git-host-base-url", "http://forgejo.example.com:3000",
                "GET", "http://attacker.example.net:3000/api/v1/repos/some-owner/some-repo/pulls/42",
            ],
            token_provider=provider,
            opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_ABSOLUTE_URL_HOST_MISMATCH
        # Fails BEFORE any I/O -- the mismatched host is never contacted, so
        # the token (already resolved/minted above, mirroring the ordinary
        # token-then-route order this module already used pre-fix) is never
        # actually sent to it.
        assert not opener_called
        stderr = capsys.readouterr().err
        assert "attacker.example.net:3000" in stderr
        assert "forgejo.example.com:3000" in stderr

    def test_absolute_forgejo_url_host_matches_ignores_differing_port(self, capsys):
        """A host-only match is not enough -- lr-69af67 requires host AND
        port to agree with the resolved git-host base. A same-hostname,
        different-port absolute URL is exactly the shape a misconfigured
        reverse proxy or a copy/paste error would produce, and must be
        refused rather than trusted."""
        opener_called = False

        def fake_opener(req, timeout=15):
            nonlocal opener_called
            opener_called = True
            return _FakeResponse(200, b"{}")

        rc = git_host_api.main(
            [
                "--git-host-base-url", "http://forgejo.example.com:3000",
                "GET", "http://forgejo.example.com:9999/api/v1/repos/some-owner/some-repo/pulls/42",
            ],
            token_provider=_PlatformRecordingProvider("forgejo-tok"),
            opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_ABSOLUTE_URL_HOST_MISMATCH
        assert not opener_called

    def test_github_target_resolves_platform_provider_when_none_injected(self, monkeypatch):
        """When no token_provider is injected (the real dispatch path),
        a GitHub target must resolve its provider via
        resolve_platform_provider(PLATFORM_GITHUB), not PLATFORM_FORGEJO --
        this is the actual fix: before lr-104a, a GitHub PATH still resolved
        a Forgejo-role token because the platform selection was hardcoded."""
        seen_platforms = []

        def fake_resolve_platform_provider(platform, **kwargs):
            seen_platforms.append(platform)
            return _PlatformRecordingProvider("resolved-tok")

        monkeypatch.setattr(
            git_host_api, "resolve_platform_provider", fake_resolve_platform_provider
        )

        def fake_opener(req, timeout=15):
            return _FakeResponse(200, b"{}")

        rc = git_host_api.main(
            ["GET", "https://api.github.com/repos/o/r/pulls/1/reviews"],
            opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert seen_platforms == [git_host_api.PLATFORM_GITHUB]

    def test_forgejo_path_still_resolves_forgejo_platform_provider(self, monkeypatch):
        """Byte-identical regression guard for the un-changed branch: a
        relative Forgejo path must still resolve PLATFORM_FORGEJO, exactly
        as before this fix."""
        seen_platforms = []

        def fake_resolve_platform_provider(platform, **kwargs):
            seen_platforms.append(platform)
            return _PlatformRecordingProvider("forgejo-tok")

        monkeypatch.setattr(
            git_host_api, "resolve_platform_provider", fake_resolve_platform_provider
        )

        def fake_opener(req, timeout=15):
            return _FakeResponse(200, b"{}")

        rc = git_host_api.main(
            ["GET", "/api/v1/repos/o/r/pulls/1.diff"],
            opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert seen_platforms == [git_host_api.PLATFORM_FORGEJO]


# ---------------------------------------------------------------------------
# GitHub-URL repo-context threading (lr-5f7971) -- the last mile of lr-104a:
# that task wired absolute-URL ROUTING (GitHub token + no base-prepend) and
# Forgejo repo-threading (_REPOS_PATH_RE), but never GitHub-URL repo
# extraction. A {repo}-templated GitHub token-minting command (GitHub App
# installation tokens are minted per-repo) needs owner/repo threaded from an
# absolute https://api.github.com/repos/{owner}/{repo}/... PATH into
# resolve_token(repo=...) -- before this fix, _REPOS_PATH_RE (anchored to
# Forgejo's /api/v1/repos/ prefix) never matched a GitHub URL, so call_repo
# was always None for a GitHub target and a {repo}-templated helper refused
# unconditionally, even for a repo-scoped GitHub read.
# ---------------------------------------------------------------------------


class TestIsGithubTargetRepoExtraction:
    def test_github_repos_url_extracts_owner_repo(self):
        match = git_host_api._GITHUB_REPOS_URL_RE.match(
            "https://api.github.com/repos/some-owner/some-repo/pulls/318/reviews"
        )
        assert match is not None
        assert match.group(1) == "some-owner"
        assert match.group(2) == "some-repo"

    def test_forgejo_repos_path_re_does_not_match_github_url(self):
        # The Forgejo-anchored pattern must never match an absolute GitHub
        # URL -- this is the exact defect: _REPOS_PATH_RE requires the
        # /api/v1/repos/ prefix, which a GitHub URL never has.
        assert git_host_api._REPOS_PATH_RE.match(
            "https://api.github.com/repos/some-owner/some-repo/pulls/1"
        ) is None

    def test_github_repos_url_re_does_not_match_non_repo_github_url(self):
        assert git_host_api._GITHUB_REPOS_URL_RE.match("https://api.github.com/user") is None


class _RepoAndPlatformRecordingProvider:
    """TokenProvider recording (role, repo) -- the GitHub-target counterpart
    of _RepoRecordingProvider, used to assert repo threading on the GitHub
    routing branch specifically (_PlatformRecordingProvider above discards
    the repo kwarg since it predates this fix)."""

    def __init__(self, token: str = "gh-reader-tok"):
        self.token = token
        self.calls: list[tuple] = []

    def resolve_token(self, role: str, *, repo: str | None = None) -> str:
        self.calls.append((role, repo))
        return self.token


class TestMainGithubRepoContext:
    def test_github_repos_url_threads_owner_repo_into_resolve_token(self, capsys):
        provider = _RepoAndPlatformRecordingProvider()

        def fake_opener(req, timeout=15):
            return _FakeResponse(200, b'{"reviews": []}')

        rc = git_host_api.main(
            [
                "--caller", "holden",
                "GET", "https://api.github.com/repos/some-owner/some-repo/pulls/318/reviews",
            ],
            token_provider=provider, opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert provider.calls == [("holden", "some-owner/some-repo")]

    def test_non_repo_github_url_passes_none(self, capsys):
        provider = _RepoAndPlatformRecordingProvider()

        def fake_opener(req, timeout=15):
            return _FakeResponse(200, b'{"login": "holden-bot"}')

        rc = git_host_api.main(
            ["--caller", "holden", "GET", "https://api.github.com/user"],
            token_provider=provider, opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert provider.calls == [("holden", None)]

    def test_forgejo_repos_path_still_threads_via_existing_path(self, capsys):
        """Byte-identical regression guard: the pre-existing Forgejo
        repo-threading path (_REPOS_PATH_RE, lr-ea28) is untouched by the
        new GitHub-only branch."""
        provider = _RepoAndPlatformRecordingProvider(token="forgejo-tok")

        def fake_opener(req, timeout=15):
            return _FakeResponse(200, b'{"ok": true}')

        rc = git_host_api.main(
            ["--caller", "holden", "/api/v1/repos/some-owner/some-repo/pulls/1.diff"],
            token_provider=provider, opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert provider.calls == [("holden", "some-owner/some-repo")]


# ---------------------------------------------------------------------------
# Cross-platform URL-shape mistake detection (lr-aa4e3c): when the resolved
# target platform's own repo-path pattern fails to match PATH, check the
# path against the OTHER platform's shape before refusing opaquely. Evidence:
# a merge-gate caller issued 'GET https://github.com/api/v1/repos/o/r/pulls/336'
# -- a Forgejo path shape on the github.com host. Platform detection resolved
# github, but repo extraction silently returned None and the {repo}-
# templated token helper refused with no clue why -- misdiagnosed as an
# environment defect. Fail-closed with a corrective error naming the exact
# fixed-up URL, WITHOUT auto-rewriting PATH and WITHOUT attempting a token
# mint.
# ---------------------------------------------------------------------------


class TestCheckCrossPlatformUrlShapeMistake:
    def test_forgejo_shaped_path_on_github_target_raises_with_corrected_url(self):
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api._check_cross_platform_url_shape_mistake(
                "https://github.com/api/v1/repos/some-owner/some-repo/pulls/336",
                git_host_api.PLATFORM_GITHUB,
                git_host_base="http://127.0.0.1:3000",
            )
        assert exc_info.value.code == git_host_api.EXIT_CROSS_PLATFORM_URL_SHAPE_MISMATCH
        message = str(exc_info.value)
        assert "some-owner/some-repo" in message
        assert "api.github.com/repos/some-owner/some-repo" in message

    def test_bare_github_shaped_path_on_forgejo_target_raises_with_corrected_url(self):
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api._check_cross_platform_url_shape_mistake(
                "/repos/some-owner/some-repo/pulls/42",
                git_host_api.PLATFORM_FORGEJO,
                git_host_base="http://forgejo.example.com:3000",
            )
        assert exc_info.value.code == git_host_api.EXIT_CROSS_PLATFORM_URL_SHAPE_MISMATCH
        message = str(exc_info.value)
        assert "some-owner/some-repo" in message
        assert "http://forgejo.example.com:3000/api/v1/repos/some-owner/some-repo" in message

    def test_correctly_shaped_github_path_does_not_raise(self):
        git_host_api._check_cross_platform_url_shape_mistake(
            "https://api.github.com/repos/some-owner/some-repo/pulls/1",
            git_host_api.PLATFORM_GITHUB,
            git_host_base="http://127.0.0.1:3000",
        )  # no raise

    def test_correctly_shaped_forgejo_path_does_not_raise(self):
        git_host_api._check_cross_platform_url_shape_mistake(
            "/api/v1/repos/some-owner/some-repo/pulls/1",
            git_host_api.PLATFORM_FORGEJO,
            git_host_base="http://forgejo.example.com:3000",
        )  # no raise

    def test_non_repo_scoped_github_url_does_not_raise(self):
        # /user matches neither shape -- call_repo simply stays None, the
        # pre-existing fail-open-to-unscoped behavior for a non-repo-scoped
        # call. This check must never manufacture a false positive here.
        git_host_api._check_cross_platform_url_shape_mistake(
            "https://api.github.com/user",
            git_host_api.PLATFORM_GITHUB,
            git_host_base="http://127.0.0.1:3000",
        )  # no raise

    def test_non_repo_scoped_forgejo_path_does_not_raise(self):
        git_host_api._check_cross_platform_url_shape_mistake(
            "/api/v1/user",
            git_host_api.PLATFORM_FORGEJO,
            git_host_base="http://forgejo.example.com:3000",
        )  # no raise


class TestMainCrossPlatformUrlShapeMismatch:
    def test_github_target_forgejo_shaped_path_exits_nonzero_no_token_mint(self, capsys):
        """The lr-aa4e3c repro end-to-end: a Forgejo-shaped path on a
        GitHub target must exit non-zero with a corrective error naming the
        api.github.com/repos/{owner}/{repo}/... shape, and MUST NOT attempt
        a token mint."""
        mint_called = {"n": False}

        class _MintTrackingProvider:
            def resolve_token(self, role: str, *, repo: str | None = None) -> str:
                mint_called["n"] = True
                return "should-never-be-used"

        opener_called = {"n": False}

        def fake_opener(req, timeout=15):
            opener_called["n"] = True
            return _FakeResponse(200, b"{}")

        rc = git_host_api.main(
            ["GET", "https://github.com/api/v1/repos/some-owner/some-repo/pulls/336"],
            token_provider=_MintTrackingProvider(),
            opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_CROSS_PLATFORM_URL_SHAPE_MISMATCH
        assert rc != 0
        assert mint_called["n"] is False
        assert opener_called["n"] is False
        stderr = capsys.readouterr().err
        assert "some-owner/some-repo" in stderr
        assert "api.github.com/repos/some-owner/some-repo" in stderr

    def test_forgejo_target_bare_repos_path_exits_nonzero_naming_api_v1_shape(self, capsys):
        """A bare /repos/... path (missing /api/v1) on a Forgejo target must
        exit non-zero with a corrective error naming the .../api/v1/repos/...
        shape, and MUST NOT attempt a token mint."""
        mint_called = {"n": False}

        class _MintTrackingProvider:
            def resolve_token(self, role: str, *, repo: str | None = None) -> str:
                mint_called["n"] = True
                return "should-never-be-used"

        opener_called = {"n": False}

        def fake_opener(req, timeout=15):
            opener_called["n"] = True
            return _FakeResponse(200, b"{}")

        rc = git_host_api.main(
            [
                "--git-host-base-url", "http://forgejo.example.com:3000",
                "GET", "/repos/some-owner/some-repo/pulls/42",
            ],
            token_provider=_MintTrackingProvider(),
            opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_CROSS_PLATFORM_URL_SHAPE_MISMATCH
        assert rc != 0
        assert mint_called["n"] is False
        assert opener_called["n"] is False
        stderr = capsys.readouterr().err
        assert "some-owner/some-repo" in stderr
        assert "http://forgejo.example.com:3000/api/v1/repos/some-owner/some-repo" in stderr

    def test_correctly_shaped_github_call_is_unaffected(self, capsys):
        """Regression guard: a correctly-shaped GitHub call must still
        succeed byte-for-byte -- this check only fires when call_repo is
        None AND the other platform's shape matches."""
        provider = _RepoAndPlatformRecordingProvider()

        def fake_opener(req, timeout=15):
            return _FakeResponse(200, b'{"reviews": []}')

        rc = git_host_api.main(
            ["GET", "https://api.github.com/repos/some-owner/some-repo/pulls/1/reviews"],
            token_provider=provider, opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert provider.calls == [(git_host_api.DEFAULT_ROLE, "some-owner/some-repo")]

    def test_correctly_shaped_forgejo_call_is_unaffected(self, capsys):
        """Regression guard: a correctly-shaped Forgejo call must still
        succeed byte-for-byte."""
        provider = _RepoRecordingProvider()

        def fake_opener(req, timeout=15):
            return _FakeResponse(200, b'{"ok": true}')

        rc = git_host_api.main(
            ["/api/v1/repos/some-owner/some-repo/pulls/1.diff"],
            token_provider=provider, opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert provider.calls == [(git_host_api.DEFAULT_ROLE, "some-owner/some-repo")]


# ---------------------------------------------------------------------------
# --expect-verdict-block (lr-30c0d0) -- tool-owned ```review-result``` fence
# construction, so a reviewer role never hand-authors the fence through the
# shell. build_expected_verdict_body / verify_verdict_block are the two new
# library functions; the class below drives them through main() end to end.
# ---------------------------------------------------------------------------

_FULL_SHA = "a" * 40


class TestBuildExpectedVerdictBody:
    def test_appends_fence_from_structured_stdin(self):
        body = git_host_api.build_expected_verdict_body(
            b'{"body":"LGTM, no issues found.","review_status":"clean"}',
            reviewer="some-reviewer",
            pr_number=42,
            expected_head_sha=_FULL_SHA,
        )
        assert body.startswith("LGTM, no issues found.\n")
        assert "```review-result" in body
        assert '"review_status": "clean"' in body

    def test_no_backtick_required_anywhere_in_stdin(self):
        # The whole point: the caller's JSON never needs a backtick.
        raw = b'{"body":"blocking finding: X","review_status":"blocking"}'
        assert b"`" not in raw
        body = git_host_api.build_expected_verdict_body(
            raw, reviewer="some-reviewer", pr_number=1, expected_head_sha=_FULL_SHA,
        )
        assert "```review-result" in body  # fence added BY THE TOOL, not the caller

    def test_non_json_stdin_rejected(self):
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.build_expected_verdict_body(
                b"not json", reviewer="r", pr_number=1, expected_head_sha=_FULL_SHA,
            )
        assert exc_info.value.code == git_host_api.EXIT_VERDICT_BLOCK_USAGE

    def test_missing_body_field_rejected(self):
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.build_expected_verdict_body(
                b'{"review_status":"clean"}', reviewer="r", pr_number=1,
                expected_head_sha=_FULL_SHA,
            )
        assert exc_info.value.code == git_host_api.EXIT_VERDICT_BLOCK_USAGE

    def test_missing_review_status_rejected(self):
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.build_expected_verdict_body(
                b'{"body":"x"}', reviewer="r", pr_number=1, expected_head_sha=_FULL_SHA,
            )
        assert exc_info.value.code == git_host_api.EXIT_VERDICT_BLOCK_USAGE

    def test_invalid_review_status_rejected(self):
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.build_expected_verdict_body(
                b'{"body":"x","review_status":"maybe"}', reviewer="r", pr_number=1,
                expected_head_sha=_FULL_SHA,
            )
        assert exc_info.value.code == git_host_api.EXIT_VERDICT_BLOCK_USAGE

    def test_pr_number_not_read_from_stdin(self):
        # A caller-supplied pr_number in the JSON is simply ignored -- the
        # PATH-derived pr_number (the *pr_number* param here) is the one
        # source of truth, so it can never disagree with the fence.
        body = git_host_api.build_expected_verdict_body(
            b'{"body":"x","review_status":"clean","pr_number":999}',
            reviewer="r", pr_number=7, expected_head_sha=_FULL_SHA,
        )
        assert '"pr_number": 7' in body

    def test_pre_embedded_fence_in_body_is_rejected(self):
        # lr-5260f9, THE SAME SHAPE OBSERVED AGAINST A FORGEJO DEPLOYMENT
        # reproduced at the unit level: a caller that has already
        # hand-embedded its own fence in 'body' before this function
        # unconditionally appended a second one -- isolated by a
        # controlled retest in this task's own comment thread (same agent,
        # same PR, same head SHA, same flag; only the pre-embedded-fence
        # variable changed). This function now REFUSES to construct the
        # malformed shape rather than silently producing two fences.
        pre_embedded_fence = git_host_api.build_verdict_block(
            "some-reviewer", "clean", _FULL_SHA, 42
        )
        raw = json.dumps(
            {"body": f"my own findings\n{pre_embedded_fence}", "review_status": "clean"}
        ).encode("utf-8")
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.build_expected_verdict_body(
                raw, reviewer="some-reviewer", pr_number=42, expected_head_sha=_FULL_SHA,
            )
        assert exc_info.value.code == git_host_api.EXIT_VERDICT_BLOCK_USAGE
        assert "already contains" in str(exc_info.value)

    def test_prose_with_no_fence_is_unaffected_by_the_refusal(self):
        # Non-adversarial, ordinary case: plain prose with no fence-shaped
        # content still constructs cleanly -- exactly one fence, tool-owned.
        body = git_host_api.build_expected_verdict_body(
            b'{"body":"no issues found here","review_status":"clean"}',
            reviewer="some-reviewer", pr_number=1, expected_head_sha=_FULL_SHA,
        )
        assert body.count("```review-result") == 1

    def test_model_attested_included_when_given(self):
        # lr-95543d: OPTIONAL, threaded through to build_verdict_block.
        body = git_host_api.build_expected_verdict_body(
            b'{"body":"LGTM.","review_status":"clean"}',
            reviewer="some-reviewer", pr_number=1, expected_head_sha=_FULL_SHA,
            model_attested="claude-opus-4-1-20250805",
        )
        assert '"model_attested": "claude-opus-4-1-20250805"' in body

    def test_model_attested_omitted_when_not_given(self):
        body = git_host_api.build_expected_verdict_body(
            b'{"body":"LGTM.","review_status":"clean"}',
            reviewer="some-reviewer", pr_number=1, expected_head_sha=_FULL_SHA,
        )
        assert "model_attested" not in body


class TestVerifyVerdictBlock:
    def test_matching_fence_passes(self):
        fence = git_host_api.build_verdict_block("some-reviewer", "clean", _FULL_SHA, 5)
        git_host_api.verify_verdict_block(
            f"prose\n{fence}",
            reviewer="some-reviewer",
            expected_review_status="clean",
            expected_head_sha=_FULL_SHA,
            expected_pr_number=5,
        )  # no raise

    def test_missing_fence_raises_mismatch(self):
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.verify_verdict_block(
                "just prose, no fence",
                reviewer="some-reviewer",
                expected_review_status="clean",
                expected_head_sha=_FULL_SHA,
                expected_pr_number=5,
            )
        assert exc_info.value.code == git_host_api.EXIT_VERDICT_BLOCK_MISMATCH

    def test_wrong_reviewer_field_raises_mismatch(self):
        fence = git_host_api.build_verdict_block("different-reviewer", "clean", _FULL_SHA, 5)
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.verify_verdict_block(
                fence,
                reviewer="some-reviewer",
                expected_review_status="clean",
                expected_head_sha=_FULL_SHA,
                expected_pr_number=5,
            )
        assert exc_info.value.code == git_host_api.EXIT_VERDICT_BLOCK_MISMATCH

    def test_wrong_review_status_raises_mismatch(self):
        fence = git_host_api.build_verdict_block("some-reviewer", "blocking", _FULL_SHA, 5)
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.verify_verdict_block(
                fence,
                reviewer="some-reviewer",
                expected_review_status="clean",
                expected_head_sha=_FULL_SHA,
                expected_pr_number=5,
            )
        assert exc_info.value.code == git_host_api.EXIT_VERDICT_BLOCK_MISMATCH

    def test_wrong_head_sha_raises_mismatch(self):
        other_sha = "b" * 40
        fence = git_host_api.build_verdict_block("some-reviewer", "clean", other_sha, 5)
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.verify_verdict_block(
                fence,
                reviewer="some-reviewer",
                expected_review_status="clean",
                expected_head_sha=_FULL_SHA,
                expected_pr_number=5,
            )
        assert exc_info.value.code == git_host_api.EXIT_VERDICT_BLOCK_MISMATCH

    def test_wrong_pr_number_raises_mismatch(self):
        fence = git_host_api.build_verdict_block("some-reviewer", "clean", _FULL_SHA, 99)
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.verify_verdict_block(
                fence,
                reviewer="some-reviewer",
                expected_review_status="clean",
                expected_head_sha=_FULL_SHA,
                expected_pr_number=5,
            )
        assert exc_info.value.code == git_host_api.EXIT_VERDICT_BLOCK_MISMATCH

    def test_matching_model_attested_passes(self):
        fence = git_host_api.build_verdict_block(
            "some-reviewer", "clean", _FULL_SHA, 5, "claude-opus-4-1-20250805"
        )
        git_host_api.verify_verdict_block(
            f"prose\n{fence}",
            reviewer="some-reviewer",
            expected_review_status="clean",
            expected_head_sha=_FULL_SHA,
            expected_pr_number=5,
            expected_model_attested="claude-opus-4-1-20250805",
        )  # no raise

    def test_wrong_model_attested_raises_mismatch(self):
        fence = git_host_api.build_verdict_block(
            "some-reviewer", "clean", _FULL_SHA, 5, "claude-haiku-4-5-20251001"
        )
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.verify_verdict_block(
                fence,
                reviewer="some-reviewer",
                expected_review_status="clean",
                expected_head_sha=_FULL_SHA,
                expected_pr_number=5,
                expected_model_attested="claude-opus-4-1-20250805",
            )
        assert exc_info.value.code == git_host_api.EXIT_VERDICT_BLOCK_MISMATCH

    def test_expected_model_attested_omitted_is_a_noop(self):
        # Not passing expected_model_attested at all (the default) means no
        # comparison happens, regardless of what the fence carries.
        fence = git_host_api.build_verdict_block(
            "some-reviewer", "clean", _FULL_SHA, 5, "anything-at-all"
        )
        git_host_api.verify_verdict_block(
            fence,
            reviewer="some-reviewer",
            expected_review_status="clean",
            expected_head_sha=_FULL_SHA,
            expected_pr_number=5,
        )  # no raise


class TestMainExpectVerdictBlockUsageGuards:
    """Preflight refusals BEFORE any I/O -- mirrors
    TestMainCommentsPostRequiresVerify's own never-touches-network pattern."""

    def _refused_before_network(self, argv):
        called = {"n": False}

        def fake_opener(req, timeout=15):
            called["n"] = True
            return _FakeResponse(200, b"{}")

        rc = git_host_api.main(argv, token_provider=_RecordingProvider(), opener=fake_opener)
        assert called["n"] is False
        return rc

    def test_wrong_endpoint_rejected(self):
        rc = self._refused_before_network(
            ["--expect-verdict-block", "some-reviewer", "--body-stdin",
             "--verify-comment", "--pr-sha", _FULL_SHA,
             "GET", "/api/v1/repos/o/r/pulls/1.diff"],
        )
        assert rc == git_host_api.EXIT_VERDICT_BLOCK_USAGE

    def test_missing_body_stdin_rejected(self):
        rc = self._refused_before_network(
            ["--expect-verdict-block", "some-reviewer",
             "--verify-comment", "--pr-sha", _FULL_SHA,
             "POST", "/api/v1/repos/o/r/issues/5/comments"],
        )
        assert rc == git_host_api.EXIT_VERDICT_BLOCK_USAGE

    def test_missing_verify_comment_rejected(self):
        # A comments POST missing --verify-comment is already refused by the
        # pre-existing, EARLIER --verify-comment gate (EXIT_VERIFY_COMMENT_
        # REQUIRED) before the --expect-verdict-block preflight even runs --
        # still refused before any I/O, just via the pre-existing exit code.
        rc = self._refused_before_network(
            ["--expect-verdict-block", "some-reviewer", "--body-stdin",
             "--pr-sha", _FULL_SHA,
             "POST", "/api/v1/repos/o/r/issues/5/comments"],
        )
        assert rc == git_host_api.EXIT_VERIFY_COMMENT_REQUIRED

    def test_missing_pr_sha_rejected(self):
        rc = self._refused_before_network(
            ["--expect-verdict-block", "some-reviewer", "--body-stdin",
             "--verify-comment",
             "POST", "/api/v1/repos/o/r/issues/5/comments"],
        )
        assert rc == git_host_api.EXIT_VERDICT_BLOCK_USAGE

    def test_invalid_reviewer_token_rejected(self):
        rc = self._refused_before_network(
            ["--expect-verdict-block", "not a safe token!", "--body-stdin",
             "--verify-comment", "--pr-sha", _FULL_SHA,
             "POST", "/api/v1/repos/o/r/issues/5/comments"],
        )
        assert rc == git_host_api.EXIT_VERDICT_BLOCK_USAGE

    def test_model_attested_without_expect_verdict_block_rejected(self):
        # lr-95543d: --model-attested is only meaningful alongside
        # --expect-verdict-block -- there is no other fence-building route
        # on this verb for it to attach to.
        rc = self._refused_before_network(
            ["--model-attested", "claude-opus-4-1-20250805", "--body-stdin",
             "--verify-comment", "--pr-sha", _FULL_SHA,
             "POST", "/api/v1/repos/o/r/issues/5/comments"],
        )
        assert rc == git_host_api.EXIT_VERDICT_BLOCK_USAGE

    def test_pre_embedded_fence_in_stdin_body_rejected(self, monkeypatch):
        # lr-5260f9 end-to-end: a pre-embedded fence in --body-stdin's
        # 'body' field is refused -- the PR #485 shape never reaches a
        # POST/comments call. build_expected_verdict_body runs AFTER the
        # existing --pr-sha readback (a real GET this invocation already
        # performs regardless of this fix -- see check_pr_sha's call site
        # ahead of build_expected_verdict_body), so this test answers that
        # GET and asserts no POST is ever issued, rather than asserting
        # zero network calls outright.
        pre_embedded_fence = git_host_api.build_verdict_block(
            "some-reviewer", "clean", _FULL_SHA, 5
        )
        stdin_json = json.dumps(
            {"body": f"my findings\n{pre_embedded_fence}", "review_status": "clean"}
        )
        monkeypatch.setattr(
            "sys.stdin", type("S", (), {"buffer": io.BytesIO(stdin_json.encode("utf-8"))})()
        )
        posted = {"n": False}

        def fake_opener(req, timeout=15):
            if req.get_method() == "GET" and "/pulls/" in req.full_url:
                return _FakeResponse(200, json.dumps({"head": {"sha": _FULL_SHA}}).encode("utf-8"))
            if req.get_method() == "POST":
                posted["n"] = True
            return _FakeResponse(200, b"{}")

        rc = git_host_api.main(
            ["--expect-verdict-block", "some-reviewer", "--body-stdin",
             "--verify-comment", "--pr-sha", _FULL_SHA,
             "POST", "/api/v1/repos/o/r/issues/5/comments"],
            token_provider=_RecordingProvider(),
            opener=fake_opener,
        )
        assert posted["n"] is False
        assert rc == git_host_api.EXIT_VERDICT_BLOCK_USAGE


class TestMainExpectVerdictBlockEndToEnd:
    def _make_opener(
        self, *, own_login="some-reviewer-bot", comment_created_at=None, verdict_body=None,
    ):
        """Answers POST comments, GET /user, GET pulls/<n> (pr-sha check),
        and GET comments (readback). The readback comment body defaults to
        whatever was actually POSTed (captured via a closure cell) unless
        *verdict_body* overrides it -- letting a test simulate the fence
        landing mangled in transit without touching the POST path itself."""
        state = {"posted_body": None}

        def fake_opener(req, timeout=15):
            url = req.full_url
            if req.get_method() == "GET" and "/pulls/" in url:
                return _FakeResponse(200, json.dumps({"head": {"sha": _FULL_SHA}}).encode("utf-8"))
            if req.get_method() == "POST" and url.endswith("/comments"):
                posted = json.loads(req.data.decode("utf-8"))
                state["posted_body"] = posted["body"]
                return _FakeResponse(200, b'{"id": 999}')
            if url.endswith("/api/v1/user"):
                return _FakeResponse(200, json.dumps({"login": own_login}).encode("utf-8"))
            if url.endswith("/comments"):
                created = comment_created_at or (
                    datetime.now(timezone.utc) + timedelta(seconds=1)
                ).isoformat().replace("+00:00", "Z")
                body = verdict_body if verdict_body is not None else state["posted_body"]
                return _FakeResponse(200, json.dumps([
                    {"id": 999, "html_url": "http://x/999", "user": {"login": own_login},
                     "body": body, "created_at": created},
                ]).encode("utf-8"))
            raise AssertionError(f"unexpected request to {url}")

        return fake_opener

    def _stdin(self, monkeypatch, body: bytes):
        monkeypatch.setattr(
            "sys.stdin", type("S", (), {"buffer": io.BytesIO(body)})()
        )

    def test_success_no_backtick_in_argv_or_stdin(self, monkeypatch, capsys):
        """The end-to-end happy path: structured stdin JSON, zero backticks
        anywhere, and the tool builds/posts/verifies the fence itself."""
        raw_stdin = b'{"body":"LGTM, no issues found.","review_status":"clean"}'
        assert b"`" not in raw_stdin
        self._stdin(monkeypatch, raw_stdin)
        opener = self._make_opener()

        argv = [
            "--caller", "some-reviewer",
            "--body-stdin", "--verify-comment", "--pr-sha", _FULL_SHA,
            "--expect-verdict-block", "some-reviewer",
            "POST", "/api/v1/repos/o/r/issues/5/comments",
        ]
        assert not any("`" in a for a in argv)

        rc = git_host_api.main(argv, token_provider=_RecordingProvider(), opener=opener)
        assert rc == git_host_api.EXIT_OK
        out = capsys.readouterr().out
        payload = json.loads(out.strip().splitlines()[-1])
        assert payload["verified_comment_id"] == 999
        assert payload["verdict_block_verified"] is True

    def test_posted_body_contains_tool_owned_fence(self, monkeypatch):
        self._stdin(monkeypatch, b'{"body":"LGTM.","review_status":"clean"}')
        captured = {}

        def fake_opener(req, timeout=15):
            url = req.full_url
            if req.get_method() == "GET" and "/pulls/" in url:
                return _FakeResponse(200, json.dumps({"head": {"sha": _FULL_SHA}}).encode("utf-8"))
            if req.get_method() == "POST" and url.endswith("/comments"):
                posted = json.loads(req.data.decode("utf-8"))
                captured["body"] = posted["body"]
                return _FakeResponse(200, b'{"id": 1}')
            if url.endswith("/api/v1/user"):
                return _FakeResponse(200, b'{"login": "some-reviewer-bot"}')
            return _FakeResponse(200, json.dumps([
                {"id": 1, "html_url": "http://x/1", "user": {"login": "some-reviewer-bot"},
                 "body": captured.get("body", ""), "created_at": "2099-01-01T00:00:01Z"},
            ]).encode("utf-8"))

        rc = git_host_api.main(
            [
                "--caller", "some-reviewer",
                "--body-stdin", "--verify-comment", "--pr-sha", _FULL_SHA,
                "--expect-verdict-block", "some-reviewer",
                "POST", "/api/v1/repos/o/r/issues/9/comments",
            ],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert "```review-result" in captured["body"]
        assert '"pr_number": 9' in captured["body"]
        assert '"reviewer": "some-reviewer"' in captured["body"]
        assert '"review_status": "clean"' in captured["body"]

    def test_mangled_fence_on_readback_fails_after_verify_comment_passes(self, monkeypatch):
        """--verify-comment's own substring match can pass (the exact POSTED
        body text still appears within the readback body) while a SECOND,
        appended fence changes what the 'last block wins' parse actually
        returns -- verify_verdict_block is the check that catches this,
        which is exactly why it re-parses the READBACK's own body via
        parse_verdict_block, not the pre-POST string."""
        self._stdin(monkeypatch, b'{"body":"LGTM.","review_status":"clean"}')
        correct_fence = git_host_api.build_verdict_block("some-reviewer", "clean", _FULL_SHA, 9)
        posted_body = f"LGTM.\n{correct_fence}"

        def fake_opener(req, timeout=15):
            url = req.full_url
            if req.get_method() == "GET" and "/pulls/" in url:
                return _FakeResponse(200, json.dumps({"head": {"sha": _FULL_SHA}}).encode("utf-8"))
            if req.get_method() == "POST" and url.endswith("/comments"):
                return _FakeResponse(200, b'{"id": 1}')
            if url.endswith("/api/v1/user"):
                return _FakeResponse(200, b'{"login": "some-reviewer-bot"}')
            # The readback body CONTAINS the exact posted_body string (so
            # --verify-comment's own substring match still passes), but a
            # SECOND fence appended after it -- simulating a platform quirk
            # or transit corruption that appends stray content -- is what
            # parse_verdict_block's "last block wins" retry semantics
            # actually returns, and it disagrees with what was requested.
            corrupted_fence = git_host_api.build_verdict_block(
                "some-reviewer", "blocking", _FULL_SHA, 9
            )
            return _FakeResponse(200, json.dumps([
                {"id": 1, "html_url": "http://x/1", "user": {"login": "some-reviewer-bot"},
                 "body": f"{posted_body}\n{corrupted_fence}", "created_at": "2099-01-01T00:00:01Z"},
            ]).encode("utf-8"))

        rc = git_host_api.main(
            [
                "--caller", "some-reviewer",
                "--body-stdin", "--verify-comment", "--pr-sha", _FULL_SHA,
                "--expect-verdict-block", "some-reviewer",
                "POST", "/api/v1/repos/o/r/issues/9/comments",
            ],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_VERDICT_BLOCK_MISMATCH

    def test_model_attested_lands_and_verifies(self, monkeypatch, capsys):
        self._stdin(monkeypatch, b'{"body":"LGTM, no issues found.","review_status":"clean"}')
        opener = self._make_opener()

        argv = [
            "--caller", "some-reviewer",
            "--body-stdin", "--verify-comment", "--pr-sha", _FULL_SHA,
            "--expect-verdict-block", "some-reviewer",
            "--model-attested", "claude-opus-4-1-20250805",
            "POST", "/api/v1/repos/o/r/issues/5/comments",
        ]
        rc = git_host_api.main(argv, token_provider=_RecordingProvider(), opener=opener)
        assert rc == git_host_api.EXIT_OK
        out = capsys.readouterr().out
        payload = json.loads(out.strip().splitlines()[-1])
        assert payload["verdict_block_verified"] is True

    def test_stale_pr_sha_fails_before_post_expect_verdict_block(self, monkeypatch):
        self._stdin(monkeypatch, b'{"body":"LGTM.","review_status":"clean"}')

        def fake_opener(req, timeout=15):
            if "/pulls/" in req.full_url:
                return _FakeResponse(200, json.dumps({"head": {"sha": "current-sha-value-x"}}).encode("utf-8"))
            raise AssertionError(f"POST/readback should never fire on stale SHA: {req.full_url}")

        rc = git_host_api.main(
            [
                "--caller", "some-reviewer",
                "--body-stdin", "--verify-comment", "--pr-sha", "stale-sha-value-x",
                "--expect-verdict-block", "some-reviewer",
                "POST", "/api/v1/repos/o/r/issues/9/comments",
            ],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_STALE_PR

    def test_review_status_blocking_round_trips(self, monkeypatch):
        self._stdin(monkeypatch, b'{"body":"Found an issue.","review_status":"blocking"}')
        opener = self._make_opener()

        rc = git_host_api.main(
            [
                "--caller", "some-reviewer",
                "--body-stdin", "--verify-comment", "--pr-sha", _FULL_SHA,
                "--expect-verdict-block", "some-reviewer",
                "POST", "/api/v1/repos/o/r/issues/5/comments",
            ],
            token_provider=_RecordingProvider(), opener=opener,
        )
        assert rc == git_host_api.EXIT_OK


# ---------------------------------------------------------------------------
# --caller-tracking-id (lr-10a996) -- tool-owned ```loadout-note``` metadata
# composition, the general-purpose counterpart to --expect-verdict-block: a
# caller carries an opaque work-item tracking id alongside a comment body
# with zero backticks/heredocs/$VAR redirect targets ever crossing the
# shell.
# ---------------------------------------------------------------------------


class TestMainCallerTrackingIdUsageGuards:
    """Preflight refusals BEFORE any I/O -- mirrors
    TestMainExpectVerdictBlockUsageGuards's own never-touches-network
    pattern."""

    def _refused_before_network(self, argv, *, stdin=None, monkeypatch=None):
        if stdin is not None:
            monkeypatch.setattr(
                "sys.stdin", type("S", (), {"buffer": io.BytesIO(stdin)})()
            )
        called = {"n": False}

        def fake_opener(req, timeout=15):
            called["n"] = True
            return _FakeResponse(200, b"{}")

        rc = git_host_api.main(argv, token_provider=_RecordingProvider(), opener=fake_opener)
        assert called["n"] is False
        return rc

    def test_wrong_endpoint_rejected(self):
        rc = self._refused_before_network(
            ["--caller-tracking-id", "lr-10a996", "--body-stdin",
             "--verify-comment", "GET", "/api/v1/repos/o/r/pulls/1.diff"],
        )
        assert rc == git_host_api.EXIT_CALLER_TRACKING_ID_USAGE

    def test_missing_verify_comment_rejected(self):
        # A comments POST missing --verify-comment is already refused by the
        # pre-existing, EARLIER --verify-comment gate -- still refused
        # before any I/O, just via the pre-existing exit code.
        rc = self._refused_before_network(
            ["--caller-tracking-id", "lr-10a996", "--body-stdin",
             "POST", "/api/v1/repos/o/r/issues/5/comments"],
        )
        assert rc == git_host_api.EXIT_VERIFY_COMMENT_REQUIRED

    def test_empty_tracking_id_rejected(self):
        rc = self._refused_before_network(
            ["--caller-tracking-id", "", "--body-stdin", "--verify-comment",
             "POST", "/api/v1/repos/o/r/issues/5/comments"],
        )
        assert rc == git_host_api.EXIT_CALLER_TRACKING_ID_USAGE

    def test_whitespace_tracking_id_rejected(self):
        rc = self._refused_before_network(
            ["--caller-tracking-id", "has space", "--body-stdin", "--verify-comment",
             "POST", "/api/v1/repos/o/r/issues/5/comments"],
        )
        assert rc == git_host_api.EXIT_CALLER_TRACKING_ID_USAGE


class TestMainCallerTrackingIdEndToEnd:
    def _make_opener(self, *, own_login="some-role-bot"):
        state = {"posted_body": None}

        def fake_opener(req, timeout=15):
            url = req.full_url
            if req.get_method() == "POST" and url.endswith("/comments"):
                posted = json.loads(req.data.decode("utf-8"))
                state["posted_body"] = posted["body"]
                return _FakeResponse(200, b'{"id": 42}')
            if url.endswith("/api/v1/user"):
                return _FakeResponse(200, json.dumps({"login": own_login}).encode("utf-8"))
            if url.endswith("/comments"):
                created = (
                    datetime.now(timezone.utc) + timedelta(seconds=1)
                ).isoformat().replace("+00:00", "Z")
                return _FakeResponse(200, json.dumps([
                    {"id": 42, "html_url": "http://x/42", "user": {"login": own_login},
                     "body": state["posted_body"], "created_at": created},
                ]).encode("utf-8"))
            raise AssertionError(f"unexpected request to {url}")

        return fake_opener

    def _stdin(self, monkeypatch, body: bytes):
        monkeypatch.setattr(
            "sys.stdin", type("S", (), {"buffer": io.BytesIO(body)})()
        )

    def test_success_composes_note_with_no_backtick_in_argv_or_stdin(self, monkeypatch, capsys):
        raw_stdin = b'{"body":"Status update: build green."}'
        assert b"`" not in raw_stdin
        self._stdin(monkeypatch, raw_stdin)
        opener = self._make_opener()

        argv = [
            "--caller", "some-role",
            "--body-stdin", "--verify-comment",
            "--caller-tracking-id", "lr-10a996",
            "POST", "/api/v1/repos/o/r/issues/7/comments",
        ]
        assert not any("`" in a for a in argv)

        rc = git_host_api.main(argv, token_provider=_RecordingProvider(), opener=opener)
        assert rc == git_host_api.EXIT_OK
        out = capsys.readouterr().out
        payload = json.loads(out.strip().splitlines()[-1])
        assert payload["verified_comment_id"] == 42

    def test_posted_body_contains_tool_owned_note_fence(self, monkeypatch):
        self._stdin(monkeypatch, b'{"body":"Status update."}')
        captured = {}

        def fake_opener(req, timeout=15):
            url = req.full_url
            if req.get_method() == "POST" and url.endswith("/comments"):
                posted = json.loads(req.data.decode("utf-8"))
                captured["body"] = posted["body"]
                return _FakeResponse(200, b'{"id": 1}')
            if url.endswith("/api/v1/user"):
                return _FakeResponse(200, b'{"login": "some-role-bot"}')
            return _FakeResponse(200, json.dumps([
                {"id": 1, "html_url": "http://x/1", "user": {"login": "some-role-bot"},
                 "body": captured.get("body", ""), "created_at": "2099-01-01T00:00:01Z"},
            ]).encode("utf-8"))

        rc = git_host_api.main(
            [
                "--caller", "some-role",
                "--body-stdin", "--verify-comment",
                "--caller-tracking-id", "lr-10a996",
                "POST", "/api/v1/repos/o/r/issues/7/comments",
            ],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert captured["body"].startswith("Status update.\n")
        assert "```loadout-note" in captured["body"]
        assert '"caller_tracking_id": "lr-10a996"' in captured["body"]

    def test_composes_on_top_of_verdict_block_when_both_supplied(self, monkeypatch):
        """--expect-verdict-block and --caller-tracking-id stack: the
        tracking-id note trails the verdict fence rather than one flag
        silently overwriting the other's composition."""
        self._stdin(
            monkeypatch,
            b'{"body":"LGTM.","review_status":"clean"}',
        )
        captured = {}

        def fake_opener(req, timeout=15):
            url = req.full_url
            if req.get_method() == "GET" and "/pulls/" in url:
                return _FakeResponse(200, json.dumps({"head": {"sha": _FULL_SHA}}).encode("utf-8"))
            if req.get_method() == "POST" and url.endswith("/comments"):
                posted = json.loads(req.data.decode("utf-8"))
                captured["body"] = posted["body"]
                return _FakeResponse(200, b'{"id": 1}')
            if url.endswith("/api/v1/user"):
                return _FakeResponse(200, b'{"login": "some-reviewer-bot"}')
            return _FakeResponse(200, json.dumps([
                {"id": 1, "html_url": "http://x/1", "user": {"login": "some-reviewer-bot"},
                 "body": captured.get("body", ""), "created_at": "2099-01-01T00:00:01Z"},
            ]).encode("utf-8"))

        rc = git_host_api.main(
            [
                "--caller", "some-reviewer",
                "--body-stdin", "--verify-comment", "--pr-sha", _FULL_SHA,
                "--expect-verdict-block", "some-reviewer",
                "--caller-tracking-id", "lr-10a996",
                "POST", "/api/v1/repos/o/r/issues/9/comments",
            ],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert "```review-result" in captured["body"]
        assert "```loadout-note" in captured["body"]
        assert '"caller_tracking_id": "lr-10a996"' in captured["body"]
        # The note trails the verdict fence, not the other way around.
        assert captured["body"].index("```review-result") < captured["body"].index(
            "```loadout-note"
        )

    def test_no_tracking_id_produces_no_note_fence(self, monkeypatch):
        """Regression guard: an ordinary comment post with no
        --caller-tracking-id must never grow a loadout-note fence it did
        not ask for."""
        self._stdin(monkeypatch, b'{"body":"Plain comment, no tracking id."}')
        opener = self._make_opener()

        rc = git_host_api.main(
            [
                "--caller", "some-role",
                "--body-stdin", "--verify-comment",
                "POST", "/api/v1/repos/o/r/issues/7/comments",
            ],
            token_provider=_RecordingProvider(), opener=opener,
        )
        assert rc == git_host_api.EXIT_OK


# ---------------------------------------------------------------------------
# --body-env (lr-10a996 BODY-TRANSPORT half): body-off-argv-and-pipe via a
# fixed, statically-analyzable staged path -- transport.body_env.
# ---------------------------------------------------------------------------


class TestMainBodyEnvUsageGuards:
    """--body-env and --body-stdin preconditions, checked BEFORE any I/O."""

    def test_both_body_flags_together_refused_before_network(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        monkeypatch.setattr(
            "sys.stdin", type("S", (), {"buffer": io.BytesIO(b'{"body":"x"}')})()
        )

        def fake_opener(req, timeout=15):
            raise AssertionError("no network call should happen on a usage error")

        rc = git_host_api.main(
            [
                "--caller", "some-role",
                "--body-stdin", "--body-env", "--verify-comment",
                "POST", "/api/v1/repos/o/r/issues/7/comments",
            ],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_BODY_INGESTION_USAGE

    def test_missing_staged_file_refused_before_network(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path))

        def fake_opener(req, timeout=15):
            raise AssertionError("no network call should happen when the staged body is missing")

        rc = git_host_api.main(
            [
                "--caller", "some-role",
                "--body-env", "--verify-comment",
                "POST", "/api/v1/repos/o/r/issues/7/comments",
            ],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_BODY_ENV_UNREADABLE


class TestMainBodyEnvEndToEnd:
    """--body-env posts the SAME way --body-stdin does -- only the
    ingestion source differs; validation, verify-comment readback, and the
    resulting posted body are identical."""

    def _stage(
        self,
        tmp_path,
        body: bytes,
        *,
        caller: str = "some-role",
        target_pr: int = 7,
        head_sha: str | None = None,
    ) -> None:
        """Stage a body + identity stamp at the CALLER-NAMESPACED path
        (lr-3a7ae8, stamped lr-becdef) -- `body.<caller>.json` /
        `body.<caller>.stamp.json` -- matching what this class's --caller
        argv value resolves to. *caller* defaults to "some-role", the
        --caller value every test method below (except the verdict-block
        one, which passes its own) supplies. *target_pr* defaults to 7,
        the PR number every test method below (except the verdict-block
        one, which uses PR 9) posts to."""
        from clagentic_loadout.transport.body_env import stage_caller_body

        stage_caller_body(
            caller=caller,
            body_bytes=body,
            target_pr=target_pr,
            head_sha=head_sha,
            env={"TMPDIR": str(tmp_path)},
        )

    def _make_opener(self, *, own_login="some-role-bot"):
        state = {"posted_body": None}

        def fake_opener(req, timeout=15):
            url = req.full_url
            if req.get_method() == "POST" and url.endswith("/comments"):
                posted = json.loads(req.data.decode("utf-8"))
                state["posted_body"] = posted["body"]
                return _FakeResponse(200, b'{"id": 42}')
            if url.endswith("/api/v1/user"):
                return _FakeResponse(200, json.dumps({"login": own_login}).encode("utf-8"))
            if url.endswith("/comments"):
                created = (
                    datetime.now(timezone.utc) + timedelta(seconds=1)
                ).isoformat().replace("+00:00", "Z")
                return _FakeResponse(200, json.dumps([
                    {"id": 42, "html_url": "http://x/42", "user": {"login": own_login},
                     "body": state["posted_body"], "created_at": created},
                ]).encode("utf-8"))
            raise AssertionError(f"unexpected request to {url}")

        return fake_opener

    def test_success_posts_and_verifies_from_staged_file(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        self._stage(tmp_path, b'{"body":"Status update via body-env."}')
        opener = self._make_opener()

        # The invoking argv is a CONSTANT string -- no per-invocation body
        # substring anywhere, unlike a --body-stdin producer's echo '{...}'.
        argv = [
            "--caller", "some-role",
            "--body-env", "--verify-comment",
            "POST", "/api/v1/repos/o/r/issues/7/comments",
        ]
        assert not any("{" in a for a in argv)

        rc = git_host_api.main(argv, token_provider=_RecordingProvider(), opener=opener)
        assert rc == git_host_api.EXIT_OK
        out = capsys.readouterr().out
        payload = json.loads(out.strip().splitlines()[-1])
        assert payload["verified_comment_id"] == 42

    def test_empty_staged_file_rejected_same_as_empty_stdin(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        self._stage(tmp_path, b"")

        def fake_opener(req, timeout=15):
            raise AssertionError("no network call should happen on an empty body")

        rc = git_host_api.main(
            [
                "--caller", "some-role",
                "--body-env", "--verify-comment",
                "POST", "/api/v1/repos/o/r/issues/7/comments",
            ],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_BODY_STDIN_EMPTY

    def test_composes_with_caller_tracking_id_same_as_body_stdin(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        self._stage(tmp_path, b'{"body":"Status update."}')
        captured = {}

        def fake_opener(req, timeout=15):
            url = req.full_url
            if req.get_method() == "POST" and url.endswith("/comments"):
                posted = json.loads(req.data.decode("utf-8"))
                captured["body"] = posted["body"]
                return _FakeResponse(200, b'{"id": 1}')
            if url.endswith("/api/v1/user"):
                return _FakeResponse(200, b'{"login": "some-role-bot"}')
            return _FakeResponse(200, json.dumps([
                {"id": 1, "html_url": "http://x/1", "user": {"login": "some-role-bot"},
                 "body": captured.get("body", ""), "created_at": "2099-01-01T00:00:01Z"},
            ]).encode("utf-8"))

        rc = git_host_api.main(
            [
                "--caller", "some-role",
                "--body-env", "--verify-comment",
                "--caller-tracking-id", "lr-10a996",
                "POST", "/api/v1/repos/o/r/issues/7/comments",
            ],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert "```loadout-note" in captured["body"]

    def test_expect_verdict_block_accepts_body_env_as_precondition(self, monkeypatch, tmp_path):
        """--expect-verdict-block's own usage guard requires SOME
        body-ingestion flag -- --body-env satisfies it exactly like
        --body-stdin does, it is not --body-stdin-specific."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        self._stage(
            tmp_path,
            b'{"body":"LGTM.","review_status":"clean"}',
            caller="some-reviewer",
            target_pr=9,
            head_sha=_FULL_SHA,
        )

        def fake_opener(req, timeout=15):
            url = req.full_url
            if req.get_method() == "GET" and "/pulls/" in url:
                return _FakeResponse(200, json.dumps({"head": {"sha": _FULL_SHA}}).encode("utf-8"))
            if req.get_method() == "POST" and url.endswith("/comments"):
                posted = json.loads(req.data.decode("utf-8"))
                fake_opener.posted_body = posted["body"]
                return _FakeResponse(200, b'{"id": 1}')
            if url.endswith("/api/v1/user"):
                return _FakeResponse(200, b'{"login": "some-reviewer-bot"}')
            return _FakeResponse(200, json.dumps([
                {"id": 1, "html_url": "http://x/1", "user": {"login": "some-reviewer-bot"},
                 "body": getattr(fake_opener, "posted_body", ""),
                 "created_at": "2099-01-01T00:00:01Z"},
            ]).encode("utf-8"))

        rc = git_host_api.main(
            [
                "--caller", "some-reviewer",
                "--body-env", "--verify-comment", "--pr-sha", _FULL_SHA,
                "--expect-verdict-block", "some-reviewer",
                "POST", "/api/v1/repos/o/r/issues/9/comments",
            ],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert "```review-result" in fake_opener.posted_body


class TestMainBodyEnvStaleReadRegression:
    """lr-becdef: PR #388 foreign-body incident regression, exercised
    through this verb's own main() entry point (the Forgejo read call
    site, git_host_api._run). A body staged for one PR must never be
    silently re-read/re-posted for a DIFFERENT PR, and a body must never
    be re-postable twice without re-staging."""

    def _stage(self, tmp_path, body: bytes, *, caller: str, target_pr: int) -> None:
        from clagentic_loadout.transport.body_env import stage_caller_body

        stage_caller_body(
            caller=caller, body_bytes=body, target_pr=target_pr,
            env={"TMPDIR": str(tmp_path)},
        )

    def test_body_staged_for_different_pr_refused_before_network(self, monkeypatch, tmp_path):
        # Direct reproduction of the PR #388 incident shape: a body staged
        # for PR 100 (a prior, unrelated review) must never be read as if
        # it were staged for PR 200.
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        self._stage(
            tmp_path, b'{"body": "BOBBIE - clean: archivist_client.py review."}',
            caller="some-role", target_pr=100,
        )

        def fake_opener(req, timeout=15):
            raise AssertionError("no network call should happen on a stale-PR body mismatch")

        rc = git_host_api.main(
            [
                "--caller", "some-role",
                "--body-env", "--verify-comment",
                "POST", "/api/v1/repos/o/r/issues/200/comments",
            ],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_BODY_ENV_UNREADABLE

    def test_read_twice_without_restaging_fails_closed(self, monkeypatch, tmp_path, capsys):
        # First invocation posts and consumes the staged body; a SECOND
        # invocation with no re-staging step (the exact PR #388 mechanism:
        # a harness's staging write skipped/guard-denied) must fail closed
        # rather than silently re-reading and re-posting the first body.
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        self._stage(tmp_path, b'{"body": "first review."}', caller="some-role", target_pr=7)

        state = {"posted_body": None}

        def fake_opener(req, timeout=15):
            url = req.full_url
            if req.get_method() == "POST" and url.endswith("/comments"):
                posted = json.loads(req.data.decode("utf-8"))
                state["posted_body"] = posted["body"]
                return _FakeResponse(200, b'{"id": 1}')
            if url.endswith("/api/v1/user"):
                return _FakeResponse(200, b'{"login": "some-role-bot"}')
            created = (
                datetime.now(timezone.utc) + timedelta(seconds=1)
            ).isoformat().replace("+00:00", "Z")
            return _FakeResponse(200, json.dumps([
                {"id": 1, "html_url": "http://x/1", "user": {"login": "some-role-bot"},
                 "body": state["posted_body"], "created_at": created},
            ]).encode("utf-8"))

        argv = [
            "--caller", "some-role",
            "--body-env", "--verify-comment",
            "POST", "/api/v1/repos/o/r/issues/7/comments",
        ]

        rc_first = git_host_api.main(argv, token_provider=_RecordingProvider(), opener=fake_opener)
        assert rc_first == git_host_api.EXIT_OK

        def refusing_opener(req, timeout=15):
            raise AssertionError("no network call should happen on the second, unstaged invocation")

        rc_second = git_host_api.main(
            argv, token_provider=_RecordingProvider(), opener=refusing_opener
        )
        assert rc_second == git_host_api.EXIT_BODY_ENV_UNREADABLE


# ---------------------------------------------------------------------------
# --delete-own-comment (lr-e2ce66) -- belt-and-suspenders self-delete: GET
# the comment, assert caller-own authorship, assert no review-result verdict
# fence, THEN DELETE. Covers get_comment / delete_own_comment as library
# functions plus the main() CLI wiring end to end (usage guards, cross-author
# refusal, verdict-fence refusal, happy path).
# ---------------------------------------------------------------------------


_DELETE_COMMENT_PATH = "/api/v1/repos/o/r/issues/comments/123"


class TestGetComment:
    def test_success_returns_parsed_comment(self):
        def fake_opener(req, timeout=15):
            return _FakeResponse(
                200,
                json.dumps({"id": 123, "user": {"login": "some-bot"}, "body": "hi"}).encode("utf-8"),
            )

        comment = git_host_api.get_comment(
            "http://git-host.example.com", "tok", "o", "r", "123", opener=fake_opener,
        )
        assert comment == {"id": 123, "user": {"login": "some-bot"}, "body": "hi"}

    def test_non_200_refused(self):
        def fake_opener(req, timeout=15):
            return _FakeResponse(404, b"{}")

        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.get_comment(
                "http://git-host.example.com", "tok", "o", "r", "123", opener=fake_opener,
            )
        assert exc_info.value.code == git_host_api.EXIT_DELETE_OWN_COMMENT_REFUSED

    def test_unparseable_json_refused(self):
        def fake_opener(req, timeout=15):
            return _FakeResponse(200, b"not json")

        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.get_comment(
                "http://git-host.example.com", "tok", "o", "r", "123", opener=fake_opener,
            )
        assert exc_info.value.code == git_host_api.EXIT_DELETE_OWN_COMMENT_REFUSED

    def test_non_object_json_refused(self):
        def fake_opener(req, timeout=15):
            return _FakeResponse(200, b"[1, 2, 3]")

        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.get_comment(
                "http://git-host.example.com", "tok", "o", "r", "123", opener=fake_opener,
            )
        assert exc_info.value.code == git_host_api.EXIT_DELETE_OWN_COMMENT_REFUSED


class TestDeleteOwnComment:
    def _opener(self, *, comment_login="some-bot", comment_body="plain comment", own_login="some-bot"):
        state = {"deleted": False}

        def fake_opener(req, timeout=15):
            url = req.full_url
            if req.get_method() == "GET" and url.endswith("/api/v1/user"):
                return _FakeResponse(200, json.dumps({"login": own_login}).encode("utf-8"))
            if req.get_method() == "GET" and "/issues/comments/" in url:
                return _FakeResponse(
                    200,
                    json.dumps(
                        {"id": 123, "user": {"login": comment_login}, "body": comment_body}
                    ).encode("utf-8"),
                )
            if req.get_method() == "DELETE":
                state["deleted"] = True
                return _FakeResponse(204, b"")
            raise AssertionError(f"unexpected request to {url}")

        fake_opener.state = state
        return fake_opener

    def test_happy_path_deletes(self):
        opener = self._opener()
        git_host_api.delete_own_comment(
            "http://git-host.example.com", "tok", "o", "r", "123", opener=opener,
        )
        assert opener.state["deleted"] is True

    def test_cross_author_refused_before_delete(self):
        opener = self._opener(comment_login="a-different-bot", own_login="some-bot")
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.delete_own_comment(
                "http://git-host.example.com", "tok", "o", "r", "123", opener=opener,
            )
        assert exc_info.value.code == git_host_api.EXIT_DELETE_OWN_COMMENT_REFUSED
        assert opener.state["deleted"] is False

    def test_verdict_fence_present_refused_before_delete_even_when_own_comment(self):
        fence = git_host_api.build_verdict_block("some-reviewer", "clean", _FULL_SHA, 5)
        opener = self._opener(comment_login="some-bot", own_login="some-bot", comment_body=f"LGTM.\n{fence}")
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.delete_own_comment(
                "http://git-host.example.com", "tok", "o", "r", "123", opener=opener,
            )
        assert exc_info.value.code == git_host_api.EXIT_DELETE_OWN_COMMENT_REFUSED
        assert opener.state["deleted"] is False

    def test_unreadable_comment_refused_before_delete(self):
        def fake_opener(req, timeout=15):
            if req.get_method() == "GET" and "/issues/comments/" in req.full_url:
                return _FakeResponse(404, b"{}")
            raise AssertionError("DELETE/user must never be reached when the comment GET fails")

        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.delete_own_comment(
                "http://git-host.example.com", "tok", "o", "r", "123", opener=fake_opener,
            )
        assert exc_info.value.code == git_host_api.EXIT_DELETE_OWN_COMMENT_REFUSED


class TestMainDeleteOwnCommentUsageGuards:
    """Preflight refusals BEFORE any I/O -- mirrors
    TestMainCommentsPostRequiresVerify's never-touches-network pattern."""

    def test_delete_without_flag_is_refused(self):
        called = {"n": False}

        def fake_opener(req, timeout=15):
            called["n"] = True
            return _FakeResponse(200, b"{}")

        rc = git_host_api.main(
            ["DELETE", _DELETE_COMMENT_PATH],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_DELETE_OWN_COMMENT_REFUSED
        assert called["n"] is False

    def test_flag_on_wrong_endpoint_is_refused(self):
        called = {"n": False}

        def fake_opener(req, timeout=15):
            called["n"] = True
            return _FakeResponse(200, b"{}")

        rc = git_host_api.main(
            ["--delete-own-comment", "GET", "/api/v1/repos/o/r/pulls/1.diff"],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_DELETE_OWN_COMMENT_REFUSED
        assert called["n"] is False

    def test_flag_on_post_comments_is_refused(self):
        """--delete-own-comment only makes sense on a DELETE to the
        single-comment endpoint -- it never combines with a comments POST.
        The --verify-comment-required check for a comments POST runs FIRST
        (checked earlier in _run's preflight ordering) and fires before
        --delete-own-comment's own usage guard gets a chance to -- still a
        hard refusal before any I/O either way, just a different exit code
        naming the FIRST contract violated."""
        rc = git_host_api.main(
            ["--delete-own-comment", "POST", "/api/v1/repos/o/r/issues/5/comments"],
            token_provider=_RecordingProvider(),
        )
        assert rc == git_host_api.EXIT_VERIFY_COMMENT_REQUIRED

    def test_delete_own_comment_does_not_require_body_ingestion_flag(self):
        """A comment DELETE sends no request body -- unlike every other
        write method, --delete-own-comment must NOT trip the mutually-
        exclusive/required body-ingestion usage guard."""

        def fake_opener(req, timeout=15):
            url = req.full_url
            if req.get_method() == "GET" and url.endswith("/api/v1/user"):
                return _FakeResponse(200, b'{"login": "some-bot"}')
            if req.get_method() == "GET" and "/issues/comments/" in url:
                return _FakeResponse(
                    200,
                    json.dumps({"id": 123, "user": {"login": "some-bot"}, "body": "hi"}).encode("utf-8"),
                )
            return _FakeResponse(204, b"")

        rc = git_host_api.main(
            ["--delete-own-comment", "DELETE", _DELETE_COMMENT_PATH],
            token_provider=_RecordingProvider(), opener=fake_opener,
        )
        assert rc == git_host_api.EXIT_OK


class TestMainDeleteOwnCommentEndToEnd:
    def _make_opener(self, *, comment_login="some-bot", own_login="some-bot", comment_body="plain comment"):
        state = {"deleted": False}

        def fake_opener(req, timeout=15):
            url = req.full_url
            if req.get_method() == "GET" and url.endswith("/api/v1/user"):
                return _FakeResponse(200, json.dumps({"login": own_login}).encode("utf-8"))
            if req.get_method() == "GET" and "/issues/comments/" in url:
                return _FakeResponse(
                    200,
                    json.dumps(
                        {"id": 123, "user": {"login": comment_login}, "body": comment_body}
                    ).encode("utf-8"),
                )
            if req.get_method() == "DELETE":
                state["deleted"] = True
                return _FakeResponse(204, b"")
            raise AssertionError(f"unexpected request to {url}")

        fake_opener.state = state
        return fake_opener

    def test_happy_path_reports_deleted_id(self, capsys):
        opener = self._make_opener()
        rc = git_host_api.main(
            ["--caller", "some-role", "--delete-own-comment", "DELETE", _DELETE_COMMENT_PATH],
            token_provider=_RecordingProvider(), opener=opener,
        )
        assert rc == git_host_api.EXIT_OK
        assert opener.state["deleted"] is True
        out = capsys.readouterr().out
        payload = json.loads(out.strip().splitlines()[-1])
        assert payload["deleted_comment_id"] == 123

    def test_cross_author_end_to_end_refused(self):
        opener = self._make_opener(comment_login="a-different-bot", own_login="some-bot")
        rc = git_host_api.main(
            ["--delete-own-comment", "DELETE", _DELETE_COMMENT_PATH],
            token_provider=_RecordingProvider(), opener=opener,
        )
        assert rc == git_host_api.EXIT_DELETE_OWN_COMMENT_REFUSED
        assert opener.state["deleted"] is False

    def test_verdict_fence_end_to_end_refused(self):
        fence = git_host_api.build_verdict_block("some-reviewer", "blocking", _FULL_SHA, 5)
        opener = self._make_opener(comment_body=f"Found an issue.\n{fence}")
        rc = git_host_api.main(
            ["--delete-own-comment", "DELETE", _DELETE_COMMENT_PATH],
            token_provider=_RecordingProvider(), opener=opener,
        )
        assert rc == git_host_api.EXIT_DELETE_OWN_COMMENT_REFUSED
        assert opener.state["deleted"] is False

    def test_known_bad_owner_still_enforced_on_delete_path(self):
        """The existing known-bad-owner check (_validate_owner, checked from
        the path match before any method-specific dispatch) applies to the
        delete-own-comment path exactly like every other endpoint -- no new
        bypass introduced."""
        rc = git_host_api.main(
            ["--delete-own-comment", "DELETE", "/api/v1/repos/bad-owner/r/issues/comments/123"],
            token_provider=_RecordingProvider(),
            known_bad_owners=frozenset({"bad-owner"}),
        )
        assert rc == git_host_api.EXIT_OWNER_REPO_NOT_FOUND
