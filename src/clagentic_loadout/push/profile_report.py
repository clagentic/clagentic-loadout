"""push.profile_report — what the repo profile is missing, reported at push time.

The push verification step is the point where an agent is already looking at its
own repository, so it is where an incomplete profile is made actionable: the
items are printed, and the verification records carry a pointer to them. This is
reporting only. It never fails a push and is no gate input; the gates read the
profile themselves, from the merge base.

The items come from the one computation every consumer shares
(review.profile_brief.read_completeness).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from clagentic_loadout.review.profile_brief import ProfileReadError, read_completeness
from clagentic_loadout.review.profile_posture import CompletenessItem

EVIDENCE_PREFIX = "profile-completeness:"


def read_items(project_root: Path) -> tuple[CompletenessItem, ...]:
    """The completeness items of the profile in the working tree at
    *project_root*; none when the repository cannot be listed, which the push
    itself reports on its own."""
    try:
        return read_completeness(project_root)
    except ProfileReadError:
        return ()


def print_items(items: Sequence[CompletenessItem], stream: TextIO | None = None) -> None:
    """One line per item on *stream* (default: standard error)."""
    out = stream if stream is not None else sys.stderr
    for item in items:
        print(f"push: incomplete repo profile: {item.render()}", file=out)


def evidence_ref(items: Sequence[CompletenessItem]) -> str | None:
    """The verdict record evidence pointing at *items*: the keys that are
    incomplete, in report order; None when the profile is complete."""
    if not items:
        return None
    return EVIDENCE_PREFIX + ",".join(item.key for item in items)


__all__ = ["EVIDENCE_PREFIX", "evidence_ref", "print_items", "read_items"]
