"""repo_test.failures — the bounded, redacted failure records ``test report
--failures`` prints.

One record per failed case, in report order. Every text value goes through the
push-diagnostics redaction (URL userinfo, ``Authorization`` values, bearer/token
literals, terminal escapes and control characters) BEFORE it is cut to size, so a
secret straddling the cut cannot leave a recognisable prefix behind. A text too
long to redact whole is first cut back to a point its redaction cannot depend on
(``_safe_cut``), and nothing past that point is emitted. The caps count
characters of message and excerpt text, before JSON escaping, and exclude the
truncation markers; the total also counts each record's id.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from clagentic_loadout.push.push_redaction import redact_push_secrets
from clagentic_loadout.repo_test.junit import FailedCase

MESSAGE_CAP_CHARS = 1_000
EXCERPT_CAP_CHARS = 4_000
TOTAL_CAP_CHARS = 40_000

# Text past this many characters is never fed to the redaction regexes: some of
# them are quadratic on a long whitespace run, so a multi-megabyte text cannot
# be redacted whole. The cut this makes can land inside a secret, and redaction
# can shrink the text so that the cut falls inside the kept output. So the
# window is first cut back to a point whose redacted form cannot depend on what
# follows it (see _safe_cut), and nothing beyond that point is ever emitted.
_REDACTION_INPUT_CHARS = 4 * EXCERPT_CAP_CHARS

# An escape sequence this close to the cut may be unterminated.
_ESCAPE_TAIL_CHARS = 64

# A URL whose userinfo is only recognised by its closing "@".
_URL_SCHEME_RE = re.compile(r"https?://")

# A bearer/token keyword whose value is still too short to be recognised.
_UNFINISHED_VALUE_RE = re.compile(r"\b(?:bearer|token=)\s*\S{0,7}\Z", re.IGNORECASE)


def _safe_cut(text: str) -> int:
    """Length of the prefix of *text* whose redacted form does not depend on
    what follows it. The whole text when it fits the redaction window.
    Otherwise the window, pulled back past every construct the cut may have
    left half-seen: a URL in the last whitespace-free run (its "@" may lie
    beyond the cut), a bearer/token keyword whose value is still short, and a
    trailing escape sequence that may be unterminated."""
    if len(text) <= _REDACTION_INPUT_CHARS:
        return len(text)
    window = text[:_REDACTION_INPUT_CHARS]
    cut = len(window)
    run_start = cut
    while run_start > 0 and not window[run_start - 1].isspace():
        run_start -= 1
    scheme = _URL_SCHEME_RE.search(window, run_start)
    if scheme:
        cut = scheme.start()
    unfinished = _UNFINISHED_VALUE_RE.search(window, 0, cut)
    if unfinished:
        cut = unfinished.start()
    last_escape = window.rfind("\x1b", 0, cut)
    if last_escape != -1 and cut - last_escape <= _ESCAPE_TAIL_CHARS:
        cut = last_escape
    last_osc = window.rfind("\x1b]", 0, cut)
    if last_osc != -1 and "\x07" not in window[last_osc:cut] and "\x1b\\" not in window[last_osc:cut]:
        cut = last_osc
    return cut


def _bounded(text: str, cap: int) -> tuple[str, int]:
    """*text* redacted and cut to at most *cap* characters, with the number of
    characters dropped (counted after redaction; text never read counts raw)."""
    cut = _safe_cut(text)
    redacted = redact_push_secrets(text[:cut])
    cap = max(0, cap)
    return redacted[:cap], max(0, len(redacted) - cap) + (len(text) - cut)


def _with_marker(kept: str, dropped: int) -> str:
    return f"{kept}...[truncated: {dropped} characters dropped]" if dropped else kept


def bound_failures(cases: Sequence[FailedCase]) -> list[dict[str, Any]]:
    """Records ``{id, kind, message, excerpt}`` for *cases*. When the total cap is
    reached, the remaining cases are replaced by one last record with only
    ``omitted_cases`` and ``note`` (it has no ``id``)."""
    records: list[dict[str, Any]] = []
    remaining = TOTAL_CAP_CHARS
    for index, case in enumerate(cases):
        if remaining <= 0:
            omitted = len(cases) - index
            records.append(
                {
                    "omitted_cases": omitted,
                    "note": f"output cap of {TOTAL_CAP_CHARS} characters reached; {omitted} failed case(s) not shown",
                }
            )
            break
        test_id, id_dropped = _bounded(case.test_id, min(MESSAGE_CAP_CHARS, remaining))
        remaining -= len(test_id)
        message, message_dropped = _bounded(case.message, min(MESSAGE_CAP_CHARS, remaining))
        remaining -= len(message)
        excerpt, excerpt_dropped = _bounded(case.text, min(EXCERPT_CAP_CHARS, remaining))
        remaining -= len(excerpt)
        records.append(
            {
                "id": _with_marker(test_id, id_dropped),
                "kind": case.kind,
                "message": _with_marker(message, message_dropped),
                "excerpt": _with_marker(excerpt, excerpt_dropped),
            }
        )
    return records


__all__ = ["EXCERPT_CAP_CHARS", "MESSAGE_CAP_CHARS", "TOTAL_CAP_CHARS", "bound_failures"]
