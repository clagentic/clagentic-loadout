"""spec.store — where sealed specs are kept, and how they are read back.

A sealed spec is one file, named by the sha256 of its canonical bytes, in a
directory named by the full sha of the commit it was sealed at::

    <specs root>/<head sha>/<spec hash>.json

The store is the shared head-keyed store (artifact_store): the same atomic
writer, the same permission rule for the root and the head directories, and the
same refusal of a root inside the repository under review as the verdict records
store. The specs root is, in order: the ``CLAGENTIC_LOADOUT_SPECS_DIR``
environment variable, the user-level ``specs.dir`` setting, then
``<user config root>/state/specs``. A spawner that filters the child environment
must pass the variable through, or give each identity the same user config root;
otherwise each identity resolves a different default root and a spec sealed by
one is not found by another.

Nothing here edits a stored file. Writing the bytes that are already stored is a
no-op, and a different file under an existing name is an error. A read recomputes
the hash and refuses a file that does not match its name.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
from clagentic_loadout.spec.model import (
    HASH_RE,
    SealedSpec,
    SpecFormatError,
    parse_sealed,
)

SPECS_DIR_ENV_VAR = "CLAGENTIC_LOADOUT_SPECS_DIR"
CONFIG_SECTION = "specs"
CONFIG_KEY_DIR = "dir"

_SPECS_KIND = StoreKind(
    label="specs",
    env_var=SPECS_DIR_ENV_VAR,
    section=CONFIG_SECTION,
    key=CONFIG_KEY_DIR,
    default_subdir="specs",
)

_MAX_SPEC_BYTES = 1024 * 1024

#: The longest chain `chain` follows; a longer one is reported, not walked.
MAX_CHAIN = 1000


class SpecNotFoundError(StoreError):
    """No sealed spec has the hash asked for."""


class SpecInvalidError(StoreError):
    """A file stored under a spec hash is not that sealed spec."""


@dataclass(frozen=True)
class StoredSpec:
    """A sealed spec read back from the store, verified against its name."""

    spec_hash: str
    doc: dict[str, Any]
    data: bytes
    path: Path


def specs_root(
    env: Mapping[str, str] | None = None, *, config_root: str | Path | None = None
) -> Path:
    """The specs root. It is not created here."""
    return resolve_root(_SPECS_KIND, env, config_root=config_root)


def write_sealed(
    root: Path, sealed: SealedSpec, *, untrusted_roots: Sequence[Path] = ()
) -> Path:
    """Store *sealed* and return its path. Storing a spec that is already stored
    changes nothing. Raises StoreError when the root is a refused location, the
    file cannot be written, or a different file already has this name."""
    head = sealed.doc["head_sha"]
    name = f"{sealed.spec_hash}.json"
    refuse_untrusted(root, untrusted_roots, _SPECS_KIND)
    existing = root / head / name
    if existing.is_symlink():
        raise StoreError(f"{str(existing)!r} is a symlink, refusing to write through it")
    if existing.is_file():
        try:
            stored = existing.read_bytes()
        except OSError as exc:
            raise StoreError(f"cannot read the stored spec {str(existing)!r}: {exc}") from exc
        if stored != sealed.data:
            raise SpecInvalidError(
                f"{str(existing)!r} already exists and does not hold the spec it is named for"
            )
        return existing
    return write_into_head_dir(
        root,
        head,
        name,
        sealed.data,
        kind=_SPECS_KIND,
        description=f"the sealed spec {sealed.spec_hash}",
        untrusted_roots=untrusted_roots,
    )


def _read_stored(path: Path, spec_hash: str, filed_under: str) -> StoredSpec:
    try:
        if path.is_symlink():
            raise SpecInvalidError(f"{path.name}: is a symlink")
        if path.stat().st_size > _MAX_SPEC_BYTES:
            raise SpecInvalidError(f"{path.name}: is larger than {_MAX_SPEC_BYTES} bytes")
        data = path.read_bytes()
    except OSError as exc:
        raise SpecInvalidError(f"{path.name}: cannot be read: {exc}") from exc
    try:
        doc = parse_sealed(data, spec_hash)
    except SpecFormatError as exc:
        raise SpecInvalidError(f"{path.name}: {exc}") from exc
    if doc["head_sha"] != filed_under:
        raise SpecInvalidError(
            f"{path.name}: is filed under {filed_under} but was sealed at {doc['head_sha']}"
        )
    return StoredSpec(spec_hash=spec_hash, doc=doc, data=data, path=path)


def load_sealed(
    root: Path, spec_hash: str, *, untrusted_roots: Sequence[Path] = ()
) -> StoredSpec:
    """The sealed spec named *spec_hash*, with its hash verified. *spec_hash* is
    untrusted: it must be 64 lowercase hex digits before it touches a path.
    Raises SpecNotFoundError when no spec has the hash and SpecInvalidError when
    the file found is not that spec."""
    if HASH_RE.match(spec_hash) is None:
        raise StoreError(f"{spec_hash!r} is not a spec hash (expected 64 lowercase hex digits)")
    refuse_untrusted(root, untrusted_roots, _SPECS_KIND)
    for directory in list_head_dirs(root, _SPECS_KIND):
        candidate = directory / f"{spec_hash}.json"
        if candidate.is_file() or candidate.is_symlink():
            return _read_stored(candidate, spec_hash, directory.name)
    raise SpecNotFoundError(f"no sealed spec {spec_hash} under {str(root)!r}")


def chain(
    root: Path, spec_hash: str, *, untrusted_roots: Sequence[Path] = ()
) -> list[StoredSpec]:
    """The sealed spec *spec_hash* and every spec it supersedes, newest first,
    each verified. Raises SpecNotFoundError when a link of the chain is missing
    and StoreError when the chain is longer than MAX_CHAIN."""
    links: list[StoredSpec] = []
    current: str | None = spec_hash
    while current is not None:
        if len(links) >= MAX_CHAIN:
            raise StoreError(f"the chain of {spec_hash} is longer than {MAX_CHAIN} seals")
        link = load_sealed(root, current, untrusted_roots=untrusted_roots)
        links.append(link)
        current = link.doc["supersedes"]
    return links


__all__ = [
    "CONFIG_KEY_DIR",
    "CONFIG_SECTION",
    "MAX_CHAIN",
    "SPECS_DIR_ENV_VAR",
    "RootUnreadableError",
    "SpecInvalidError",
    "SpecNotFoundError",
    "StoreError",
    "StoredSpec",
    "chain",
    "load_sealed",
    "specs_root",
    "write_sealed",
]
