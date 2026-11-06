"""review.class_guidance — the reviewer's statement of a recurring defect class,
and the guidance built from it.

A finding may carry two optional keys, ``defect_class`` (the recurring class it
is an instance of) and ``class_fix`` (the structural change that removes the
class). Both are the reviewer's own words: this module bounds and cleans them,
groups findings by class, and builds the guidance object; the fixed guidance
text it carries lives in merge.guidance_section. Neither key ever changes a severity, and neither is grounds to add,
drop or escalate a finding; the evaluator never reads them.

The guidance object (every key always present)::

    {"classes": [{"defect_class": "...", "class_fix": "..." | null,
                  "findings": ["file:line:rule_id", ...]}],
     "more": 0,
     "class_repeated": false,
     "text": "<guidance text>" | ""}

``classes`` is capped at MAX_GUIDANCE_CLASSES, most findings first; ``more``
counts the classes left out. ``class_repeated`` is true when one class is on
two findings. ``text`` is empty when there is no class to speak of.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

from clagentic_loadout.merge.fence_state import (
    KEY_CLASS_FIX,
    KEY_DEFECT_CLASS,
    MAX_CLASS_CHARS,
)
from clagentic_loadout.merge.guidance_section import (
    KEY_CLASS_REPEATED,
    KEY_CLASSES,
    KEY_FINDINGS,
    KEY_MORE,
    KEY_TEXT,
    guidance_text,
)

#: The most classes the guidance lists; the rest are counted in ``more``.
MAX_GUIDANCE_CLASSES = 5
#: The most finding ids listed under one class.
MAX_IDS_PER_CLASS = 20

#: What the reply contract says about the two keys. Part of the prompt only when
#: the deployment states classes (review.profile_config).
CLASS_FIELDS_CONTRACT = """\
  "defect_class": OPTIONAL, a short name for the recurring class of defect this
    finding is one instance of, at most {limit} characters (string, or null)
  "class_fix": OPTIONAL, the one structural change that would remove the whole
    class, at most {limit} characters (string, or null)
Do not invent a class: a finding that is isolated is a complete answer, and
null for both keys is the correct reply for it. These two keys never change a
severity and are never grounds to add, drop or escalate a finding.
""".format(limit=MAX_CLASS_CHARS)


def clean_class_value(value: Any) -> str | None:
    """*value* as a stored class string: bounded to MAX_CLASS_CHARS, one line,
    with control characters stripped and backticks replaced (the value sits in a
    comment and a fence). None for anything that is not a string or is empty
    once cleaned, so a malformed value costs the key and never the finding."""
    if not isinstance(value, str):
        return None
    spaced = "".join(" " if ch in "\t\n\r\v\f" else ch for ch in value)
    kept = "".join(
        ch
        for ch in spaced
        if not unicodedata.category(ch).startswith("C")
        and unicodedata.category(ch) not in ("Zl", "Zp")
    )
    flat = " ".join(kept.replace("`", "'").split())
    bounded = flat[:MAX_CLASS_CHARS].rstrip()
    return bounded or None


def class_fields_of(item: Mapping[str, Any]) -> dict[str, str]:
    """The cleaned class keys *item* carries; a key that is absent, null or
    malformed is left out."""
    out: dict[str, str] = {}
    for key in (KEY_DEFECT_CLASS, KEY_CLASS_FIX):
        cleaned = clean_class_value(item.get(key))
        if cleaned is not None:
            out[key] = cleaned
    return out


def _finding_id(item: Mapping[str, Any]) -> str | None:
    file, line, rule_id = item.get("file"), item.get("line"), item.get("rule_id")
    if isinstance(file, str) and type(line) is int and isinstance(rule_id, str):
        return f"{file}:{line}:{rule_id}"
    return None


def build_guidance(findings: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The guidance object for *findings* (the open and advisory findings of a
    review). A praise note is not a defect and is skipped, as is a finding that
    states no class. Findings group by class ignoring case and spacing; the
    first spelling and the first class_fix stated for a class stand."""
    groups: dict[str, dict[str, Any]] = {}
    for item in findings:
        if item.get("severity") == "praise":
            continue
        fields = class_fields_of(item)
        name = fields.get(KEY_DEFECT_CLASS)
        finding_id = _finding_id(item)
        if name is None or finding_id is None:
            continue
        group = groups.setdefault(
            name.casefold(), {KEY_DEFECT_CLASS: name, KEY_CLASS_FIX: None, KEY_FINDINGS: []}
        )
        if group[KEY_CLASS_FIX] is None:
            group[KEY_CLASS_FIX] = fields.get(KEY_CLASS_FIX)
        if finding_id not in group[KEY_FINDINGS]:
            group[KEY_FINDINGS].append(finding_id)
    ordered = sorted(groups.values(), key=lambda g: -len(g[KEY_FINDINGS]))
    repeated = any(len(g[KEY_FINDINGS]) >= 2 for g in ordered)
    listed = [
        {**g, KEY_FINDINGS: g[KEY_FINDINGS][:MAX_IDS_PER_CLASS]}
        for g in ordered[:MAX_GUIDANCE_CLASSES]
    ]
    return {
        KEY_CLASSES: listed,
        KEY_MORE: max(0, len(ordered) - MAX_GUIDANCE_CLASSES),
        KEY_CLASS_REPEATED: repeated,
        KEY_TEXT: guidance_text(repeated=repeated) if listed else "",
    }


def first_class_by_key(
    pairs: Iterable[tuple[tuple[str, str], Mapping[str, Any]]],
) -> dict[tuple[str, str], tuple[str, str | None]]:
    """For each (rule_id, file) key, the class statement of the first finding
    under it that states a defect_class, as (defect_class, class_fix). Used to
    quote the class of a churning key; a key whose findings state none is left
    out."""
    out: dict[tuple[str, str], tuple[str, str | None]] = {}
    for key, item in pairs:
        fields = class_fields_of(item)
        if KEY_DEFECT_CLASS in fields and key not in out:
            out[key] = (fields[KEY_DEFECT_CLASS], fields.get(KEY_CLASS_FIX))
    return out


def without_class_keys(findings: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """*findings* without the two class keys, for a deployment that states none."""
    return [
        {k: v for k, v in item.items() if k not in (KEY_DEFECT_CLASS, KEY_CLASS_FIX)}
        for item in findings
    ]


__all__ = [
    "CLASS_FIELDS_CONTRACT",
    "MAX_GUIDANCE_CLASSES",
    "build_guidance",
    "class_fields_of",
    "first_class_by_key",
    "without_class_keys",
    "clean_class_value",
]
