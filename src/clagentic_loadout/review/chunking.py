"""review.chunking — deterministic, line-bounded splitting of a unified diff.

Chunking is code, never a model's opinion about where to cut: the same diff
and bound always produce the same plan. Whole files are grouped into a chunk
while they fit; a single file larger than the bound is split on hunk
boundaries, and a single hunk larger than the bound is split on line
boundaries. Every piece of a split file carries the file's header lines so a
reviewer reading one chunk alone still knows which file it is looking at.
Chunks that come out tiny are merged into a neighbour when the result still
fits the bound.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_CHUNK_LINES = 600

_FILE_MARKER = "diff --git "
_HUNK_MARKER = "@@"
_CONTINUATION_LINE = "@@ (hunk continued from the previous chunk) @@"
_B_PATH_SEPARATOR = " b/"


@dataclass(frozen=True)
class Chunk:
    """One reviewable slice of a diff. ``index`` is 1-based."""

    index: int
    text: str
    files: tuple[str, ...]
    lines: int


@dataclass(frozen=True)
class _Piece:
    lines: tuple[str, ...]
    files: tuple[str, ...]


def _file_name(header_line: str) -> str:
    """Best-effort path of a `diff --git a/x b/x` header (the b/ side)."""
    rest = header_line[len(_FILE_MARKER):]
    if _B_PATH_SEPARATOR in rest:
        return rest.rsplit(_B_PATH_SEPARATOR, 1)[1].strip()
    return rest.strip()


def _split_files(diff_text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    name = ""
    for line in diff_text.splitlines():
        if line.startswith(_FILE_MARKER):
            if current is not None:
                sections.append((name, current))
            current = [line]
            name = _file_name(line)
        elif current is not None:
            current.append(line)
        # Lines before the first file header carry no reviewable content.
    if current is not None:
        sections.append((name, current))
    return sections


def _split_hunks(lines: list[str]) -> tuple[list[str], list[list[str]]]:
    header: list[str] = []
    hunks: list[list[str]] = []
    for line in lines:
        if line.startswith(_HUNK_MARKER):
            hunks.append([line])
        elif hunks:
            hunks[-1].append(line)
        else:
            header.append(line)
    return header, hunks


def _split_oversized_file(name: str, lines: list[str], max_lines: int) -> list[_Piece]:
    header, hunks = _split_hunks(lines)
    budget = max(1, max_lines - len(header))
    pieces: list[_Piece] = []
    body: list[str] = []

    def flush() -> None:
        nonlocal body
        if body:
            pieces.append(_Piece(tuple(header + body), (name,)))
            body = []

    for hunk in hunks:
        if len(hunk) <= budget:
            if len(body) + len(hunk) > budget:
                flush()
            body.extend(hunk)
            continue
        flush()
        position = 0
        first = True
        while position < len(hunk):
            room = budget if first else budget - 1
            room = max(1, room)
            part = hunk[position:position + room]
            position += len(part)
            body = ([] if first else [_CONTINUATION_LINE]) + part
            first = False
            flush()
    flush()
    if not pieces:
        # A file with no hunks at all (rename, mode change, binary) larger
        # than the bound is header-only; keep it whole rather than drop it.
        pieces.append(_Piece(tuple(lines), (name,)))
    return pieces


def _merge_tiny(pieces: list[_Piece], max_lines: int, min_lines: int) -> list[_Piece]:
    merged: list[_Piece] = []
    for piece in pieces:
        if (
            merged
            and (len(piece.lines) < min_lines or len(merged[-1].lines) < min_lines)
            and len(merged[-1].lines) + len(piece.lines) <= max_lines
        ):
            previous = merged.pop()
            merged.append(
                _Piece(previous.lines + piece.lines, previous.files + piece.files)
            )
        else:
            merged.append(piece)
    return merged


def plan_chunks(diff_text: str, max_lines: int = DEFAULT_CHUNK_LINES) -> list[Chunk]:
    """Split *diff_text* into chunks of at most *max_lines* lines each.

    A file whose header alone exceeds the bound is the only way a chunk can
    exceed it. An empty diff yields no chunks.
    """
    if max_lines < 1:
        raise ValueError(f"max_lines must be >= 1, got {max_lines!r}")

    packed: list[_Piece] = []
    current: list[str] = []
    current_files: list[str] = []

    def flush() -> None:
        nonlocal current, current_files
        if current:
            packed.append(_Piece(tuple(current), tuple(current_files)))
            current, current_files = [], []

    for name, lines in _split_files(diff_text):
        if len(lines) > max_lines:
            flush()
            packed.extend(_split_oversized_file(name, lines, max_lines))
            continue
        if current and len(current) + len(lines) > max_lines:
            flush()
        current.extend(lines)
        current_files.append(name)
    flush()

    min_lines = max(1, max_lines // 6)
    final = _merge_tiny(packed, max_lines, min_lines)
    return [
        Chunk(
            index=position,
            text="\n".join(piece.lines) + "\n",
            files=tuple(dict.fromkeys(piece.files)),
            lines=len(piece.lines),
        )
        for position, piece in enumerate(final, start=1)
    ]
