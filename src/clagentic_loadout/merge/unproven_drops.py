"""merge.unproven_drops — name the dropped candidates whose read revision is unproven.

A reviewer records, per dropped candidate, the revision it read the file at
(``read_at``) and whether that equals the reviewed head (``proven``). The merge
reports each entry whose ``proven`` key is exactly False, in its output and its
merge-completion attestation. It is report only: nothing here refuses, blocks or
changes a gate outcome.

A fence written before the keys existed has no ``proven`` key on its dropped
entries and is not reported, so a pull request already in review gains no noise.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: What a line says when the entry names no revision it was read at.
NO_READ_AT = "no read_at recorded"


def unproven_drop_lines(reviewer: str, dropped: Iterable[Mapping[str, Any]]) -> list[str]:
    """One line per dropped entry whose ``proven`` key is False, naming the
    reviewer, the candidate as file:line:rule_id and the revision it was read at
    (or that none was recorded)."""
    lines: list[str] = []
    for entry in dropped:
        if entry.get("proven") is not False:
            continue
        read_at = entry.get("read_at")
        read = f"read at {read_at}" if isinstance(read_at, str) and read_at else NO_READ_AT
        lines.append(
            f"{reviewer}: dropped {entry.get('file')}:{entry.get('line')}:{entry.get('rule_id')} "
            f"is unproven ({read})"
        )
    return lines


__all__ = ["NO_READ_AT", "unproven_drop_lines"]
