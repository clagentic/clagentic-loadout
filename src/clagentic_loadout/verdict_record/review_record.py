"""verdict_record.review_record — what one posted review judged, kept by comment id.

The comment fence is the only other copy of a review's findings, and it holds
neither the chunk boundaries the review was cut along nor, for a later reader,
anything that survives the comment being edited away. A **review record** keeps
them where the host cannot change them: one file per verified comment, in the
shared head-keyed store (artifact_store), named by the comment id::

    <review records root>/<head sha>/<comment id>.json

The root is, in order: the ``CLAGENTIC_LOADOUT_REVIEW_RECORDS_DIR`` environment
variable, the user-level ``review_records.dir`` setting, then
``<user config root>/state/review-records``; the same resolution, atomic writer
and permission rule as the verdict records store.

A record is written only for a comment that was read back from the host with its
verdict block verified, so a comment id that has a record is a review that landed.
Nothing here edits a stored file; a second post that reuses the same comment
rewrites the same record. A comment reused after the head moved leaves one record
per head; the reader returns the one with the newest ``recorded_at`` unless a head
is named.

Fields (``schema_version`` 1): ``comment_id``, ``repo``, ``pr``, ``head_sha``,
``role``, ``policy_base_sha``, ``spec_hash`` (null when none was given),
``engines``, ``recorded_at``, ``findings`` (``evaluated``, ``open``,
``advisory``), ``dropped`` (each with its reason) and ``chunks``: one entry per
chunk with its ``index``, ``files`` and ``hunks`` (the line spans of each hunk,
or null when the findings file predates span recording). ``chunks`` is null when
the findings file carried no chunk record at all.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from clagentic_loadout.artifact_store import (
    RootUnreadableError,
    StoreError,
    StoreKind,
    list_head_dirs,
    refuse_untrusted,
    resolve_root,
    write_into_head_dir,
)
from clagentic_loadout.verdict_record.record import utc_timestamp

REVIEW_RECORDS_DIR_ENV_VAR = "CLAGENTIC_LOADOUT_REVIEW_RECORDS_DIR"
CONFIG_SECTION = "review_records"
CONFIG_KEY_DIR = "dir"
SCHEMA_VERSION = 1

#: A host's comment id is a decimal integer; anything else never reaches a path.
COMMENT_ID_RE = re.compile(r"\A[0-9]{1,20}\Z")

_REVIEW_RECORDS_KIND = StoreKind(
    label="review records",
    env_var=REVIEW_RECORDS_DIR_ENV_VAR,
    section=CONFIG_SECTION,
    key=CONFIG_KEY_DIR,
    default_subdir="review-records",
)

_MAX_RECORD_BYTES = 8 * 1024 * 1024
_SPAN_KEYS = ("first", "last", "old_first", "old_last")

#: Every required top-level field of a stored record and the types it may hold
#: (``type(None)`` where the schema allows null). The reader validates against it.
_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "schema_version": (int,),
    "comment_id": (str,),
    "repo": (str,),
    "pr": (int,),
    "head_sha": (str,),
    "role": (str,),
    "policy_base_sha": (str, type(None)),
    "spec_hash": (str, type(None)),
    "engines": (list,),
    "recorded_at": (str,),
    "findings": (dict,),
    "dropped": (list,),
    "chunks": (list, type(None)),
}
_FINDINGS_KEYS = ("evaluated", "open", "advisory")


class ReviewRecordNotFoundError(StoreError):
    """No review record has the comment id asked for."""


class ReviewRecordInvalidError(StoreError):
    """A file stored under a comment id is not a valid review record."""


def review_records_root(
    env: Mapping[str, str] | None = None, *, config_root: str | Path | None = None
) -> Path:
    """The review records root. It is not created here."""
    return resolve_root(_REVIEW_RECORDS_KIND, env, config_root=config_root)


def _spans_of(value: Any) -> list[dict[str, Any]] | None:
    """The hunk spans a findings file recorded for one chunk, or None when it
    recorded none or they are not well formed (a file from an earlier release)."""
    if not isinstance(value, list):
        return None
    spans: list[dict[str, Any]] = []
    for span in value:
        if not isinstance(span, Mapping) or not isinstance(span.get("file"), str):
            return None
        if not all(
            isinstance(span.get(key), int) and not isinstance(span.get(key), bool)
            for key in _SPAN_KEYS
        ):
            return None
        spans.append({"file": span["file"], **{key: span[key] for key in _SPAN_KEYS}})
    return spans


def chunk_boundaries(chunks: Any) -> list[dict[str, Any]] | None:
    """The per-chunk boundaries a findings file's ``chunks`` array supports: index,
    files and hunk spans. None when the file has no chunk record; an entry whose
    spans were not recorded carries ``hunks: null``."""
    if not isinstance(chunks, list):
        return None
    boundaries: list[dict[str, Any]] = []
    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        files = chunk.get("files")
        boundaries.append(
            {
                "index": chunk.get("index"),
                "files": [name for name in files if isinstance(name, str)]
                if isinstance(files, list)
                else [],
                "hunks": _spans_of(chunk.get("hunks")),
            }
        )
    return boundaries


def build_review_record(
    *,
    comment_id: Any,
    repo: str,
    pr: int,
    head_sha: str,
    role: str,
    policy_base_sha: str | None,
    spec_hash: str | None,
    engines: Sequence[Mapping[str, Any]] | None,
    evaluated: Sequence[Mapping[str, Any]],
    open_findings: Sequence[Mapping[str, Any]],
    advisory: Sequence[Mapping[str, Any]],
    dropped: Sequence[Mapping[str, Any]] | None,
    chunks: Any,
) -> dict[str, Any]:
    """The record for one verified comment. Raises StoreError when *comment_id*
    is not a decimal id."""
    comment = str(comment_id)
    if COMMENT_ID_RE.match(comment) is None:
        raise StoreError(f"{comment!r} is not a comment id (expected decimal digits)")
    return {
        "schema_version": SCHEMA_VERSION,
        "comment_id": comment,
        "repo": repo,
        "pr": pr,
        "head_sha": head_sha,
        "role": role,
        "policy_base_sha": policy_base_sha,
        "spec_hash": spec_hash,
        "engines": [dict(entry) for entry in engines or ()],
        "recorded_at": utc_timestamp(),
        "findings": {
            "evaluated": [dict(item) for item in evaluated],
            "open": [dict(item) for item in open_findings],
            "advisory": [dict(item) for item in advisory],
        },
        "dropped": [dict(item) for item in dropped or ()],
        "chunks": chunk_boundaries(chunks),
    }


def write_review_record(
    root: Path, record: Mapping[str, Any], *, untrusted_roots: Sequence[Path] = ()
) -> Path:
    """Store *record* under its head and comment id and return its path. Raises
    StoreError when the root is a refused location or the file cannot be written."""
    payload = json.dumps(record, indent=2, sort_keys=True).encode("utf-8")
    return write_into_head_dir(
        root,
        record["head_sha"],
        f"{record['comment_id']}.json",
        payload,
        kind=_REVIEW_RECORDS_KIND,
        description=f"the review record for comment {record['comment_id']}",
        untrusted_roots=untrusted_roots,
    )


def _read_file(path: Path, filed_under: str, comment_id: str) -> dict[str, Any]:
    try:
        if path.is_symlink():
            raise ReviewRecordInvalidError(f"{path.name}: is a symlink")
        if path.stat().st_size > _MAX_RECORD_BYTES:
            raise ReviewRecordInvalidError(f"{path.name}: is larger than {_MAX_RECORD_BYTES} bytes")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewRecordInvalidError(f"{path.name}: cannot be read: {exc}") from exc
    if not isinstance(data, dict):
        raise ReviewRecordInvalidError(f"{path.name}: is not a JSON object")
    if data.get("comment_id") != comment_id:
        raise ReviewRecordInvalidError(
            f"{path.name}: names comment {data.get('comment_id')!r}, not the id in its file name"
        )
    if data.get("head_sha") != filed_under:
        raise ReviewRecordInvalidError(
            f"{path.name}: is filed under {filed_under} but records head {data.get('head_sha')!r}"
        )
    _validate_fields(data, path.name)
    return data


def _is_type(value: Any, allowed: tuple[type, ...]) -> bool:
    """isinstance that does not let a bool stand in for an int."""
    if isinstance(value, bool) and bool not in allowed:
        return False
    return isinstance(value, allowed)


def _validate_fields(data: Mapping[str, Any], name: str) -> None:
    """Raise ReviewRecordInvalidError naming the first required field that is
    missing or of the wrong type, using the one field table above."""
    for field, allowed in _FIELD_TYPES.items():
        if field not in data:
            raise ReviewRecordInvalidError(f"{name}: is missing the required field {field!r}")
        if not _is_type(data[field], allowed):
            raise ReviewRecordInvalidError(
                f"{name}: field {field!r} is a {type(data[field]).__name__}, "
                f"expected {' or '.join(t.__name__ for t in allowed)}"
            )
    findings = data["findings"]
    for key in _FINDINGS_KEYS:
        if not isinstance(findings.get(key), list):
            raise ReviewRecordInvalidError(
                f"{name}: field 'findings' needs a list under {key!r}"
            )
    for position, chunk in enumerate(data["chunks"] or ()):
        _validate_chunk(chunk, position, name)


def _is_span(span: Any) -> bool:
    """One hunk span in the shape ``chunk_boundaries`` writes."""
    return (
        isinstance(span, dict)
        and isinstance(span.get("file"), str)
        and all(_is_type(span.get(key), (int,)) for key in _SPAN_KEYS)
    )


def _validate_chunk(chunk: Any, position: int, name: str) -> None:
    """Raise ReviewRecordInvalidError naming the chunk field that is wrong."""
    where = f"{name}: field 'chunks' entry {position}"
    if not isinstance(chunk, dict):
        raise ReviewRecordInvalidError(f"{where} is not an object")
    files = chunk.get("files")
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise ReviewRecordInvalidError(f"{where} needs 'files' as a list of strings")
    hunks = chunk.get("hunks")
    if hunks is not None and (not isinstance(hunks, list) or not all(_is_span(s) for s in hunks)):
        raise ReviewRecordInvalidError(
            f"{where} needs 'hunks' as null or a list of spans "
            f"(file, {', '.join(_SPAN_KEYS)})"
        )


def load_review_records(
    root: Path,
    comment_id: str,
    *,
    head: str | None = None,
    untrusted_roots: Sequence[Path] = (),
) -> list[dict[str, Any]]:
    """Every record that holds *comment_id*, newest ``recorded_at`` first (ties
    break on the head sha, so the order is stable). A comment id can be reused
    across a head change, so more than one head may hold it. With *head*, only
    that head's record, or none. *comment_id* is untrusted: it must be decimal
    digits before it touches a path. Raises ReviewRecordNotFoundError when no
    record matches, ReviewRecordInvalidError when a file found is not a valid
    record and RootUnreadableError when the root cannot be listed."""
    if COMMENT_ID_RE.match(comment_id) is None:
        raise StoreError(f"{comment_id!r} is not a comment id (expected decimal digits)")
    refuse_untrusted(root, untrusted_roots, _REVIEW_RECORDS_KIND)
    found: list[dict[str, Any]] = []
    for directory in list_head_dirs(root, _REVIEW_RECORDS_KIND):
        if head is not None and directory.name != head:
            continue
        candidate = directory / f"{comment_id}.json"
        if candidate.is_file() or candidate.is_symlink():
            found.append(_read_file(candidate, directory.name, comment_id))
    if not found:
        where = f" at head {head}" if head is not None else ""
        raise ReviewRecordNotFoundError(
            f"no review record for comment {comment_id}{where} under {str(root)!r}"
        )
    found.sort(key=lambda record: (record["recorded_at"], record["head_sha"]), reverse=True)
    return found


def load_review_record(
    root: Path,
    comment_id: str,
    *,
    head: str | None = None,
    untrusted_roots: Sequence[Path] = (),
) -> dict[str, Any]:
    """The newest record for *comment_id* (see load_review_records), or the one
    at *head* when given."""
    return load_review_records(root, comment_id, head=head, untrusted_roots=untrusted_roots)[0]


__all__ = [
    "COMMENT_ID_RE",
    "CONFIG_KEY_DIR",
    "CONFIG_SECTION",
    "REVIEW_RECORDS_DIR_ENV_VAR",
    "SCHEMA_VERSION",
    "ReviewRecordInvalidError",
    "ReviewRecordNotFoundError",
    "RootUnreadableError",
    "StoreError",
    "build_review_record",
    "chunk_boundaries",
    "load_review_record",
    "load_review_records",
    "review_records_root",
    "write_review_record",
]
