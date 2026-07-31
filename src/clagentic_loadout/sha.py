"""sha.py — typed git commit SHA normalization and comparison helper.

Ported from the reference implementation's _sha.py (lr-09f9, Wave A slice 1,
tome #688). The source copy stays primary until that project's separate
CUT OVER + RETIRE + VERIFY-GONE task for this slice.

Root cause (lr-56b4 EPIC A, absorbing lr-6495 and lr-503d):
SHAs were hand-carried as raw strings and compared with a bare `!=`, with no
normalization and no format validation at the point of use. Two independent
defect symptoms trace to that one root cause:

  - lr-6495: a merge-gate consumer received a 7-char abbreviated SHA. A bare
    string compare against the 40-char HEAD sha false-positived a stale-gate
    refusal on what was actually the IDENTICAL commit.
  - lr-503d: a verdict/expected SHA arrived 41 characters long with stray
    whitespace (trailing newline from a shell capture). The bare compare
    false-positived a stale-gate refusal.

Fix: every SHA comparison in a merge-gate path goes through
compare_sha_values() (or normalize_sha() before an existing custom compare),
which:
  1. Strips whitespace from both operands FIRST (lr-503d class).
  2. Validates each non-empty operand matches ^[0-9a-f]{7,40}$ (lower-hex,
     abbreviated-to-full-length) — a value outside that shape is a caller bug,
     not a stale-gate condition, and is reported as such.
  3. Compares using abbreviated-SHA-aware semantics: a short (7-39 char)
     value matches a full 40-char value when the short value is a prefix of
     the full value (git's own abbreviation semantics) — this is the lr-6495
     class fix. Two full 40-char values must match exactly.

Zero third-party dependencies.
"""

from __future__ import annotations

import re

#: Full 40-character lowercase-hex git SHA.
FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: Abbreviated-or-full lowercase-hex git SHA (7-40 chars) — the shape accepted
#: at the envelope/CLI boundary before a tool can confirm the full value via
#: `git rev-parse` or a platform API read.
ABBREVIATED_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


class InvalidShaError(ValueError):
    """Raised when a SHA value (after normalization) does not match the
    expected hex-digest shape. Distinct from a stale-SHA mismatch: this means
    the CALLER supplied a malformed value, not that the branch advanced."""


def normalize_sha(raw: str) -> str:
    """Strip surrounding whitespace and lowercase a SHA string.

    Whitespace-only or empty input normalizes to "" (callers treat empty as
    "no SHA supplied" — unchanged no-op contract for optional SHA checks).
    Does NOT validate shape; call validate_sha() separately when the caller
    must reject malformed input rather than silently comparing it.
    """
    return raw.strip().lower()


def validate_sha(raw: str, *, allow_abbreviated: bool = True) -> str:
    """Normalize and validate a SHA string; raise InvalidShaError if malformed.

    Empty string (after normalization) is always accepted and returned as-is
    — callers use emptiness to mean "no SHA supplied," which is a distinct
    concern from format validity.

    allow_abbreviated=True (default) accepts 7-40 lowercase-hex characters
    (git's own abbreviation floor). allow_abbreviated=False requires exactly
    40 characters — use this for values a tool computed itself (e.g. via
    `git rev-parse HEAD` or a platform API's full head.sha field), where a
    short value indicates an upstream bug, not legitimate abbreviation.
    """
    normalized = normalize_sha(raw)
    if not normalized:
        return normalized
    pattern = ABBREVIATED_SHA_RE if allow_abbreviated else FULL_SHA_RE
    if not pattern.match(normalized):
        expected = "7-40 lowercase hex characters" if allow_abbreviated else "exactly 40 lowercase hex characters"
        raise InvalidShaError(
            f"SHA value {raw!r} (normalized: {normalized!r}) does not match the "
            f"expected shape ({expected}). This is a malformed input, not a "
            f"stale-gate condition — check the caller that produced this value."
        )
    return normalized


def compare_sha_values(expected: str, actual: str) -> bool:
    """Compare two SHA values with abbreviated-SHA-aware, whitespace-safe semantics.

    Returns True when the SHAs should be considered equal:
      - Both empty → True (no-op comparison; callers gate on emptiness separately).
      - Both normalize to the identical string → True.
      - One is a shorter (abbreviated, 7-39 char) prefix of the other, and the
        other is a full 40-char SHA → True (git abbreviation semantics; fixes
        the lr-6495 7-char false-mismatch class).
      - Otherwise → False.

    Does not raise on malformed input — a caller that wants strict validation
    should call validate_sha() first. This function only answers "do these
    two values refer to the same commit," which is well-defined for any two
    lowercase-hex strings via the prefix rule above.

    Whitespace is stripped from both operands before any comparison (fixes
    the lr-503d stray-whitespace/41-char false-mismatch class).
    """
    exp = normalize_sha(expected)
    act = normalize_sha(actual)

    if exp == act:
        return True
    if not exp or not act:
        return False

    # Abbreviated-SHA prefix match: shorter value must be a prefix of the
    # longer value, and the longer value must be a full 40-char SHA (we do
    # not treat two different abbreviations of unknown full length as
    # equivalent — only a genuine abbreviation-vs-full-value pair).
    if len(exp) < len(act) and FULL_SHA_RE.match(act):
        return act.startswith(exp)
    if len(act) < len(exp) and FULL_SHA_RE.match(exp):
        return exp.startswith(act)
    return False
