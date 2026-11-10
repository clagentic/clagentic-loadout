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
  - ANSI escape sequences and C0/C1 control characters, including DEL
    (pre-merge security review finding, hardened per a second review pass
    that caught the docstring initially overclaiming C1 coverage the regex
    did not yet deliver): remote-controlled text (a "remote: "-prefixed
    line, a parsed reject-reason string) reaches operator-visible stderr
    verbatim otherwise, and a malicious remote can inject terminal escapes
    into it (cursor movement, screen-clearing, title-bar manipulation, or a
    terminal-emulator-specific escape with its own side effects). Stripped
    unconditionally -- this is not a secret-shaped pattern, it is a
    character-class removal, and it runs regardless of whether any secret
    pattern above also matched. `\\t`/`\\n`/`\\r` are preserved (ordinary
    formatting whitespace this module's own multi-line messages rely on);
    every other C0 control character, DEL (0x7F), and the full C1 range
    (U+0080-U+009F) are stripped. This function operates on `str`
    (already-decoded text), never raw bytes -- see `_CONTROL_CHAR_RE`'s own
    comment for why stripping the C1 codepoint range is safe post-decode
    but would NOT be safe at the byte level (0x80-0x9F are UTF-8
    continuation bytes pre-decode).
"""

from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

#: URL userinfo: scheme://user:secret@host -- keep the scheme and host,
#: redact only the password half of the userinfo.
#: Split into a scheme+user prefix and a delimiter finder so the password
#: scan can be shared across prefixes: one combined pattern rescans the
#: whole whitespace-free run from every `http://a:` start, which is
#: quadratic on a long run of them with no `@`.
_URL_USERINFO_PREFIX_RE = re.compile(r"https?://[^:/@\s]+:", re.IGNORECASE)
_URL_PASSWORD_END_RE = re.compile(r"[@\s]")

#: Authorization:/Credential: header VALUES -- the header name is kept
#: (diagnostic value: "an Authorization header was present"), only what
#: follows the colon is masked, through the end of the line. The value is a
#: plain negated class: a lazy `.*?` with a trailing-whitespace lookahead
#: rescans a long whitespace run once per candidate end.
_AUTH_HEADER_RE = re.compile(
    r"((?:Authorization|Credential)\s*:\s*)(\S[^\r\n]*)",
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

#: C0 control characters and DEL, plus the C1 range, other than the ones
#: this module's own multi-line messages rely on (`\t`=0x09, `\n`=0x0a,
#: `\r`=0x0d -- kept, since a message body legitimately uses these for
#: formatting). Matches a single control CODEPOINT at a time -- no
#: quantifier at all, so there is nothing here for a regex engine to
#: backtrack over.
#:
#: C1 (U+0080-U+009F) IS SAFE TO STRIP HERE because this function operates
#: on `str` (already-decoded Unicode text), never on raw bytes -- every
#: caller in this package passes a `str` obtained via `subprocess.run(...,
#: text=True)`, which has already performed UTF-8 decoding. At the BYTE
#: level, 0x80-0x9F are continuation bytes inside legitimate multi-byte
#: UTF-8 sequences, and stripping them naively pre-decode would corrupt
#: valid non-ASCII content in a remote message. Post-decode, each Python
#: `str` character in this range is an unambiguous, individually-addressable
#: C1 control codepoint (not a byte fragment) -- stripping it cannot split
#: or corrupt a multi-byte sequence, because decoding has already resolved
#: every sequence into whole codepoints by the time this regex ever runs.
#: See `test_control_char_stripping_preserves_legitimate_multibyte_utf8`
#: for a regression case asserting genuine non-ASCII text survives intact.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]")


def _strip_ansi_and_control_chars(text: str) -> str:
    """Remove ANSI escape sequences and non-formatting C0/C1 control
    characters (including DEL) from *text* (pre-merge security review
    finding): remote-controlled text reaching operator-visible stderr must
    not be able to inject terminal escapes. Unconditional -- not gated on
    any secret-shaped match. *text* MUST be a `str` (already-decoded) --
    see `_CONTROL_CHAR_RE`'s own comment for why the C1 range is only safe
    to strip post-decode, never on raw bytes."""
    stripped = _ANSI_ESCAPE_RE.sub("", text)
    return _CONTROL_CHAR_RE.sub("", stripped)


def _redact_url_userinfo(text: str) -> str:
    """Mask the password half of `scheme://user:password@host` in linear time.

    The password is the non-whitespace, non-`@` run after `user:` and counts
    only when an `@` ends it. The end of that run is looked up once and
    reused by every later prefix that falls inside it, so a long run of
    prefixes with no `@` is scanned once, not once per prefix."""
    out: list[str] = []
    pos = 0  # start of text not yet copied to *out*
    search_from = 0
    run_end = -1  # cached index of the next `@`/whitespace at or after a prior start
    while True:
        prefix = _URL_USERINFO_PREFIX_RE.search(text, search_from)
        if prefix is None:
            break
        password_start = prefix.end()
        if run_end < password_start:
            delim = _URL_PASSWORD_END_RE.search(text, password_start)
            run_end = delim.start() if delim else len(text)
        if run_end > password_start and run_end < len(text) and text[run_end] == "@":
            out.append(text[pos:password_start])
            out.append(_REDACTED)
            pos = run_end  # the `@` is kept
            search_from = run_end + 1
        else:
            search_from = password_start
    out.append(text[pos:])
    return "".join(out)


def redact_push_secrets(text: str, *, known_secrets: tuple[str, ...] = ()) -> str:
    """Return *text* with every known-secret pattern masked AND every ANSI
    escape / control character stripped.

    *known_secrets*: literal values this call site holds and knows must
    never appear in output (the minted token, verbatim) -- masked by EXACT
    VALUE MATCH first, before the generic pattern-based passes below, so a
    short or unusually-shaped token is still caught even if it happens not
    to match `_BEARER_OR_TOKEN_RE`'s shape assumption.
    """
    # Stripped first: a control character spliced into a keyword
    # (`Bea<BEL>rer <token>`) would otherwise defeat every pattern below.
    redacted = _strip_ansi_and_control_chars(text)
    for secret in known_secrets:
        if secret:
            redacted = redacted.replace(secret, _REDACTED)
    redacted = _redact_url_userinfo(redacted)
    redacted = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}{_REDACTED}", redacted)
    redacted = _BEARER_OR_TOKEN_RE.sub(_REDACTED, redacted)
    return redacted


__all__ = ["redact_push_secrets"]
