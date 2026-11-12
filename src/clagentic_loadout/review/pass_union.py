"""review.pass_union — several independent passes, one set of findings.

One sampled pass of a model is not something a gate should depend on: two
passes over the same diff can raise different findings. With ``passes`` above 1
(see review.profile_config) the pipeline reviews the same diff that many times,
each pass sampling on its own, then joins what they found:

  * The union is built by review.findings_contract.merge_findings, the same
    identity and duplicate rule a hunk split across chunks goes through. There is
    no second comparison here: a finding is the same finding when finding_key
    says so, in a pass or across passes.
  * Each finding of the union carries ``passes_seen`` (how many passes raised it)
    and ``passes_total``.
  * A stability report says whether every pass blocked on the same findings and
    lists the findings only some passes raised.

The verdict is computed once, over the union, by the caller. Stability is
reported, never a merge rule: the gate reads the fence as it always has.

A single pass has no stability to report: the union is that pass's merged
findings, untouched, so a default review is byte-for-byte what it was.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.merge.fence_state import (
    KEY_PASSES_SEEN,
    KEY_PASSES_TOTAL,
    KEY_SEEN_IN_SOME_PASSES,
    KEY_STABLE,
)
from clagentic_loadout.review.findings_contract import finding_key, merge_findings

#: Key, in a findings document of a multi-pass run, of the chunk records of every
#: pass after the first (the first pass's are the document's ``chunks``, as a
#: single pass's always were).
KEY_PASS_CHUNKS = "pass_chunks"

#: One pass's findings as merge_findings takes them: (chunk index, findings).
PassFindings = Sequence[tuple[int, list[dict[str, Any]]]]

#: Names the blocking findings of one pass's merged findings, as a set of ids.
BlockingIds = Callable[[list[dict[str, Any]]], frozenset[str]]


@dataclass(frozen=True)
class PassUnion:
    findings: list[dict[str, Any]]
    #: The stability block for the fence; None for a single pass.
    stability: dict[str, Any] | None


def union_passes(per_pass: Sequence[PassFindings], blocking_ids: BlockingIds) -> PassUnion:
    """The union of *per_pass* (one entry per pass, pass 0 first) and, for more
    than one pass, its stability report. *blocking_ids* judges one pass's merged
    findings and returns the ids of those that block."""
    if len(per_pass) == 1:
        return PassUnion(merge_findings(list(per_pass[0])), None)
    total = len(per_pass)
    each = [merge_findings(list(pairs)) for pairs in per_pass]
    union = merge_findings([pair for pairs in per_pass for pair in pairs])
    seen = Counter(key for findings in each for key in {finding_key(f) for f in findings})
    tagged = [
        {**finding, KEY_PASSES_SEEN: seen[finding_key(finding)], KEY_PASSES_TOTAL: total}
        for finding in union
    ]
    blocking = [blocking_ids(findings) for findings in each]
    partial: dict[tuple[Any, ...], dict[str, Any]] = {}
    for finding in tagged:
        if finding[KEY_PASSES_SEEN] < total:
            partial.setdefault(
                finding_key(finding),
                {
                    "file": finding["file"],
                    "line": finding["line"],
                    "rule_id": finding["rule_id"],
                    "message": finding["message"],
                    KEY_PASSES_SEEN: finding[KEY_PASSES_SEEN],
                },
            )
    stability = {
        KEY_PASSES_TOTAL: total,
        KEY_STABLE: all(ids == blocking[0] for ids in blocking),
        KEY_SEEN_IN_SOME_PASSES: list(partial.values()),
    }
    return PassUnion(tagged, stability)


__all__ = ["KEY_PASS_CHUNKS", "BlockingIds", "PassFindings", "PassUnion", "union_passes"]
