"""review.finding_identity — a link hint that survives line shifts.

A finding is pinned to ``file:line``, so the same defect reported at line 3177
in one run and 3447 in the next (an unrelated insertion above it) looks like
two findings to anyone comparing runs. This module adds one OPTIONAL field,
``fingerprint``, derived from what the finding is about rather than where it
sits: the rule id, the file, and the text of the line the finding points at
with its leading and trailing whitespace stripped (internal whitespace is
significant, so ``x = 1`` and ``x  =  1`` differ, as do string literals that
differ only in spacing).

LINK HINT, NOT IDENTITY. Two findings sharing a fingerprint MAY be the same
defect seen again; they need not be. Identical lines under one rule (two
copies of the same offending statement) get the same fingerprint, so equality
cannot tell twins apart. Loadout therefore never dedupes, drops, merges or
keys on a fingerprint: a finding is kept or merged exactly as it was before
the field existed. A consumer that wants cross-run identity must disambiguate
twins itself (for example fingerprint plus occurrence index).

ADDITIVE ONLY. ``file``, ``line``, ``rule_id``, ``severity`` and ``message``
keep their names and meaning. Nothing here may be required of a findings
file; a finding without a fingerprint (an older run, a hand-made file, a line
the chunk does not show) loads exactly as it always did.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Any

from clagentic_loadout.review.chunking import Chunk

KEY_FINGERPRINT = "fingerprint"

#: Prefix that versions the derivation, so a future change to what is hashed
#: can never be mistaken for a match with this one.
_ALGORITHM = "fp2"
_DIGEST_CHARS = 16

_FINGERPRINT_RE = re.compile(rf"^{_ALGORITHM}:[0-9a-f]{{{_DIGEST_CHARS}}}\Z")


def compute_fingerprint(file: str, rule_id: str, line_text: str) -> str | None:
    """The fingerprint of a finding on *line_text*, or None when the line is
    blank: a blank line identifies nothing, so such a finding gets no hint."""
    code = line_text.strip()
    if not code:
        return None
    digest = hashlib.sha256("\0".join((_ALGORITHM, file, rule_id, code)).encode("utf-8"))
    return f"{_ALGORITHM}:{digest.hexdigest()[:_DIGEST_CHARS]}"


def is_fingerprint(value: Any) -> bool:
    """True for a well-formed fingerprint string."""
    return isinstance(value, str) and _FINGERPRINT_RE.match(value) is not None


def with_fingerprints(findings: Iterable[dict[str, Any]], chunk: Chunk) -> list[dict[str, Any]]:
    """Copies of *findings* from *chunk*'s review, each given a fingerprint
    computed from the line it points at in the chunk's diff. A finding whose
    line the chunk does not show is returned unchanged. Any fingerprint the
    engine put on a finding is discarded first: the hint is derived here from
    the diff, never taken from a model reply."""
    out: list[dict[str, Any]] = []
    for finding in findings:
        item = {k: v for k, v in finding.items() if k != KEY_FINGERPRINT}
        text = chunk.new_side_line_text(item["file"], item["line"])
        fingerprint = compute_fingerprint(item["file"], item["rule_id"], text) if text else None
        if fingerprint is not None:
            item[KEY_FINGERPRINT] = fingerprint
        out.append(item)
    return out
