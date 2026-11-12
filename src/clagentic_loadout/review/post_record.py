"""review.post_record — the review record `post` leaves after a verified comment.

The review landed before this runs and cannot be taken back, so a failure here is
reported (in the post result and on stderr) and never changes what the post
returned: no record is a gap in the audit trail, not a refusal.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from clagentic_loadout.verdict_record.review_record import (
    StoreError,
    build_review_record,
    review_records_root,
    write_review_record,
)

#: Why `post` leaves no record when the landed comment was not verified.
REASON_UNVERIFIED = "the posted comment was not verified, so no review record was written"


def unrecorded(reason: str) -> dict[str, Any]:
    """The ``record`` entry of a post result that wrote no record."""
    return {"written": False, "reason": reason}


def record_posted_review(
    *,
    config_root: str | Path | None,
    untrusted_roots: Sequence[Path] = (),
    **fields: Any,
) -> dict[str, Any]:
    """Write the review record for one verified comment and return the ``record``
    entry for the post result: ``{"written": true, "comment_id", "path"}``, or
    ``{"written": false, "reason"}`` when the record could not be built or stored.
    *fields* are build_review_record's."""
    try:
        record = build_review_record(**fields)
        path = write_review_record(
            review_records_root(config_root=config_root),
            record,
            untrusted_roots=untrusted_roots,
        )
    except (StoreError, OSError, TypeError, ValueError) as exc:
        reason = f"no review record was written for comment {fields.get('comment_id')}: {exc}"
        print(f"loadout-review: WARNING -- {reason}", file=sys.stderr)
        return unrecorded(reason)
    return {"written": True, "comment_id": record["comment_id"], "path": str(path)}


__all__ = ["REASON_UNVERIFIED", "record_posted_review", "unrecorded"]
