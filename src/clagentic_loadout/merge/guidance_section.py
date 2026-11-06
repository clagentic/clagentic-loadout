"""merge.guidance_section — the fixed class guidance text, and the comment
section built from a guidance object.

This is the one place the guidance wording lives. review.class_guidance builds
the guidance object from a review's findings and reads this text; the verdict
comment builder (merge.verdict) renders the section from it. It sits in the
merge package, with no import from review, because the comment builder cannot
import the review package.

The section's wording is fixed here and regenerated at render time; only the
structure of a guidance object (class names, fixes, finding ids) is taken from
its caller, each as one line with no fence syntax. The guidance is the
reviewer's own statement: it never changes a severity.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from clagentic_loadout.merge.fence_state import KEY_CLASS_FIX, KEY_DEFECT_CLASS
from clagentic_loadout.merge.fence_syntax import find_fence_syntax

KEY_GUIDANCE = "guidance"
#: The key of the staged comment body that carries the guidance to render.
KEY_COMMENT_GUIDANCE = "class_guidance"
KEY_CLASSES = "classes"
KEY_MORE = "more"
KEY_CLASS_REPEATED = "class_repeated"
KEY_TEXT = "text"
KEY_FINDINGS = "findings"

#: Labels the guidance as the reviewer's output, never as a verified fact.
LABEL_LINE = (
    "The classes below are the reviewer's own statements. Check each against the code "
    "before acting on it."
)
FIX_ONCE_LINE = (
    "Fix a class once, at the shared primitive: list every site with the same defect shape "
    "before changing any, then fix them together."
)
GUARD_ROW_LINE = (
    "Add one guard row to the area's existing test table instead of a separate test body "
    "per site."
)
REPEATED_LINE = (
    "A class is repeated across findings. Stop patching site by site: find the missing "
    "shared primitive and fix that."
)
SECTION_TITLE = "Class guidance"


def guidance_text(*, repeated: bool) -> str:
    """The fixed guidance text; the stop-patching line is added when a class is
    repeated."""
    lines = [LABEL_LINE, FIX_ONCE_LINE, GUARD_ROW_LINE]
    if repeated:
        lines.append(REPEATED_LINE)
    return " ".join(lines)


def _single_line(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{where} must be a single line")
    if find_fence_syntax(value) is not None:
        raise ValueError(f"{where} contains fence-delimiter syntax")
    return value


def validate_guidance(value: Any) -> dict[str, Any]:
    """*value* checked as a guidance object a comment may render, and returned.
    Raises ValueError for the first thing wrong: the comment is built from these
    strings, so each is a single line with no fence syntax."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{KEY_GUIDANCE} must be a JSON object")
    classes = value.get(KEY_CLASSES)
    if not isinstance(classes, list):
        raise ValueError(f"{KEY_GUIDANCE}.{KEY_CLASSES} must be a JSON array")
    more = value.get(KEY_MORE, 0)
    if type(more) is not int or more < 0:
        raise ValueError(f"{KEY_GUIDANCE}.{KEY_MORE} must be a non-negative integer")
    repeated = value.get(KEY_CLASS_REPEATED, False)
    if not isinstance(repeated, bool):
        raise ValueError(f"{KEY_GUIDANCE}.{KEY_CLASS_REPEATED} must be a boolean")
    checked = []
    for index, entry in enumerate(classes):
        where = f"{KEY_GUIDANCE}.{KEY_CLASSES}[{index}]"
        if not isinstance(entry, Mapping):
            raise ValueError(f"{where} must be a JSON object")
        fix = entry.get(KEY_CLASS_FIX)
        ids = entry.get(KEY_FINDINGS)
        if not isinstance(ids, list):
            raise ValueError(f"{where}.{KEY_FINDINGS} must be a JSON array")
        checked.append(
            {
                KEY_DEFECT_CLASS: _single_line(entry.get(KEY_DEFECT_CLASS), f"{where}.{KEY_DEFECT_CLASS}"),
                KEY_CLASS_FIX: None if fix is None else _single_line(fix, f"{where}.{KEY_CLASS_FIX}"),
                KEY_FINDINGS: [_single_line(i, f"{where}.{KEY_FINDINGS}[]") for i in ids],
            }
        )
    return {KEY_CLASSES: checked, KEY_MORE: more, KEY_CLASS_REPEATED: repeated}


def section_lines(guidance: Mapping[str, Any]) -> list[str]:
    """The comment section for a validated *guidance* object, starting with a
    blank line, or no lines when it names no class. The text is regenerated here
    from the fixed lines, never taken from the object."""
    classes = guidance.get(KEY_CLASSES) or []
    if not classes:
        return []
    repeated = bool(guidance.get(KEY_CLASS_REPEATED))
    lines = ["", f"{SECTION_TITLE} ({len(classes) + int(guidance.get(KEY_MORE) or 0)}):"]
    lines.append(guidance_text(repeated=repeated))
    for entry in classes:
        fix = entry.get(KEY_CLASS_FIX)
        lines.append(
            f"- {entry[KEY_DEFECT_CLASS]}"
            + (f" (fix: {fix})" if fix else "")
            + f": {', '.join(entry[KEY_FINDINGS])}"
        )
    more = int(guidance.get(KEY_MORE) or 0)
    if more:
        lines.append(f"- and {more} more")
    return lines


__all__ = [
    "FIX_ONCE_LINE",
    "GUARD_ROW_LINE",
    "KEY_CLASSES",
    "KEY_CLASS_REPEATED",
    "KEY_COMMENT_GUIDANCE",
    "KEY_FINDINGS",
    "KEY_GUIDANCE",
    "KEY_MORE",
    "KEY_TEXT",
    "LABEL_LINE",
    "REPEATED_LINE",
    "SECTION_TITLE",
    "guidance_text",
    "section_lines",
    "validate_guidance",
]
