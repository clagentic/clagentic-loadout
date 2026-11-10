"""_source_commit.py — the source commit an installed release was built from.

scripts/install.sh (venv tier) writes a small key=value record into the root of
each release venv it builds: the full 40-hex commit of the source checkout and
whether that checkout's tree was dirty. This module reads the record back, so an
install readback can confirm the installed commit without trusting the release
directory name.

It is deliberately separate from ``_version.get_version()``, which every verb's
``--version`` output consumes and which must not change.

The reader never raises and never fails an install or a command: no record (an
uninstalled checkout, an editable install, a non-git source, an install tier
that records nothing) reads as ``unknown``.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: File name of the record, relative to the release venv root (sys.prefix).
RECORD_FILENAME = "clagentic-loadout-source-commit"

UNKNOWN = "unknown"

_FULL_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class SourceCommit:
    """commit: 40-hex sha or ``unknown``. dirty: ``true``, ``false`` or ``unknown``."""

    commit: str
    dirty: str

    def render(self) -> str:
        return f"commit={self.commit}\ndirty={self.dirty}"


_UNRECORDED = SourceCommit(UNKNOWN, UNKNOWN)


def parse_record(text: str) -> SourceCommit:
    """Parse the installer's key=value record; anything malformed reads as unknown."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    commit = fields.get("commit", UNKNOWN)
    if not _FULL_SHA.fullmatch(commit):
        return _UNRECORDED
    dirty = fields.get("dirty", UNKNOWN)
    if dirty not in ("true", "false"):
        dirty = UNKNOWN
    return SourceCommit(commit, dirty)


def get_source_commit(prefix: Path | str | None = None) -> SourceCommit:
    """Read the commit recorded in the release rooted at *prefix* (default: the
    running interpreter's prefix, which is the release venv for an installed
    console script)."""
    root = Path(prefix if prefix is not None else sys.prefix)
    try:
        text = (root / RECORD_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return _UNRECORDED
    return parse_record(text)
