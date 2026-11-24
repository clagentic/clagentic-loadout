"""review.profile_spec_section -- the ``spec`` section of ``loadout-profile brief --spec``.

A builder's brief carries the sealed spec it builds from, so the text handed to
it is one document and no caller assembles it. The section holds the stored
document exactly as sealed (the store loader has recomputed its hash) with the
fields a reader needs to place it. The section reports; it never refuses.
"""

from __future__ import annotations

import json
import shlex
from typing import Any

from clagentic_loadout.spec.store import StoredSpec


def head_note(spec_head: str, read_commit: str | None) -> str | None:
    """A line naming both commits when the brief was read at a commit other than
    the one the spec was sealed at, else None. No commit read, no note."""
    if read_commit is None or read_commit == spec_head:
        return None
    return (
        f"the spec was sealed at {spec_head} but this brief was read at {read_commit}; "
        "the repository has moved since the seal"
    )


FORM_DOCUMENT = "document"
FORM_REFERENCE = "reference"
SPEC_FORMS = (FORM_DOCUMENT, FORM_REFERENCE)

#: Document keys the reference form reports on their own, so its field index
#: does not list them again.
_HEADER_KEYS = frozenset({"head_sha", "sealed_at", "role", "supersedes", "schema_version"})


def show_line(spec_hash: str, field: str | None = None) -> str:
    """The `loadout-spec show` command line that prints the spec, or one top-level
    field of it. Every value is shell-quoted."""
    line = f"loadout-spec show {shlex.quote(spec_hash)}"
    return line if field is None else f"{line} --field {shlex.quote(field)}"


def _field_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def spec_section(
    stored: StoredSpec, *, read_commit: str | None = None, form: str = FORM_DOCUMENT
) -> dict[str, Any]:
    """The ``spec`` section for *stored*. *read_commit* is the commit the brief
    read the repository at, when it was read at one. The ``document`` form holds
    the whole sealed document; the ``reference`` form holds an index of it (each
    top-level field's name, size and the line that prints it), so its size does
    not grow with the spec."""
    doc = stored.doc
    section: dict[str, Any] = {
        "spec_hash": stored.spec_hash,
        "head_sha": doc["head_sha"],
        "sealed_at": doc["sealed_at"],
        "role": doc["role"],
        "supersedes": doc["supersedes"],
    }
    if form == FORM_REFERENCE:
        section["show"] = show_line(stored.spec_hash)
        section["fields"] = [
            {
                "name": name,
                "bytes": _field_bytes(value),
                "show": show_line(stored.spec_hash, name),
            }
            for name, value in doc.items()
            if name not in _HEADER_KEYS
        ]
    else:
        section["document"] = doc
    note = head_note(doc["head_sha"], read_commit)
    if note is not None:
        section["note"] = note
    return section


__all__ = ["FORM_DOCUMENT", "FORM_REFERENCE", "SPEC_FORMS", "head_note", "show_line", "spec_section"]
