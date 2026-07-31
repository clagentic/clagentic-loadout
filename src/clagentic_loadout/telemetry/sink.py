"""sink.py — pluggable telemetry sinks (Wave A slice 5, tome #688).

REBUILD, not a port (tome #687 §11 sink model). The source writers
(trace_writer.py, dispatch_sidecar.py, agent_run_post.py) hardwired their
destination — a fixed home-directory data path or a fixed internal HTTP
endpoint. That coupling is right for one specific deployment; it is wrong
for a published package. This module replaces both destinations with a
single SINK abstraction selected by config, so the package itself never
names, imports, or assumes any particular collector.

Three sink kinds:

    none        — no-op. The DEFAULT. Emitting does nothing: no files, no
                  network. Keeps the package inert with zero required
                  config.
    filesystem  — atomic JSONL append of each event record under a
                  CONFIGURED directory (never a hardcoded path).
    webhook     — POST each event record (JSON) to a CONFIGURED URL, with
                  an optional bearer token, using stdlib urllib only.
                  Failures are swallowed (logged), never raised — a sink
                  problem must never crash the caller's dispatch.

Whoever consumes a filesystem or webhook sink (a log watcher, a collector
service, anything) is entirely a deployment concern (CLAUDE.md hard rule
2) — this module does not name, import, or assume any such consumer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: Sink selection env var. One of "none" (default), "filesystem", "webhook".
ENV_SINK = "CLAGENTIC_LOADOUT_TELEMETRY_SINK"

#: filesystem sink: directory event records are appended under. Required
#: when sink=filesystem; no default path is assumed — never a hardcoded
#: home-directory or other fixed location.
ENV_FILESYSTEM_DIR = "CLAGENTIC_LOADOUT_TELEMETRY_DIR"

#: webhook sink: destination URL. Required when sink=webhook; no default
#: host is assumed.
ENV_WEBHOOK_URL = "CLAGENTIC_LOADOUT_TELEMETRY_WEBHOOK_URL"

#: webhook sink: optional bearer token for the Authorization header.
ENV_WEBHOOK_TOKEN = "CLAGENTIC_LOADOUT_TELEMETRY_WEBHOOK_TOKEN"

#: webhook sink: optional request timeout override, in seconds.
ENV_WEBHOOK_TIMEOUT = "CLAGENTIC_LOADOUT_TELEMETRY_WEBHOOK_TIMEOUT"

DEFAULT_WEBHOOK_TIMEOUT_SECONDS = 5.0

SINK_NONE = "none"
SINK_FILESYSTEM = "filesystem"
SINK_WEBHOOK = "webhook"
VALID_SINKS = frozenset({SINK_NONE, SINK_FILESYSTEM, SINK_WEBHOOK})


class SinkConfigError(ValueError):
    """Raised when the requested sink is missing required configuration
    (e.g. sink=filesystem with no directory, sink=webhook with no URL)."""


class PathEscapeError(ValueError):
    """Raised when a record-derived file name would resolve outside the
    sink's configured directory. Filesystem writes fail closed on this —
    a traversal attempt is refused, never silently redirected or written
    outside the configured dir (security review finding, lr-61b9)."""


class TelemetrySink(Protocol):
    """A telemetry sink accepts one event record at a time and delivers it
    (or drops it) — it never raises to the caller. emit() returns True when
    the record was durably handed off (written / accepted by the remote
    end), False otherwise. Callers that only care about "did this crash",
    not "did it land", can ignore the return value."""

    def emit(self, record: dict[str, Any]) -> bool: ...


class NoneSink:
    """Default sink. Emitting does nothing — no files, no network. This is
    what makes the package inert out of the box: a caller that never
    configures a sink gets zero side effects from telemetry emission."""

    def emit(self, record: dict[str, Any]) -> bool:
        del record  # intentionally discarded
        return True


class FilesystemSink:
    """Appends each event record as one JSON line under a configured
    directory. One file per (record kind, record key) so concurrent event
    streams for different dispatches/runs don't interleave; each write is
    atomic (tmp file + os.replace, both inside the configured directory).

    file_name_fn resolves the destination file name from a record — the
    default groups by the record's own "schema" field plus, when present, a
    stable per-stream key (dispatch_id / invocation_id / record_id), falling
    back to "events.jsonl" when neither is present. The stream key is
    caller-supplied (it flows in from a dispatch envelope / agent-run record)
    and schema-validated only for minLength, not charset — it is therefore
    treated as untrusted input. Two independent defenses apply before any
    write (path-traversal security review finding, lr-61b9):

      1. _sanitize_path_component() allowlists a safe charset for the
         key/schema-derived pieces of the file name, so a value like
         "../../etc/evil" or "a/b/c" cannot introduce a path separator or a
         traversal sequence in the first place.
      2. _resolve_contained_path() re-resolves the final path after joining
         and confirms it still lives under the configured directory. This is
         deliberately kept even though (1) should already make an escape
         unreachable — defense in depth, not either/or.

    A record whose derived path would escape the configured directory is
    refused (PathEscapeError, caught and logged, emit() returns False) —
    failing closed, never a silent write outside the sink's directory.
    """

    def __init__(self, directory: str | Path, *, file_name_fn=None):
        self._dir = Path(directory)
        self._file_name_fn = file_name_fn or _default_file_name

    @property
    def directory(self) -> Path:
        return self._dir

    def emit(self, record: dict[str, Any]) -> bool:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            file_name = self._file_name_fn(record)
            path = _resolve_contained_path(self._dir, file_name)
            _atomic_append_jsonl(self._dir, path, record)
            return True
        except (OSError, PathEscapeError) as exc:
            log.warning("[clagentic_loadout.telemetry] filesystem sink write failed: %s", exc)
            return False


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses to follow any 3xx redirect. urllib's default
    HTTPRedirectHandler re-issues the request (WITH its original headers,
    including Authorization) against the Location target — if that target
    is a different host, the configured bearer token is forwarded to
    whatever host the redirect points at. Returning None from
    redirect_request() tells urllib not to follow; the original 3xx response
    then surfaces to the caller as an HTTPError, which emit() treats as a
    failed delivery like any other non-2xx status (security review finding,
    lr-61b9)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 -- stdlib override
        del req, fp, code, msg, headers, newurl
        return None


def _default_webhook_opener():
    """Build a urllib opener that never follows redirects (see
    _NoRedirectHandler). Constructed lazily per-sink so a test-injected
    opener never has to go through this at all."""
    return urllib.request.build_opener(_NoRedirectHandler)


class WebhookSink:
    """POSTs each event record as JSON to a configured URL via stdlib
    urllib. No default URL — the caller must configure one. Redirects are
    never followed (see _NoRedirectHandler) so a 3xx response can never
    cause the configured bearer token to be forwarded to a different host.
    Any failure (network error, non-2xx/redirect response, timeout) is
    logged and swallowed; emit() returns False but never raises, so a
    telemetry problem never interrupts the caller's real work.
    """

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        timeout: float = DEFAULT_WEBHOOK_TIMEOUT_SECONDS,
        opener=None,
    ):
        if not url:
            raise SinkConfigError("WebhookSink requires a non-empty url.")
        self._url = url
        self._token = token
        self._timeout = timeout
        # Injectable for tests — defaults to a redirect-blocking opener
        # (never bare urllib.request.urlopen) so no test ever performs a
        # real network call, and production traffic never auto-follows a
        # cross-host redirect with the bearer token attached.
        self._urlopen = opener or _default_webhook_opener().open

    @property
    def url(self) -> str:
        return self._url

    def emit(self, record: dict[str, Any]) -> bool:
        try:
            data = json.dumps(record).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            request = urllib.request.Request(self._url, data=data, headers=headers, method="POST")
            with self._urlopen(request, timeout=self._timeout) as resp:
                status = getattr(resp, "status", None)
                if status is None:
                    status = resp.getcode()
                return 200 <= status < 300
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            log.warning("[clagentic_loadout.telemetry] webhook sink POST failed: %s", exc)
            return False


#: Positive charset allowlist for a path-component fragment derived from
#: caller-supplied telemetry fields (schema name, dispatch_id, invocation_id,
#: record_id). Anything outside this set is replaced with "_" — a pure
#: os.path.basename() is NOT sufficient on its own (it does not neutralize
#: ".." segments or encoded-separator lookalikes embedded mid-string), so
#: this is a positive allowlist rather than a denylist of "bad" substrings.
_SAFE_PATH_COMPONENT_CHARS = re.compile(r"[^A-Za-z0-9._-]")

#: Collapse any run of 2+ literal dots left after the charset filter, so a
#: sanitized fragment can never reassemble into a ".." traversal segment
#: (e.g. "..." or ".." survive the single-char allowlist above but must not
#: survive this pass).
_DOT_RUN = re.compile(r"\.{2,}")


def _sanitize_path_component(value: str) -> str:
    """Reduce `value` to a safe single path-component fragment.

    Replaces every character outside [A-Za-z0-9._-] with "_", then collapses
    any run of 2+ dots (which would otherwise reconstitute a ".." traversal
    segment) into a single "_". Never returns a value containing "/" or a
    ".." sequence. An empty result (e.g. the input was entirely unsafe
    characters) falls back to "_".
    """
    cleaned = _SAFE_PATH_COMPONENT_CHARS.sub("_", value)
    cleaned = _DOT_RUN.sub("_", cleaned)
    cleaned = cleaned.strip(".")  # no leading/trailing dot segments either
    return cleaned or "_"


def _default_file_name(record: dict[str, Any]) -> str:
    schema = _sanitize_path_component(str(record.get("schema", "events")))
    key = (
        record.get("dispatch_id")
        or record.get("invocation_id")
        or record.get("record_id")
        or "stream"
    )
    key = _sanitize_path_component(str(key))
    return f"{schema}-{key}.jsonl"


def _resolve_contained_path(directory: Path, file_name: str) -> Path:
    """Join `file_name` onto `directory` and assert the result is still
    inside `directory` once both are fully resolved.

    This is the second, independent defense (containment check) layered on
    top of _sanitize_path_component()'s charset allowlist — it must hold
    even if a future file_name_fn override or a gap in the sanitizer would
    otherwise let a traversal segment through.

    Raises:
        PathEscapeError: the resolved path is not contained in `directory`.
    """
    base = directory.resolve()
    candidate = (directory / file_name).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise PathEscapeError(
            f"telemetry file name {file_name!r} resolves outside the configured "
            f"directory {base} — refusing to write."
        ) from exc
    return candidate


def _atomic_append_jsonl(directory: Path, path: Path, record: dict[str, Any]) -> None:
    """Append one JSON-serialized record as a line to `path`, atomically.

    Reads existing bytes, writes existing+new to a temp file, then
    os.replace() onto the final path — a crash mid-write leaves readers
    seeing either the pre- or post-write state, never a partial line. The
    temp file is created inside `directory` (never /tmp or any other
    filesystem) so the rename is same-filesystem-atomic AND so the
    containment guarantee established by _resolve_contained_path() covers
    every file this sink ever creates, including the transient temp file.
    """
    existing = path.read_bytes() if path.is_file() else b""
    line = json.dumps(record, sort_keys=True).encode("utf-8") + b"\n"
    payload = existing + line

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def resolve_sink(env: dict[str, str] | None = None) -> TelemetrySink:
    """Resolve the configured sink from environment variables.

    Args:
        env: mapping to read from instead of os.environ (tests pass an
            explicit dict so the suite never depends on process env state).

    Returns:
        A TelemetrySink instance. Defaults to NoneSink when
        CLAGENTIC_LOADOUT_TELEMETRY_SINK is unset or "none" — the zero-
        config default that keeps the package inert.

    Raises:
        SinkConfigError: sink is "filesystem" with no directory configured,
            "webhook" with no URL configured, or an unrecognized sink name.
    """
    source = env if env is not None else os.environ
    sink_name = source.get(ENV_SINK, SINK_NONE).strip().lower() or SINK_NONE

    if sink_name == SINK_NONE:
        return NoneSink()

    if sink_name == SINK_FILESYSTEM:
        directory = source.get(ENV_FILESYSTEM_DIR, "").strip()
        if not directory:
            raise SinkConfigError(
                f"{ENV_SINK}=filesystem requires {ENV_FILESYSTEM_DIR} to be set."
            )
        return FilesystemSink(directory)

    if sink_name == SINK_WEBHOOK:
        url = source.get(ENV_WEBHOOK_URL, "").strip()
        if not url:
            raise SinkConfigError(f"{ENV_SINK}=webhook requires {ENV_WEBHOOK_URL} to be set.")
        token = source.get(ENV_WEBHOOK_TOKEN, "").strip() or None
        timeout_raw = source.get(ENV_WEBHOOK_TIMEOUT, "").strip()
        timeout = float(timeout_raw) if timeout_raw else DEFAULT_WEBHOOK_TIMEOUT_SECONDS
        return WebhookSink(url, token=token, timeout=timeout)

    raise SinkConfigError(
        f"{ENV_SINK}={sink_name!r} is not a recognized sink "
        f"(expected one of {sorted(VALID_SINKS)})."
    )


__all__ = [
    "DEFAULT_WEBHOOK_TIMEOUT_SECONDS",
    "ENV_FILESYSTEM_DIR",
    "ENV_SINK",
    "ENV_WEBHOOK_TIMEOUT",
    "ENV_WEBHOOK_TOKEN",
    "ENV_WEBHOOK_URL",
    "FilesystemSink",
    "NoneSink",
    "PathEscapeError",
    "SINK_FILESYSTEM",
    "SINK_NONE",
    "SINK_WEBHOOK",
    "SinkConfigError",
    "TelemetrySink",
    "VALID_SINKS",
    "WebhookSink",
    "resolve_sink",
]
