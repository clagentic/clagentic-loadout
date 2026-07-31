"""dispatch.py — HMAC-signed "task shipped" release-event dispatcher.

Release-signal dispatch verb (lr-51d4, Wave A slice 6, tome #688). Ported
from the reference implementation; the source copy stays primary until its
separate CUT OVER + RETIRE + VERIFY-GONE task per the migration plan.

A thin, deployment-agnostic module that fires a release-authorizing caller's
inbound "task shipped" event hook once a release/merge signal names the work
item that shipped. This module does not import, modify, or depend on any
particular release-signal service's source — the wire contract below is a
generic event-hook shape (task_id / dispatcher / status / version), signed
and delivered exactly the same way regardless of which service consumes it.
Endpoint, secret, and dispatcher-name values are ALL caller-supplied
configuration (tome #687 §12) — this module never bakes in a specific host,
secret path, or dispatcher literal.

    POST <status-hook-url>
    Header: <signature-header>: sha256=<hex HMAC-SHA256 of the raw body>
    Body (JSON):
        {
          "task_id": "<opaque work-item ref>",  # required
          "dispatcher": "<caller-supplied>",     # optional, narrows lookup
          "status": "shipped",                   # required, only value today
          "version": "1.2.3"                     # optional, free-form
        }

The release-signal service resolves any additional context (repo, issue,
url) SERVER-SIDE from its own index; this module sends only the fields
above. A task_id unknown to that index is expected to return a safe,
idempotent no-op response — never treated as a failure by this module.

RESOLUTION SOURCES (v1):
  1. Same-repo GitHub case: `Closes #NN` / `Task: <id>` are read directly
     off a merged PR body — see parse_trailers(). The body TEXT is supplied
     via --merged-pr-body-stdin (stdin content, not a caller-named path —
     see the flag's own help for why); this module never fetches the body
     itself, since it is deliberately git-host-agnostic (see above) and
     never resolves a repo/PR/token on its own.
  2. Cross-git-host / manual crossing: the caller supplies task_id/
     dispatcher/version explicitly at signal time. This module never
     invents, seeds, or maintains any persistent id -> issue map.

Trigger is caller-driven (a release-authorizing caller, CI job, or the
generic v*-tag detector in detector.py, which calls dispatch_task_shipped()
below once per distinct resolved task rather than re-implementing trailer
parsing, HMAC signing, or hook-firing).

Secret resolution (HMAC signing key): read via
clagentic_loadout.release.secrets_config.read_role_env_file() from a
loadout-standard role-scoped .env file (STATUS_HOOK_SECRET=...), OR directly
from an already-set process env var via --secret-env-var (useful for CI/
operator shells that inject the secret another way).

Exit codes:
    0   OK — hook fired and returned 200 (covers ok / unknown-task-id /
        ignored — all are successful, idempotent outcomes) OR the no-op
        case (no task_id resolved, nothing to fire).
    1   Usage error (bad arguments, no resolvable task_id).
    2   Secret resolution failed.
    3   Hook call failed (non-200, network error, or malformed response).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from clagentic_loadout._version import get_version
from clagentic_loadout.release.secrets_config import (
    DEFAULT_ROLE,
    SecretEnvError,
    read_role_env_file,
)

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_SECRET_FAILED = 2
EXIT_HOOK_FAILED = 3

# ---------------------------------------------------------------------------
# Status-hook URL validation (SSRF hardening).
#
# The single validator every caller of fire_status_hook goes through
# (including this module's own CLI and detector.py's AUTO-dispatch path) —
# one validator, not two (reuse-before-new-logic).
# ---------------------------------------------------------------------------

ALLOWED_STATUS_HOOK_SCHEMES = frozenset({"https", "http"})

#: Wire-protocol header name the reference release-signal service expects
#: (deployment contract, not internal identity — see PR body for the
#: disposition). Callers may override via --signature-header /
#: fire_status_hook(signature_header=...) for a deployment whose
#: release-signal endpoint expects a different header name; this is the
#: shipped default only.
DEFAULT_SIGNATURE_HEADER = "x-clagentic-signature"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses to follow any 3xx redirect. urllib's default
    HTTPRedirectHandler re-issues the request (WITH its original headers,
    including the HMAC signature header) against the Location target — if
    that target is a different host, the signed payload and its signature
    are replayed to whatever host the redirect points at. Returning None
    from redirect_request() tells urllib not to follow; the original 3xx
    response then surfaces to the caller as an HTTPError, which
    fire_status_hook() treats as a failed delivery like any other non-2xx
    status (security-review finding — mirrors the equivalent handler
    already shipped in clagentic_loadout.telemetry.sink.WebhookSink;
    duplicated locally, not imported, so the two verbs stay decoupled).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 -- stdlib override
        del req, fp, code, msg, headers, newurl
        return None


def _default_status_hook_opener():
    """Build a urllib opener that never follows redirects (see
    _NoRedirectHandler). Constructed lazily so a test-injected opener never
    has to go through this at all."""
    return urllib.request.build_opener(_NoRedirectHandler)


def is_valid_status_hook_url(url: str) -> bool:
    """
    True if *url* has an allowed scheme and a non-empty host.

    Minimal allow-list check (scheme in {http, https}, hostname present) --
    this is a shape/sanity gate on the operator-supplied endpoint, not a full
    SSRF defense (no DNS-rebind protection, no private-IP denylist). It stops
    the obviously-wrong cases (file://, javascript:, no host) from ever
    reaching urllib. This is the ONE validator both this module's own
    fire_status_hook and detector.py's AUTO-dispatch path use.
    """
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme in ALLOWED_STATUS_HOOK_SCHEMES and bool(parsed.hostname)


# ---------------------------------------------------------------------------
# Trailer parsing (same-repo GitHub case) — reads the keyword-linking
# grammar off a merged PR body: `Task: <id>` and `Closes #NN`, each on their
# own line, no colon on the Closes form.
# ---------------------------------------------------------------------------

_TASK_TRAILER_RE = re.compile(r"(?im)^\s*task:\s*(\S+)\s*$")
_CLOSES_TRAILER_RE = re.compile(r"(?im)^\s*closes\s+#(\d+)\s*$")


def parse_trailers(pr_body: str) -> tuple[str | None, int | None]:
    """
    Extract (task_id, issue_number) from a merged PR body's footer trailers.

    task_id is an opaque work-item reference — any non-whitespace token, not
    a validated shape (deployment decides its own id format; tome #687 §11.3).

    Either field may be absent (task_id absent is a caller error; issue_number
    absent is the genuine no-linked-issue case — never fail closed on it).
    Returns (None, None) for an empty/missing body.
    """
    if not pr_body:
        return None, None
    task_match = _TASK_TRAILER_RE.search(pr_body)
    closes_match = _CLOSES_TRAILER_RE.search(pr_body)
    task_id = task_match.group(1) if task_match else None
    issue_number = int(closes_match.group(1)) if closes_match else None
    return task_id, issue_number


# ---------------------------------------------------------------------------
# Status-hook payload + HMAC signing
# ---------------------------------------------------------------------------


def build_status_hook_payload(
    task_id: str,
    *,
    dispatcher: str | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    """
    Build the JSON payload for the release-signal service's inbound status
    hook: {task_id, dispatcher?, status: "shipped", version?}. Optional
    fields are omitted entirely when None rather than sent as null/empty,
    keeping the wire payload minimal.
    """
    payload: dict[str, Any] = {"task_id": task_id, "status": "shipped"}
    if dispatcher is not None:
        payload["dispatcher"] = dispatcher
    if version is not None:
        payload["version"] = version
    return payload


def sign_payload(secret: str, raw_body: bytes) -> str:
    """
    HMAC-SHA256 sign *raw_body* with *secret*, formatted as
    "sha256=<hex digest>" for the signature header.
    """
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def fire_status_hook(
    url: str,
    payload: dict[str, Any],
    secret: str,
    *,
    timeout: int = 15,
    signature_header: str = DEFAULT_SIGNATURE_HEADER,
    opener=None,
) -> tuple[int, dict[str, Any]]:
    """
    POST *payload* to the release-signal service's inbound status-hook
    *url*, HMAC-signed.

    SSRF hardening: *url* is validated via is_valid_status_hook_url() --
    scheme must be http/https and a host must be present -- BEFORE it ever
    reaches urllib. This guards every caller of fire_status_hook (this
    module's own CLI, dispatch_task_shipped(), and transitively
    detector.py), not just one gated call site. Rejection raises
    SystemExit(EXIT_USAGE) -- a bad URL is a usage error, not a hook-call
    failure.

    Redirect hardening (security-review finding): the request is sent via a
    urllib opener that never follows 3xx redirects (see
    _NoRedirectHandler) -- bare urllib.request.urlopen uses the default
    HTTPRedirectHandler, which would re-issue this signed POST (payload +
    signature header intact) against whatever host a redirect Location
    names. A redirect response therefore surfaces as an HTTPError, handled
    identically to any other non-2xx status below -- the redirect is never
    followed, only reported as a failure.

    Args:
        opener: inject a urllib opener's .open callable for tests (mainly
            so a test can assert exactly one request was made and inspect
            it, without ever performing a real network call). Defaults to
            a fresh no-redirect opener when omitted.

    Returns (status_code, response_body_dict). Raises SystemExit(EXIT_HOOK_FAILED)
    on network/connection errors (the hook endpoint is unreachable — a real
    failure, not a no-op case). A 401 (bad/missing signature — should not
    happen given we always sign) and any other non-2xx status are also
    treated as SystemExit(EXIT_HOOK_FAILED); a 200 with body.status ==
    "unknown_task_id" or "ignored" is NOT an error — those are the
    documented safe no-op responses and are returned normally to the caller.
    """
    if not is_valid_status_hook_url(url):
        _die(
            EXIT_USAGE,
            f"status-hook url {url!r} is not a valid http(s) URL with a host.",
        )
    raw_body = json.dumps(payload).encode("utf-8")
    signature = sign_payload(secret, raw_body)
    req = urllib.request.Request(
        url,
        data=raw_body,
        headers={
            "Content-Type": "application/json",
            signature_header: signature,
        },
        method="POST",
    )
    urlopen = opener if opener is not None else _default_status_hook_opener().open
    try:
        with urlopen(req, timeout=timeout) as resp:
            body_raw = resp.read().decode("utf-8")
            body = json.loads(body_raw) if body_raw else {}
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
            return status, body
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            err_body: dict[str, Any] = json.loads(raw) if raw else {}
        except Exception:
            err_body = {}
        _die(
            EXIT_HOOK_FAILED,
            f"status-hook call returned HTTP {exc.code}: {err_body.get('error', '(no error field)')}",
        )
    except urllib.error.URLError as exc:
        _die(EXIT_HOOK_FAILED, f"status-hook unreachable at {url!r}: {exc.reason!r}")


# ---------------------------------------------------------------------------
# Secret resolution — loadout-standard role-scoped .env self-fetch (see
# secrets_config.py); no new secret-storage mechanism invented here.
# ---------------------------------------------------------------------------


def resolve_hook_secret(
    *,
    secret_env_caller: str | None,
    secret_env_var: str | None,
) -> str:
    """
    Resolve the status-hook HMAC secret.

    Precedence:
    1. --secret-env-var NAME: read directly from an already-set process env
       var (operator/CI shells that inject the secret their own way).
    2. --secret-env-caller NAME (default: DEFAULT_ROLE): self-fetch from the
       loadout-standard role-scoped .env file, key STATUS_HOOK_SECRET=.

    Raises SystemExit(EXIT_SECRET_FAILED) on any failure. Never logs or
    echoes the secret value.
    """
    if secret_env_var:
        value = os.environ.get(secret_env_var, "")
        if not value:
            _die(
                EXIT_SECRET_FAILED,
                f"--secret-env-var {secret_env_var!r} is unset or empty in this process's environment.",
            )
        return value

    role = secret_env_caller or DEFAULT_ROLE
    try:
        kvs = read_role_env_file(role, ("STATUS_HOOK_SECRET",))
    except SecretEnvError as exc:
        _die(EXIT_SECRET_FAILED, f"secret self-fetch REFUSED — {exc}")
    return kvs["STATUS_HOOK_SECRET"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _die(exit_code: int, msg: str) -> None:
    print(f"release-dispatch: error: {msg}", file=sys.stderr)
    sys.exit(exit_code)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "release-dispatch — fire a release-authorizing caller's inbound "
            "'task shipped' status hook for a work item that just shipped."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  # Same-repo case: derive task_id from a merged PR body, piped\n"
            "  # in on stdin (fetch it from the git host yourself, e.g. via\n"
            "  # loadout-git-host-api GET .../pulls/<pr> and its '.body' field).\n"
            "  echo \"$PR_BODY\" | clagentic-loadout release dispatch \\\n"
            "      --merged-pr-body-stdin \\\n"
            "      --version 1.2.3 \\\n"
            "      --status-hook-url https://triage.example.com/status-hook \\\n"
            "      --dispatcher some-dispatcher\n"
            "\n"
            "  # Cross-git-host / manual case: explicit task_id, no PR body.\n"
            "  clagentic-loadout release dispatch \\\n"
            "      --task-id proj-a68f \\\n"
            "      --version 0.9.0-beta.3 \\\n"
            "      --status-hook-url https://triage.example.com/status-hook\n"
        ),
    )
    parser.add_argument(
        "--cli-version",
        action="version",
        version=f"release-dispatch {get_version()}",
        help="Show the clagentic-loadout package version and exit. Named "
        "--cli-version, not --version, because this verb's own "
        "--version flag is a release-version-string BUSINESS argument "
        "(see below) that predates the CLI conformance rule and cannot be "
        "renamed without breaking existing callers.",
    )
    task_group = parser.add_mutually_exclusive_group(required=True)
    task_group.add_argument(
        "--task-id",
        help="Explicit work-item id (opaque string). Use for a cross-git-host "
        "or otherwise manual crossing — no PR body to parse.",
    )
    task_group.add_argument(
        "--merged-pr-body-stdin",
        action="store_true",
        help="Read the merged PR body text from stdin; task_id is parsed "
        "from its 'Task: <id>' trailer (same-repo case). Takes NO path "
        "value — this module never reads a caller-named file (that class "
        "of exposure was eliminated in loadout-push/loadout-stage-body per "
        "the same operator posture: a caller-supplied path is refused even "
        "validated, only content is accepted). Fetch the merged PR body "
        "yourself first (e.g. loadout-git-host-api GET on the PR endpoint, "
        "reading its '.body' field) and pipe it in.",
    )
    parser.add_argument(
        "--dispatcher",
        default=None,
        help="Dispatcher name to narrow the release-signal service's lookup "
        "(caller-supplied; no default is assumed).",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Release version string, rendered into the receiving service's "
        "comment template.",
    )
    parser.add_argument(
        "--status-hook-url",
        required=True,
        help="Full URL of the release-signal service's inbound status-hook "
        "endpoint (e.g. https://triage.example.com/status-hook).",
    )
    parser.add_argument(
        "--secret-env-caller",
        default=None,
        help=f"Role/name whose secret-env file holds STATUS_HOOK_SECRET "
        f"(default: {DEFAULT_ROLE!r}).",
    )
    parser.add_argument(
        "--secret-env-var",
        default=None,
        help="Read the HMAC secret directly from this already-set env var "
        "instead of self-fetching from a role-scoped .env file.",
    )
    parser.add_argument(
        "--signature-header",
        default=DEFAULT_SIGNATURE_HEADER,
        help=f"HTTP header name the HMAC signature is sent under "
        f"(default: {DEFAULT_SIGNATURE_HEADER!r} — the reference "
        f"release-signal service's documented header).",
    )
    return parser


def dispatch_task_shipped(
    task_id: str,
    *,
    status_hook_url: str,
    dispatcher: str | None = None,
    version: str | None = None,
    secret_env_caller: str | None = None,
    secret_env_var: str | None = None,
    signature_header: str = DEFAULT_SIGNATURE_HEADER,
) -> tuple[int, dict[str, Any]]:
    """
    Resolve the HMAC secret, build the status-hook payload for *task_id*,
    and fire it. This is the single callable entrypoint both this module's
    CLI and other callers (e.g. detector.py's v*-tag scan) use to emit a
    "task shipped" signal — added so callers can invoke this dispatch
    without duplicating secret resolution, payload shape, or HMAC signing.

    Returns (status_code, response_body_dict) exactly as fire_status_hook
    does. Raises SystemExit on secret-resolution or hook-call failure,
    matching the CLI's existing exit-code contract (EXIT_SECRET_FAILED,
    EXIT_HOOK_FAILED) unchanged.
    """
    secret = resolve_hook_secret(
        secret_env_caller=secret_env_caller,
        secret_env_var=secret_env_var,
    )
    payload = build_status_hook_payload(task_id, dispatcher=dispatcher, version=version)
    return fire_status_hook(
        status_hook_url, payload, secret, signature_header=signature_header
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.merged_pr_body_stdin:
        pr_body = sys.stdin.read()
        task_id, issue_number = parse_trailers(pr_body)
        if not task_id:
            # Genuine no-op: a merged PR with no Task: trailer at all. Never
            # fail closed — nothing to signal.
            print(
                "release-dispatch: no 'Task: <id>' trailer found in "
                "--merged-pr-body-stdin — nothing to dispatch (no-op).",
                file=sys.stderr,
            )
            return EXIT_OK
        if issue_number is None:
            # Genuine no-linked-issue case: still fire the hook using
            # task_id alone — the receiving service's own index is the
            # authority on whether there's a public issue to notify. This
            # module never fails closed on the no-issue case.
            print(
                "release-dispatch: no 'Closes #NN' trailer in "
                f"--merged-pr-body-stdin (genuine no-linked-issue case) — "
                f"firing hook for {task_id} anyway; the receiving service's "
                "own index determines whether there is anything to notify.",
                file=sys.stderr,
            )
    else:
        task_id = args.task_id
        issue_number = None

    status, body = dispatch_task_shipped(
        task_id,
        status_hook_url=args.status_hook_url,
        dispatcher=args.dispatcher,
        version=args.version,
        secret_env_caller=args.secret_env_caller,
        secret_env_var=args.secret_env_var,
        signature_header=args.signature_header,
    )

    print(
        f"release-dispatch: status-hook responded HTTP {status}: {json.dumps(body)}",
        file=sys.stderr,
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
