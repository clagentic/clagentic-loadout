"""review.changeset_paths — the paths a pull request touches, from what the host served.

Classification needs every path a change touches and the git mode of each, and
errs toward code when it cannot tell. A host serves two partial answers: the
changed-file list (the name of each file after the change, no modes) and the
unified diff (old and new names for a rename, and the modes, but with a quoted
name for a path git has to escape). Both are read and merged, so a rename shows
the old name as well as the new one, and a mode change on a file the list calls
plain is seen.

A diff section whose names cannot be read (a C-quoted name) is reported as the
path ``UNREADABLE_PATH``, which is not a repository-relative path and so is
never documentation: the change is code.
"""

from __future__ import annotations

import re

from clagentic_loadout.acquire.contract import AcquiredPr
from clagentic_loadout.review.docs_only_policy import ChangedPath, merge_changed

#: Reported for a diff section whose file names cannot be read.
UNREADABLE_PATH = "/unreadable-diff-header"

_HEADER = "diff --git "
_SAME_SIDES_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
_MODE_RE = re.compile(
    r"^(?:old mode|new mode|new file mode|deleted file mode) (\d{6})$"
)
_INDEX_MODE_RE = re.compile(r"^index [0-9a-fA-F]+\.\.[0-9a-fA-F]+ (\d{6})$")
_RENAME_RE = re.compile(r"^(?:rename|copy) (?:from|to) (.+)$")
_SIDE_RE = re.compile(r"^(?:--- a/|\+\+\+ b/)(.+?)(?:\t.*)?$")


def _section_paths(lines: list[str]) -> set[str]:
    header = lines[0]
    paths: set[str] = set()
    named = _SAME_SIDES_RE.match(header)
    if '"' in header or named is None:
        return {UNREADABLE_PATH}
    old, new = named.group(1), named.group(2)
    if old == new:
        paths.add(new)
    for line in lines[1:]:
        if line.startswith("@@"):
            break
        for pattern in (_RENAME_RE, _SIDE_RE):
            match = pattern.match(line)
            if match is None:
                continue
            name = match.group(1)
            if name.startswith('"'):
                return {UNREADABLE_PATH}
            paths.add(name)
    # A section with a header git could split but no name lines (a binary
    # file replaced, a mode-only change with differing sides) still names a
    # path: both header halves are taken so nothing is missed.
    if not paths or old != new:
        paths.update((old, new))
    return paths


def from_diff_text(diff_text: str) -> list[ChangedPath]:
    """The paths and modes in a unified diff with ``diff --git`` headers. A diff
    without such headers yields nothing; the caller's other source then decides."""
    sections: list[list[str]] = []
    for line in diff_text.split("\n"):
        if line.startswith(_HEADER):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
    entries: list[ChangedPath] = []
    for lines in sections:
        modes = tuple(
            match.group(1)
            for line in lines
            if (match := _MODE_RE.match(line) or _INDEX_MODE_RE.match(line)) is not None
        )
        entries.extend(ChangedPath(path, modes) for path in sorted(_section_paths(lines)))
    return entries


def from_acquired(acquired: AcquiredPr) -> list[ChangedPath]:
    """Every path *acquired* touches, from its changed-file list and its diff."""
    listed = [ChangedPath(name) for name in acquired.changed_filenames]
    return merge_changed(listed, from_diff_text(acquired.diff_text))


__all__ = ["UNREADABLE_PATH", "from_acquired", "from_diff_text"]
