"""review.head_stage — read-only copies of a pull request's head files, and their removal.

``stage`` pins the head the host API reports at the time of the call and writes,
under one new directory, the head version of every changed file that exists there,
with a manifest (review.stage_manifest) recording the sha256 of each written copy.
Everything is read through the host API: no checkout, worktree or fetch. A changed
file with no retrievable head content (binary, or a read that failed) or whose path
would resolve outside the directory is not written and is listed under ``missing``
with its reason; a file the pull request removes is listed under ``deleted``. The
head is never a caller's claim, so there is nothing for it to disagree with.

``teardown`` removes what a stage wrote, named by its manifest, and nothing else.

Neither refuses on what it finds: a count the host states that the stage does not
account for, or a stage with no copies, is reported as ``staging_gap``.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from clagentic_loadout.acquire.contract import AcquiredPr, PolicyFileBackend
from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.review.stage_manifest import (
    KEY_SCHEMA,
    KEY_STAGING_GAP,
    MANIFEST_NAME,
    SCHEMA_ID,
    Manifest,
    unsafe_path_reason,
)
from clagentic_loadout.sha import FULL_SHA_RE

#: A removed file has no head version; both hosts name that state differently.
_REMOVED_STATUSES = frozenset({"removed", "deleted"})
_REASON_DELETED = "removed by the pull request"
_REASON_BINARY = "binary: the head content is not UTF-8 text"
_REASON_RESERVED = "path is the stage manifest's own name"
_MAX_REASON_CHARS = 300


class StageRefusedError(Exception):
    """The output path is not usable; nothing was written."""


class StageHeadError(Exception):
    """The host gave no usable head for the pull request."""


def check_out_dir(raw: str) -> Path:
    """The resolved output directory when it does not exist or is an empty
    directory; otherwise StageRefusedError naming the resolved path. An existing
    non-empty directory or a file is never replaced."""
    resolved = Path(raw).expanduser().resolve()
    if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
        raise StageRefusedError(
            f"the output path {str(resolved)!r} exists and is not an empty directory; "
            "nothing was written (an existing directory is never replaced; pass a new --out)"
        )
    return resolved


def _one_line(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:_MAX_REASON_CHARS]


def _write_copy(directory: Path, path: str, data: bytes) -> None:
    target = directory / path
    if directory not in target.resolve().parents:
        raise ValueError("resolves outside the output directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def _staging_gap(acquired: AcquiredPr, staged: int, accounted: int) -> dict[str, Any] | None:
    reasons = []
    host = acquired.changed_file_count
    if host is not None and host != accounted:
        reasons.append(
            f"the host reports {host} changed file(s) but the stage accounts for {accounted} "
            "(the host's file list is paged or capped, or the pull request moved)"
        )
    if staged == 0:
        reasons.append("no file copy was staged")
    if not reasons:
        return None
    return {"reason": "; ".join(reasons), "staged": staged, "accounted": accounted, "host": host}


def stage_head(
    backend: PolicyFileBackend,
    acquired: AcquiredPr,
    *,
    directory: Path,
    repo: str,
    platform: str,
) -> tuple[Path, dict[str, Any]]:
    """Write the head copies of *acquired*'s changed files under *directory* (a
    path from check_out_dir) and the manifest; return the manifest path and the
    manifest object. *repo* is recorded as the caller gave it."""
    if not FULL_SHA_RE.match(acquired.head_sha):
        raise StageHeadError(
            f"the host reported the head {acquired.head_sha!r} for {repo}#{acquired.pr_number}, "
            "which is not a full 40-hex SHA; nothing was staged"
        )
    directory.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, str]] = []
    deleted: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    for changed in acquired.changed_files:
        path = changed.filename
        if changed.status in _REMOVED_STATUSES:
            deleted.append({"path": path, "reason": _REASON_DELETED})
            continue
        unsafe = unsafe_path_reason(path)
        if unsafe is not None:
            missing.append({"path": path, "reason": unsafe})
            continue
        if path == MANIFEST_NAME:
            missing.append({"path": path, "reason": _REASON_RESERVED})
            continue
        try:
            data = backend.fetch_file_bytes(
                owner=acquired.owner, repo=acquired.repo, path=path, ref=acquired.head_sha
            )
        except AcquireFetchError as exc:
            missing.append({"path": path, "reason": f"unreadable: {_one_line(str(exc))}"})
            continue
        if b"\x00" in data or _not_utf8(data):
            missing.append({"path": path, "reason": _REASON_BINARY})
            continue
        try:
            _write_copy(directory, path, data)
        except (OSError, ValueError) as exc:
            missing.append({"path": path, "reason": f"not written: {_one_line(str(exc))}"})
            continue
        files.append({"path": path, "sha256": hashlib.sha256(data).hexdigest()})

    manifest: dict[str, Any] = {
        KEY_SCHEMA: SCHEMA_ID,
        "repo": repo,
        "pr": acquired.pr_number,
        "platform": platform,
        "head_sha": acquired.head_sha,
        "base_sha": acquired.base_sha,
        "dir": str(directory),
        "files": files,
        "deleted": deleted,
        "missing": missing,
    }
    gap = _staging_gap(acquired, len(files), len(files) + len(deleted) + len(missing))
    if gap is not None:
        manifest[KEY_STAGING_GAP] = gap
    manifest_path = directory / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path, manifest


def _not_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def teardown_stage(manifest: Manifest) -> list[str]:
    """Remove the copies the manifest lists, the manifest, and the directories
    that are then empty, up to and including the manifest's directory. Returns
    what is left in the directory (empty when it is gone): only files the manifest
    does not name, and listed paths whose parent no longer resolves inside the
    directory (a component swapped for a symlink after staging), are left, and
    they are never touched."""
    directory = manifest.directory.resolve()
    skipped: set[str] = set()
    for path in manifest.files:
        if unsafe_path_reason(path) is not None:
            continue
        target = directory / path
        parent = target.parent.resolve()
        if parent != directory and directory not in parent.parents:
            skipped.add(path)
            continue
        try:
            target.unlink(missing_ok=True)
        except OSError:
            continue
    manifest.path.unlink(missing_ok=True)
    for root, _dirs, _names in os.walk(directory, topdown=False):
        try:
            Path(root).rmdir()
        except OSError:
            continue
    if not directory.exists():
        return []
    left = {str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file()}
    return sorted(left | skipped)


__all__ = [
    "StageHeadError",
    "StageRefusedError",
    "check_out_dir",
    "stage_head",
    "teardown_stage",
]
