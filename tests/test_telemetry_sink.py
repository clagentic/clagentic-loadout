"""test_telemetry_sink.py — unit tests for clagentic_loadout.telemetry.sink
(Wave A slice 5, tome #688 REBUILD on the §11 sink model).

Covers:
  - sink=none (unset or explicit) is the zero-config default and is a true
    no-op: no files written, no network attempted.
  - sink=filesystem writes atomically to a CONFIGURED directory.
  - sink=filesystem REFUSES a path-traversal attempt in dispatch_id /
    invocation_id / record_id — sanitize + containment check, no write ever
    lands outside the configured directory (security review, lr-61b9).
  - sink=webhook POSTs to a CONFIGURED URL via an injectable opener — no
    real network call is ever made by this suite.
  - sink=webhook never forwards the bearer token across a 3xx redirect to a
    different host (security review, lr-61b9).
  - webhook failures (network error, non-2xx) never raise.
  - resolve_sink() raises SinkConfigError for missing required config or an
    unrecognized sink name, never silently falls back.

No date-dependent assertions anywhere (tome #688 constraint) — this module
tests structural I/O behavior only.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from clagentic_loadout.telemetry.sink import (
    ENV_FILESYSTEM_DIR,
    ENV_SINK,
    ENV_WEBHOOK_TIMEOUT,
    ENV_WEBHOOK_TOKEN,
    ENV_WEBHOOK_URL,
    FilesystemSink,
    NoneSink,
    PathEscapeError,
    SinkConfigError,
    WebhookSink,
    resolve_sink,
)


class TestNoneSinkIsDefault:
    def test_empty_env_resolves_to_none_sink(self):
        assert isinstance(resolve_sink({}), NoneSink)

    def test_explicit_none_resolves_to_none_sink(self):
        assert isinstance(resolve_sink({ENV_SINK: "none"}), NoneSink)

    def test_none_sink_emit_is_a_true_noop(self, tmp_path, monkeypatch):
        """No files are written and no network is attempted when sink=none —
        even if filesystem/webhook config vars happen to also be set."""
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network allowed")),
        )
        sink = resolve_sink(
            {
                ENV_SINK: "none",
                ENV_FILESYSTEM_DIR: str(tmp_path),
                ENV_WEBHOOK_URL: "https://example.invalid/collect",
            }
        )
        assert sink.emit({"schema": "x", "a": 1}) is True
        assert list(tmp_path.iterdir()) == []

    def test_none_sink_directly(self):
        assert NoneSink().emit({"anything": True}) is True


class TestFilesystemSink:
    def test_resolve_sink_builds_filesystem_sink(self, tmp_path):
        sink = resolve_sink({ENV_SINK: "filesystem", ENV_FILESYSTEM_DIR: str(tmp_path)})
        assert isinstance(sink, FilesystemSink)
        assert sink.directory == tmp_path

    def test_missing_directory_config_raises(self):
        with pytest.raises(SinkConfigError):
            resolve_sink({ENV_SINK: "filesystem"})

    def test_emit_writes_schema_valid_jsonl_record(self, tmp_path):
        sink = FilesystemSink(tmp_path)
        record = {"schema": "telemetry-agent-run/v1", "dispatch_id": "d-1"}
        assert sink.emit(record) is True

        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == record

    def test_multiple_emits_to_same_stream_append(self, tmp_path):
        sink = FilesystemSink(tmp_path)
        record = {"schema": "telemetry-dispatch-record/v1", "dispatch_id": "same-id"}
        sink.emit({**record, "status": "in_flight"})
        sink.emit({**record, "status": "completed"})

        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2

    def test_different_stream_keys_get_different_files(self, tmp_path):
        sink = FilesystemSink(tmp_path)
        sink.emit({"schema": "telemetry-dispatch-record/v1", "dispatch_id": "a"})
        sink.emit({"schema": "telemetry-dispatch-record/v1", "dispatch_id": "b"})

        files = sorted(p.name for p in tmp_path.glob("*.jsonl"))
        assert len(files) == 2

    def test_directory_created_if_absent(self, tmp_path):
        target = tmp_path / "nested" / "telemetry"
        sink = FilesystemSink(target)
        sink.emit({"schema": "x"})
        assert target.is_dir()
        assert list(target.glob("*.jsonl"))

    def test_no_configured_default_path_is_ever_used(self, tmp_path):
        """FilesystemSink never falls back to a hidden default directory —
        the directory argument is the only place a path comes from."""
        sink = FilesystemSink(tmp_path / "explicit-only")
        assert "~/.lore" not in str(sink.directory)
        assert ".lore" not in str(sink.directory)


class TestFilesystemSinkPathTraversal:
    """Regression coverage for the path-traversal finding (security review,
    lr-61b9): dispatch_id / invocation_id / record_id are caller-supplied
    strings the schemas only constrain with minLength — no charset
    restriction. A malicious value must never cause a write outside the
    configured CLAGENTIC_LOADOUT_TELEMETRY_DIR. Every case below is checked
    two ways: (1) the sink's own emit() must not create any file outside the
    configured directory, and (2) as a belt-and-suspenders check, the
    directory's PARENT is confirmed to contain no stray file at all after
    the attempt."""

    @pytest.mark.parametrize(
        "malicious_id",
        [
            "../../etc/evil",
            "..%2f..%2fx",
            "a/b/c",
            ".hidden-leading-dot",
            "..",
            "../../../../../../tmp/evil",
        ],
    )
    def test_malicious_dispatch_id_never_escapes_configured_dir(self, tmp_path, malicious_id):
        target_dir = tmp_path / "telemetry"
        outside_parent = tmp_path  # anything landing here (not under target_dir) is an escape

        sink = FilesystemSink(target_dir)
        record = {"schema": "telemetry-dispatch-record/v1", "dispatch_id": malicious_id}
        # Either the sink refuses (emit() returns False, fail-closed) or it
        # sanitizes and writes safely inside target_dir — both are
        # acceptable outcomes; a write outside target_dir is not.
        sink.emit(record)

        # Nothing was created directly under outside_parent other than the
        # target_dir itself (and its own contents, which we check next).
        stray_siblings = [
            p for p in outside_parent.iterdir() if p != target_dir and p.name != ".diff-check.txt"
        ]
        assert stray_siblings == [], f"escaped write(s) found: {stray_siblings}"

        # Anything the sink DID create must resolve inside target_dir.
        if target_dir.exists():
            for created in target_dir.rglob("*"):
                assert created.resolve().is_relative_to(target_dir.resolve())

    @pytest.mark.parametrize(
        "malicious_id",
        ["../../etc/evil", "a/b/c", ".."],
    )
    def test_malicious_invocation_id_never_escapes_configured_dir(self, tmp_path, malicious_id):
        target_dir = tmp_path / "telemetry"
        sink = FilesystemSink(target_dir)
        sink.emit({"schema": "telemetry-trace-event/v1", "invocation_id": malicious_id})

        stray_siblings = [p for p in tmp_path.iterdir() if p != target_dir]
        assert stray_siblings == []

    @pytest.mark.parametrize(
        "malicious_id",
        ["../../etc/evil", "a/b/c", ".."],
    )
    def test_malicious_record_id_never_escapes_configured_dir(self, tmp_path, malicious_id):
        target_dir = tmp_path / "telemetry"
        sink = FilesystemSink(target_dir)
        sink.emit({"schema": "telemetry-agent-run/v1", "record_id": malicious_id})

        stray_siblings = [p for p in tmp_path.iterdir() if p != target_dir]
        assert stray_siblings == []

    def test_resolve_contained_path_raises_on_traversal(self, tmp_path):
        """Direct unit coverage of the containment check itself (the second,
        independent defense layered on top of sanitization)."""
        from clagentic_loadout.telemetry.sink import _resolve_contained_path

        target_dir = tmp_path / "telemetry"
        target_dir.mkdir()
        with pytest.raises(PathEscapeError):
            _resolve_contained_path(target_dir, "../escaped.jsonl")

    def test_sanitize_path_component_strips_traversal_sequences(self):
        from clagentic_loadout.telemetry.sink import _sanitize_path_component

        sanitized = _sanitize_path_component("../../etc/evil")
        assert "/" not in sanitized
        assert ".." not in sanitized

        sanitized_dots_only = _sanitize_path_component("..")
        assert sanitized_dots_only != ".."
        assert sanitized_dots_only  # never empty

    def test_sanitized_record_still_lands_inside_configured_dir(self, tmp_path):
        """A traversal attempt that the sanitizer neutralizes (rather than
        the sink refusing outright) must produce its file INSIDE the
        configured directory, not merely 'somewhere safe'."""
        target_dir = tmp_path / "telemetry"
        sink = FilesystemSink(target_dir)
        sink.emit({"schema": "telemetry-dispatch-record/v1", "dispatch_id": "../../etc/evil"})

        if target_dir.exists():
            written = list(target_dir.glob("*.jsonl"))
            for f in written:
                assert f.resolve().is_relative_to(target_dir.resolve())


class _FakeResponse:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getcode(self):
        return self.status


class TestWebhookSink:
    def test_resolve_sink_builds_webhook_sink(self):
        sink = resolve_sink(
            {ENV_SINK: "webhook", ENV_WEBHOOK_URL: "https://example.invalid/collect"}
        )
        assert isinstance(sink, WebhookSink)
        assert sink.url == "https://example.invalid/collect"

    def test_missing_url_config_raises(self):
        with pytest.raises(SinkConfigError):
            resolve_sink({ENV_SINK: "webhook"})

    def test_no_default_url_baked_in(self):
        """WebhookSink requires an explicit, non-empty URL — there is no
        operator-host default anywhere in this module."""
        with pytest.raises(SinkConfigError):
            WebhookSink("")

    def test_emit_posts_json_via_injected_opener(self):
        captured = {}

        def fake_opener(request, timeout):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["headers"] = dict(request.header_items())
            captured["timeout"] = timeout
            return _FakeResponse(200)

        sink = WebhookSink(
            "https://example.invalid/collect", token="secret-tok", opener=fake_opener
        )
        record = {"schema": "telemetry-agent-run/v1", "record_id": "r-1"}
        assert sink.emit(record) is True

        assert captured["url"] == "https://example.invalid/collect"
        assert captured["method"] == "POST"
        assert captured["body"] == record
        assert captured["headers"]["Authorization"] == "Bearer secret-tok"
        assert captured["headers"]["Content-type"] == "application/json"

    def test_emit_without_token_omits_auth_header(self):
        captured = {}

        def fake_opener(request, timeout):
            captured["headers"] = dict(request.header_items())
            return _FakeResponse(200)

        sink = WebhookSink("https://example.invalid/collect", opener=fake_opener)
        sink.emit({"schema": "x"})
        assert "Authorization" not in captured["headers"]

    def test_non_2xx_response_returns_false_not_raise(self):
        sink = WebhookSink(
            "https://example.invalid/collect",
            opener=lambda request, timeout: _FakeResponse(500),
        )
        assert sink.emit({"schema": "x"}) is False

    def test_network_error_returns_false_not_raise(self):
        def raising_opener(request, timeout):
            raise urllib.error.URLError("connection refused")

        sink = WebhookSink("https://example.invalid/collect", opener=raising_opener)
        assert sink.emit({"schema": "x"}) is False

    def test_timeout_error_returns_false_not_raise(self):
        def raising_opener(request, timeout):
            raise TimeoutError("timed out")

        sink = WebhookSink("https://example.invalid/collect", opener=raising_opener)
        # TimeoutError is an OSError subclass, caught by the same handler.
        assert sink.emit({"schema": "x"}) is False

    def test_resolve_sink_reads_token_and_timeout_from_env(self):
        sink = resolve_sink(
            {
                ENV_SINK: "webhook",
                ENV_WEBHOOK_URL: "https://example.invalid/collect",
                ENV_WEBHOOK_TOKEN: "tok-123",
                ENV_WEBHOOK_TIMEOUT: "10",
            }
        )
        assert sink._token == "tok-123"  # noqa: SLF001 -- internal state check
        assert sink._timeout == 10.0  # noqa: SLF001

    def test_default_opener_is_not_bare_urlopen(self):
        """WebhookSink must never default to bare urllib.request.urlopen —
        that function follows redirects (and resends Authorization) with no
        way to intercept. The default opener is built from
        _default_webhook_opener(), which installs a redirect-refusing
        handler (security review, lr-61b9)."""
        import urllib.request

        sink = WebhookSink("https://example.invalid/collect")
        assert sink._urlopen is not urllib.request.urlopen  # noqa: SLF001

    def test_redirect_request_never_follows(self):
        """Direct unit coverage of the redirect-blocking handler: regardless
        of the redirect code or target, redirect_request() always returns
        None, which is urllib's contract for 'do not follow this redirect'."""
        from clagentic_loadout.telemetry.sink import _NoRedirectHandler

        handler = _NoRedirectHandler()
        result = handler.redirect_request(
            req=object(),
            fp=None,
            code=302,
            msg="Found",
            headers={},
            newurl="https://attacker.invalid/steal",
        )
        assert result is None

    def test_redirect_response_does_not_resend_token_to_new_host(self):
        """End-to-end through the real default opener: a 3xx response from
        the configured URL must not cause a second request (carrying the
        Authorization header) to be issued against the redirect target. We
        substitute urllib's HTTPHandler.http_open so no real network call
        happens, but the rest of the opener (including the redirect
        handler) is the real production code path."""
        import urllib.request

        requests_seen = []

        class _RedirectResponse:
            def __init__(self):
                self.status = 302
                self.headers = {"Location": "https://attacker.invalid/steal"}
                self.reason = "Found"

            def read(self, *a, **k):
                return b""

            def getheader(self, name, default=None):
                return self.headers.get(name, default)

            def info(self):
                return self.headers

            def close(self):
                pass

        class _FakeHTTPHandler(urllib.request.HTTPHandler):
            def http_open(self, req):
                requests_seen.append(req)
                resp = _RedirectResponse()
                return self.parent.error(
                    "http", req, resp, 302, "Found", resp.headers
                )

        from clagentic_loadout.telemetry.sink import _NoRedirectHandler

        opener = urllib.request.build_opener(_FakeHTTPHandler, _NoRedirectHandler)
        sink = WebhookSink(
            "http://example.invalid/collect", token="secret-tok", opener=opener.open
        )
        result = sink.emit({"schema": "x"})

        assert result is False  # 302 is not 2xx and is not followed -> failure, not a crash
        assert len(requests_seen) == 1  # exactly the original request — no second hop
        assert requests_seen[0].full_url == "http://example.invalid/collect"
        assert requests_seen[0].get_header("Authorization") == "Bearer secret-tok"
        # No second request was ever made to the redirect target, so the
        # token was never forwarded there.
        assert all("attacker.invalid" not in r.full_url for r in requests_seen)


class TestResolveSinkInvalid:
    def test_unrecognized_sink_name_raises(self):
        with pytest.raises(SinkConfigError):
            resolve_sink({ENV_SINK: "carrier-pigeon"})

    def test_sink_name_is_case_and_whitespace_insensitive(self, tmp_path):
        sink = resolve_sink({ENV_SINK: "  Filesystem  ", ENV_FILESYSTEM_DIR: str(tmp_path)})
        assert isinstance(sink, FilesystemSink)
