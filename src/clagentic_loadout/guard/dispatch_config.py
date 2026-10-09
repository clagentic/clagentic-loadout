"""guard.dispatch_config — deployment-supplied extras for
`guard.dispatch_discipline`.

`guard.dispatch_discipline` is a pure predicate library and reads no
configuration. A deployment's own directory conventions (the directories
its harness keeps documentation or agent state in) are not product
vocabulary, so they arrive here, from the USER-LEVEL config file's `guard:`
section, and a harness adapter passes the result to
`is_trivial_path`/`check_dispatch_discipline` as `trivial_dir_segments`:

    guard:
      trivial_dir_segments: [.agent-state, .notes]

The configured names are ADDED to `DEFAULT_TRIVIAL_DIR_SEGMENTS`; the
default is never dropped. Lenient like the other advisory-only readers in
this package: a non-list value, or a non-string / empty / path-like entry,
is ignored, because this feeds a warn-only guard that must never fail a
session over a malformed knob.
"""

from __future__ import annotations

from pathlib import Path

from clagentic_loadout.guard.dispatch_discipline import DEFAULT_TRIVIAL_DIR_SEGMENTS
from clagentic_loadout.transport.provider_config import load_user_config_section

#: Top-level section of the user-level config file this module reads.
CONFIG_SECTION_GUARD = "guard"

#: Key within `guard:` listing extra trivial directory segment names.
CONFIG_KEY_TRIVIAL_DIR_SEGMENTS = "trivial_dir_segments"


def load_trivial_dir_segments(
    *,
    config_root: str | Path | None = None,
) -> frozenset[str]:
    """`DEFAULT_TRIVIAL_DIR_SEGMENTS` plus the user-level
    `guard.trivial_dir_segments` entries (a bare segment name, no `/`)."""
    section = load_user_config_section(CONFIG_SECTION_GUARD, config_root=config_root)
    raw = section.get(CONFIG_KEY_TRIVIAL_DIR_SEGMENTS)
    if not isinstance(raw, list):
        return DEFAULT_TRIVIAL_DIR_SEGMENTS
    extras = {
        entry.strip()
        for entry in raw
        if isinstance(entry, str) and entry.strip() and "/" not in entry
    }
    return DEFAULT_TRIVIAL_DIR_SEGMENTS | extras


__all__ = [
    "CONFIG_KEY_TRIVIAL_DIR_SEGMENTS",
    "CONFIG_SECTION_GUARD",
    "load_trivial_dir_segments",
]
