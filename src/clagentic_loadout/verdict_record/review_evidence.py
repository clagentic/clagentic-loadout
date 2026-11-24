"""verdict_record.review_evidence — the files a review kept, filed by comment id.

A reviewer's working files (a findings file, the records it judged from, a scanner
result) are the evidence behind a posted comment, and the comment alone cannot show
them. They are kept in the shared head-keyed store (artifact_store), one directory
per verified comment::

    <review evidence root>/<head sha>/<comment id>/<file name>

The root is, in order: the ``CLAGENTIC_LOADOUT_REVIEW_EVIDENCE_DIR`` environment
variable, the user-level ``review_evidence.dir`` setting, then
``<user config root>/state/review-evidence``; the same resolution, permission rule
and atomic writer as the review records store.

Files are copied byte for byte. Keeping evidence for a comment that already has
some replaces the whole earlier copy, never merges into it: the new copy is built
beside the old one and swapped in, so a failure at any step leaves the earlier copy
as it was.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clagentic_loadout.artifact_store import (
    FILE_MODE,
    StoreError,
    StoreKind,
    ensure_head_dir,
    resolve_root,
)
from clagentic_loadout.verdict_record.review_record import COMMENT_ID_RE

REVIEW_EVIDENCE_DIR_ENV_VAR = "CLAGENTIC_LOADOUT_REVIEW_EVIDENCE_DIR"
CONFIG_SECTION = "review_evidence"
CONFIG_KEY_DIR = "dir"

_REVIEW_EVIDENCE_KIND = StoreKind(
    label="review evidence",
    env_var=REVIEW_EVIDENCE_DIR_ENV_VAR,
    section=CONFIG_SECTION,
    key=CONFIG_KEY_DIR,
    default_subdir="review-evidence",
)


def review_evidence_root(
    env: Mapping[str, str] | None = None, *, config_root: str | Path | None = None
) -> Path:
    """The review evidence root. It is not created here."""
    return resolve_root(_REVIEW_EVIDENCE_KIND, env, config_root=config_root)


def describe_files(files: Mapping[str, bytes]) -> list[dict[str, Any]]:
    """``name``, ``bytes`` and ``sha256`` of each file, in the order given."""
    return [
        {"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in files.items()
    ]


def keep_files(root: Path, head_sha: str, comment_id: str, files: Mapping[str, bytes]) -> Path:
    """Store *files* (file name to content) as the evidence of *comment_id* at
    *head_sha* and return the directory. An earlier copy for the same comment is
    replaced as a whole; it survives any failure. Raises StoreError when
    *comment_id* is not decimal digits, *head_sha* is not a full sha, a name is
    not a plain file name, or the copy cannot be written."""
    # Imported here, not at module level, for the reason artifact_store gives.
    from clagentic_loadout.review.atomic_io import write_bytes_atomic

    if COMMENT_ID_RE.match(comment_id) is None:
        raise StoreError(f"{comment_id!r} is not a comment id (expected decimal digits)")
    for name in files:
        if name in ("", ".", "..") or "/" in name or "\x00" in name:
            raise StoreError(f"{name!r} is not a plain file name")
    try:
        head = ensure_head_dir(root, head_sha)
        final = head / comment_id
        if final.is_symlink() or (final.exists() and not final.is_dir()):
            raise StoreError(f"{str(final)!r} is not a directory, refusing to replace it")
        staged = head / f".{comment_id}.{uuid.uuid4().hex}.new"
        mode = stat.S_IMODE(head.stat().st_mode)
        staged.mkdir(mode=mode)
        try:
            os.chmod(staged, mode)
            for name, data in files.items():
                target = staged / name
                write_bytes_atomic(target, data, mode=FILE_MODE)
                os.chmod(target, FILE_MODE)
            _swap_in(staged, final, head, comment_id)
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise
    except OSError as exc:
        raise StoreError(
            f"cannot write review evidence for comment {comment_id} under {str(root)!r} "
            f"(errno {exc.errno}): {exc}"
        ) from exc
    return final


def _swap_in(staged: Path, final: Path, head: Path, comment_id: str) -> None:
    if not final.exists():
        os.rename(staged, final)
        return
    earlier = head / f".{comment_id}.{uuid.uuid4().hex}.old"
    os.rename(final, earlier)
    try:
        os.rename(staged, final)
    except BaseException:
        os.rename(earlier, final)
        raise
    shutil.rmtree(earlier, ignore_errors=True)


__all__ = [
    "CONFIG_KEY_DIR",
    "CONFIG_SECTION",
    "REVIEW_EVIDENCE_DIR_ENV_VAR",
    "describe_files",
    "keep_files",
    "review_evidence_root",
]
