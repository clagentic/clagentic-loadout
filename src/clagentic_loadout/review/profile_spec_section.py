"""review.profile_spec_section -- the ``spec`` section of ``loadout-profile brief --spec``.

A builder's brief carries the sealed spec it builds from, so the text handed to
it is one document and no caller assembles it. The section holds the stored
document exactly as sealed (the store loader has recomputed its hash) with the
fields a reader needs to place it. The section reports; it never refuses.
"""

from __future__ import annotations

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


def spec_section(stored: StoredSpec, *, read_commit: str | None = None) -> dict[str, Any]:
    """The ``spec`` section for *stored*. *read_commit* is the commit the brief
    read the repository at, when it was read at one."""
    doc = stored.doc
    section: dict[str, Any] = {
        "spec_hash": stored.spec_hash,
        "head_sha": doc["head_sha"],
        "sealed_at": doc["sealed_at"],
        "role": doc["role"],
        "supersedes": doc["supersedes"],
        "document": doc,
    }
    note = head_note(doc["head_sha"], read_commit)
    if note is not None:
        section["note"] = note
    return section


__all__ = ["head_note", "spec_section"]
