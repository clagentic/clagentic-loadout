"""merge.fence_syntax — the one predicate for fence-shaped caller text.

A tool-constructed verdict comment must never carry fence syntax it did not emit
itself: caller data that could open or close a fenced block could smuggle a second
verdict block into the body. Every field the body builder and the findings-state
validator accept goes through `find_fence_syntax`, so there is exactly one
definition of "fence-shaped".

What counts as fence syntax:

  - a run of three or more backticks ANYWHERE in the value. The verdict reader's
    own fence pattern is not anchored to a line start, so a backtick run in the
    middle of a line can still open a block it recognises.
  - a run of three or more tildes at the START of a line (after optional
    indentation), the only position where a tilde run is a fence.

What does NOT count: the plain word `review-result`, or any other text that is not
fence syntax. A repository path such as `schemas/review-result.schema.json` is
ordinary data and must be postable.
"""

from __future__ import annotations

import re

_BACKTICK_RUN = re.compile(r"`{3,}")
_TILDE_FENCE_LINE = re.compile(r"^[ \t]*~{3,}", re.MULTILINE)


def find_fence_syntax(value: str) -> str | None:
    """Return the offending fence-syntax fragment in *value*, or None if clean."""
    match = _BACKTICK_RUN.search(value)
    if match:
        return match.group(0)
    match = _TILDE_FENCE_LINE.search(value)
    if match:
        return match.group(0).strip()
    return None


__all__ = ["find_fence_syntax"]
