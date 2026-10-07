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

import re
from dataclasses import dataclass

DEFAULT_CHUNK_LINES = 600

_FILE_MARKER = "diff --git "
_HUNK_MARKER = "@@"
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_CONTINUATION_NOTE = " (hunk continued from the previous chunk)"
_B_PATH_SEPARATOR = " b/"
_QUOTED_B_SEPARATOR = ' "b/'
_UNHEADED_NAME = "(diff without file headers)"
_OCTAL_DIGITS = "01234567"
_C_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11, '"': 34, "\\": 92}
_DEV_NULL = "/dev/null"


@dataclass(frozen=True)
class HunkSpan:
    """The lines one hunk covers in one file, as inclusive ranges on both sides
    of the diff: ``first``/``last`` in the new numbering, ``old_first``/
    ``old_last`` in the old numbering."""

    file: str
    first: int
    last: int
    old_first: int
    old_last: int


@dataclass(frozen=True)
class Chunk:
    """One reviewable slice of a diff. ``index`` is 1-based. ``hunks`` lists
    the hunks the chunk itself holds, which for a file split across chunks is
    only that chunk's share of it."""

    index: int
    text: str
    files: tuple[str, ...]
    lines: int
    hunks: tuple[HunkSpan, ...] = ()

    def covers_prior_line(self, file: str, line: int) -> bool:
        """True when *line* of *file*, numbered as in the diff's OLD side, lies
        inside one of this chunk's hunks. A finding from an earlier review
        carries the numbering of the head that review saw, which is the old
        side of a since..head delta diff; matching it against new-side lines
        would miss it whenever earlier insertions shifted the file."""
        return any(s.file == file and s.old_first <= line <= s.old_last for s in self.hunks)


@dataclass(frozen=True)
class _Piece:
    lines: tuple[str, ...]
    files: tuple[str, ...]


def _read_quoted(text: str) -> tuple[str, str]:
    """Split a leading C-quoted token (``text[0] == '"'``) into its raw inner
    text and whatever follows the closing quote."""
    position = 1
    while position < len(text):
        if text[position] == "\\":
            position += 2
            continue
        if text[position] == '"':
            return text[1:position], text[position + 1:]
        position += 1
    return text[1:], ""


def _unquote_c(inner: str) -> str:
    """Decode the C-style escapes git uses when it quotes a path: single-char
    escapes and 1-3 digit octal bytes (a UTF-8 path arrives as octal bytes)."""
    out = bytearray()
    position = 0
    while position < len(inner):
        char = inner[position]
        if char != "\\" or position + 1 >= len(inner):
            out += char.encode("utf-8")
            position += 1
            continue
        following = inner[position + 1]
        if following in _OCTAL_DIGITS:
            digits = 1
            while digits < 3 and position + 1 + digits < len(inner) and (
                inner[position + 1 + digits] in _OCTAL_DIGITS
            ):
                digits += 1
            out.append(int(inner[position + 1:position + 1 + digits], 8) & 0xFF)
            position += 1 + digits
        elif following in _C_ESCAPES:
            out.append(_C_ESCAPES[following])
            position += 2
        else:
            out += following.encode("utf-8")
            position += 2
    return out.decode("utf-8", "replace")


def _path_token(token: str) -> str:
    """A path as written in a diff header: unquoted when C-quoted."""
    if token.startswith('"'):
        return _unquote_c(_read_quoted(token)[0])
    return token.strip()


def _file_name(header_line: str) -> str:
    """Best-effort path of a `diff --git a/x b/x` header (the b/ side).

    An unrenamed file repeats its path on both sides, so when the two halves
    match exactly that is the answer even if the path itself contains " b/".
    A rename (differing sides) falls back to the last " b/" separator. A
    header with a C-quoted side (spaces, tabs, quotes, non-ASCII) is split on
    the quote boundary and the quoted path is decoded."""
    rest = header_line[len(_FILE_MARKER):]
    b_side: str | None = None
    if rest.startswith('"'):
        b_side = _read_quoted(rest)[1].lstrip(" ")
    elif _QUOTED_B_SEPARATOR in rest:
        b_side = '"b/' + rest.rsplit(_QUOTED_B_SEPARATOR, 1)[1]
    if b_side is not None:
        path = _path_token(b_side)
        return path[2:] if path.startswith("b/") else path
    path_len = (len(rest) - len("a/") - len(_B_PATH_SEPARATOR)) // 2
    if (
        path_len > 0
        and rest.startswith("a/")
        and rest[2 + path_len:2 + path_len + len(_B_PATH_SEPARATOR)] == _B_PATH_SEPARATOR
        and rest[2:2 + path_len] == rest[2 + path_len + len(_B_PATH_SEPARATOR):]
    ):
        return rest[2:2 + path_len]
    if _B_PATH_SEPARATOR in rest:
        return rest.rsplit(_B_PATH_SEPARATOR, 1)[1].strip()
    return rest.strip()


def _diff_lines(diff_text: str) -> list[str]:
    """Split on LF only. str.splitlines() also breaks on form feed, vertical
    tab, lone CR, and U+2028/U+2029, all of which can sit inside a changed
    line's content and would silently change the diff's line structure."""
    lines = diff_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _split_files(diff_text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    name = ""
    all_lines = _diff_lines(diff_text)
    for line in all_lines:
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
    if not sections and any(line.strip() for line in all_lines):
        # A plain `diff -u` has no `diff --git` headers. Dropping it would
        # report a clean review of a diff that was never read.
        sections.extend(_split_plain_diff(all_lines))
    return sections


def _plain_name(old_line: str, new_line: str) -> str:
    """Path of a plain-diff file from its `--- old` / `+++ new` lines: the new
    side, or the old side for a deletion. A trailing tab-separated timestamp
    and an a/ or b/ prefix are dropped."""
    def clean(line: str) -> str:
        text = line[4:]
        token = text if text.startswith('"') else text.split("\t", 1)[0]
        return _path_token(token)

    new_path = clean(new_line)
    if new_path == _DEV_NULL:
        new_path = clean(old_line)
        prefix = "a/"
    else:
        prefix = "b/"
    return new_path[2:] if new_path.startswith(prefix) else new_path


def _split_plain_diff(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Split a diff with no `diff --git` headers at each `---`/`+++`/`@@`
    triple, so every file is its own section instead of later files' headers
    landing inside the first file's hunk body. The three-line check keeps a
    removed line that merely begins with `-- ` from being taken for a header."""
    starts = [
        position
        for position in range(len(lines) - 2)
        if lines[position].startswith("--- ")
        and lines[position + 1].startswith("+++ ")
        and lines[position + 2].startswith(_HUNK_MARKER)
    ]
    if len(starts) < 2:
        # Zero or one file: nothing to separate, and a lone section keeps the
        # generic label because its path cannot be told apart from prose.
        return [(_UNHEADED_NAME, lines)]
    boundaries = [0] + starts[1:] + [len(lines)]
    return [
        (_plain_name(lines[start], lines[start + 1]), lines[begin:end])
        for start, begin, end in zip(starts, boundaries, boundaries[1:])
    ]


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


def _line_counts(body: list[str]) -> tuple[int, int]:
    """(old, new) line counts a hunk body spans. A `\\ No newline` marker
    belongs to the line before it and counts for neither side."""
    old = new = 0
    for line in body:
        if line.startswith("\\"):
            continue
        if line.startswith("+"):
            new += 1
        elif line.startswith("-"):
            old += 1
        else:
            old += 1
            new += 1
    return old, new


def _range(first: int, count: int) -> str:
    # A zero-length range is written as the line BEFORE the position.
    start = first if count > 0 else first - 1
    return f"{start}" if count == 1 else f"{start},{count}"


def _split_hunk(hunk: list[str], budget: int) -> list[list[str]]:
    """Split one oversized hunk into pieces of at most *budget* lines, each
    led by a real `@@ -a,b +c,d @@` header whose offsets and counts are
    recomputed for the piece, so a reviewer can map a line to a file offset
    from any chunk alone."""
    match = _HUNK_HEADER_RE.match(hunk[0])
    if match is None:
        # A malformed header carries no offsets to continue from. Fabricating
        # a start would look real to the reviewer, so the original header is
        # kept verbatim on every piece instead (continuations are annotated).
        old_first = new_first = 0
        section = ""
    else:
        old_start, old_count, new_start, new_count, section = match.groups()
        # A zero-count start already names the line BEFORE the hunk.
        old_first = int(old_start) + (1 if old_count == "0" else 0)
        new_first = int(new_start) + (1 if new_count == "0" else 0)

    body = hunk[1:]
    room = max(1, budget - 1)
    pieces: list[list[str]] = []
    position = 0
    while position < len(body):
        end = min(position + room, len(body))
        # A `\ No newline` marker belongs to the line before it, so a cut may
        # not land between them: pull that line into the next piece, or take
        # the marker along when the piece holds nothing else.
        if end < len(body) and body[end].startswith("\\"):
            end = end - 1 if end - position > 1 else end + 1
        part = body[position:end]
        old_count_piece, new_count_piece = _line_counts(part)
        if match is None:
            header = hunk[0] + ("" if position == 0 else _CONTINUATION_NOTE)
        else:
            header = (
                f"@@ -{_range(old_first, old_count_piece)} "
                f"+{_range(new_first, new_count_piece)} @@"
                + (section if position == 0 else _CONTINUATION_NOTE)
            )
        pieces.append([header] + part)
        old_first += old_count_piece
        new_first += new_count_piece
        position += len(part)
    return pieces or [hunk]


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
        for piece_lines in _split_hunk(hunk, budget):
            body = piece_lines
            flush()
    flush()
    if not pieces:
        # A file with no hunks at all (rename, mode change, binary) larger
        # than the bound is header-only; keep it whole rather than drop it.
        pieces.append(_Piece(tuple(lines), (name,)))
    return pieces


def _hunk_spans(lines: tuple[str, ...], default_file: str) -> tuple[HunkSpan, ...]:
    """The hunks in a chunk's lines, each with the file it belongs to. A span
    keeps the two sides as separate ranges; a union would claim unchanged
    lines between the two starts. A hunk whose header does not parse has no
    span."""
    spans: list[HunkSpan] = []
    name = default_file
    for position, line in enumerate(lines):
        if line.startswith(_FILE_MARKER):
            name = _file_name(line)
            continue
        if (
            line.startswith("--- ")
            and position + 2 < len(lines)
            and lines[position + 1].startswith("+++ ")
            and lines[position + 2].startswith(_HUNK_MARKER)
        ):
            name = _plain_name(line, lines[position + 1])
            continue
        match = _HUNK_HEADER_RE.match(line)
        if match is None:
            continue
        old_start, old_count, new_start, new_count, _ = match.groups()
        first, last = _side_range(int(new_start), new_count)
        old_first, old_last = _side_range(int(old_start), old_count)
        spans.append(HunkSpan(name, first, last, old_first, old_last))
    return tuple(spans)


def _side_range(start: int, count_text: str | None) -> tuple[int, int]:
    """Inclusive line range one side of a hunk header covers. A zero-count side
    (a pure deletion on the new side, a pure addition on the old side) has no
    lines of its own; it is anchored at the line it sits next to."""
    count = int(count_text) if count_text is not None else 1
    if count > 0:
        return start, start + count - 1
    anchor = max(start, 1)
    return anchor, anchor


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
    exceed it, apart from one degenerate case: with a bound so small that a
    split hunk has room for a single body line, a `\\ No newline` marker is
    kept with its line, so that piece is one line over. A diff with no
    content yields no chunks; a diff with content but no `diff --git` headers
    is reviewed as one unnamed section, never dropped.
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
            hunks=_hunk_spans(piece.lines, piece.files[0] if piece.files else ""),
        )
        for position, piece in enumerate(final, start=1)
    ]
