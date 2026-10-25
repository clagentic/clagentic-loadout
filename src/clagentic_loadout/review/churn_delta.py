"""review.churn_delta — the net delta of a churning chain: what changed, per file,
between the head the chain began at and the current head.

Read from the host's range diff through the transport the review already uses.
It is context for the caller, never a gate, so a delta that cannot be read is a
reason, not an error: the churn output carries ``net_delta: null`` and says why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.acquire.contract import RangeDiffBackend
from clagentic_loadout.acquire.errors import AcquireFetchError

_HEADER_RE = re.compile(r"\Adiff --git a/.+ b/(.+)\Z")
_HEADER_PREFIX = "diff --git "


@dataclass(frozen=True)
class FileDelta:
    path: str
    added: int
    removed: int

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "added": self.added, "removed": self.removed}


@dataclass(frozen=True)
class NetDelta:
    from_head: str
    to_head: str
    files: tuple[FileDelta, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_head": self.from_head,
            "to_head": self.to_head,
            "files": [item.to_dict() for item in self.files],
        }


def count_changes(diff_text: str) -> tuple[FileDelta, ...]:
    """Added and removed line counts per file of a unified diff with ``diff --git``
    headers, by the file's new name, sorted by path. Only lines inside a hunk
    count, so the ``---``/``+++`` file lines never do."""
    counts: dict[str, list[int]] = {}
    current: list[int] | None = None
    in_hunk = False
    for line in diff_text.split("\n"):
        if line.startswith(_HEADER_PREFIX):
            match = _HEADER_RE.match(line)
            name = match.group(1) if match else line[len(_HEADER_PREFIX):]
            current = counts.setdefault(name, [0, 0])
            in_hunk = False
        elif current is None:
            continue
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            current[0] += 1
        elif in_hunk and line.startswith("-"):
            current[1] += 1
    return tuple(FileDelta(path, added, removed) for path, (added, removed) in sorted(counts.items()))


def read_net_delta(
    backend: Any, *, owner: str, repo: str, from_head: str, to_head: str
) -> tuple[NetDelta | None, str | None]:
    """``(delta, None)`` for the change from *from_head* to *to_head*, or
    ``(None, reason)`` when the host cannot give it: the backend has no range
    diff, the read failed, or *to_head* does not descend from *from_head* (the
    history was rewritten)."""
    if not isinstance(backend, RangeDiffBackend):
        return None, "the host backend cannot read a range diff"
    try:
        span = backend.fetch_range_diff(
            owner=owner, repo=repo, base_sha=from_head, head_sha=to_head
        )
    except AcquireFetchError as exc:
        return None, f"the range diff could not be read: {exc}"
    if not span.fast_forward:
        return None, "the current head is not a fast-forward of the chain's first head"
    return NetDelta(from_head, to_head, count_changes(span.diff_text)), None


__all__ = ["FileDelta", "NetDelta", "count_changes", "read_net_delta"]
