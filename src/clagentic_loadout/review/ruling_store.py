"""review.ruling_store — the write side of the standalone ruling registry.

One function, ``record_ruling``, adds a ruling to the registry file that
review.ruling_provider.FileRulingProvider reads. It applies the provider's trust
rules to the destination first, so a ruling is never written to a place the
reader would refuse, and it holds an exclusive lock for the whole
read-modify-write so two concurrent writers cannot lose an entry.

The file is rewritten atomically (review.atomic_io), readable by every user so
the reviewer and merger identities on the host can read what the lead role
wrote, and writable by its owner only.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import os
from collections.abc import Sequence
from pathlib import Path

import yaml

from clagentic_loadout.review.atomic_io import write_bytes_atomic
from clagentic_loadout.review.policy_reader import MAX_POLICY_BYTES
from clagentic_loadout.review.ruling_provider import untrusted_reason
from clagentic_loadout.review.rulings import (
    Ruling,
    RulingFormatError,
    compile_reference_pattern,
    parse_rulings,
    rulings_document,
    same_repo,
)

_FILE_MODE = 0o644
_DIR_MODE = 0o755
_LOCK_MODE = 0o600


class RulingStoreError(Exception):
    """The ruling could not be recorded; the message names the registry path."""


class UntrustedRegistryError(RulingStoreError):
    """The destination is a location the pull request under review could write."""


class DuplicateRulingError(RulingStoreError):
    """An active ruling already covers this finding at this pull request."""


def _same_finding(left: Ruling, right: Ruling) -> bool:
    return (
        same_repo(left.repo, right.repo)
        and left.pr == right.pr
        and left.fingerprint == right.fingerprint
        and left.rule_id == right.rule_id
        and left.path == right.path
    )


def record_ruling(
    path: Path,
    ruling: Ruling,
    *,
    today: dt.date,
    reference_pattern: str | None = None,
    untrusted_roots: Sequence[Path] = (),
) -> int:
    """Append *ruling* to the registry at *path* and return the number of
    rulings now stored. Raises UntrustedRegistryError for a destination the
    pull request could write, DuplicateRulingError when an unexpired ruling
    already covers the same finding, and RulingStoreError for anything else."""
    reason = untrusted_reason(path, untrusted_roots)
    if reason is not None:
        raise UntrustedRegistryError(
            f"refusing to write the rulings registry {path}: {reason}"
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        # Checked again once the directory exists: its permissions were not
        # readable before.
        reason = untrusted_reason(path, untrusted_roots)
        if reason is not None:
            raise UntrustedRegistryError(
                f"refusing to write the rulings registry {path}: {reason}"
            )
        lock_path = path.parent / f".{path.name}.lock"
        # A descriptor opened only to hold the lock: nothing is ever written to it.
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, _LOCK_MODE)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            existing = _read(path, reference_pattern)
            for other in existing:
                if _same_finding(other, ruling) and not other.is_expired(today):
                    raise DuplicateRulingError(
                        f"an unexpired ruling ({other.kind}, by {other.by}, {other.at.isoformat()}) "
                        f"already covers {ruling.path} rule {ruling.rule_id} at "
                        f"{ruling.repo}#{ruling.pr} in {path}"
                    )
            stored = [*existing, ruling]
            payload = yaml.safe_dump(
                rulings_document(stored), sort_keys=False, allow_unicode=True
            ).encode("utf-8")
            write_bytes_atomic(path, payload, mode=_FILE_MODE)
            os.chmod(path, _FILE_MODE)
        finally:
            os.close(lock_fd)
    except OSError as exc:
        raise RulingStoreError(f"cannot record the ruling in {path}: {exc}") from exc
    return len(stored)


def _read(path: Path, reference_pattern: str | None) -> tuple[Ruling, ...]:
    if not path.exists():
        return ()
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_POLICY_BYTES:
            raise RulingStoreError(f"{path} is larger than {MAX_POLICY_BYTES} bytes")
        return parse_rulings(
            yaml.safe_load(raw.decode("utf-8")),
            reference_pattern=compile_reference_pattern(reference_pattern),
        )
    except (UnicodeDecodeError, yaml.YAMLError, RulingFormatError) as exc:
        # Appending to a registry the readers would reject would hide the new
        # ruling behind the old fault, so the writer stops and names it.
        raise RulingStoreError(
            f"the existing registry {path} is unusable and was left unchanged: {exc}"
        ) from exc


__all__ = [
    "DuplicateRulingError",
    "RulingStoreError",
    "UntrustedRegistryError",
    "record_ruling",
]
