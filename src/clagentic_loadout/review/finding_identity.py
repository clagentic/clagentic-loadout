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


def _line_distance(a: dict[str, Any], b: dict[str, Any]) -> int:
    la, lb = a.get("line"), b.get("line")
    if isinstance(la, int) and isinstance(lb, int):
        return abs(la - lb)
    return 0


def pair_findings(
    priors: Sequence[dict[str, Any]],
    fresh: Sequence[dict[str, Any]],
    *,
    fingerprint_only: bool = False,
) -> list[tuple[int, int]]:
    """One-to-one pairing of *priors* with *fresh* as ``(prior_index,
    fresh_index)``. Findings sharing a fingerprint (two identical lines under
    one rule) are interchangeable by identity alone, so each prior takes at
    most one fresh finding and each fresh finding at most one prior, nearest
    line first and original order breaking ties: distinct findings are never
    collapsed into one. With *fingerprint_only*, a pair needs both sides
    fingerprinted."""
    by_print: dict[str, tuple[list[int], list[int]]] = {}
    for side, items in ((0, priors), (1, fresh)):
        for index, item in enumerate(items):
            value = item.get(KEY_FINGERPRINT)
            if is_fingerprint(value):
                by_print.setdefault(value, ([], []))[side].append(index)
    pairs: list[tuple[int, int]] = []
    used_prior: set[int] = set()
    used_fresh: set[int] = set()
    for prior_ids, fresh_ids in by_print.values():
        for i, j in _order_preserving_pairs(priors, prior_ids, fresh, fresh_ids):
            pairs.append((i, j))
            used_prior.add(i)
            used_fresh.add(j)
    if not fingerprint_only:
        # What is left is matched by the old position identity, one to one.
        open_fresh: dict[tuple[Any, Any, Any], list[int]] = {}
        for j, item in enumerate(fresh):
            if j not in used_fresh:
                open_fresh.setdefault(_position_key(item), []).append(j)
        for i, prior in enumerate(priors):
            if i in used_prior:
                continue
            for j in open_fresh.get(_position_key(prior), []):
                if j not in used_fresh and same_finding(prior, fresh[j]):
                    pairs.append((i, j))
                    used_prior.add(i)
                    used_fresh.add(j)
                    break
    return sorted(pairs)


def _position_key(finding: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (finding.get("file"), finding.get("line"), finding.get("rule_id"))


def _order_preserving_pairs(
    priors: Sequence[dict[str, Any]],
    prior_ids: list[int],
    fresh: Sequence[dict[str, Any]],
    fresh_ids: list[int],
) -> list[tuple[int, int]]:
    """Pair interchangeable findings (equal fingerprint) so as to match as many
    as possible, then to minimise the total line distance, without crossing:
    the nth occurrence before an insertion stays the nth after it. Original
    order breaks any remaining tie."""
    ps = sorted(prior_ids, key=lambda i: (_line_of(priors[i]), i))
    fs = sorted(fresh_ids, key=lambda j: (_line_of(fresh[j]), j))
    n, m = len(ps), len(fs)
    # best[a][b]: (-matches, distance) over ps[a:] and fs[b:].
    best = [[(0, 0)] * (m + 1) for _ in range(n + 1)]
    for a in range(n - 1, -1, -1):
        for b in range(m - 1, -1, -1):
            gap = abs(_line_of(priors[ps[a]]) - _line_of(fresh[fs[b]]))
            take = best[a + 1][b + 1]
            options = [(take[0] - 1, take[1] + gap), best[a + 1][b], best[a][b + 1]]
            best[a][b] = min(options)
    pairs: list[tuple[int, int]] = []
    a = b = 0
    while a < n and b < m:
        gap = abs(_line_of(priors[ps[a]]) - _line_of(fresh[fs[b]]))
        take = best[a + 1][b + 1]
        if best[a][b] == (take[0] - 1, take[1] + gap):
            pairs.append((ps[a], fs[b]))
            a += 1
            b += 1
        elif best[a][b] == best[a + 1][b]:
            a += 1
        else:
            b += 1
    return pairs


def _line_of(finding: dict[str, Any]) -> int:
    line = finding.get("line")
    return line if isinstance(line, int) and not isinstance(line, bool) else 0


def match_priors(
    findings: Sequence[dict[str, Any]], priors: Sequence[dict[str, Any]]
) -> list[dict[str, Any] | None]:
    """For each of *findings*, the prior it is the same finding as, else None;
    a prior answers for at most one finding (see pair_findings)."""
    matched: list[dict[str, Any] | None] = [None] * len(findings)
    for prior_index, finding_index in pair_findings(priors, findings):
        matched[finding_index] = priors[prior_index]
    return matched


def match_prior(
    finding: dict[str, Any], priors: Sequence[dict[str, Any]]
) -> dict[str, Any] | None:
    """The prior that is the same finding as *finding* and nearest to it by
    line (the first, on a tie), else None. Matching a whole batch? Use
    match_priors, which never gives one prior to two findings."""
    best: dict[str, Any] | None = None
    best_distance = 0
    for prior in priors:
        if not same_finding(finding, prior):
            continue
        distance = _line_distance(finding, prior)
        if best is None or distance < best_distance:
            best, best_distance = prior, distance
    return best


def drop_rereported(
    carried: Sequence[dict[str, Any]], fresh: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """*carried* findings minus those a fresh finding re-reports at a shifted
    line. Only a fingerprint match counts: the fresh copy carries the line as
    it is now, so keeping the stale carried one would list one defect twice.
    The match is one-to-one: a fresh finding retires one carried finding, so a
    second carried finding on an identical line is kept. Findings without
    fingerprints are left alone, as they always were."""
    dropped = {i for i, _ in pair_findings(carried, fresh, fingerprint_only=True)}
    return [f for i, f in enumerate(carried) if i not in dropped]
