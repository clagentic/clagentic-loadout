"""transport.host_match — shared "does this URL's host:port match that
authority" predicate.

Extracted (lr-0e39f9) from transport.git_host_api's own
`_absolute_url_host_matches_git_host_base` (lr-69af67), which fixed the exact
defect class this task closes on a SECOND call site: a live bearer token
must never be attached to a request whose destination host was never
verified against an expected authority. git_host_api.py already anchors an
absolute-URL PATH argument against its own resolved git-host base; push.verb
had no equivalent anchor at all for the host its OWN resolved `api_base`
(derived from the live git remote, see push.git_coords.parse_forgejo_coords)
actually targets. Rather than push.verb re-deriving the same
urlsplit-and-compare-netloc logic as a second, independently-drifting copy
(the exact "two copies of the same resolution logic" defect lr-cd3113 named
and closed for a different value), both call sites now import this one
predicate. `git_host_api.py` re-exports its pre-existing private name as a
thin alias so it stays byte-for-byte unaffected; `push.host_guard` imports
`host_matches` directly.
"""

from __future__ import annotations

import urllib.parse


def _netloc(value: str) -> str:
    """Extract the lowercased host:port authority from *value*, which may be
    a full URL ("scheme://host:port/path...") or a bare authority
    ("host:port" or "host", no scheme).

    `urllib.parse.urlsplit` only populates `.netloc` when its input has a
    "//" authority marker -- a bare "host:port" string with no scheme has an
    EMPTY `.netloc` (and, confusingly, "host" is parsed as a SCHEME with
    "port" as the path, since a bare "name:value" string matches urlsplit's
    generic scheme grammar). Prepending "//" when *value* has no such marker
    forces urlsplit to parse it as an authority instead, exactly as if the
    caller had written "//host:port" -- the scheme-vs-authority ambiguity a
    bare value otherwise creates is resolved BEFORE urlsplit ever sees it,
    rather than working around whichever component urlsplit happened to
    guess the bare string was.
    """
    normalized = value if "//" in value else f"//{value}"
    return urllib.parse.urlsplit(normalized).netloc.lower()


def host_matches(candidate: str, reference: str) -> bool:
    """True iff *candidate* and *reference* share the same host:port
    authority component.

    Each argument may be a bare authority ("host:port" or "host"), or a full
    URL ("scheme://host:port/path...") -- see `_netloc` for how each shape is
    normalized to its authority component before comparison, so a caller
    comparing two full URLs, two bare authorities, or one of each all get the
    same, correct answer. Comparison is case-insensitive (hostnames are not
    case-sensitive) and exact on the netloc string otherwise -- host AND port
    must both agree; a same-hostname-different-port pair (e.g. a
    reverse-proxy misconfiguration or copy-paste typo) is correctly treated
    as a mismatch, never silently normalized to "close enough."

    This is a pure string comparison against an ALREADY-RESOLVED reference
    authority -- it answers "does this candidate match the expected host",
    never "is the expected host itself correct" (that is each caller's own
    resolution-precedence concern, e.g. git_host_api._resolve_git_host_base
    or push.host_guard.resolve_allowed_hosts).
    """
    return _netloc(candidate) == _netloc(reference)


__all__ = ["host_matches"]
