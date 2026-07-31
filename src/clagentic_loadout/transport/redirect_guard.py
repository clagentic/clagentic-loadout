"""transport.redirect_guard — the one no-redirect urllib opener every
bearer-token-carrying HTTP call in this package's transport layer builds
through.

Extracted (lr-412f pre-merge security review finding) from git_host_api.py's
local _NoRedirectHandler after the SAME redirect-token-leak class recurred
for a fourth time in this package's history (independent local copies in
earlier release-verb slices, then Wave B slice 2's github_backend.py
rolling a bespoke urlopen with none of the hardening at all). Prior modules
in this package each carried their own local copy of this exact handler
with a "duplicated, not imported, so the two verbs stay decoupled"
rationale; that rationale stops applying once a THIRD caller in the same
package (github_backend.py) needs the identical protection for a
bearer/App-installation token header — at that point "stay decoupled" is
costing a real security property (a caller can forget the local copy,
exactly as github_backend.py did), not buying isolation.

WHY THIS MATTERS: every call site importing this module sends a live bearer
token in its Authorization header. Python's urllib.request.HTTPRedirectHandler
(the default) follows a 3xx response and RE-ISSUES the request — original
headers intact, including Authorization — against the redirect's Location
target. If the target host is compromised, misconfigured, or sits behind a
misbehaving reverse proxy, that Location can be an attacker-controlled host,
and the live token would be replayed to it.

USAGE: call no_redirect_opener() to get a urllib opener whose .open is safe
to pass as request()'s `opener` callable (or call directly). A 3xx response
surfaces as urllib.error.HTTPError to the caller, handled by the ordinary
error path like any other failure — never a silent second request.
"""

from __future__ import annotations

import urllib.request


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses to follow any 3xx redirect (security review finding,
    lr-412f — a recurring redirect-token-leak class across this package's
    transport-carrying verbs).

    urllib's default HTTPRedirectHandler re-issues the request — WITH its
    original headers, including a live bearer token in Authorization — against
    the Location target. If that target is a different (possibly
    attacker-controlled) host, the token is replayed to it. Returning None
    from redirect_request() tells urllib not to follow; the original 3xx
    response then surfaces to the caller as an HTTPError, treated by the
    caller as an ordinary failed call like any other non-2xx status — never
    a silent second request.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802 -- stdlib override
        del req, fp, code, msg, headers, newurl
        return None


def no_redirect_opener():
    """Build a urllib opener that never follows redirects (see
    NoRedirectHandler). Constructed lazily by each caller so a test-injected
    opener never has to go through this at all — no shared/module-level
    opener instance, so callers never accidentally share connection state."""
    return urllib.request.build_opener(NoRedirectHandler)


__all__ = ["NoRedirectHandler", "no_redirect_opener"]
