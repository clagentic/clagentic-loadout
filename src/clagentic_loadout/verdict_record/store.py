"""verdict_record.store — where verdict records are kept, and how they are read back.

One record is one file, named by its id, in a directory named by the full sha of
the commit it measured::

    <records root>/<head sha>/<record id>.json

A record is written atomically (review.atomic_io, the same writer the ruling
registry uses), so a reader never sees a half-written file, and is readable by
every user on the host: the identities that measure (a builder pushing, a
reviewer posting, the merge verb) are not one process or one user. The read
functions open nothing for write and create nothing.

The records root is, in order: the ``CLAGENTIC_LOADOUT_VERDICT_RECORDS_DIR``
environment variable, the user-level ``verdict_records.dir`` setting, then
``<user config root>/state/verdict-records``. An unconfigured deployment
therefore writes and reads records without any setup; a deployment whose
identities do not share one home directory points all of them at one shared
location with the variable or the setting.

TRUST. The pull request under review must not be able to write its own
measurements. A root inside a repository the caller names as untrusted (the
checkout the pull request lives in) is refused on write and on read. Code that
runs as the same user as the reader can still write to the root; that is the
same limit the ruling registry documents, and the optional pre-checks sandbox
(``merge.pre_checks_sandbox``) is the control for it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.transport import provider_config
from clagentic_loadout.verdict_record.record import (
    RECORD_ID_RE,
    RecordFormatError,
    VerdictRecord,
    is_full_sha,
    validate_record_dict,
)

RECORDS_DIR_ENV_VAR = "CLAGENTIC_LOADOUT_VERDICT_RECORDS_DIR"
CONFIG_SECTION = "verdict_records"
CONFIG_KEY_DIR = "dir"

_FILE_MODE = 0o644
_DIR_MODE = 0o755
_MAX_RECORD_BYTES = 64 * 1024

#: Files read when looking for the newest record of one gate across every head.
#: Bounds the cost of describing a miss; the answer is only ever a diagnostic.
DIAGNOSTIC_SCAN_LIMIT = 200


class StoreError(Exception):
    """The records root could not be resolved or used, or a record could not be
    read. The message names the path or id and what was found."""


class RecordNotFoundError(StoreError):
    """No record has the id asked for."""


@dataclass(frozen=True)
class HeadRecords:
    """What the store holds for one commit: the records that read cleanly and
    the reason for every file that did not."""

    records: tuple[VerdictRecord, ...] = ()
    skipped: tuple[str, ...] = ()


def records_root(
    env: Mapping[str, str] | None = None, *, config_root: str | Path | None = None
) -> Path:
    """The records root. It is not created here."""
    environ = os.environ if env is None else env
    configured = environ.get(RECORDS_DIR_ENV_VAR, "").strip()
    if configured:
        return Path(configured).expanduser()
    section = provider_config.load_user_config_section(CONFIG_SECTION, config_root=config_root)
    from_config = section.get(CONFIG_KEY_DIR)
    if isinstance(from_config, str) and from_config.strip():
        return Path(from_config.strip()).expanduser()
    root = Path(config_root) if config_root is not None else provider_config.DEFAULT_USER_CONFIG_ROOT
    return root / "state" / "verdict-records"


def _refuse_untrusted(root: Path, untrusted_roots: Sequence[Path]) -> None:
    resolved = root.resolve()
    for untrusted in untrusted_roots:
        base = Path(untrusted).resolve()
        if resolved == base or base in resolved.parents:
            raise StoreError(
                f"the verdict records root {str(root)!r} is inside {str(base)!r}, a repository "
                f"the pull request under review can write; set {RECORDS_DIR_ENV_VAR} or "
                f"{CONFIG_SECTION}.{CONFIG_KEY_DIR} to a location outside it"
            )


def _head_dir(root: Path, head_sha: str) -> Path:
    if not is_full_sha(head_sha):
        raise StoreError(f"{head_sha!r} is not a full commit sha (40 or 64 lowercase hex digits)")
    return root / head_sha


def write_record(
    root: Path, record: VerdictRecord, *, untrusted_roots: Sequence[Path] = ()
) -> Path:
    """Store *record* and return its path. Raises StoreError when the root is a
    refused location or the file cannot be written."""
    # Imported here, not at module level: the review package imports the review
    # verb, which writes records, so a module-level import would make importing
    # this store first (for example from the push verb) a circular import.
    from clagentic_loadout.review.atomic_io import write_bytes_atomic

    _refuse_untrusted(root, untrusted_roots)
    directory = _head_dir(root, record.head_sha)
    payload = json.dumps(record.as_dict(), indent=2, sort_keys=True).encode("utf-8")
    try:
        if directory.is_symlink():
            raise StoreError(f"{str(directory)!r} is a symlink, refusing to write through it")
        # Directories this call creates are made traversable for every user (the
        # umask could otherwise narrow them); one that already exists is left as
        # its owner set it.
        for created in (root, directory):
            if not created.exists():
                created.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
                os.chmod(created, _DIR_MODE)
        target = directory / f"{record.id}.json"
        write_bytes_atomic(target, payload, mode=_FILE_MODE)
        os.chmod(target, _FILE_MODE)
    except OSError as exc:
        raise StoreError(f"cannot write the verdict record {record.id} under {str(root)!r}: {exc}") from exc
    return target


def _read_file(path: Path, expected_head: str | None) -> VerdictRecord:
    """The record in *path*. Raises StoreError naming the file and the reason."""
    try:
        if path.is_symlink():
            raise StoreError(f"{path.name}: is a symlink")
        if path.stat().st_size > _MAX_RECORD_BYTES:
            raise StoreError(f"{path.name}: is larger than {_MAX_RECORD_BYTES} bytes")
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StoreError(f"{path.name}: cannot be read: {exc}") from exc
    try:
        record = validate_record_dict(data)
    except RecordFormatError as exc:
        raise StoreError(f"{path.name}: {exc}") from exc
    if path.stem != record.id:
        raise StoreError(f"{path.name}: names id {record.id!r}, not the id in its file name")
    if expected_head is not None and record.head_sha != expected_head:
        raise StoreError(
            f"{path.name}: is filed under {expected_head} but measured {record.head_sha}"
        )
    return record


def _head_dirs(root: Path) -> list[Path]:
    try:
        return [
            entry
            for entry in root.iterdir()
            if is_full_sha(entry.name) and entry.is_dir() and not entry.is_symlink()
        ]
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise StoreError(f"cannot read the verdict records root {str(root)!r}: {exc}") from exc


def records_at_head(
    root: Path, head_sha: str, *, untrusted_roots: Sequence[Path] = ()
) -> HeadRecords:
    """Every record filed under *head_sha*, oldest first. A file that does not
    read as a valid record for this head is reported in ``skipped`` and is never
    counted. A root that does not exist holds no records."""
    _refuse_untrusted(root, untrusted_roots)
    directory = _head_dir(root, head_sha)
    if directory.is_symlink() or not directory.is_dir():
        return HeadRecords()
    records: list[VerdictRecord] = []
    skipped: list[str] = []
    try:
        names = sorted(
            entry.name
            for entry in directory.iterdir()
            if entry.name.endswith(".json") and RECORD_ID_RE.match(entry.name[: -len(".json")])
        )
    except OSError as exc:
        raise StoreError(f"cannot read {str(directory)!r}: {exc}") from exc
    for name in names:
        try:
            records.append(_read_file(directory / name, head_sha))
        except StoreError as exc:
            skipped.append(str(exc))
    return HeadRecords(tuple(records), tuple(skipped))


def load_record(root: Path, record_id: str, *, untrusted_roots: Sequence[Path] = ()) -> VerdictRecord:
    """The record named *record_id*. *record_id* is untrusted: it must match the
    id grammar before it touches a path. Raises RecordNotFoundError when no
    record has the id and StoreError when the one found cannot be read."""
    if RECORD_ID_RE.match(record_id) is None:
        raise StoreError(
            f"{record_id!r} is not a verdict record id (expected YYYYMMDDTHHMMSSZ-<8 hex digits>)"
        )
    _refuse_untrusted(root, untrusted_roots)
    for directory in _head_dirs(root):
        candidate = directory / f"{record_id}.json"
        if candidate.is_file() or candidate.is_symlink():
            return _read_file(candidate, directory.name)
    raise RecordNotFoundError(f"no verdict record {record_id} under {str(root)!r}")


def newest_record_for_gate(
    root: Path,
    gate: str,
    *,
    repo: str | None = None,
    untrusted_roots: Sequence[Path] = (),
) -> VerdictRecord | None:
    """The most recently written record of *gate* at any head, for describing a
    miss ("the record found was for another commit"). A record that names a
    different repository than *repo* is passed over. Reads at most
    ``DIAGNOSTIC_SCAN_LIMIT`` files, newest first by id."""
    _refuse_untrusted(root, untrusted_roots)
    candidates: list[tuple[str, Path]] = []
    for directory in _head_dirs(root):
        try:
            for entry in directory.iterdir():
                stem = entry.name[: -len(".json")] if entry.name.endswith(".json") else ""
                if RECORD_ID_RE.match(stem):
                    candidates.append((stem, entry))
        except OSError:
            continue
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    for _stem, path in candidates[:DIAGNOSTIC_SCAN_LIMIT]:
        try:
            record = _read_file(path, path.parent.name)
        except StoreError:
            continue
        if record.gate != gate:
            continue
        if repo is not None and record.repo not in (None, repo):
            continue
        return record
    return None


__all__ = [
    "CONFIG_KEY_DIR",
    "CONFIG_SECTION",
    "DIAGNOSTIC_SCAN_LIMIT",
    "RECORDS_DIR_ENV_VAR",
    "HeadRecords",
    "RecordNotFoundError",
    "StoreError",
    "load_record",
    "newest_record_for_gate",
    "records_at_head",
    "records_root",
    "write_record",
]
