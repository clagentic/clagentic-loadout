"""writer.py — the opt-in `--write` idempotent settings merge (lr-4e04).

Default behavior for the allowlist generator is PRINT (safe, copy-
pasteable — see cli.py). `--write` is an explicit opt-in that merges a
role's fragment into a harness settings JSON file IN PLACE:

  - existing `permissions.allow` entries are never duplicated, reordered,
    or removed — this role's new entries are appended only if not already
    present, and everything else in the file (including entries belonging
    to OTHER roles' fragments, previously merged) is left byte-for-byte
    untouched apart from that append;
  - a missing settings file is created with the minimal
    `{"permissions": {"allow": [...]}}` shape;
  - a settings file that already has unrelated top-level keys, or an
    unrelated `permissions.*` key alongside `allow`, keeps them all.

No sed/awk hand-editing of JSON — this reads the file with the stdlib
`json` module, mutates the in-memory structure, and rewrites the whole
file, which is the only way to guarantee "never duplicate/reorder/remove
existing entries" for a structured format (a line-oriented text edit cannot
make that guarantee against a file whose existing entries could be in any
order or formatting).

Write-path hardening (lr-3dfe, non-blocking pre-merge security-audit
finding on PR #27 — the target path is trusted CLI/env input here, not
attacker-controlled, so this is defense-in-depth, not an exploit fix).
Mirrors scripts/install.sh's own path-handling discipline for its
settings-adjacent config.yaml write (`_seed_git_host_config`):

  - symlink check: a symlink AT the target path is refused outright,
    before any read or write — never followed, never resolved-and-
    validated. install.sh's own precedent here is explicit that a symlink
    at a settings-shaped path is never expected in normal use, so refusing
    is not a usability regression.
  - atomic write: the merged document is written to a temp file in the
    SAME directory as the target (same filesystem, so os.replace() is an
    atomic rename, not a cross-filesystem copy) and swapped onto the
    target only after the write succeeds — a process failure mid-merge
    leaves the original file (or no file, if it did not exist yet)
    untouched rather than a partial/corrupt one.
  - explicit file mode: the written file is chmod'd 0600 (install.sh's own
    mode for its settings-adjacent config.yaml — this file can carry
    sensitive config, so it gets the tighter default rather than relying
    on the umask).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

PERMISSIONS_KEY = "permissions"
ALLOW_KEY = "allow"

#: File mode for a written settings file (lr-3dfe) — matches install.sh's
#: own mode discipline for its settings-adjacent config.yaml write
#: (`_seed_git_host_config`'s `chmod 600 "$_CONFIG_FILE"`). Settings files
#: can carry sensitive config, so this is the tighter default rather than
#: relying on the process umask.
SETTINGS_FILE_MODE = 0o600


class SettingsWriteError(ValueError):
    """Raised when the target settings file exists but is not a JSON
    object (or its `permissions`/`permissions.allow` keys are present but
    not the expected shape) — refuses rather than guessing how to merge
    into an incompatible structure. Also raised (lr-3dfe) when the target
    path is a symlink — refuses to read or write through it, mirroring
    install.sh's own symlink refusal for its settings-adjacent config.yaml
    write."""


def _refuse_if_symlink(path: Path) -> None:
    """Refuse outright if *path* is a symlink (lr-3dfe).

    `Path.is_symlink()` does NOT follow the link (unlike `.exists()`/
    `.is_file()`, which resolve through it to test whatever it points at)
    — this must run BEFORE any read or write, or a symlink pointing at a
    regular file would pass a naive existence check and have its target's
    contents read/replaced instead of being refused. No resolve-and-
    validate fallback is offered, matching install.sh's own precedent: a
    symlink at a settings-file path is never expected in normal use, so
    refusing is not a usability regression. The error reports the
    RESOLVED offending path (conformance rule 4 — never a stale guess).
    """
    if path.is_symlink():
        resolved = path.resolve()
        raise SettingsWriteError(
            f"{path} is a symlink (resolves to {resolved}) — refusing to "
            f"read or write through it. Remove the symlink and re-run, or "
            f"point --settings-file/CLAGENTIC_LOADOUT_SETTINGS_FILE at a "
            f"real path instead."
        )


def _load_existing(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsWriteError(f"{path}: could not be read as JSON: {exc}.") from exc
    if not isinstance(raw, dict):
        raise SettingsWriteError(
            f"{path}: top-level document must be a JSON object, got "
            f"{type(raw).__name__}."
        )
    return raw


def merge_fragment_into_settings(path: str | Path, fragment: list[str]) -> list[str]:
    """Idempotently merge *fragment* (a list of `Bash(...)` allowlist entry
    strings) into `<path>`'s `permissions.allow` array, creating the file
    (and any missing parent directories) if needed.

    Returns the fully resolved `permissions.allow` list after the merge
    (existing entries, in their original order, followed by any NEW
    fragment entries not already present — new entries are appended in
    *fragment*'s own order, not re-sorted, so a caller can tell exactly
    what this call added).

    Raises:
        SettingsWriteError: the file exists but is not a JSON object, or
            its `permissions`/`permissions.allow` keys exist but are not
            the expected dict/list shape; or the target path is a symlink
            (lr-3dfe — refused before any read or write).
    """
    target = Path(path)
    _refuse_if_symlink(target)
    doc = _load_existing(target)

    permissions = doc.setdefault(PERMISSIONS_KEY, {})
    if not isinstance(permissions, dict):
        raise SettingsWriteError(
            f"{target}: {PERMISSIONS_KEY!r} key exists but is not an "
            f"object, got {type(permissions).__name__}."
        )

    allow = permissions.setdefault(ALLOW_KEY, [])
    if not isinstance(allow, list):
        raise SettingsWriteError(
            f"{target}: {PERMISSIONS_KEY}.{ALLOW_KEY} key exists but is "
            f"not an array, got {type(allow).__name__}."
        )

    existing = set(allow)
    for entry in fragment:
        if entry not in existing:
            allow.append(entry)
            existing.add(entry)

    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, json.dumps(doc, indent=2) + "\n")
    return allow


def _atomic_write(target: Path, content: str) -> None:
    """Write *content* to *target* atomically (lr-3dfe): a temp file is
    created in *target*'s own directory (same filesystem, so the final
    swap is an atomic `os.replace()` rename, never a cross-filesystem
    copy), given the explicit SETTINGS_FILE_MODE, and swapped onto
    *target* only once the write has fully succeeded — a failure any time
    before the replace leaves the ORIGINAL file (or no file, if *target*
    did not exist yet) untouched, never a partial/corrupt one.

    The temp file is created with `tempfile.mkstemp` (not a predictable
    name) and its own mode is set explicitly rather than inherited from
    the umask, since `os.replace()` preserves the temp file's mode/inode
    onto the target path, not the target's pre-existing mode.
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(content)
        os.chmod(tmp_name, SETTINGS_FILE_MODE)
        os.replace(tmp_name, target)
    except BaseException:
        # Best-effort cleanup of the temp file on any failure (including
        # the write itself, os.chmod, or os.replace) -- the original
        # target is never touched before os.replace succeeds, so no
        # cleanup is needed there.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


__all__ = [
    "ALLOW_KEY",
    "PERMISSIONS_KEY",
    "SettingsWriteError",
    "merge_fragment_into_settings",
]
