"""
test_release_dispatch.py — tests for clagentic_loadout.release.dispatch
(lr-51d4, Wave A slice 6, ported from an internal deployment's own
lr-7360/T4 lineage).

Coverage:
  - is_valid_status_hook_url: SSRF hardening (lr-f176 lineage).
  - parse_trailers: same-repo GitHub case — extracts Task:/Closes # trailers
    from a merged PR body; genuine no-issue and no-task cases return None,
    never raise.
  - build_status_hook_payload: {task_id, dispatcher?, status: "shipped",
    version?}; optional fields omitted when absent, never sent as null.
  - sign_payload: HMAC-SHA256 "sha256=<hex>", verified byte-identical against
    a hand-rolled reference computation.
  - fire_status_hook: success path (200), unknown_task_id/ignored treated as
    non-error no-ops, non-2xx and network errors fail closed with
    EXIT_HOOK_FAILED. Configurable signature header. Redirects are never
    followed (BOBBIE bobbie.sast.7) — a 3xx surfaces as a hook failure
    rather than replaying the signed payload to the Location host.
  - resolve_hook_secret: --secret-env-var direct-read path and the
    role-scoped .env self-fetch path (via secrets_config.read_role_env_file,
    monkeypatched here — not re-testing secrets_config's own file-permission
    logic, covered in test_release_secrets_config.py).
  - CLI wiring: --task-id vs --merged-pr-body-stdin mutual exclusivity;
    no-task-trailer and no-issue-trailer are no-ops, never fail closed.
    --merged-pr-body-stdin reads body text from stdin only — no caller-
    supplied path (lr-aa849d: the third instance of the class PR #136
    eliminated for loadout-push/loadout-stage-body; the fix here is the same
    elimination, not a containment check on a path).
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import urllib.error

import pytest

from clagentic_loadout.release import dispatch


# ---------------------------------------------------------------------------
# is_valid_status_hook_url — SSRF hardening.
# ---------------------------------------------------------------------------


class TestIsValidStatusHookUrl:
    def test_https_with_host_is_valid(self):
        assert dispatch.is_valid_status_hook_url(
            "https://triage.example.com:8743/status-hook"
        ) is True

    def test_http_with_host_is_valid(self):
        assert dispatch.is_valid_status_hook_url(
            "http://triage.example.com:8743/status-hook"
        ) is True

    def test_file_scheme_is_rejected(self):
        assert dispatch.is_valid_status_hook_url("file:///etc/passwd") is False

    def test_no_scheme_is_rejected(self):
        assert dispatch.is_valid_status_hook_url(
            "triage.example.com/status-hook"
        ) is False

    def test_no_host_is_rejected(self):
        assert dispatch.is_valid_status_hook_url("https:///status-hook") is False

    def test_javascript_scheme_is_rejected(self):
        assert dispatch.is_valid_status_hook_url("javascript:alert(1)") is False


# ---------------------------------------------------------------------------
# parse_trailers
# ---------------------------------------------------------------------------


class TestParseTrailers:
    def test_extracts_both_trailers(self):
        body = "## Summary\nDid a thing.\n\nTask: proj-a68f\nCloses #263\n"
        task_id, issue = dispatch.parse_trailers(body)
        assert task_id == "proj-a68f"
        assert issue == 263

    def test_no_closes_trailer_is_none_not_error(self):
        """Genuine no-linked-issue case — never raises."""
        body = "## Summary\nDid a thing.\n\nTask: proj-1234\n"
        task_id, issue = dispatch.parse_trailers(body)
        assert task_id == "proj-1234"
        assert issue is None

    def test_no_task_trailer_is_none(self):
        body = "## Summary\nDid a thing.\n\nCloses #42\n"
        task_id, issue = dispatch.parse_trailers(body)
        assert task_id is None
        assert issue == 42

    def test_empty_body_returns_none_none(self):
        assert dispatch.parse_trailers("") == (None, None)

    def test_colon_form_does_not_match_closes(self):
        """The 'Closes: #NN' colon form is not the keyword-linking grammar
        and must not be treated as a match."""
        body = "Task: proj-9999\nCloses: #42\n"
        task_id, issue = dispatch.parse_trailers(body)
        assert task_id == "proj-9999"
        assert issue is None

    def test_case_insensitive_trailer_keys(self):
        body = "task: proj-abcd\nCLOSES #7\n"
        task_id, issue = dispatch.parse_trailers(body)
        assert task_id == "proj-abcd"
        assert issue == 7

    def test_task_id_is_opaque_any_shape(self):
        """task_id is an opaque work-item ref, not a validated lr-XXXX shape
        (tome #687 §11.3) — any non-whitespace token matches."""
        body = "Task: ANY-TOKEN.123\n"
        task_id, _issue = dispatch.parse_trailers(body)
        assert task_id == "ANY-TOKEN.123"


# ---------------------------------------------------------------------------
# build_status_hook_payload
# ---------------------------------------------------------------------------


class TestBuildStatusHookPayload:
    def test_full_payload_matches_contract(self):
        payload = dispatch.build_status_hook_payload(
            "proj-a68f", dispatcher="some-dispatcher", version="0.9.0-beta.3"
        )
        assert payload == {
            "task_id": "proj-a68f",
            "dispatcher": "some-dispatcher",
            "status": "shipped",
            "version": "0.9.0-beta.3",
        }

    def test_omits_optional_fields_when_absent(self):
        payload = dispatch.build_status_hook_payload("proj-a68f")
        assert payload == {"task_id": "proj-a68f", "status": "shipped"}
        assert "dispatcher" not in payload
        assert "version" not in payload

    def test_status_is_always_shipped(self):
        payload = dispatch.build_status_hook_payload("proj-x")
        assert payload["status"] == "shipped"


# ---------------------------------------------------------------------------
# sign_payload — HMAC-SHA256
# ---------------------------------------------------------------------------


class TestSignPayload:
    def test_signature_matches_reference_hmac(self):
        secret = "hook-secret"
        raw_body = b'{"task_id":"proj-a68f","status":"shipped"}'
        expected = "sha256=" + hmac.new(
            secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        assert dispatch.sign_payload(secret, raw_body) == expected

    def test_signature_is_deterministic(self):
        secret = "s3cr3t"
        body = b'{"a":1}'
        assert dispatch.sign_payload(secret, body) == dispatch.sign_payload(secret, body)

    def test_different_secrets_produce_different_signatures(self):
        body = b'{"a":1}'
        sig1 = dispatch.sign_payload("secret-one", body)
        sig2 = dispatch.sign_payload("secret-two", body)
        assert sig1 != sig2


# ---------------------------------------------------------------------------
# fire_status_hook
# ---------------------------------------------------------------------------


class _FakeHTTPResponse:
    def __init__(self, status: int, body: dict):
        self.status = status
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestFireStatusHook:
    """fire_status_hook's *opener* parameter is injected directly (matching
    clagentic_loadout.telemetry.sink.WebhookSink's own test convention)
    rather than monkeypatching urllib.request.urlopen — fire_status_hook
    builds its own no-redirect OpenerDirector internally (BOBBIE
    bobbie.sast.7) rather than calling the module-level urlopen, so a
    monkeypatch of urllib.request.urlopen would silently stop intercepting
    the call."""

    def test_success_returns_status_and_body(self):
        captured = {}

        def fake_opener(req, timeout=15):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["data"] = req.data
            return _FakeHTTPResponse(200, {"status": "ok", "posted": True, "labeled": True})

        status, body = dispatch.fire_status_hook(
            "http://example.com/status-hook",
            {"task_id": "proj-a68f", "status": "shipped"},
            "sekret",
            opener=fake_opener,
        )
        assert status == 200
        assert body == {"status": "ok", "posted": True, "labeled": True}
        assert "X-clagentic-signature" in captured["headers"] or "X-Clagentic-Signature" in captured["headers"]

    def test_custom_signature_header_is_used(self):
        """The signature header name is configurable per deployment contract
        (tome #687 §12 disposition) — a caller may override the default."""
        captured = {}

        def fake_opener(req, timeout=15):
            captured["headers"] = dict(req.header_items())
            return _FakeHTTPResponse(200, {"status": "ok"})

        dispatch.fire_status_hook(
            "http://example.com/status-hook",
            {"task_id": "proj-a68f", "status": "shipped"},
            "sekret",
            signature_header="x-custom-signature",
            opener=fake_opener,
        )
        header_names = {name.lower() for name in captured["headers"]}
        assert "x-custom-signature" in header_names
        assert "x-clagentic-signature" not in header_names

    def test_unknown_task_id_is_not_an_error(self):
        """Documented safe no-op — must return normally, not raise."""

        def fake_opener(req, timeout=15):
            return _FakeHTTPResponse(200, {"status": "unknown_task_id"})

        status, body = dispatch.fire_status_hook(
            "http://example.com/status-hook",
            {"task_id": "does-not-exist", "status": "shipped"},
            "sekret",
            opener=fake_opener,
        )
        assert status == 200
        assert body["status"] == "unknown_task_id"

    def test_ignored_unsupported_status_is_not_an_error(self):
        def fake_opener(req, timeout=15):
            return _FakeHTTPResponse(200, {"status": "ignored", "reason": "unsupported_status"})

        status, body = dispatch.fire_status_hook(
            "http://example.com/status-hook",
            {"task_id": "proj-x", "status": "shipped"},
            "sekret",
            opener=fake_opener,
        )
        assert status == 200
        assert body["status"] == "ignored"

    def test_http_error_fails_closed(self):
        def fake_opener(req, timeout=15):
            raise urllib.error.HTTPError(
                req.full_url, 401, "unauthorized", {}, io.BytesIO(b'{"error":"unauthorized"}')
            )

        with pytest.raises(SystemExit) as exc_info:
            dispatch.fire_status_hook(
                "http://example.com/status-hook",
                {"task_id": "proj-x", "status": "shipped"},
                "sekret",
                opener=fake_opener,
            )
        assert exc_info.value.code == dispatch.EXIT_HOOK_FAILED

    def test_network_error_fails_closed(self):
        def fake_opener(req, timeout=15):
            raise urllib.error.URLError("connection refused")

        with pytest.raises(SystemExit) as exc_info:
            dispatch.fire_status_hook(
                "http://unreachable.example.com/status-hook",
                {"task_id": "proj-x", "status": "shipped"},
                "sekret",
                opener=fake_opener,
            )
        assert exc_info.value.code == dispatch.EXIT_HOOK_FAILED

    def test_out_of_scheme_url_is_rejected_before_urlopen(self):
        """SSRF hardening: fire_status_hook itself must reject a non-http(s)
        scheme -- e.g. file:// -- for EVERY caller. The opener must never be
        reached."""
        called = {"urlopen": False}

        def fake_opener(req, timeout=15):
            called["urlopen"] = True
            return _FakeHTTPResponse(200, {"status": "ok"})

        with pytest.raises(SystemExit) as exc_info:
            dispatch.fire_status_hook(
                "file:///etc/passwd",
                {"task_id": "proj-x", "status": "shipped"},
                "sekret",
                opener=fake_opener,
            )
        assert exc_info.value.code == dispatch.EXIT_USAGE
        assert called["urlopen"] is False

    def test_out_of_host_url_is_rejected_before_urlopen(self):
        """A scheme-valid but hostless URL (e.g. https:///status-hook) is
        also rejected -- the opener must never be reached."""
        called = {"urlopen": False}

        def fake_opener(req, timeout=15):
            called["urlopen"] = True
            return _FakeHTTPResponse(200, {"status": "ok"})

        with pytest.raises(SystemExit) as exc_info:
            dispatch.fire_status_hook(
                "https:///status-hook",
                {"task_id": "proj-x", "status": "shipped"},
                "sekret",
                opener=fake_opener,
            )
        assert exc_info.value.code == dispatch.EXIT_USAGE
        assert called["urlopen"] is False

    def test_valid_https_url_still_passes(self):
        """A well-formed https URL with a host must still succeed -- the
        SSRF guard must not reject legitimate calls."""

        def fake_opener(req, timeout=15):
            return _FakeHTTPResponse(200, {"status": "ok"})

        status, body = dispatch.fire_status_hook(
            "https://triage.example.com:8743/status-hook",
            {"task_id": "proj-x", "status": "shipped"},
            "sekret",
            opener=fake_opener,
        )
        assert status == 200
        assert body == {"status": "ok"}

    def test_default_opener_never_follows_redirects(self, monkeypatch):
        """No opener injected: fire_status_hook must build its own
        no-redirect opener internally rather than falling back to
        urllib.request.urlopen's default redirect-following behavior
        (BOBBIE bobbie.sast.7). Verified by asserting the opener actually
        constructed uses _NoRedirectHandler, without performing any real
        network call."""
        captured = {}

        class _FakeOpenerDirector:
            def open(self, req, timeout=15):
                captured["req"] = req
                return _FakeHTTPResponse(200, {"status": "ok"})

        def fake_build_opener(*handlers):
            captured["handlers"] = handlers
            return _FakeOpenerDirector()

        monkeypatch.setattr(dispatch.urllib.request, "build_opener", fake_build_opener)

        status, _body = dispatch.fire_status_hook(
            "http://example.com/status-hook",
            {"task_id": "proj-x", "status": "shipped"},
            "sekret",
        )
        assert status == 200
        assert dispatch._NoRedirectHandler in captured["handlers"]

    def test_redirect_response_fails_closed_without_second_request(self):
        """A 3xx from the configured status-hook URL must not be followed:
        exactly one request reaches the originally-configured host, the
        redirect Location is never contacted, and the failure is reported
        via the ordinary EXIT_HOOK_FAILED path (BOBBIE bobbie.sast.7) —
        never a silent swallow."""
        request_log = []

        def fake_opener(req, timeout=15):
            # Record every request this fake opener actually receives so the
            # test can assert there was exactly one, and that it targeted
            # the originally-configured host (never a redirect Location).
            request_log.append(req.full_url)
            # A real (redirect-following) urlopen would raise HTTPError here
            # only if the *final* hop failed; a NO-redirect opener instead
            # raises HTTPError for the 3xx itself, without ever dispatching
            # a second request. This fake models that contract directly.
            raise urllib.error.HTTPError(
                req.full_url,
                302,
                "Found",
                {"Location": "https://attacker.example.net/collect"},
                io.BytesIO(b""),
            )

        with pytest.raises(SystemExit) as exc_info:
            dispatch.fire_status_hook(
                "http://example.com/status-hook",
                {"task_id": "proj-x", "status": "shipped"},
                "sekret",
                opener=fake_opener,
            )
        assert exc_info.value.code == dispatch.EXIT_HOOK_FAILED
        # Exactly one request was made, and only to the configured host —
        # the signed payload/signature header were never resent to the
        # redirect Location.
        assert request_log == ["http://example.com/status-hook"]


# ---------------------------------------------------------------------------
# resolve_hook_secret
# ---------------------------------------------------------------------------


class TestResolveHookSecret:
    def test_secret_env_var_direct_read(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET_VAR", "direct-secret")
        secret = dispatch.resolve_hook_secret(
            secret_env_caller=None, secret_env_var="MY_SECRET_VAR"
        )
        assert secret == "direct-secret"

    def test_secret_env_var_empty_fails_closed(self, monkeypatch):
        monkeypatch.delenv("EMPTY_VAR", raising=False)
        with pytest.raises(SystemExit) as exc_info:
            dispatch.resolve_hook_secret(secret_env_caller=None, secret_env_var="EMPTY_VAR")
        assert exc_info.value.code == dispatch.EXIT_SECRET_FAILED

    def test_default_role_self_fetches_via_secrets_config(self, monkeypatch):
        captured = {}

        def fake_read_role_env_file(role, required_keys):
            captured["role"] = role
            captured["required_keys"] = required_keys
            return {"STATUS_HOOK_SECRET": "fetched-secret"}

        monkeypatch.setattr(dispatch, "read_role_env_file", fake_read_role_env_file)

        secret = dispatch.resolve_hook_secret(secret_env_caller=None, secret_env_var=None)
        assert secret == "fetched-secret"
        assert captured["role"] == dispatch.DEFAULT_ROLE
        assert captured["required_keys"] == ("STATUS_HOOK_SECRET",)

    def test_explicit_role_overrides_default(self, monkeypatch):
        captured = {}

        def fake_read_role_env_file(role, required_keys):
            captured["role"] = role
            return {"STATUS_HOOK_SECRET": "x"}

        monkeypatch.setattr(dispatch, "read_role_env_file", fake_read_role_env_file)

        dispatch.resolve_hook_secret(secret_env_caller="merger", secret_env_var=None)
        assert captured["role"] == "merger"

    def test_secrets_config_error_fails_closed(self, monkeypatch):
        from clagentic_loadout.release.secrets_config import SecretEnvError

        def fake_read_role_env_file(role, required_keys):
            raise SecretEnvError("file not found")

        monkeypatch.setattr(dispatch, "read_role_env_file", fake_read_role_env_file)

        with pytest.raises(SystemExit) as exc_info:
            dispatch.resolve_hook_secret(secret_env_caller=None, secret_env_var=None)
        assert exc_info.value.code == dispatch.EXIT_SECRET_FAILED


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestCLI:
    def _patch_common(self, monkeypatch):
        monkeypatch.setattr(
            dispatch, "resolve_hook_secret", lambda **kwargs: "test-secret"
        )
        captured = {}

        def fake_fire(url, payload, secret, timeout=15, signature_header=dispatch.DEFAULT_SIGNATURE_HEADER):
            captured["url"] = url
            captured["payload"] = payload
            captured["secret"] = secret
            return 200, {"status": "ok", "posted": True, "labeled": True}

        monkeypatch.setattr(dispatch, "fire_status_hook", fake_fire)
        return captured

    def test_explicit_task_id_manual_case(self, monkeypatch):
        captured = self._patch_common(monkeypatch)
        rc = dispatch.main(
            [
                "--task-id", "proj-a68f",
                "--version", "0.9.0-beta.3",
                "--status-hook-url", "http://triage.example.com:8743/status-hook",
                "--dispatcher", "some-dispatcher",
            ]
        )
        assert rc == dispatch.EXIT_OK
        assert captured["payload"] == {
            "task_id": "proj-a68f",
            "dispatcher": "some-dispatcher",
            "status": "shipped",
            "version": "0.9.0-beta.3",
        }

    def test_merged_pr_body_stdin_extracts_task_id(self, monkeypatch):
        captured = self._patch_common(monkeypatch)
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO("## Summary\nShipped.\n\nTask: proj-a68f\nCloses #263\n"),
        )

        rc = dispatch.main(
            [
                "--merged-pr-body-stdin",
                "--version", "1.0.0",
                "--status-hook-url", "http://triage.example.com:8743/status-hook",
            ]
        )
        assert rc == dispatch.EXIT_OK
        assert captured["payload"]["task_id"] == "proj-a68f"

    def test_merged_pr_body_no_task_trailer_is_noop(self, monkeypatch):
        """No Task: trailer at all — nothing to dispatch, never fails closed."""
        captured = self._patch_common(monkeypatch)
        monkeypatch.setattr(
            "sys.stdin", io.StringIO("## Summary\nNo trailers here.\n")
        )

        rc = dispatch.main(
            [
                "--merged-pr-body-stdin",
                "--status-hook-url", "http://triage.example.com:8743/status-hook",
            ]
        )
        assert rc == dispatch.EXIT_OK
        assert "payload" not in captured  # fire_status_hook was never called

    def test_merged_pr_body_no_closes_trailer_still_fires(self, monkeypatch):
        """Genuine no-linked-issue case: still fire using task_id alone."""
        captured = self._patch_common(monkeypatch)
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO("## Summary\nInternal-only change.\n\nTask: proj-9999\n"),
        )

        rc = dispatch.main(
            [
                "--merged-pr-body-stdin",
                "--status-hook-url", "http://triage.example.com:8743/status-hook",
            ]
        )
        assert rc == dispatch.EXIT_OK
        assert captured["payload"]["task_id"] == "proj-9999"

    def test_no_path_parameter_exists_for_pr_body_source(self):
        """lr-aa849d: --merged-pr-body-file (a caller-supplied path read with
        no containment check) is REMOVED, not validated -- mirrors PR #136's
        elimination of loadout-push/loadout-stage-body's --body-file. This
        asserts the flag is actually gone, not merely undocumented."""
        with pytest.raises(SystemExit) as exc_info:
            dispatch.main(
                [
                    "--merged-pr-body-file", "/etc/passwd",
                    "--status-hook-url", "http://triage.example.com:8743/status-hook",
                ]
            )
        assert exc_info.value.code == 2  # argparse: unrecognized argument

    def test_task_id_and_pr_body_stdin_are_mutually_exclusive(self, monkeypatch):
        self._patch_common(monkeypatch)
        monkeypatch.setattr("sys.stdin", io.StringIO("Task: proj-1\n"))
        with pytest.raises(SystemExit) as exc_info:
            dispatch.main(
                [
                    "--task-id", "proj-a68f",
                    "--merged-pr-body-stdin",
                    "--status-hook-url", "http://triage.example.com:8743/status-hook",
                ]
            )
        assert exc_info.value.code == 2  # argparse usage error

    def test_missing_task_source_fails_usage(self, monkeypatch):
        self._patch_common(monkeypatch)
        with pytest.raises(SystemExit) as exc_info:
            dispatch.main(
                ["--status-hook-url", "http://triage.example.com:8743/status-hook"]
            )
        assert exc_info.value.code == 2  # argparse: required mutually-exclusive group

    def test_no_dispatcher_default_is_none(self, monkeypatch):
        """The 'lore' literal is gone — no dispatcher default is assumed
        (tome #687 §12); an omitted --dispatcher yields None, not a baked-in
        deployment name."""
        captured = self._patch_common(monkeypatch)
        dispatch.main(
            [
                "--task-id", "proj-a68f",
                "--status-hook-url", "http://triage.example.com:8743/status-hook",
            ]
        )
        assert "dispatcher" not in captured["payload"]
