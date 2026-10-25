"""review.churn_delta — the net delta of a churning chain: what changed, per file,
between the head the chain began at and the current head.

Read from the host's range diff through the transport the review already uses:
the host's structured per-file fields when it gives a complete list, else the
diff text. It is context for the caller, never a gate, so a delta that cannot
be read is a reason, not an error: the churn output carries ``net_delta: null``
and says why.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clagentic_loadout.acquire.contract import RangeDiffBackend
from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.review.chunking import read_quoted, unquote_c

_HEADER_PREFIX = "diff --git "
_PLAIN_SEPARATOR = " b/"
_QUOTED_SEPARATOR = ' "b/'
_TARGET_PREFIXES = ("rename to ", "copy to ")


class AmbiguousDiffHeader(ValueError):
    """A ``diff --git`` header whose file cannot be named without guessing."""


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


def _path_token(token: str) -> str:
    if token.startswith('"'):
        return unquote_c(read_quoted(token)[0])
    return token


def _header_path(rest: str) -> str | None:
    """The new-side path of a header's text after ``diff --git ``, or None when
    the two sides cannot be told apart from the header alone (a rename whose
    paths contain `` b/``)."""
    if rest.startswith('"'):
        path = _path_token(read_quoted(rest)[1].lstrip(" "))
    elif _QUOTED_SEPARATOR in rest:
        if rest.count(_QUOTED_SEPARATOR) > 1:
            return None
        path = _path_token('"b/' + rest.split(_QUOTED_SEPARATOR, 1)[1])
    else:
        # An unrenamed file spells the same path on both sides, so the halves
        # of the header must match exactly.
        half = (len(rest) - len("a/") - len(_PLAIN_SEPARATOR)) // 2
        old, new = rest[2:2 + half], rest[2 + half + len(_PLAIN_SEPARATOR):]
        if (
            half > 0
            and rest.startswith("a/")
            and rest[2 + half:2 + half + len(_PLAIN_SEPARATOR)] == _PLAIN_SEPARATOR
            and old == new
        ):
            return new
        if rest.count(_PLAIN_SEPARATOR) != 1:
            return None
        path = "b/" + rest.split(_PLAIN_SEPARATOR, 1)[1]
    return path[2:] if path.startswith("b/") else None


def count_changes(diff_text: str) -> tuple[FileDelta, ...]:
    """Added and removed line counts per file of a unified diff with ``diff --git``
    headers, by the file's new name, sorted by path. Only lines inside a hunk
    count, so the ``---``/``+++`` file lines never do. Git C-quoted paths are
    unquoted; a header whose two sides cannot be told apart is settled by its
    ``rename to``/``copy to`` line.

    Raises:
        AmbiguousDiffHeader: a file's name cannot be settled, so no count could
            be attributed to it with confidence."""
    counts: dict[str, list[int]] = {}
    current: list[int] | None = None
    header = ""
    unresolved = False
    in_hunk = False
    for line in diff_text.split("\n"):
        if line.startswith(_HEADER_PREFIX):
            if unresolved:
                raise AmbiguousDiffHeader(header)
            header = line
            name = _header_path(line[len(_HEADER_PREFIX):])
            unresolved = name is None
            current = [0, 0] if name is None else counts.setdefault(name, [0, 0])
            in_hunk = False
        elif current is None:
            continue
        elif unresolved and not in_hunk and line.startswith(_TARGET_PREFIXES):
            current = counts.setdefault(_path_token(line.split(" to ", 1)[1]), [0, 0])
            unresolved = False
        elif line.startswith("@@"):
            if unresolved:
                raise AmbiguousDiffHeader(header)
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            current[0] += 1
        elif in_hunk and line.startswith("-"):
            current[1] += 1
    if unresolved:
        raise AmbiguousDiffHeader(header)
    return tuple(FileDelta(path, added, removed) for path, (added, removed) in sorted(counts.items()))


def _from_structured(listed: Any) -> tuple[FileDelta, ...]:
    totals: dict[str, list[int]] = {}
    for item in listed:
        total = totals.setdefault(item.path, [0, 0])
        total[0] += item.added
        total[1] += item.removed
    return tuple(FileDelta(path, added, removed) for path, (added, removed) in sorted(totals.items()))


def read_net_delta(
    backend: Any, *, owner: str, repo: str, from_head: str, to_head: str
) -> tuple[NetDelta | None, str | None]:
    """``(delta, None)`` for the change from *from_head* to *to_head*, or
    ``(None, reason)`` when the host cannot give it: the backend has no range
    diff, the read failed, *to_head* does not descend from *from_head* (the
    history was rewritten), or a file name in the diff text is ambiguous."""
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
    if span.files is not None:
        return NetDelta(from_head, to_head, _from_structured(span.files)), None
    try:
        files = count_changes(span.diff_text)
    except AmbiguousDiffHeader as exc:
        return None, f"a file name in the range diff is ambiguous: {exc}"
    return NetDelta(from_head, to_head, files), None


__all__ = ["AmbiguousDiffHeader", "FileDelta", "NetDelta", "count_changes", "read_net_delta"]
