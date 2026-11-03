"""review.changed_lines — the lines a pull request adds or changes, per file.

A scanner reports a hit against a line of the file as it is at the reviewed
head. Whether that line is part of the change under review is a question about
the diff, so it is answered here from the unified diff the host served for the
reviewed head and from nothing a reviewer or a scanner wrote.

A line is in the change when the diff adds it (a ``+`` line, numbered on the
new side). A context line, a line outside every hunk, a line of a file the diff
does not touch, and a line of a deleted file are not.

Hunk bodies are read by the line counts in each ``@@`` header, so a changed
line whose own text starts with ``--- `` or ``+++ `` is never mistaken for a
file header.
"""

from __future__ import annotations

import re

_HEADER = "diff --git "
_HUNK_RE = re.compile(r"\A@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_NEW_SIDE_RE = re.compile(r"\A\+\+\+ (.+?)(?:\t.*)?\Z")
_DEV_NULL = "/dev/null"
# One alternation scanned left to right, so each backslash sequence is consumed
# exactly once and a decoded byte can never start another escape.
_ESCAPE_RE = re.compile(r'\\([0-7]{3}|[\\"abfnrtv])|([^\\]+)|(\\)')
_SIMPLE_ESCAPES = {
    "\\": b"\\", '"': b'"', "a": b"\a", "b": b"\b", "f": b"\f",
    "n": b"\n", "r": b"\r", "t": b"\t", "v": b"\v",
}


class DiffUnreadableError(ValueError):
    """The diff text carries no file section that can be read."""


def _unquote(name: str) -> str:
    """A git C-quoted path (``"a\\303\\251.py"``) as the real path."""
    raw = bytearray()
    for match in _ESCAPE_RE.finditer(name[1:-1]):
        escape, literal, lone = match.groups()
        if escape is not None:
            raw += bytes([int(escape, 8)]) if escape.isdigit() else _SIMPLE_ESCAPES[escape]
        elif literal is not None:
            raw += literal.encode("utf-8")
        else:
            raw += b"\\"
    # surrogateescape keeps bytes that are not UTF-8 distinct, so they can only
    # fail to match a reported path, never be altered into another one.
    return raw.decode("utf-8", errors="surrogateescape")


def _new_path(side: str) -> str | None:
    """The repository path named by a ``+++`` line's operand, None for a deleted file."""
    if side == _DEV_NULL:
        return None
    if side.startswith('"') and side.endswith('"') and len(side) >= 2:
        side = _unquote(side)
    return side[2:] if side.startswith("b/") else side


def added_lines(diff_text: str) -> dict[str, frozenset[int]]:
    """For each file the diff touches, the new-side numbers of the lines it adds.

    A file whose diff has a ``+++`` path but no added line (for example a pure
    deletion of lines) maps to an empty set, so the caller can tell a touched file
    from an untouched one. A deleted, mode-only or binary file has no ``+++`` path
    to name, so it is omitted from the result, as an untouched file is. Raises
    DiffUnreadableError when the text has no ``diff --git`` section at all,
    because then nothing can be said about any line."""
    result: dict[str, set[int]] = {}
    current: str | None = None
    in_section = False
    saw_section = False
    old_left = new_left = 0
    new_line = 0
    for line in diff_text.split("\n"):
        if old_left > 0 or new_left > 0:
            marker = line[:1]
            if marker == "+":
                if current is not None:
                    result[current].add(new_line)
                new_line += 1
                new_left -= 1
            elif marker == "-":
                old_left -= 1
            elif marker == "\\":
                pass
            else:
                new_line += 1
                old_left -= 1
                new_left -= 1
            continue
        if line.startswith("\\"):
            continue
        if line.startswith(_HEADER):
            saw_section = in_section = True
            current = None
            continue
        if not in_section:
            continue
        side = _NEW_SIDE_RE.match(line)
        if side is not None:
            current = _new_path(side.group(1))
            if current is not None:
                result.setdefault(current, set())
            continue
        hunk = _HUNK_RE.match(line)
        if hunk is not None:
            old_left = int(hunk.group(1)) if hunk.group(1) is not None else 1
            new_line = int(hunk.group(2))
            new_left = int(hunk.group(3)) if hunk.group(3) is not None else 1
    if not saw_section:
        raise DiffUnreadableError("the diff text has no 'diff --git' file section to read")
    return {path: frozenset(lines) for path, lines in result.items()}


__all__ = ["DiffUnreadableError", "added_lines"]
