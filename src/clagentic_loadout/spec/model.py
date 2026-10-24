"""spec.model — the standard spec: its shape, its canonical form and its hash.

A spec is a JSON document the published contract ``schemas/spec.schema.json``
describes. It exists in two forms:

  draft   what `spec new` writes and an author edits: every field of the sealed
          form except ``head_sha`` and ``sealed_at``. A draft is an ordinary
          file; nothing about it is protected.
  sealed  the draft plus the commit it was sealed at and the time. Its
          serialization is canonical (keys sorted, no insignificant whitespace,
          ASCII), and its name is the sha256 of exactly those bytes, so the name
          proves the content: a reader that recomputes the hash and finds a
          different one knows the file is not what was sealed.

The role is an opaque key. This module records the string and checks its shape;
it knows no role, and no section depends on which role authored the spec.

Sections with no automatic source yet (group rules, consumers, profile
completeness) are present and empty in a new draft, each carrying a ``source`` of
null and a note saying so. The sections the repo profile can fill (class rules,
the declared test commands) are filled by `apply_brief`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clagentic_loadout.envelope import validate_against_schema

SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "spec.schema.json"

#: The same grammar every other role key in this package is held to.
ROLE_RE = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
HASH_RE = re.compile(r"\A[0-9a-f]{64}\Z")

SOURCE_PROFILE = "profile"
NO_SOURCE_NOTE = "no source yet"

#: Fields only a seal sets. A draft that already has one is a sealed spec being
#: sealed again in place, which is refused.
SEAL_KEYS = ("head_sha", "sealed_at")


class SpecFormatError(ValueError):
    """A spec, or a value meant for one, does not satisfy the published schema;
    the message names the field and what it held."""


class AlreadySealedError(SpecFormatError):
    """The document given to be sealed is already a sealed spec."""


@dataclass(frozen=True)
class SealedSpec:
    """A sealed spec: the document, its canonical bytes, and their hash."""

    doc: dict[str, Any]
    data: bytes
    spec_hash: str


def canonical_bytes(doc: dict[str, Any]) -> bytes:
    """The canonical serialization of *doc*: the bytes that are hashed, stored
    and copied into a pull request body."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_spec_hash(value: object) -> bool:
    return isinstance(value, str) and HASH_RE.match(value) is not None


def _empty_section(items_key: str = "items") -> dict[str, Any]:
    return {"source": None, "note": NO_SOURCE_NOTE, items_key: []}


def new_draft(role: str) -> dict[str, Any]:
    """An empty draft for *role*. Raises SpecFormatError when *role* is not a
    well-formed role key."""
    if not isinstance(role, str) or ROLE_RE.match(role) is None:
        raise SpecFormatError(
            f"role {role!r} is not a role key (letters, digits, '-' and '_', at most 64 "
            "characters, starting with a letter or digit)"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "role": role,
        "supersedes": None,
        "goal": "",
        "scope": {"in": [], "out": []},
        "acceptance_criteria": [],
        "class_rules": {"source": None, "note": None, "class": None, "rules": {}, "paths": []},
        "group_rules": _empty_section(),
        "consumers": _empty_section(),
        "test_plan": {"source": None, "note": None, "declared": None, "plan": []},
        "verification_evidence_owed": [],
        "rulings_already_made": [],
        "profile_completeness": _empty_section(),
        "docs_only_justification": None,
    }


def apply_brief(
    draft: dict[str, Any], brief: dict[str, Any] | None, *, absent_note: str
) -> dict[str, Any]:
    """*draft* with its verb-sourced sections (class rules, declared test
    commands) set from *brief*, the repo profile's brief (see
    review.profile_brief). With no brief (the repository has no profile) both are
    left empty and say why with *absent_note*. Authored content, including the
    author's test plan, is kept."""
    out = copy.deepcopy(draft)
    plan = out["test_plan"]["plan"]
    if brief is None:
        out["class_rules"] = {
            "source": None, "note": absent_note, "class": None, "rules": {}, "paths": [],
        }
        out["test_plan"] = {
            "source": None, "note": absent_note, "declared": None, "plan": plan,
        }
        return out
    out["class_rules"] = {
        "source": SOURCE_PROFILE,
        "note": None,
        "class": brief["class"],
        "rules": brief["class_rules"],
        "paths": brief["paths"],
    }
    declared = brief["tests"]
    out["test_plan"] = {
        "source": SOURCE_PROFILE if declared else None,
        "note": None if declared else "the repo profile declares no test commands",
        "declared": declared,
        "plan": plan,
    }
    return out


def draft_from_sealed(sealed: dict[str, Any], sealed_hash: str) -> dict[str, Any]:
    """A draft that starts from the sealed spec *sealed* (whose hash is
    *sealed_hash*) and names it in ``supersedes``. The only way to change a
    sealed spec is to edit a draft made this way and seal it."""
    draft = {k: copy.deepcopy(v) for k, v in sealed.items() if k not in SEAL_KEYS}
    draft["supersedes"] = sealed_hash
    return draft


def validate_sealed_doc(doc: object) -> dict[str, Any]:
    """*doc* if it satisfies the published schema and its acceptance criterion
    ids are unique. Raises SpecFormatError naming each problem."""
    if not isinstance(doc, dict):
        raise SpecFormatError(f"a spec must be a JSON object, got {type(doc).__name__}")
    errors = validate_against_schema(doc, SCHEMA_PATH)
    if errors:
        raise SpecFormatError("; ".join(errors))
    seen: set[str] = set()
    for criterion in doc["acceptance_criteria"]:
        if criterion["id"] in seen:
            raise SpecFormatError(
                f"acceptance_criteria: id {criterion['id']!r} is used more than once"
            )
        seen.add(criterion["id"])
    return doc


def seal(draft: object, *, head_sha: str, sealed_at: str) -> SealedSpec:
    """Seal *draft* at *head_sha* and *sealed_at*. Raises AlreadySealedError when
    *draft* already carries a seal, and SpecFormatError when the result does not
    satisfy the schema (an empty goal, no acceptance criterion, a malformed
    field)."""
    if not isinstance(draft, dict):
        raise SpecFormatError(f"a spec must be a JSON object, got {type(draft).__name__}")
    already = [key for key in SEAL_KEYS if key in draft]
    if already:
        raise AlreadySealedError(
            f"the document already carries {already}: it is a sealed spec, and a sealed spec is "
            "never sealed again in place. Make a draft that supersedes it with "
            "`spec new --supersedes <hash>`, edit the draft, and seal that"
        )
    doc = validate_sealed_doc({**draft, "head_sha": head_sha, "sealed_at": sealed_at})
    data = canonical_bytes(doc)
    return SealedSpec(doc=doc, data=data, spec_hash=hash_bytes(data))


def parse_sealed(data: bytes, expected_hash: str) -> dict[str, Any]:
    """The sealed spec *data* holds, given that its name says it hashes to
    *expected_hash*. Raises SpecFormatError when the bytes do not hash to that
    name, are not a valid spec, or are not in canonical form."""
    actual = hash_bytes(data)
    if actual != expected_hash:
        raise SpecFormatError(
            f"the content hashes to {actual}, not to the name {expected_hash} it is stored under"
        )
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpecFormatError(f"the content is not JSON: {exc}") from exc
    validate_sealed_doc(doc)
    if canonical_bytes(doc) != data:
        raise SpecFormatError("the content is not in canonical form")
    return doc


__all__ = [
    "HASH_RE",
    "NO_SOURCE_NOTE",
    "ROLE_RE",
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "SEAL_KEYS",
    "SOURCE_PROFILE",
    "AlreadySealedError",
    "SealedSpec",
    "SpecFormatError",
    "apply_brief",
    "canonical_bytes",
    "draft_from_sealed",
    "hash_bytes",
    "is_spec_hash",
    "new_draft",
    "parse_sealed",
    "seal",
    "validate_sealed_doc",
]
