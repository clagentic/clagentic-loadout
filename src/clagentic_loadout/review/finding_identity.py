"""review.finding_identity — a finding identity that survives line shifts.

A finding is pinned to ``file:line``, so the same defect reported at line 3177
in one run and 3447 in the next (an unrelated insertion above it) looks like
two findings to any cross-run tracking. This module adds one OPTIONAL field,
``fingerprint``, derived from what the finding is about rather than where it
sits: the rule id, the file, and the whitespace-normalised text of the line the
finding points at.

ADDITIVE ONLY. ``file``, ``line``, ``rule_id``, ``severity`` and ``message``
keep their names and meaning, and a finding without a fingerprint (an older
run, a hand-made file, a line the chunk does not show) is matched exactly as it
always was, by ``(file, line, rule_id)``. Nothing here may be required of a
findings file.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from typing import Any

from clagentic_loadout.review.chunking import Chunk

KEY_FINGERPRINT = "fingerprint"

#: Prefix that versions the derivation, so a future change to what is hashed
#: can never be mistaken for a match with this one.
_ALGORITHM = "fp1"
_DIGEST_CHARS = 16

_FINGERPRINT_RE = re.compile(rf"^{_ALGORITHM}:[0-9a-f]{{{_DIGEST_CHARS}}}\Z")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_code_line(text: str) -> str:
    """*text* with every whitespace run collapsed to one space and the ends
    trimmed, so re-indenting or re-wrapping whitespace does not change identity."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def compute_fingerprint(file: str, rule_id: str, line_text: str) -> str | None:
    """The fingerprint of a finding on *line_text*, or None when the line is
    blank: a blank line identifies nothing, so such a finding keeps the
    position-only identity."""
    code = normalize_code_line(line_text)
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
    engine put on a finding is discarded first: identity is derived here from
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


def same_finding(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True when *a* and *b* are the same finding. Two fingerprinted findings
    are the same when their fingerprints are equal, wherever they sit; when
    either lacks one, the old identity applies: same file, line and rule."""
    fa, fb = a.get(KEY_FINGERPRINT), b.get(KEY_FINGERPRINT)
    if is_fingerprint(fa) and is_fingerprint(fb):
        return fa == fb
    return (a.get("file"), a.get("line"), a.get("rule_id")) == (
        b.get("file"),
        b.get("line"),
        b.get("rule_id"),
    )


def match_prior(
    finding: dict[str, Any], priors: Sequence[dict[str, Any]]
) -> dict[str, Any] | None:
    """The first of *priors* that is the same finding as *finding*, else None."""
    return next((prior for prior in priors if same_finding(finding, prior)), None)


def drop_rereported(
    carried: Sequence[dict[str, Any]], fresh: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """*carried* findings minus those a fresh finding re-reports at a shifted
    line. Only a fingerprint match counts: the fresh copy carries the line as
    it is now, so keeping the stale carried one would list one defect twice.
    Findings without fingerprints are left alone, as they always were."""
    fresh_prints = {f[KEY_FINGERPRINT] for f in fresh if is_fingerprint(f.get(KEY_FINGERPRINT))}
    return [
        f for f in carried
        if not (is_fingerprint(f.get(KEY_FINGERPRINT)) and f[KEY_FINGERPRINT] in fresh_prints)
    ]
