"""review.prior_view — where a prior finding's line went, and what the code
around it reads now.

A finding from an earlier review is numbered against the head that review saw,
which is the old side of a since..head delta diff. To judge it, a reviewer must
see the line it named and the code now standing around it; a delta diff holds
only the changed hunks, so everything here is read from those hunks and nothing
else. A line the diff does not show is not shown.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from clagentic_loadout.review.chunking import Chunk, HunkRows

#: Lines of the new head shown on either side of the mapped location.
EXCERPT_RADIUS = 20

#: Bounds on the excerpt as a whole and on any one line of it.
MAX_EXCERPT_CHARS = 3000
MAX_LINE_CHARS = 200


@dataclass(frozen=True)
class PriorLocation:
    """Where a prior line sits in the new head. *mapped* is its line number
    there, or None when the diff removed it or cannot place it; *anchor* is
    always a line number to center a view on (the mapped line, else the nearest
    new-head position)."""

    mapped: int | None
    anchor: int


def _hunks_of(chunks: Sequence[Chunk], file: str) -> list[HunkRows]:
    found: list[HunkRows] = []
    for chunk in sorted(chunks, key=lambda c: c.index):
        found.extend(chunk.hunk_rows(file))
    return sorted(found, key=lambda h: (h.old_start, h.new_start))


def _old_end(hunk: HunkRows) -> int:
    # A zero-length old side names the line before the hunk as its position.
    return hunk.old_start + hunk.old_count - 1 if hunk.old_count > 0 else hunk.old_start


def locate_prior_line(chunks: Sequence[Chunk], file: str, line: int) -> PriorLocation:
    """Map *line* of *file* at the last reviewed head to the new head.

    A line outside every hunk moves by the net size of the hunks above it. A
    context line inside a hunk maps exactly. A line the diff removed has no
    counterpart: it is unmapped, anchored at the new line that now follows it."""
    hunks = _hunks_of(chunks, file)
    for hunk in hunks:
        if hunk.old_count > 0 and hunk.old_start <= line <= _old_end(hunk):
            for position, row in enumerate(hunk.rows):
                if row.old_number != line:
                    continue
                if row.new_number is not None:
                    return PriorLocation(row.new_number, row.new_number)
                following = next(
                    (r.new_number for r in hunk.rows[position:] if r.new_number is not None),
                    None,
                )
                return PriorLocation(None, following or hunk.new_start + hunk.new_count)
    shift = sum(h.new_count - h.old_count for h in hunks if _old_end(h) < line)
    return PriorLocation(line + shift, line + shift)


def prior_line_text(chunks: Sequence[Chunk], file: str, line: int) -> str | None:
    """The text *line* had at the last reviewed head, when the diff shows it (a
    context or removed line); None otherwise."""
    for hunk in _hunks_of(chunks, file):
        for row in hunk.rows:
            if row.old_number == line:
                return row.text
    return None


def new_head_excerpt(
    chunks: Sequence[Chunk],
    file: str,
    anchor: int,
    radius: int = EXCERPT_RADIUS,
    max_chars: int = MAX_EXCERPT_CHARS,
) -> list[str]:
    """Numbered lines of the new head within *radius* of *anchor* that the diff
    shows, in order, with ``...`` marking a gap. Bounded by *max_chars* in
    total; each line is cut to MAX_LINE_CHARS."""
    shown: dict[int, str] = {}
    for hunk in _hunks_of(chunks, file):
        for row in hunk.rows:
            if row.new_number is not None and abs(row.new_number - anchor) <= radius:
                shown[row.new_number] = row.text
    out: list[str] = []
    used = 0
    previous: int | None = None
    for number in sorted(shown):
        text = shown[number]
        if len(text) > MAX_LINE_CHARS:
            text = text[: MAX_LINE_CHARS - 3] + "..."
        entry = f"{number}: {text}"
        if used + len(entry) > max_chars:
            out.append("...")
            break
        if previous is not None and number != previous + 1:
            out.append("...")
        out.append(entry)
        used += len(entry)
        previous = number
    return out
