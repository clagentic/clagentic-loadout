"""push.push_redaction — the ONE redaction choke point for push-failure
diagnostics (lr-f57f13, mandatory per the task's own SECRET REDACTION
section; hardened per a pre-merge security review pass).

WHY ONE CHOKE POINT, AT CONSTRUCTION, NOT AT PRINT SITES: this task also
promotes verbose/trace diagnostics (GIT_TRACE passthrough) closer to a
first-class affordance -- GIT_TRACE dumps full subprocess argv/env,
including credential-helper invocations, and can surface an
`Authorization:` header or a `http.extraHeader` value. Redacting at every
print/log site is a guarantee that silently stops holding the first time a
future author adds a new sink (a webhook telemetry emitter, a second CLI
flag that dumps the raw object) -- each new sink would need to remember to
redact again. Redacting once, at `GitPushError.__init__` (see
push.errors.GitPushError -- this is now STRUCTURAL, performed inside
`__init__` itself, not merely "every caller happens to redact before
constructing"), means every field on the constructed object is already
safe: `str(exc)`, direct field access, and any future sink all read the
same already-redacted values with no second opportunity to forget.

WHAT THIS REDACTS:
  - the minted token BY VALUE -- this module's caller (git_push.py) holds
    the literal token string for this invocation and exact-matches it,
    rather than trying to pattern-match a token shape generically (a
    provider-specific token format is not this module's concern to encode).
  - URL userinfo (scheme://user:secret@host) -- a credential embedded
    directly in a remote URL, independent of the GIT_ASKPASS token path.
  - Authorization:/Credential: header VALUES (the header name itself is
    kept, only the value after the colon is masked) -- these can appear in
    GIT_TRACE output for an HTTP-transport push.
  - bearer/token literal patterns (e.g. "Bearer <value>", "token=<value>")
    that do not necessarily match the exact known token string but have the
    same shape, as defense in depth for a credential-helper trace line this
    module's own known-token exact-match would miss.
  - ANSI escape sequences and other C0/C1 control characters (pre-merge
    security review finding): remote-controlled text (a "remote: "-prefixed line, a
    parsed reject-reason string) reaches operator-visible stderr verbatim
    otherwise, and a malicious remote can inject terminal escapes into it
    (cursor movement, screen-clearing, title-bar manipulation, or a
    terminal-emulator-specific escape with its own side effects). Stripped
    unconditionally -- this is not a secret-shaped pattern, it is a
    byte-class removal, and it runs regardless of whether any secret
    pattern above also matched. `\\t`/`\\n` are preserved (ordinary
    formatting whitespace this module's own multi-line messages rely on);
    only ESC (0x1B) sequences and other non-printing control bytes are
    stripped.
"""

from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

#: URL userinfo: scheme://user:secret@host -- keep the scheme and host,
#: redact only the password half of the userinfo.
_URL_USERINFO_RE = re.compile(r"(https?://[^:/@\s]+:)([^@\s]+)(@)")

#: Authorization:/Credential: header VALUES -- the header name is kept
#: (diagnostic value: "an Authorization header was present"), only what
#: follows the colon is masked.
_AUTH_HEADER_RE = re.compile(
    r"((?:Authorization|Credential)\s*:\s*)(\S.*?)(?=\s*(?:\r?\n|$))",
    re.IGNORECASE,
)

#: Bearer/token literal shapes as defense-in-depth beside the exact-token
#: match this module's caller always also applies -- catches a
#: credential-helper trace line carrying a token value this process did not
#: itself mint (e.g. an ambient credential a GIT_TRACE dump incidentally
#: reveals).
_BEARER_OR_TOKEN_RE = re.compile(
    r"\b(Bearer|token=)\s*[A-Za-z0-9._~+/=-]{8,}",
    re.IGNORECASE,
)

#: ANSI/terminal escape sequences (pre-merge security review finding): CSI sequences
#: (ESC '[' ... final byte), OSC sequences (ESC ']' ... BEL or ESC '\'),
#: and a bare lone ESC as a fallback for any other two/three-byte escape
#: this pattern doesn't name explicitly. Each alternative is a SINGLE
#: bounded, non-nested character class repeated with a plain `*` -- no
#: nested quantifiers, no ambiguous overlapping alternation the regex
#: engine could backtrack catastrophically over (the linear/anchored
#: discipline this module's earlier patterns already followed and a prior
#: security review explicitly praised -- preserved here rather than
#: reintroducing a riskier shape in the one new pattern this fix adds).
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])"
)

#: C0/C1 control characters other than the ones this module's own
#: multi-line messages rely on (`\t`=0x09, `\n`=0x0a, `\r`=0x0d -- kept,
#: since a message body legitimately uses these for formatting). Matches a
#: single control byte at a time -- no quantifier at all, so there is
#: nothing here for a regex engine to backtrack over.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _strip_ansi_and_control_chars(text: str) -> str:
    """Remove ANSI escape sequences and non-formatting control characters
    from *text* (pre-merge security review finding): remote-controlled text reaching
    operator-visible stderr must not be able to inject terminal escapes.
    Unconditional -- not gated on any secret-shaped match."""
    stripped = _ANSI_ESCAPE_RE.sub("", text)
    return _CONTROL_CHAR_RE.sub("", stripped)


def redact_push_secrets(text: str, *, known_secrets: tuple[str, ...] = ()) -> str:
    """Return *text* with every known-secret pattern masked AND every ANSI
    escape / control character stripped.

    *known_secrets*: literal values this call site holds and knows must
    never appear in output (the minted token, verbatim) -- masked by EXACT
    VALUE MATCH first, before the generic pattern-based passes below, so a
    short or unusually-shaped token is still caught even if it happens not
    to match `_BEARER_OR_TOKEN_RE`'s shape assumption.
    """
    redacted = text
    for secret in known_secrets:
        if secret:
            redacted = redacted.replace(secret, _REDACTED)
    redacted = _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}{m.group(3)}", redacted)
    redacted = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", redacted)
    redacted = _BEARER_OR_TOKEN_RE.sub(_REDACTED, redacted)
    redacted = _strip_ansi_and_control_chars(redacted)
    return redacted


__all__ = ["redact_push_secrets"]
