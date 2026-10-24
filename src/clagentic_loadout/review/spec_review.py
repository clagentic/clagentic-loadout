"""review.spec_review — a review that runs against a sealed spec.

A review given a spec hash reads the sealed spec through the spec store (which
verifies the hash against the stored bytes), tags every finding it states with
exactly one of in-spec, out-of-spec or fix-caused, and runs a coverage pass that
marks each acceptance criterion of that spec met or unmet. Tags and coverage ride
in the verdict fence as evidence (see merge.fence_state); the coverage pass is
also recorded as a verdict record by the review-post verb.

Nothing here is reachable without a spec hash. A review given none takes none of
these paths, which is what keeps it exactly as it was.

The coverage file is a JSON array, one entry per acceptance criterion of the
spec::

    [{"criterion": "ac-1", "status": "met"}, {"criterion": "ac-2", "status": "unmet"}]

It must name every criterion of the spec exactly once and no other: a coverage
pass that skips a criterion is not a coverage pass.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from clagentic_loadout.artifact_store import StoreError
from clagentic_loadout.merge.fence_state import COVERAGE_STATUSES, KEY_SPEC_TAG
from clagentic_loadout.spec import store as spec_store

_MAX_COVERAGE_BYTES = 1024 * 1024


class SpecReviewError(Exception):
    """The spec or the coverage a review was given cannot be used. The message
    names the file or hash and what was found."""


def load_spec_criterion_ids(
    spec_hash: str,
    *,
    config_root: str | Path | None = None,
    untrusted_roots: Sequence[Path] = (),
) -> tuple[str, ...]:
    """The acceptance criterion ids of the sealed spec *spec_hash*, in spec order.
    Raises SpecReviewError when the spec is not in the store, does not verify
    against its hash, or the store cannot be used."""
    root = spec_store.specs_root(config_root=config_root)
    try:
        stored = spec_store.load_sealed(root, spec_hash, untrusted_roots=untrusted_roots)
    except StoreError as exc:
        raise SpecReviewError(str(exc)) from exc
    return tuple(criterion["id"] for criterion in stored.doc["acceptance_criteria"])


def load_coverage(path: str, criterion_ids: Sequence[str]) -> list[dict[str, str]]:
    """The coverage file at *path*, checked against *criterion_ids* (the spec's
    acceptance criteria) and returned in spec order. Raises SpecReviewError when
    it cannot be read, is malformed, or does not name each criterion exactly
    once."""
    try:
        size = Path(path).stat().st_size
        if size > _MAX_COVERAGE_BYTES:
            raise SpecReviewError(
                f"the coverage file {path!r} is {size} bytes, larger than {_MAX_COVERAGE_BYTES}"
            )
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpecReviewError(f"cannot read the coverage file {path!r}: {exc}") from exc
    if not isinstance(data, list):
        raise SpecReviewError(
            f"the coverage file {path!r} must be a JSON array of "
            f'{{"criterion": ..., "status": ...}} objects, got {type(data).__name__}'
        )
    marked: dict[str, str] = {}
    for position, entry in enumerate(data, start=1):
        if not isinstance(entry, Mapping) or set(entry) != {"criterion", "status"}:
            raise SpecReviewError(
                f"coverage entry {position} in {path!r} must be an object with exactly "
                f"'criterion' and 'status', got {entry!r}"
            )
        criterion, status = entry["criterion"], entry["status"]
        if not isinstance(criterion, str) or criterion not in criterion_ids:
            raise SpecReviewError(
                f"coverage entry {position} in {path!r} names {criterion!r}, which is not an "
                f"acceptance criterion of the spec (it has {list(criterion_ids)})"
            )
        if status not in COVERAGE_STATUSES:
            raise SpecReviewError(
                f"coverage entry {position} in {path!r} marks {criterion!r} as {status!r}; "
                f"expected one of {', '.join(COVERAGE_STATUSES)}"
            )
        if criterion in marked:
            raise SpecReviewError(f"coverage in {path!r} marks {criterion!r} more than once")
        marked[criterion] = status
    unmarked = [cid for cid in criterion_ids if cid not in marked]
    if unmarked:
        raise SpecReviewError(
            f"coverage in {path!r} does not mark every acceptance criterion of the spec; "
            f"missing {unmarked}"
        )
    return [{"criterion": cid, "status": marked[cid]} for cid in criterion_ids]


def tags_for_fence(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The fence's ``spec_tags`` for *findings*, each of which already carries
    its tag (review.findings_contract.validate_finding with carry_spec_tag)."""
    return [
        {
            "file": finding["file"],
            "line": finding["line"],
            "rule_id": finding["rule_id"],
            "message": finding["message"],
            KEY_SPEC_TAG: finding[KEY_SPEC_TAG],
        }
        for finding in findings
    ]


__all__ = [
    "SpecReviewError",
    "load_coverage",
    "load_spec_criterion_ids",
    "tags_for_fence",
]
