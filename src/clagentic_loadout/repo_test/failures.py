"""repo_test.failures — the bounded, redacted failure records ``test report
--failures`` prints.

One record per failed case, in report order. Each text value is redacted WHOLE
with the push-diagnostics redaction (URL userinfo, ``Authorization`` values,
bearer/token literals, terminal escapes and control characters), and only the
redacted output is cut to size, so a secret straddling a cap cannot leave a
recognisable prefix behind. A value whose raw length is over the redaction
ceiling is not read at all: it is replaced by a marker that counts its
characters. The caps count characters of message and excerpt text, before JSON
escaping, and exclude the truncation markers; the total also counts each
record's id.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from clagentic_loadout.push.push_redaction import redact_push_secrets
from clagentic_loadout.repo_test.junit import FailedCase

MESSAGE_CAP_CHARS = 1_000
EXCERPT_CAP_CHARS = 4_000
TOTAL_CAP_CHARS = 40_000

# A value longer than this many characters is never redacted or emitted.
REDACTION_CEILING_CHARS = 8 * 1024 * 1024


def _bounded(text: str, cap: int) -> tuple[str, int]:
    """*text* redacted whole and then cut to at most *cap* characters, with the
    number of characters dropped (counted after redaction). Over the ceiling,
    the marker alone and no text, cut to *cap* like any other value so the total
    cap holds."""
    cap = max(0, cap)
    if len(text) > REDACTION_CEILING_CHARS:
        marker = f"[omitted: value of {len(text)} characters exceeds the 8 MiB redaction ceiling]"
        return marker[:cap], 0
    redacted = redact_push_secrets(text)
    return redacted[:cap], max(0, len(redacted) - cap)


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


__all__ = [
    "EXCERPT_CAP_CHARS",
    "MESSAGE_CAP_CHARS",
    "REDACTION_CEILING_CHARS",
    "TOTAL_CAP_CHARS",
    "bound_failures",
]
