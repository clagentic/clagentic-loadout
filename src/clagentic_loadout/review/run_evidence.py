"""review.run_evidence — what a `run` output says about how the review was made.

A posted verdict is only as credible as its provenance: which commit range was
read, and which engine did the reading. Both are already recorded in the merged
findings file `run` writes (the base/head SHAs, the mode, and one record per
chunk). This module reads them back out as the evidence fields the verdict
fence carries (see merge.fence_state), so the post verb states them from the
run's own record instead of from anything the caller types.

A findings file that is a bare array, or a run output without these records,
yields nothing: no evidence is invented.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clagentic_loadout.merge.fence_state import (
    ENGINE_CARRIER,
    ENGINE_FALLBACK,
    KEY_ENGINES,
    KEY_RANGE,
    RANGE_BASIS_BASE_HEAD,
    RANGE_BASIS_SINCE,
)
from clagentic_loadout.review.delta import MODE_DELTA
from clagentic_loadout.sha import FULL_SHA_RE

#: Reason recorded for a fallback that took over because the carrier ran and
#: failed on its last permitted attempt, as opposed to being unavailable.
REASON_CARRIER_FAILED = "carrier_failed"


def _sha(value: Any) -> str | None:
    return value if isinstance(value, str) and FULL_SHA_RE.match(value) else None


def _label(value: Any) -> str | None:
    """A model or reason label usable in a one-line verdict field."""
    if isinstance(value, str) and value.strip() and "\n" not in value and "\r" not in value:
        return value.strip()
    return None


def range_of(document: Mapping[str, Any]) -> dict[str, str] | None:
    """The commit range the run reviewed: since a prior head for a delta run,
    base..head otherwise; None when the document does not record one."""
    head = _sha(document.get("head_sha"))
    if head is None:
        return None
    since = _sha(document.get("since_head"))
    if document.get("mode") == MODE_DELTA and since is not None:
        return {"basis": RANGE_BASIS_SINCE, "since": since, "head": head}
    base = _sha(document.get("base_sha"))
    if base is None:
        return None
    return {"basis": RANGE_BASIS_BASE_HEAD, "base": base, "head": head}


def engines_of(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The engines that answered the run's chunks, one entry per distinct
    (engine, model, reason) in first-seen order, with how many chunks each
    answered. A carrier is named by the model label its chunk record carries,
    the same source a fallback uses. Empty when the document records no chunk engines."""
    chunks = document.get("chunks")
    if not isinstance(chunks, list):
        return []
    counts: dict[tuple[str, str | None, str | None], int] = {}
    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        engine = chunk.get("engine")
        if engine == ENGINE_CARRIER:
            key = (ENGINE_CARRIER, _label(chunk.get("engine_label")), None)
        elif engine == ENGINE_FALLBACK:
            reason = _label(chunk.get("carrier_unavailable_reason"))
            if reason is None and chunk.get("carrier_failure"):
                reason = REASON_CARRIER_FAILED
            key = (ENGINE_FALLBACK, _label(chunk.get("engine_label")), reason)
        else:
            continue
        counts[key] = counts.get(key, 0) + 1
    entries = []
    for (engine, model, reason), count in counts.items():
        entry: dict[str, Any] = {"engine": engine}
        if model is not None:
            entry["model"] = model
        if reason is not None:
            entry["reason"] = reason
        entry["chunks"] = count
        entries.append(entry)
    return entries


def evidence_from_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """The fence evidence fields (range, engines) a run output supports."""
    evidence: dict[str, Any] = {}
    commit_range = range_of(document)
    if commit_range is not None:
        evidence[KEY_RANGE] = commit_range
    engines = engines_of(document)
    if engines:
        evidence[KEY_ENGINES] = engines
    return evidence


__all__ = ["engines_of", "evidence_from_document", "range_of"]
