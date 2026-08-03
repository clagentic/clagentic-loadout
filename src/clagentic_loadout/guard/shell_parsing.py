"""guard.shell_parsing — shell-word normalization / compound-command
detection CORE (lr-7ff55e, port of the reference deployment's guard-bash.py
shell-parsing layer; lr-5a8d epic, guard-bash decomposition slice 1 of 3).

WHY THIS IS ITS OWN MODULE (lr-5a8d comment #7): the reference deployment's
guard-bash.py is a single 5,842-line file — porting it whole would violate
this project's no-god-file hard rule and would not be independently
reviewable. The reference decomposes into three natural seams: this
PARSING/NORMALIZATION core (pure text processing, no policy), a CONTAINMENT/
SCRATCH/SCOPE layer (lr-288fad, builds on this module plus the already-landed
`guard.scratch_policy`), and a PER-ROLE ALLOW-CHECKER layer (lr-19ae42, the
bulk of the file, role-keyed per CLAUDE.md rule 1). This module lands FIRST
because both later layers consume it.

PORT PATTERN (mirrors `guard.write_scope`'s port-pattern doc, lr-fd279d):

1. STRIP THE HOOK SHELL. The reference functions are called from a
   PreToolUse stdin-JSON-in/exit-code-out hook `main()`. None of that
   harness plumbing is ported — every function here is a pure function over
   an explicit `str` command line (or a value derived from one), with no
   stdin read, no process exit, no hook-contract awareness at all
   (CLAUDE.md rule 6a).
2. NO POLICY, NO IDENTITY. Every function in this module answers a
   structural question about a shell command string — "what are its real
   words," "does it contain a compound operator," "what does a redirect
   target resolve to" — never "is this role/verb allowed." The reference
   file's `_compound_check` also carries a policy carve-out for three
   specific loadout-transport verbs' `--body-stdin` pipe shape
   (`_is_admitted_loadout_body_stdin_pipe` /
   `_classify_body_stdin_pipe_ambiguity`, reference guard-bash.py
   ll.598-767); that carve-out is deliberately NOT ported here — it is a
   per-verb admission decision (policy), not shell parsing, and belongs
   with whichever later slice owns transport-verb admission. This module's
   `compound_check` only ever answers the structural question ("is this a
   compound/piped/backgrounded shell expression"); a caller wanting a
   narrow pipe carve-out for a specific verb shape composes that check
   BEFORE calling `compound_check`, exactly as the reference's
   `_compound_check` composes its own carve-out before falling through to
   the same structural scan this module ports.
3. NO HARDCODED PATHS. `detect_tmp_redirect_target` and
   `is_staging_redirect_target` take their staging-root prefix
   (`$TMPDIR` by convention — lr-f8649f: `$HOME` dropped, TMPDIR-only, see
   `guard.scratch_policy`'s own "TMPDIR-ONLY NARROWING" — matching the
   shell's own unexpanded variable syntax) as a plain string literal with no
   machine-specific absolute path baked in anywhere — a caller's shell
   environment resolves `$TMPDIR` itself; this module only recognizes the
   unexpanded token form in command text, mirroring the reference's own
   posture (reference guard-bash.py ll.1374-1384, `_WRITE_REDIRECT_TARGET_RE`
   / `_is_staging_redirect_target`).
4. FAIL CLOSED ON AMBIGUITY. Every normalization function that can fail to
   confidently parse a command (unbalanced quoting, an unresolvable ANSI-C
   escape, a quoted command-substitution opener) returns `None` rather than
   guessing. This is the reference file's core security invariant
   (lr-8916 "deny-on-ambiguity, fail CLOSED") and is preserved exactly: a
   caller MUST treat `None` as "cannot confidently normalize" and fall back
   to its own deny-on-ambiguity posture — `None` is never a permissive
   signal.

FUNCTIONS PORTED (reference guard-bash.py ll.148-1400, per lr-7ff55e scope):

  - `decode_for_match` — iterative URL-decode for evasion-resistant
    substring matching (reference `_decode_for_match`, ll.322-337).
  - `unquoted_spans` — spans of a command outside quotes/backslash-escapes
    (reference `_unquoted_spans`, ll.3901-3932; ported here because
    `mask_quoted_spans` depends on it and both are pure span-arithmetic
    with no policy content — despite living far apart in the reference
    file's line numbering, they are one cohesive parsing primitive).
  - `mask_quoted_spans` — blank every ISOLATED quoted argv token to spaces,
    quote-aware and command-substitution-aware (reference
    `_mask_quoted_spans`, ll.417-471).
  - `decode_ansi_c_escapes` — decode the bash ANSI-C ($'...') backslash-
    escape grammar (reference `_decode_ansi_c_escapes`, ll.1135-1197).
  - `quote_delimited_spans` — every quoted region (bare and ANSI-C) with
    its quote-removed literal content (reference `_quote_delimited_spans`,
    ll.1200-1261).
  - `has_unresolved_ansi_c_quote` — trigger condition for the ANSI-C
    hard-deny-on-ambiguity path (reference `_has_unresolved_ansi_c_quote`,
    ll.1280-1288).
  - `split_glued_redirect_operators` — separate a glued fd-dup + write
    redirect pair (`2>&1>/x`) into independent tokens (reference
    `_split_glued_redirect_operators`, ll.1044-1053).
  - `normalize_shell_words` — the shell-word-normalization pre-pass: joins
    glued quote fragments into real shell words, blanks isolated quoted
    spans, splits glued redirects, fails closed on ambiguity (reference
    `_normalize_shell_words`, ll.1291-1371).
  - `split_segments` — split a command at top-level `; && || | &`,
    quote-aware (reference `_split_segments`, ll.3823-3898; the tightly-
    coupled tokenizer `compound_check`'s structural scan is built on top
    of, alongside `normalize_shell_words`).
  - `cmd_head` — strip heredoc body content so only the actual command
    token is scanned (reference `_cmd_head`, ll.890-903).
  - `is_safe_redirect_only` — true iff a command's only shell syntax is
    simple `<`/`>` redirects, no pipes/chains (reference
    `_is_safe_redirect_only`, ll.411-414).
  - `has_background_operator` — detect an unquoted shell backgrounding `&`
    (excluding fd-dup redirects like `2>&1`) (reference
    `_has_background_operator`, ll.868-887).
  - `compound_check` — the top-level structural gate: compound/piped/
    chained/backgrounded shell expression detection, quote-aware, built
    from all of the above (reference `_compound_check`, ll.770-843, MINUS
    the loadout-verb pipe carve-out — see point 2 above).
  - `detect_tmp_redirect_target` — resolve an unquoted `/tmp/...`
    write-redirect target, quote-aware (reference
    `_detect_tmp_redirect_target`, ll.963-983).
  - `is_staging_redirect_target` — true iff a redirect target is `$TMPDIR`
    spawn-scoped staging (lr-f8649f: `$HOME` dropped, TMPDIR-only), never a
    project-tree mutation (reference `_is_staging_redirect_target`,
    ll.1377-1384).

NOT PORTED HERE (tracked in later slices, lr-5a8d comment #8):
  - The containment/scratch/scope layer built on top of this parsing core
    (lr-288fad) — reconciles with the already-landed `guard.scratch_policy`
    rather than duplicating its boundary.
  - The per-role allow-checkers (lr-19ae42) that call into this module's
    `compound_check` and friends as one of several admission gates.
  - The loadout-transport `--body-stdin` pipe carve-out (see point 2 above).
"""

from __future__ import annotations

import re
import shlex
from urllib.parse import unquote, unquote_plus

# Bound the URL-decode loop: defends against multi-encoding (%252F,
# %25252F, ...) without unbounded work. Three passes collapses any
# realistic nesting (reference guard-bash.py `_URL_DECODE_MAX_PASSES`).
_URL_DECODE_MAX_PASSES = 3


def decode_for_match(cmd: str) -> str:
    """Iteratively URL-decode `cmd` so percent-encoded evasion attempts
    (e.g. a path separator encoded as %2F, or double-encoded as %252F)
    cannot hide a substring a caller's own downstream matcher is scanning
    for. `unquote_plus` also folds `+` -> space so a `+`-obfuscated query
    string cannot split a matched path either. Stops early once decoding
    is stable; bounded to `_URL_DECODE_MAX_PASSES` iterations.
    """
    prev = cmd
    for _ in range(_URL_DECODE_MAX_PASSES):
        nxt = unquote_plus(unquote(prev))
        if nxt == prev:
            break
        prev = nxt
    return prev


def unquoted_spans(s: str) -> list[tuple[int, int]]:
    """Return (start, end) spans of `s` that are outside quotes/backslash
    escapes. The complement of `quote_delimited_spans`'s regions."""
    spans: list[tuple[int, int]] = []
    start = 0
    i, n = 0, len(s)
    quote: str | None = None
    while i < n:
        c = s[i]
        if quote:
            if c == "\\" and quote == '"' and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
                i += 1
                start = i
                continue
            i += 1
            continue
        if c in ("'", '"'):
            if i > start:
                spans.append((start, i))
            quote = c
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        i += 1
    if quote is None and start < n:
        spans.append((start, n))
    return spans


def mask_quoted_spans(s: str) -> str | None:
    """Return `s` with the contents of every quoted argv token replaced by
    spaces, preserving string length and all unquoted characters verbatim.

    Used to make a scan (compound-shell detection, redirect detection)
    quote-aware: a metacharacter (`|`, `&&`, `;`, `(` `)`) INSIDE a quoted
    token — e.g. a comment body, search alternation, or task description —
    is data, not shell syntax, and must not be visible to the scan. Only
    characters in `unquoted_spans(s)` remain unmasked.

    Returns None on malformed/unbalanced quoting so the caller can fall
    back to a conservative deny (deny-on-ambiguity) rather than silently
    admitting a command whose quoting could not be parsed.

    Also returns None when a quoted span contains `$(` or a backtick:
    double quotes do not block command substitution, so e.g.
    `echo "$(git push --force)"` is a REAL executable mutation sitting
    inside a quoted argv span, not narrative data. Blanking that span
    would hide the live command from every scan that consumes this
    function's output — fail closed instead, exactly as unbalanced-quote
    parsing already does.
    """
    try:
        shlex.split(s, posix=True)
    except ValueError:
        return None
    spans = unquoted_spans(s)

    is_unquoted = [False] * len(s)
    for start, end in spans:
        for i in range(start, end):
            is_unquoted[i] = True

    for i, ch in enumerate(s):
        if is_unquoted[i]:
            continue
        if ch == "`" or (ch == "$" and s[i + 1 : i + 2] == "("):
            return None

    masked = [" "] * len(s)
    for start, end in spans:
        masked[start:end] = s[start:end]
    return "".join(masked)


# Bash ANSI-C quoting, $'...' (and the locale-translated form $"..."), is a
# DISTINCT quote operator from bare '...'/"...". A naive tokenizer that
# treats the leading `$` as an ordinary glued character and the following
# '...'/"..." as an independent bare-quote span normalizes $'git' to the
# CONCATENATION `$` + `git` = `$git` — a string containing no literal `git`
# token at all, hiding the real verb from every downstream matcher. This
# decoder recognizes `$'` / `$"` as the START of the quoted region (the `$`
# is consumed as part of the region) and decodes the ANSI-C backslash-escape
# grammar (POSIX Shell & Utilities, "Quoting" ANSI-C section) into the
# literal characters bash would substitute.
_ANSI_C_SIMPLE_ESCAPES = {
    "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n",
    "r": "\r", "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"',
    "?": "?",
}


def decode_ansi_c_escapes(raw: str) -> str:
    """Decode the ANSI-C ($'...') backslash-escape grammar in `raw`.

    Supports the simple single-character escapes, octal (\\nnn, 1-3
    digits), hex (\\xHH, 1-2 hex digits), and Unicode (\\uHHHH / \\UHHHHHHHH)
    forms. Raises ValueError on any backslash sequence not in that
    enumerated set — deny-on-ambiguity: an escape this function cannot
    confidently resolve must never be silently dropped or passed through,
    since that could hide or fabricate a character in the joined word.
    """
    out: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            raise ValueError("trailing backslash in ANSI-C quote")
        nxt = raw[i + 1]
        if nxt in _ANSI_C_SIMPLE_ESCAPES:
            out.append(_ANSI_C_SIMPLE_ESCAPES[nxt])
            i += 2
            continue
        if nxt in "01234567":
            j = i + 1
            digits = ""
            while j < n and len(digits) < 3 and raw[j] in "01234567":
                digits += raw[j]
                j += 1
            out.append(chr(int(digits, 8) & 0xFF))
            i = j
            continue
        if nxt == "x":
            j = i + 2
            digits = ""
            while j < n and len(digits) < 2 and raw[j] in "0123456789abcdefABCDEF":
                digits += raw[j]
                j += 1
            if not digits:
                raise ValueError("\\x with no hex digits in ANSI-C quote")
            out.append(chr(int(digits, 16)))
            i = j
            continue
        if nxt in ("u", "U"):
            width = 4 if nxt == "u" else 8
            j = i + 2
            digits = ""
            while j < n and len(digits) < width and raw[j] in "0123456789abcdefABCDEF":
                digits += raw[j]
                j += 1
            if not digits:
                raise ValueError(f"\\{nxt} with no hex digits in ANSI-C quote")
            out.append(chr(int(digits, 16)))
            i = j
            continue
        # Unrecognized escape (e.g. \c, \d...) — fail closed rather than
        # guess. Bash itself has additional rare forms (\cX control chars)
        # this deliberately does not model; refusing to normalize is the
        # safe outcome, never a silent pass-through of the backslash.
        raise ValueError(f"unrecognized ANSI-C escape '\\{nxt}'")
    return "".join(out)


def quote_delimited_spans(s: str) -> list[tuple[int, int, str, bool]]:
    """Return (start, end, content, is_ansi_c) for every quoted region in
    `s`.

    `start`/`end` bound the ENTIRE quoted region including both quote-mark
    characters (the complement of `unquoted_spans`) — for an ANSI-C region
    ($'...'/$"...") `start` points at the leading `$`, so the dollar sign
    is consumed as part of the region rather than left outside it as an
    ordinary glued character. `content` is the quote-removed literal text
    (marks stripped, backslash-escapes resolved: ANSI-C backslash grammar
    for $'...'/$"..." via `decode_ansi_c_escapes`, double-quote
    backslash-escapes for "...") — exactly what bash substitutes for that
    region once quote-removal happens. `is_ansi_c` tells the caller this
    region is ALWAYS a live shell word in its own right (bash evaluates
    $'...' unconditionally, unlike a bare '...'/"..." span which can be
    genuinely isolated narrative data) — the caller must never blank an
    ANSI-C span to spaces regardless of adjacency to neighboring
    characters, or the exact verb-hiding bypass this fix closes reappears
    in the "isolated" branch instead of the "glued" one.

    Raises ValueError on unbalanced quoting or an ANSI-C escape sequence
    that cannot be confidently resolved (caller is expected to have
    already validated via shlex.split for the non-ANSI-C shapes; ANSI-C
    validity is checked independently here since shlex has no model of
    the $'...' operator at all).
    """
    regions: list[tuple[int, int, str, bool]] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        is_ansi_c = c == "$" and i + 1 < n and s[i + 1] in ("'", '"')
        if is_ansi_c or c in ("'", '"'):
            region_start = i
            if is_ansi_c:
                i += 1  # consume the leading '$' into the region
            quote = s[i]
            i += 1
            buf: list[str] = []
            while i < n and s[i] != quote:
                if s[i] == "\\" and (quote == '"' or is_ansi_c) and i + 1 < n:
                    if is_ansi_c:
                        # Deferred to decode_ansi_c_escapes below over the
                        # whole raw span — here we must only avoid
                        # stopping on an escaped quote char (\'/\") mid-scan.
                        buf.append(s[i])
                        buf.append(s[i + 1])
                        i += 2
                        continue
                    buf.append(s[i + 1])
                    i += 2
                    continue
                buf.append(s[i])
                i += 1
            if i >= n:
                raise ValueError("unbalanced quote")
            i += 1  # consume closing quote mark
            raw_content = "".join(buf)
            content = decode_ansi_c_escapes(raw_content) if is_ansi_c else raw_content
            regions.append((region_start, i, content, is_ansi_c))
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        i += 1
    return regions


# An ANSI-C span ($'...'/$"...") that `normalize_shell_words` cannot resolve
# (unrecognized escape) returns None, and every caller's documented posture
# on None is "fall back to scanning the RAW string" — but the raw string
# STILL contains the intact `$'...'` wrapper, which hides whatever verb is
# inside it from every plain-text matcher (`^git\b` does not match inside
# `$'\cXgit'`) exactly as thoroughly as the original bypass. A caller that
# only re-scans the raw string on ambiguity is not actually deny-on-ambiguity
# for THIS shape — it is silently permissive. Any caller enforcing a
# security boundary must treat "normalization failed AND the raw string
# contains an ANSI-C opener" as an unconditional deny, not merely fall back
# to a scan that cannot see through the wrapper either.
_ANSI_C_OPENER_RE = re.compile(r"\$['\"]")


def has_unresolved_ansi_c_quote(cmd: str) -> bool:
    """Return True if `cmd` contains a `$'`/`$"` opener at all.

    Intended as the trigger condition for a caller's hard-deny-on-ambiguity
    path: a caller should check this ONLY after `normalize_shell_words` has
    already returned None for the same string, so a resolvable ANSI-C span
    (which normalizes successfully and never reaches this check) never
    triggers it.
    """
    return bool(_ANSI_C_OPENER_RE.search(cmd))


# A glued fd-dup target (&<digit>) immediately followed by a redirect
# operator with NO separating whitespace (`2>&1>/workspace/evil`, bash's own
# valid syntax) is a REGEX-CAPTURE HAZARD for any `>` -> greedy-`\S+` target
# matcher: the capture swallows the write operator and its target into one
# fabricated string (`&1>/workspace/evil`) rather than exposing the real
# write target (`/workspace/evil`) as its own token. Splitting the two
# redirects into independent, whitespace-separated tokens BEFORE any such
# matcher scans closes that hazard at the tokenization layer rather than
# patching every downstream regex individually.
_REDIRECT_OPERATOR_RE_FRAGMENT = r"(?:[0-9]*>>?|&>>?)"
_GLUED_REDIRECT_BOUNDARY_RE = re.compile(
    r"(&[0-9]+)(?=" + _REDIRECT_OPERATOR_RE_FRAGMENT + r")"
)


def split_glued_redirect_operators(s: str) -> str:
    """Insert a space between an fd-dup target (&<digit>) and an
    immediately following redirect operator with no separating whitespace,
    so two glued redirects (`2>&1>/workspace/evil`) tokenize as independent
    words instead of one regex match swallowing both.

    Length-changing (inserts characters) — callers must scan the RETURNED
    string, not mix indices between the original and the split form.
    """
    return _GLUED_REDIRECT_BOUNDARY_RE.sub(r"\1 ", s)


def normalize_shell_words(s: str) -> str | None:
    """Return `s` normalized to the shell's actual words, or None on
    ambiguity.

    Every mutation/redirect matcher that scans a raw or naively-masked
    command string is vulnerable to a class of bypass rooted in one fact:
    bash tokenizes commands into WORDS by concatenating adjacent quoted/
    unquoted fragments that have no separating whitespace. Blanking every
    quoted span to spaces unconditionally is correct when the span is
    genuinely isolated narrative data (whitespace on both sides), but WRONG
    when bash would actually splice that span onto neighboring characters
    into a single word with no gap (`g""it push` -> bash's real argv[0] is
    `git`, not two fragments `g` and `it` separated by a blank). Blanking-
    to-space in that shape inserts a fake word boundary the shell never
    had, hiding the real verb from every downstream `^git\\b`-style
    matcher.

    This function normalizes the command into the shell's ACTUAL words
    ONCE, over the FULL command string, before any verb/redirect matcher
    runs:

      1. A quoted span GLUED to an adjacent non-whitespace unquoted
         character (no separating space) is part of the SAME shell word as
         that character — its literal content is preserved (unmasked) so
         the joined word reads correctly to every downstream matcher.
      2. A quoted span with whitespace (or string boundary) on BOTH sides
         is isolated narrative data, exactly like `mask_quoted_spans`'s
         behavior — it is blanked to spaces so its contents never leak
         into the operator/verb scan.
      3. An ANSI-C span ($'...'/$"...") is NEVER blanked, regardless of
         adjacency: unlike a bare '...'/"..." span, bash evaluates $'...'
         unconditionally as a live word of its own — always splicing its
         decoded literal content in is what closes the
         `$'git' push --force` bypass (mis-tokenized by a naive scanner as
         `$` + `git`, and would otherwise be correctly de-quoted but then
         WRONGLY blanked to spaces by the isolated-span branch since
         $'git' sits at a word boundary on both sides).
      4. Glued redirect operators (`2>&1>/workspace/evil`) are then split
         into independent tokens via `split_glued_redirect_operators`, run
         over the FULL joined string (not per-span).
      5. DENY-ON-AMBIGUITY (fail closed, security boundary): unbalanced
         quoting, or a quoted span containing `$(`/backtick (command
         substitution — live shell, not data) makes normalization
         impossible to trust. Returns None; every caller MUST treat None
         as "cannot confidently normalize" and fall back to its own
         existing deny-on-ambiguity posture (scanning the raw string),
         never to a permissive/allow outcome.
    """
    try:
        shlex.split(s, posix=True)
    except ValueError:
        return None

    try:
        quoted = quote_delimited_spans(s)
    except ValueError:
        return None

    # Command substitution inside a quoted span is live shell, not data —
    # deny-on-ambiguity, identical rule to mask_quoted_spans.
    for _start, _end, content, _is_ansi_c in quoted:
        if "`" in content or "$(" in content:
            return None

    def _is_word_char(ch: str) -> bool:
        # Whitespace is the only thing that separates shell words; every
        # other character (including operators like > | & ; ( )) counts
        # as "glued" if it sits immediately adjacent to a quote boundary
        # with no space — bash's quote-removal joins purely on adjacency,
        # not character class.
        return not ch.isspace()

    out_parts: list[str] = []
    cursor = 0
    for q_start, q_end, content, is_ansi_c in quoted:
        out_parts.append(s[cursor:q_start])
        left_glued = q_start > 0 and _is_word_char(s[q_start - 1])
        right_glued = q_end < len(s) and _is_word_char(s[q_end])
        if is_ansi_c or left_glued or right_glued:
            # Same shell word as its neighbor — splice the quote-removed
            # literal content directly in, no marks, no padding. ANSI-C
            # spans always take this branch (see docstring): $'...' is
            # bash-evaluated syntax, never narrative data, independent of
            # whitespace adjacency.
            out_parts.append(content)
        else:
            # Isolated quoted span — narrative data, blank to spaces
            # (length-preserving, matches mask_quoted_spans's posture).
            out_parts.append(" " * (q_end - q_start))
        cursor = q_end
    out_parts.append(s[cursor:])

    normalized = "".join(out_parts)
    return split_glued_redirect_operators(normalized)


def split_segments(s: str) -> list[tuple[str, str]]:
    """Split `s` at top-level `; && || | &` — respect single/double quotes
    and backslash escapes.

    Returns list of (op, segment_text) where op is the operator preceding
    the segment ("" for the first, one of "|", ";", "&&", "||", "&").

    Raises ValueError on unbalanced quotes.
    """
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    op = ""
    i, n = 0, len(s)
    quote: str | None = None
    while i < n:
        c = s[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(s[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            buf.append(c)
            buf.append(s[i + 1])
            i += 2
            continue
        if c == ";":
            out.append((op, "".join(buf)))
            buf = []
            op = ";"
            i += 1
            continue
        if c == "&" and i + 1 < n and s[i + 1] == "&":
            out.append((op, "".join(buf)))
            buf = []
            op = "&&"
            i += 2
            continue
        if c == "|" and i + 1 < n and s[i + 1] == "|":
            out.append((op, "".join(buf)))
            buf = []
            op = "||"
            i += 2
            continue
        if c == "|":
            out.append((op, "".join(buf)))
            buf = []
            op = "|"
            i += 1
            continue
        if c == "&":
            prev = buf[-1] if buf else ""
            nxt = s[i + 1] if i + 1 < n else ""
            if prev == ">" or nxt == ">":
                buf.append(c)
                i += 1
                continue
            out.append((op, "".join(buf)))
            buf = []
            op = "&"
            i += 1
            continue
        buf.append(c)
        i += 1
    if quote is not None:
        raise ValueError("unbalanced quote")
    out.append((op, "".join(buf)))
    return out


_HEREDOC_DELIMITER_RE = re.compile(r"<<\s*'?[A-Z_]+'?\s*\n")


def cmd_head(cmd: str) -> str:
    """Return only the shell command portion, stripping heredoc body
    content.

    A heredoc (`cat > /tmp/f << 'EOF'\\n...\\nEOF`) arrives as one string
    including the body. Forbidden-substring/compound-operator checks must
    NOT scan the body — a review comment quoting 'git add .' or 'a | b' is
    not a live git mutation or pipe. Strip everything from the first
    heredoc delimiter onward so only the actual command token is checked.
    Simple redirects (no heredoc) are returned unchanged.
    """
    heredoc_match = _HEREDOC_DELIMITER_RE.search(cmd)
    if heredoc_match:
        return cmd[: heredoc_match.start()]
    return cmd


def is_safe_redirect_only(cmd: str) -> bool:
    """Permit simple file redirects (< file, > file) but not pipes or
    chains — true iff, once every `<`/`>` redirect-and-target pair is
    stripped, none of `&& || | ; ( )` remain."""
    stripped = re.sub(r"\s*[<>]\s*\S+", " ", cmd)
    return not any(token in stripped for token in ("&&", "||", "|", ";", "(", ")"))


# Matches a bare backgrounding `&` (not part of `&&`) ANYWHERE in the
# scanned command — a single unquoted `&` is the shell backgrounding
# operator regardless of what follows it (a `cmd & disown` / `cmd & true` /
# `cmd & sleep 60` shape backgrounds exactly as much as a trailing `cmd &`
# does; an end-anchored pattern misses all of them).
#
# Excludes fd-duplication redirects (`2>&1`, `>&2`, `1>&2`, etc.) — these
# are a DIFFERENT shell construct (duplicate a file descriptor) with no
# backgrounding semantics, and are common in legitimate diagnostic commands
# (e.g. `cmd 2>&1`). The negative lookbehind requires the `&` not be
# immediately preceded by `>`, so `2>&1` is excluded while a genuine
# backgrounding `&` (preceded by whitespace, a command, `)`, etc.) still
# matches.
_BACKGROUND_OPERATOR_RE = re.compile(r"(?<!&)(?<!>)&(?!&)")


def has_background_operator(scan_target: str) -> bool:
    """Return True if `scan_target` contains an unquoted shell
    backgrounding `&`.

    A bare, unquoted, single `&` is the shell backgrounding operator no
    matter what follows it — this fires wherever it appears, not just at
    end-of-string. Fd-duplication redirects (`2>&1`) are excluded (see
    `_BACKGROUND_OPERATOR_RE` docstring) so a diagnostic command like
    `cmd 2>&1` is not a false-positive.
    """
    return bool(_BACKGROUND_OPERATOR_RE.search(scan_target))


_COMPOUND_SHELL_DENY_REASON = (
    "compound shell expressions not permitted: split this into separate "
    "invocations, one command per call — do not pipe/compound calls "
    "together to save a round-trip; pipes are for build/test data-flow "
    "only."
)

_BACKGROUND_OPERATOR_DENY_REASON = (
    "shell backgrounding ('&') not permitted; run the command in the "
    "foreground with a bounded timeout instead of backgrounding it."
)


def compound_check(cmd: str) -> tuple[bool, str]:
    """Return (ok, reason). ok=False if a compound/piped/chained/
    backgrounded shell expression is detected.

    Uses `cmd_head` to strip heredoc body content before scanning — a
    string that quotes a pipeline (e.g. "grep foo | wc -l") in a heredoc
    body must not trigger this check; only the command head (before the
    heredoc delimiter) is inspected.

    Quote-aware: a metacharacter (`|`, `&&`, `||`, `;`, `(` `)`) INSIDE an
    ISOLATED quoted argv token (e.g. `grep -E "x|y" file`, or a
    `--body "...|..."` argument) is data, not a shell operator, and must
    not trigger this check. The scan runs against
    `normalize_shell_words(cmd_head(cmd))` — the shell-word-normalization
    pre-pass that joins glued quote fragments into real shell words first,
    then blanks genuinely isolated quoted spans — so only operators bash
    would actually execute are visible. A true UNQUOTED (or quote-glued)
    compound (`cmd && rm -rf /`, a bare `cmd | mutating_cmd`) still denies.
    On ambiguity (unparseable quoting, quoted command substitution) the
    normalizer returns None and this falls back to scanning the raw head,
    matching the deny-on-ambiguity posture used throughout this module.

    SCOPE NOTE: this is the pure structural gate only — it has no opinion
    on any specific verb's documented pipe shape (e.g. a transport verb
    whose sole body-input path is a stdin pipe). A caller that wants to
    admit one narrow, specific pipe shape for a specific verb evaluates
    that admission BEFORE calling this function and short-circuits on a
    match; `compound_check` itself never carries verb-specific carve-outs
    (see module docstring point 2).
    """
    head = cmd_head(cmd)
    scan_target = normalize_shell_words(head)
    if scan_target is None:
        scan_target = head  # deny-on-ambiguity: unparseable quoting, scan raw
    if (
        " && " in scan_target or " || " in scan_target or " ; " in scan_target
        or scan_target.startswith("(") or "|" in scan_target
    ):
        if not is_safe_redirect_only(scan_target):
            return False, _COMPOUND_SHELL_DENY_REASON
    if has_background_operator(scan_target):
        return False, _BACKGROUND_OPERATOR_DENY_REASON
    return True, ""


_TMP_REDIRECT_TARGET_RE = re.compile(r">>?\s*(/tmp/\S+)")


def detect_tmp_redirect_target(cmd: str) -> str | None:
    """Return the resolved /tmp path if `cmd` write-redirects there, else
    None.

    Quote-aware (reuses `normalize_shell_words`, same posture as
    `compound_check`): a literal "/tmp/..." string inside an ISOLATED
    quoted argv span (narrative data) is not a redirect target and must
    not be reported as one. A quote-glued redirect (`>""/tmp/evil`) IS
    joined into its real word first, so it is correctly recognized. Only
    a genuine `>`/`>>` immediately followed by a /tmp path counts.

    This is a detection primitive only — it never itself decides
    allow/deny; a caller uses the returned path (or its absence) as one
    input to its own policy layer.
    """
    head = cmd_head(cmd)
    scan_target = normalize_shell_words(head)
    if scan_target is None:
        scan_target = head  # deny-on-ambiguity: unparseable quoting, scan raw
    match = _TMP_REDIRECT_TARGET_RE.search(scan_target)
    if match is None:
        return None
    return match.group(1)


# A leading `NAME=value` shell-assignment word (bash prefixes a simple
# command with zero or more of these to scope an env var to that one
# invocation, e.g. `FOO=bar cmd --caller x`) is not part of the invoked
# command's own argv at all -- a verb-prefix matcher anchored on `^cmd\b`
# never sees past it and silently fails to admit (or, worse, a
# forbidden-pattern scanner anchored the same way never sees the real verb
# either). Bash's own grammar: one or more `NAME=value` words, each token
# a bare identifier (no shell metacharacters) followed by `=`, then the
# assigned value with no separating whitespace before the `=`. This mirrors
# `_ROLE_TOKEN_RE`'s own bare-identifier grammar for the NAME half; the
# value half is deliberately permissive (bash accepts any word there) since
# stripping the prefix, not validating its value, is this function's job.
_ENV_ASSIGN_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+")


def strip_env_assignment_prefix(cmd: str) -> str:
    """Return `cmd` with every leading `NAME=value` shell-assignment word
    removed, so a verb-prefix matcher anchored on `^<verb>\\b` sees the
    actual invoked command rather than being defeated by an env-assignment
    prefix (`FOO=bar cmd --caller x` -> `cmd --caller x`).

    Quote-and-ANSI-C-agnostic by design: this only strips a LEADING glued
    `NAME=value` word with no interior whitespace before its own trailing
    separator, so it never mis-parses a quoted argument value elsewhere in
    the command. Operates on the command AS GIVEN -- a caller wanting this
    composed with `normalize_shell_words`'s ANSI-C/quote handling should
    call that first and pass the normalized result in, exactly as every
    other verb-prefix matcher in this module's caller (`guard.
    role_allowlist`) already does for its own bare-verb grants.

    Idempotent and total: a command with no leading assignment is returned
    unchanged. The pattern requires TRAILING whitespace before it strips a
    word, matching bash's own grammar (an assignment word is only an
    assignment PREFIX when something follows it) -- so a string that is
    entirely `NAME=value` words separated by whitespace strips down to just
    its final word (which still looks like an assignment, but has nothing
    after it to prefix), never to an empty string; no verb-prefix pattern
    will match that residual word either, so the practical "nothing left to
    admit" outcome still holds.
    """
    stripped = cmd
    while True:
        match = _ENV_ASSIGN_PREFIX_RE.match(stripped)
        if match is None:
            return stripped
        stripped = stripped[match.end() :]


def is_staging_redirect_target(target: str) -> bool:
    """Return True if `target` is `$TMPDIR` spawn-scoped staging — never a
    project-tree mutation. Matches the shell's own unexpanded variable
    syntax (`$TMPDIR`, `$TMPDIR/...`); a caller with an already-expanded
    absolute path should compare against its own resolved boundary instead
    (see `guard.scratch_policy` for resolved-path containment).

    lr-f8649f: `$HOME` is no longer recognized here — a `$HOME`-spelled
    redirect target now falls through to `False` exactly like any other
    non-staging path, so `detect_project_tree_write_targets` correctly
    reports it as a project-tree-write candidate rather than exempting it
    as staging (mirrors `guard.scratch_policy.SCRATCH_ROOT_ENV_VARS`'s own
    TMPDIR-only narrowing — this function and that module's boundary set
    must never disagree about which root is sanctioned, or a `$HOME`-
    spelled redirect could pass this staging exemption while
    `is_scratch_contained` denies the equivalent `mkdir`/`mv`/etc.
    invocation, an inconsistency rather than a deny-loop but the same
    underlying bug class the task's own "flip the hint/predicate together"
    rule exists to prevent)."""
    if target == "$TMPDIR" or target.startswith("$TMPDIR/"):
        return True
    return False


__all__ = [
    "cmd_head",
    "compound_check",
    "decode_ansi_c_escapes",
    "decode_for_match",
    "detect_tmp_redirect_target",
    "has_background_operator",
    "has_unresolved_ansi_c_quote",
    "is_safe_redirect_only",
    "is_staging_redirect_target",
    "mask_quoted_spans",
    "normalize_shell_words",
    "quote_delimited_spans",
    "split_glued_redirect_operators",
    "split_segments",
    "strip_env_assignment_prefix",
    "unquoted_spans",
]
