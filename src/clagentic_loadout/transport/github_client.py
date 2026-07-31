"""transport.github_client — the shared, redirect-hardened GitHub REST
request primitive every GitHub-facing backend in this package builds on.

Post-Wave-B extraction (lr-e1f9, tome #687 EPIC E). Wave B built three
independent GitHub backends (review/github_backend.py, push/github_backend.py,
merge/github_backend.py), each rolling its own private
`_github_request`-shaped urllib wrapper: build a urllib.request.Request with
a bearer-token Authorization header, send it through
transport.redirect_guard.no_redirect_opener() by default (never bare
urlopen — see redirect_guard's own docstring for the token-replay class this
guards against), and translate the response (or an HTTPError) into
(status_code, parsed_body). All three wrappers agreed on: the same public
GitHub API base URL, the same X-GitHub-Api-Version header value, the same
redirect-refused-is-a-3xx-HTTPError-never-a-second-request contract, and the
same "non-2xx/redirect never raises here — the caller's own status-code
check does the failing closed" posture.

WHAT MOVED HERE: the transport-shaping primitive only —
GITHUB_API_BASE + request_json(). Every verb's OWN endpoint URLs, payload
shapes, response-field extraction, and error-type translation stay in that
verb's own github_backend.py. Same "extract the shared transport, keep the
divergent gate logic local" shape the redirect_guard extraction (Wave B
slice 2, PR#9) established for the Forgejo side, one level up.

WHAT DID NOT MOVE, AND WHY (evidence-based — genuinely divergent, not a
missed abstraction):
  - Response-body parsing strategy differs PER VERB, preserved exactly via
    `parse_mode`:
      "strict"       (push, merge): a non-empty body is ALWAYS json.loads'd
                      — a malformed JSON body on a 2xx raises
                      json.JSONDecodeError uncaught, exactly as the
                      pre-extraction push/merge backends did (neither ever
                      checked Content-Type; both parsed unconditionally).
      "content_type" (review): a non-empty body is JSON-parsed only when
                      the response's Content-Type header contains "json";
                      otherwise the raw text is returned. review is the
                      only verb whose GitHub calls (GET /user) can
                      legitimately return a non-JSON body on the success
                      path, hence the extra check the other two never
                      needed.
    Force-fitting these into one parse rule would either raise where a verb
    used to tolerate a parse failure (content_type callers) or silently
    swallow a malformed body where a verb used to fail loudly (strict
    callers) — a real behavior change either direction, so the mode stays
    an explicit parameter rather than a single hardcoded rule.
  - review.github_backend's identity resolution (GET /user, with a 403
    resolving the bot login from a configured App slug rather than any
    further live call — see that module's lr-d31e fix for why GET /app
    can never be used here), merge.github_backend's merge_pr
    (PUT .../merge, the 200+merged:false "processed but did not merge"
    shape, 405/409/404 disambiguation), and push.github_backend's
    create_pr/update_pr (POST/PATCH .../pulls) are each a single-purpose
    endpoint call with its own payload/response shape and its own
    error-translation vocabulary (ReviewPostError/ReviewVerifyError vs
    MergeExecutionError vs PrOpenError) — these stay in each verb's own
    module, never forced into one shared "do a GitHub PR thing" call.
  - Network-error handling diverges deliberately: push/merge backends catch
    urllib.error.URLError/TimeoutError/OSError at their own call sites and
    translate to their own exception type; review's backend lets a network
    error propagate unwrapped. request_json() does not swallow or translate
    network errors itself — each verb backend's own call site keeps doing
    that translation, matching its own pre-extraction behavior byte-for-byte.

Redirect hardening (lr-412f security review finding, preserved unchanged):
every call here carries a live GitHub bearer/App-installation token in its
Authorization header. request_json() defaults to
transport.redirect_guard.no_redirect_opener() — the SAME hardened opener
transport.git_host_api's Forgejo path and every pre-extraction GitHub
backend already used — rather than bare urlopen; a 3xx surfaces as
urllib.error.HTTPError, handled by the ordinary non-2xx error path, never a
silent second request to the redirect's Location target.

`opener` injects a urllib opener's .open callable for tests — no real
network call is ever made when a fake opener is supplied.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Literal

from clagentic_loadout.transport.redirect_guard import no_redirect_opener

#: The real public GitHub API base URL — brand-neutral, not an operator
#: host; every GitHub deployment consuming these backends talks to this same
#: endpoint (GitHub Enterprise Server users pass their own base via a future
#: config seam if/when that becomes a real caller — not invented here
#: speculatively). Single source of truth: review/push/merge github_backend
#: modules each re-export this value under their own pre-existing constant
#: name (review._GITHUB_API, push.GITHUB_API_BASE, merge.GITHUB_API_BASE) so
#: no caller of those modules' public surface has to change.
GITHUB_API_BASE = "https://api.github.com"

#: GitHub REST API version header value, sent by every call site that used
#: to set it locally (all three pre-extraction backends agreed on this
#: value).
GITHUB_API_VERSION = "2022-11-28"

#: Response-body parsing strategy — see this module's docstring
#: "WHAT DID NOT MOVE" section for why this stays a parameter rather than
#: one shared rule.
ParseMode = Literal["strict", "content_type"]


def request_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    *,
    accept: str = "application/vnd.github+json",
    extra_headers: dict[str, str] | None = None,
    parse_mode: ParseMode = "strict",
    timeout: int = 30,
    opener=None,
    opener_factory=no_redirect_opener,
) -> tuple[int, Any]:
    """Make an authenticated GitHub API request. Token appears ONLY in the
    Authorization header — never in the URL, never logged.

    Returns (status_code, parsed_body). Success-path parsing depends on
    parse_mode (see module docstring):
      "strict"       — a non-empty body is always json.loads'd; malformed
                       JSON on a 2xx propagates uncaught, matching push/
                       merge's pre-extraction backends exactly.
      "content_type" — a non-empty body is JSON-parsed only when the
                       response Content-Type contains "json"; otherwise the
                       raw text is returned, matching review's
                       pre-extraction backend exactly.
    An empty body returns {} under both modes.

    A non-2xx HTTPError response is NEVER raised here — callers key their
    own fail-closed behavior off the returned status code. A malformed or
    empty error-response body is tolerated (returns {}) so a caller's own
    status-code check is what fails closed, not a body-parse exception.

    Network-level failures (urllib.error.URLError, TimeoutError, OSError)
    propagate uncaught — each verb backend's own call site translates those
    to its own exception vocabulary, matching pre-extraction behavior.

    Redirect hardening (lr-412f): defaults to
    transport.redirect_guard.no_redirect_opener() rather than bare urlopen —
    see this module's own docstring. `opener` injects a urllib opener's
    .open callable for tests; no real network call is ever made when a fake
    opener is supplied.

    `opener_factory` (default: the real no_redirect_opener) is a SEPARATE
    injection point from `opener`, called with no arguments to build the
    opener ONLY when `opener` itself is None. It exists so each verb's own
    github_backend.py module can pass its OWN module-level `no_redirect_
    opener` name through (e.g. `opener_factory=no_redirect_opener` imported
    into that module's namespace) — each pre-extraction backend's own
    redirect-hardening test suite monkeypatches that name at ITS module
    path (e.g. `clagentic_loadout.merge.github_backend.no_redirect_opener`),
    and this seam keeps that patch target meaningful after the request
    shaping itself moved into this shared module.
    """
    headers = {
        "Authorization": f"token {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    if extra_headers:
        headers.update(extra_headers)

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    urlopen = opener if opener is not None else opener_factory().open
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = getattr(resp, "status", None) or resp.getcode()
            if parse_mode == "content_type":
                content_type = (
                    resp.headers.get("Content-Type", "") if hasattr(resp, "headers") else ""
                )
                if "json" in content_type:
                    return status, json.loads(raw.decode("utf-8")) if raw else {}
                return status, raw.decode("utf-8", errors="replace")
            return status, json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            # A 3xx here means no_redirect_opener()'s handler refused to
            # follow it (see this module's docstring) -- a redirect refusal,
            # not an ordinary HTTP error response. Every caller checks for a
            # specific success status before treating a result as success,
            # so returning the bare 3xx status here already fails closed,
            # and never even attempts to read/parse a body whose content
            # (e.g. a Location-bearing redirect page) was never meant for
            # this caller.
            return exc.code, {}
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            body: Any = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        return exc.code, body


__all__ = ["GITHUB_API_BASE", "GITHUB_API_VERSION", "ParseMode", "request_json"]
