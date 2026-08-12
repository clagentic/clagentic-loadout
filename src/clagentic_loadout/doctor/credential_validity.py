"""doctor.credential_validity — the pre-flight credential-validity probe
(lr-0eeb0c).

WHAT THIS IS NOT: a token-EXPIRY warning feature. This task was originally
filed that way on a since-falsified diagnosis (see this module's own task
history) and was corrected before any code shipped. The observed failure
that motivated this check was a MALFORMED-TOKEN-SHAPE rejection — Forgejo's
own credential validator returning "token is malformed: token contains an
invalid number of segments" for a broker-minted token, the SAME token SHA
across three independent mints. An expiry-only check would have reported
GREEN on that token and missed the failure entirely.

WHAT THIS DOES: performs ONE cheap authenticated GET per platform (Forgejo:
`GET /api/v1/user`; GitHub: `GET /user`) using the caller's ALREADY-RESOLVED
credential (never a config-file read — see NEVER READ CONFIG below) and
classifies the host's own response into exactly one of five states, never
collapsing them (task acceptance criteria, verbatim):

  - CREDENTIAL_STATE_OK              — well-formed and accepted.
  - CREDENTIAL_STATE_MALFORMED       — malformed/unparseable token SHAPE
                                        (the case this task exists for).
  - CREDENTIAL_STATE_REJECTED        — expired or revoked (a well-formed
                                        credential the host refuses).
  - CREDENTIAL_STATE_INSUFFICIENT_SCOPE — accepted, but a configured repo
                                        the caller works in returned 403/404
                                        on a repo-read the credential should
                                        be able to see.
  - CREDENTIAL_STATE_UNREACHABLE     — infrastructure fault (DNS/connect/
                                        timeout), never reported as a
                                        credential fault.

MALFORMED-TOKEN VOCABULARY, REUSED NOT REINVENTED (task instruction,
CLAUDE.md reuse-first rule): the exact same generic, RFC 7519-anchored
malformed-credential-SHAPE vocabulary `push.git_push._MALFORMED_TOKEN_MARKERS`
already established (from lr-91bac6, PR #14/#15) is imported and reused here
verbatim — this module does not invent a second, parallel taxonomy for the
identical failure class. `push.push_failure_labels.SUB_CAUSE_MALFORMED_TOKEN`
is reused as this module's own MALFORMED state label for the same reason: one
enumerable vocabulary for "the credential-minting/broker path produced a
structurally broken artifact," shared by both the reactive (post-push-failure)
classifier and this proactive (pre-flight) probe.

NEVER READ CONFIG, NEVER MINT A NEW CREDENTIAL (task hard constraint):
credential material lives under the operator-home boundary enforced by
guard-credentials.py and is NOT agent-readable. This module never opens a
credential config file and never calls a TokenProvider itself — every
function here takes an ALREADY-RESOLVED token string as a plain argument,
resolved by the CALLER (e.g. doctor.checks, or a future caller) via the
existing `transport.credential_provider` seam exactly like every other verb
in this package already does. This module is a probe of what the HOST says
about a credential a caller already holds, never a second credential-
resolution path.

NO PUSH, NO WRITE, EVER: every probe in this module is a single GET. No
function here calls `git push`, POSTs, PATCHes, or DELETEs anything.

NO CREDENTIAL MATERIAL LOGGED OR PERSISTED: the raw token value never
appears in any `CredentialProbeResult` field, never in a raised exception,
never printed. `token_sha256` is the SHA-256 hex digest of the token
(Forgejo itself reports a token SHA in its own credential error bodies —
see the task's own evidence quote) — a token cannot be recovered from its
hash, so surfacing the digest is safe while still letting an operator
correlate "this probe's token" with "the token named in a host error line"
without ever seeing the secret itself.

DEGRADE TO UNKNOWN, NEVER A FALSE GREEN: a host response this module cannot
confidently classify (e.g. a non-JSON 200 body, or a 5xx with no recognizable
shape) is reported as `CREDENTIAL_STATE_UNKNOWN` — this module never guesses
"ok" from an ambiguous signal (task hard constraint, "Degrade to unknown
rather than a false green where a host does not expose something").
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
from dataclasses import dataclass, field

from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.push.git_push import _MALFORMED_TOKEN_MARKERS
from clagentic_loadout.push.push_failure_labels import SUB_CAUSE_MALFORMED_TOKEN
from clagentic_loadout.transport import git_host_api
from clagentic_loadout.transport.github_client import GITHUB_API_BASE, request_json

#: Credential is well-formed and the host accepted it.
CREDENTIAL_STATE_OK = "ok"

#: The host's own credential validator repudiated the token's SHAPE
#: (malformed / wrong number of segments / a parse failure) rather than an
#: expiry/revocation/scope denial of an otherwise well-formed token. Reuses
#: push.push_failure_labels.SUB_CAUSE_MALFORMED_TOKEN as its literal value —
#: see this module's own docstring, MALFORMED-TOKEN VOCABULARY, REUSED NOT
#: REINVENTED.
CREDENTIAL_STATE_MALFORMED = SUB_CAUSE_MALFORMED_TOKEN

#: A well-formed credential the host refused — dead/expired/revoked. Never
#: collapsed with CREDENTIAL_STATE_MALFORMED (task acceptance criteria: an
#: expired/revoked credential must report distinguishably from the malformed
#: case).
CREDENTIAL_STATE_REJECTED = "rejected"

#: The credential authenticated successfully (the identity probe succeeded)
#: but a configured repo returned a scope-shaped refusal (403, or 404 on a
#: repo this identity is declared to work in) — reported distinctly from an
#: auth failure, never folded into CREDENTIAL_STATE_REJECTED.
CREDENTIAL_STATE_INSUFFICIENT_SCOPE = "insufficient-scope"

#: The host could not be reached at all (DNS failure, connection refused,
#: timeout) — an INFRASTRUCTURE fault, never reported as a credential fault
#: (task acceptance criteria).
CREDENTIAL_STATE_UNREACHABLE = "unreachable"

#: The host's response could not be confidently classified into any of the
#: above — degrade here rather than report a false green (task hard
#: constraint).
CREDENTIAL_STATE_UNKNOWN = "unknown"

#: The full enumerable set every classification in this module returns —
#: mirrors push.push_failure_labels.SUB_CAUSE_LABELS' own
#: self-policing-corpus convention, so a future state addition here is
#: equally impossible to add without also naming a covering case.
CREDENTIAL_STATES: frozenset[str] = frozenset(
    {
        CREDENTIAL_STATE_OK,
        CREDENTIAL_STATE_MALFORMED,
        CREDENTIAL_STATE_REJECTED,
        CREDENTIAL_STATE_INSUFFICIENT_SCOPE,
        CREDENTIAL_STATE_UNREACHABLE,
        CREDENTIAL_STATE_UNKNOWN,
    }
)

#: Cheap-read timeout, seconds. A doctor probe is a fast health check, never
#: a long-poll — mirrors doctor.checks.PROBE_TIMEOUT_SECONDS' own rationale
#: for the credential-command probe.
PROBE_TIMEOUT_SECONDS = 10


def _token_sha256(token: str) -> str:
    """Return the SHA-256 hex digest of *token* — never the token itself.

    A digest is one-way: it lets an operator correlate this probe's result
    with a token SHA the host's OWN error body names (see this module's
    docstring, the Forgejo evidence quote: "access token does not exist
    [sha: 03cf31...]") without ever reconstructing the secret from the
    report."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CredentialProbeResult:
    """One platform's credential-validity probe outcome. Never carries the
    token itself — see this module's docstring, NO CREDENTIAL MATERIAL
    LOGGED OR PERSISTED.

    `state` is always a member of CREDENTIAL_STATES. `detail` is a
    human-readable, host-response-derived explanation (host status code,
    recognized vocabulary, or the observed transport error) — never a guess
    dressed as a fact (CLAUDE.md hard rule 4). `remaining_lifetime_seconds`
    is populated ONLY when the host's own response exposed an expiry value
    this probe could parse; `None` means the host did not expose one, which
    is a legitimate, expected state for most deployments, not a failure.
    """

    platform: str
    state: str
    detail: str
    token_sha256: str
    remaining_lifetime_seconds: float | None = None
    resolved: dict = field(default_factory=dict)


def _classify_forgejo_status(
    status: int, raw_body: bytes
) -> tuple[str, str]:
    """Classify a Forgejo `GET /api/v1/user` response into (state, detail).

    Forgejo's malformed-token/expired-token rejections both arrive as HTTP
    401 (the SAME transport shape push.git_push's own
    _is_malformed_token_failure/_is_auth_failure distinguish for the
    reactive push-failure path) — the only observable signal distinguishing
    them is the response BODY's own text, which this function inspects using
    the SAME reused vocabulary (see module docstring)."""
    body_text = raw_body.decode("utf-8", errors="replace")
    if status == 200:
        try:
            parsed = json.loads(body_text) if body_text else {}
        except json.JSONDecodeError:
            return (
                CREDENTIAL_STATE_UNKNOWN,
                "GET /api/v1/user returned HTTP 200 but an unparseable body -- "
                "cannot confirm the credential resolved to a real identity.",
            )
        if isinstance(parsed, dict) and parsed.get("login"):
            return (
                CREDENTIAL_STATE_OK,
                f"GET /api/v1/user returned HTTP 200, login={parsed['login']!r}.",
            )
        return (
            CREDENTIAL_STATE_UNKNOWN,
            "GET /api/v1/user returned HTTP 200 but no 'login' field -- "
            "cannot confirm the credential resolved to a real identity.",
        )
    if status == 401:
        lower = body_text.lower()
        if any(marker in lower for marker in _MALFORMED_TOKEN_MARKERS):
            return (
                CREDENTIAL_STATE_MALFORMED,
                f"GET /api/v1/user returned HTTP 401 with malformed-token-shape "
                f"vocabulary in the response body: {body_text.strip()[:300]!r}.",
            )
        return (
            CREDENTIAL_STATE_REJECTED,
            f"GET /api/v1/user returned HTTP 401 (well-formed credential "
            f"shape, rejected -- dead/expired/revoked): "
            f"{body_text.strip()[:300]!r}.",
        )
    if status == 403:
        return (
            CREDENTIAL_STATE_INSUFFICIENT_SCOPE,
            f"GET /api/v1/user returned HTTP 403: {body_text.strip()[:300]!r}.",
        )
    return (
        CREDENTIAL_STATE_UNKNOWN,
        f"GET /api/v1/user returned HTTP {status} -- not a status this probe "
        f"recognizes as ok/malformed/rejected/insufficient-scope: "
        f"{body_text.strip()[:300]!r}.",
    )


def probe_forgejo_credential(
    git_host_base: str,
    token: str,
    *,
    opener=None,
) -> CredentialProbeResult:
    """Probe a Forgejo credential's validity via a single `GET
    /api/v1/user` — the cheapest authenticated read Forgejo exposes, and the
    SAME endpoint push.git_host_api.resolve_bot_login already uses for its
    own identity resolution (no new endpoint invented here).

    Never pushes, never writes. Distinguishes:
      - 200 with a 'login' field -> CREDENTIAL_STATE_OK.
      - 401 with malformed-token-shape vocabulary in the body -> MALFORMED.
      - 401 without that vocabulary -> REJECTED (well-formed, dead/expired/
        revoked).
      - 403 -> INSUFFICIENT_SCOPE.
      - A network-level failure (DNS, connection refused, timeout) ->
        UNREACHABLE — an infrastructure fault, never a credential fault.
      - Anything else (unparseable 200 body, unrecognized status) ->
        UNKNOWN, never a false green (module docstring).

    *token* is the caller's ALREADY-RESOLVED credential (this function never
    resolves one itself — see module docstring, NEVER READ CONFIG).
    """
    try:
        status, raw_body = git_host_api.request(
            git_host_base,
            "GET",
            "/api/v1/user",
            token,
            timeout=PROBE_TIMEOUT_SECONDS,
            opener=opener,
        )
    except git_host_api.GitHostApiError as exc:
        return CredentialProbeResult(
            platform=PLATFORM_FORGEJO,
            state=CREDENTIAL_STATE_UNREACHABLE,
            detail=f"GET /api/v1/user unreachable -- {exc}",
            token_sha256=_token_sha256(token),
            resolved={"git_host_base": git_host_base},
        )

    state, detail = _classify_forgejo_status(status, raw_body)
    return CredentialProbeResult(
        platform=PLATFORM_FORGEJO,
        state=state,
        detail=detail,
        token_sha256=_token_sha256(token),
        resolved={"git_host_base": git_host_base, "http_status": status},
    )


def _classify_github_status(status: int, body: object) -> tuple[str, str]:
    """Classify a GitHub `GET /user` response into (state, detail).

    GitHub App INSTALLATION tokens legitimately 403 on GET /user (documented
    behavior — see review.github_backend.resolve_own_login's own docstring
    for the full token-type-aware rationale); this probe reports that shape
    as INSUFFICIENT_SCOPE (a scope/endpoint mismatch for this credential
    type), never as REJECTED or MALFORMED — a caller whose deployment uses
    App installation tokens is expected to see this, and it is not a broken
    credential."""
    message = ""
    if isinstance(body, dict):
        message = str(body.get("message", ""))
    if status == 200:
        if isinstance(body, dict) and body.get("login"):
            return (
                CREDENTIAL_STATE_OK,
                f"GET /user returned HTTP 200, login={body['login']!r}.",
            )
        return (
            CREDENTIAL_STATE_UNKNOWN,
            "GET /user returned HTTP 200 but no 'login' field -- cannot "
            "confirm the credential resolved to a real identity.",
        )
    if status == 401:
        lower = message.lower()
        if any(marker in lower for marker in _MALFORMED_TOKEN_MARKERS):
            return (
                CREDENTIAL_STATE_MALFORMED,
                f"GET /user returned HTTP 401 with malformed-token-shape "
                f"vocabulary in the response body: {message!r}.",
            )
        return (
            CREDENTIAL_STATE_REJECTED,
            f"GET /user returned HTTP 401 (well-formed credential shape, "
            f"rejected -- dead/expired/revoked): {message!r}.",
        )
    if status == 403:
        return (
            CREDENTIAL_STATE_INSUFFICIENT_SCOPE,
            f"GET /user returned HTTP 403: {message!r}. This is the "
            f"documented shape for a GitHub App installation token (which "
            f"cannot call GET /user) as well as an ordinary scope refusal -- "
            f"either way, the credential authenticated but this endpoint "
            f"refused it.",
        )
    return (
        CREDENTIAL_STATE_UNKNOWN,
        f"GET /user returned HTTP {status} -- not a status this probe "
        f"recognizes as ok/malformed/rejected/insufficient-scope: "
        f"{message!r}.",
    )


def probe_github_credential(token: str, *, opener=None) -> CredentialProbeResult:
    """Probe a GitHub credential's validity via a single `GET /user` — the
    cheapest authenticated read GitHub exposes.

    Never pushes, never writes. Distinguishes the same five states
    probe_forgejo_credential does (see that function's docstring) — GitHub's
    own vocabulary for a malformed/garbled token also uses generic
    credential-shape language ("invalid", "malformed") that
    push.git_push._MALFORMED_TOKEN_MARKERS already recognizes, reused
    verbatim here (module docstring, MALFORMED-TOKEN VOCABULARY).

    *token* is the caller's ALREADY-RESOLVED credential (never resolved by
    this function — see module docstring, NEVER READ CONFIG).
    """
    url = f"{GITHUB_API_BASE}/user"
    try:
        status, body = request_json("GET", url, token, None, opener=opener, timeout=PROBE_TIMEOUT_SECONDS)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return CredentialProbeResult(
            platform=PLATFORM_GITHUB,
            state=CREDENTIAL_STATE_UNREACHABLE,
            detail=f"GET /user unreachable -- {exc}",
            token_sha256=_token_sha256(token),
            resolved={"api_base": GITHUB_API_BASE},
        )

    state, detail = _classify_github_status(status, body)
    return CredentialProbeResult(
        platform=PLATFORM_GITHUB,
        state=state,
        detail=detail,
        token_sha256=_token_sha256(token),
        resolved={"api_base": GITHUB_API_BASE, "http_status": status},
    )


__all__ = [
    "CREDENTIAL_STATES",
    "CREDENTIAL_STATE_INSUFFICIENT_SCOPE",
    "CREDENTIAL_STATE_MALFORMED",
    "CREDENTIAL_STATE_OK",
    "CREDENTIAL_STATE_REJECTED",
    "CREDENTIAL_STATE_UNKNOWN",
    "CREDENTIAL_STATE_UNREACHABLE",
    "PROBE_TIMEOUT_SECONDS",
    "CredentialProbeResult",
    "probe_forgejo_credential",
    "probe_github_credential",
]
