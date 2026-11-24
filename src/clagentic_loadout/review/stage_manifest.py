"""review.stage_manifest — the manifest `loadout-review stage` writes, and its reader.

The manifest is the one record of a stage: the pull request and the head it was
pinned at, the copies written (path and sha256 of the written bytes), the files
the pull request deleted and the changed files that have no copy, each with its
reason. `teardown` and `check-drops` take it by path and nothing else, so a
manifest that is not one, or that has been moved from the directory it recorded,
is refused by this module's reader before either acts on it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from clagentic_loadout.sha import FULL_SHA_RE

SCHEMA_ID = "loadout-review-stage/1"
#: Written inside the stage directory. A changed file of this exact path cannot
#: be staged, because its copy would be the manifest's own path.
MANIFEST_NAME = ".loadout-review-stage.json"

KEY_SCHEMA = "schema"
KEY_STAGING_GAP = "staging_gap"


class ManifestError(Exception):
    """The manifest cannot be used; the message names the path and the reason."""


@dataclass(frozen=True)
class Manifest:
    path: Path
    directory: Path
    repo: str
    pr: int
    platform: str
    head_sha: str
    base_sha: str
    files: dict[str, str]
    deleted: dict[str, str]
    missing: dict[str, str]
    staging_gap: dict[str, Any] | None


def unsafe_path_reason(path: str) -> str | None:
    """Why a repository path cannot be written under a directory, or None when it
    can: empty, absolute (either convention), a drive or UNC path, a backslash, a
    NUL, or any '..' segment."""
    if not path:
        return "empty path"
    if "\x00" in path:
        return "path holds a NUL character"
    if "\\" in path:
        return "path holds a backslash"
    if path.startswith("/") or PurePosixPath(path).is_absolute() or PureWindowsPath(path).drive:
        return "absolute path would resolve outside the output directory"
    if ".." in PurePosixPath(path).parts:
        return "path with a '..' segment would resolve outside the output directory"
    return None


def _entries(data: dict[str, Any], key: str, where: str, *, keys: tuple[str, str]) -> dict[str, str]:
    value = data.get(key)
    if not isinstance(value, list):
        raise ManifestError(f"{where}: {key!r} must be an array")
    out: dict[str, str] = {}
    for entry in value:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get(keys[0]), str)
            or not isinstance(entry.get(keys[1]), str)
        ):
            raise ManifestError(f"{where}: every {key!r} entry needs string {keys[0]!r} and {keys[1]!r}")
        out[entry[keys[0]]] = entry[keys[1]]
    return out


def read_manifest(raw_path: str, *, require_own_directory: bool) -> Manifest:
    """Read and validate the manifest at *raw_path*. With *require_own_directory*
    the directory the manifest records must be the directory that holds it, which
    is what lets teardown remove that directory and nothing else."""
    path = Path(raw_path).expanduser()
    try:
        resolved = path.resolve()
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"cannot read the manifest {str(path)!r}: {exc}") from exc
    where = str(resolved)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"the manifest {where!r} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get(KEY_SCHEMA) != SCHEMA_ID:
        raise ManifestError(f"{where!r} is not a stage manifest (its {KEY_SCHEMA!r} must be {SCHEMA_ID!r})")
    recorded = data.get("dir")
    if not isinstance(recorded, str) or not recorded:
        raise ManifestError(f"the manifest {where!r} records no directory")
    directory = Path(recorded)
    if require_own_directory and (
        resolved.name != MANIFEST_NAME or directory.resolve() != resolved.parent
    ):
        raise ManifestError(
            f"the manifest {where!r} records the directory {recorded!r}, which is not the "
            f"directory that holds it ({str(resolved.parent)!r}); nothing was removed"
        )
    head = data.get("head_sha")
    if not isinstance(head, str) or not FULL_SHA_RE.match(head):
        raise ManifestError(f"the manifest {where!r} has no 40-hex head_sha")
    pr = data.get("pr")
    if isinstance(pr, bool) or not isinstance(pr, int):
        raise ManifestError(f"the manifest {where!r} has no integer pr")
    gap = data.get(KEY_STAGING_GAP)
    if gap is not None and not isinstance(gap, dict):
        raise ManifestError(f"the manifest {where!r} has a staging_gap that is not an object")
    return Manifest(
        path=resolved,
        directory=directory,
        repo=str(data.get("repo", "")),
        pr=pr,
        platform=str(data.get("platform", "")),
        head_sha=head,
        base_sha=str(data.get("base_sha", "")),
        files=_entries(data, "files", where, keys=("path", "sha256")),
        deleted=_entries(data, "deleted", where, keys=("path", "reason")),
        missing=_entries(data, "missing", where, keys=("path", "reason")),
        staging_gap=gap,
    )


__all__ = [
    "MANIFEST_NAME",
    "SCHEMA_ID",
    "Manifest",
    "ManifestError",
    "read_manifest",
    "unsafe_path_reason",
]
