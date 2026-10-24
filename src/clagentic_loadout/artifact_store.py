"""artifact_store — the head-keyed file store primitives shared by every store
that keeps one file per measured or sealed artifact under a commit.

A store is a root directory holding one directory per full commit sha, each
holding the artifacts that belong to that commit::

    <root>/<head sha>/<file name>

What lives here is the part every such store must do the same way: resolve the
root (environment variable, then the user-level config section, then a default
under the user config root), refuse a root inside a repository the caller names
as untrusted, and write a file atomically with the permission rule below. What
a file is called and what it holds belong to the store that uses this module
(verdict_record.store for verdict records, spec.store for sealed specs).

A root this module creates is mode 0755, so only its creator can write: that is
single-user by design. A head directory created under an existing root takes that
root's permission bits, setgid included, so a multi-user deployment pre-creates
the root once with a shared group (for example mode 2775) and every identity in
the group can write.

TRUST. The pull request under review must not be able to write its own
measurements or its own specification. A root inside a repository the caller
names as untrusted (the checkout the pull request lives in) is refused on write
and on read. Code that runs as the same user as the reader can still write to
the root; the optional pre-checks sandbox (``merge.pre_checks_sandbox``) is the
control for that.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.transport import provider_config
from clagentic_loadout.verdict_record.record import is_full_sha

FILE_MODE = 0o644
DIR_MODE = 0o755


class StoreError(Exception):
    """A store root could not be resolved or used, or a file could not be read.
    The message names the path or id and what was found."""


class RootUnreadableError(StoreError):
    """The store root exists but cannot be listed."""


@dataclass(frozen=True)
class StoreKind:
    """How one store names itself: *label* is the plural noun used in messages
    ("verdict records", "specs"); the root comes from *env_var*, then the
    user-level config *section*.*key*, then ``<user config root>/state/<default_subdir>``."""

    label: str
    env_var: str
    section: str
    key: str
    default_subdir: str


def resolve_root(
    kind: StoreKind,
    env: Mapping[str, str] | None = None,
    *,
    config_root: str | Path | None = None,
) -> Path:
    """The store root for *kind*. It is not created here."""
    environ = os.environ if env is None else env
    configured = environ.get(kind.env_var, "").strip()
    if configured:
        return Path(configured).expanduser()
    section = provider_config.load_user_config_section(kind.section, config_root=config_root)
    from_config = section.get(kind.key)
    if isinstance(from_config, str) and from_config.strip():
        return Path(from_config.strip()).expanduser()
    root = Path(config_root) if config_root is not None else provider_config.DEFAULT_USER_CONFIG_ROOT
    return root / "state" / kind.default_subdir


def refuse_untrusted(root: Path, untrusted_roots: Sequence[Path], kind: StoreKind) -> None:
    """Raise StoreError when *root* is, or is inside, one of *untrusted_roots*."""
    resolved = root.resolve()
    for untrusted in untrusted_roots:
        base = Path(untrusted).resolve()
        if resolved == base or base in resolved.parents:
            raise StoreError(
                f"the {kind.label} root {str(root)!r} is inside {str(base)!r}, a repository "
                f"the pull request under review can write; set {kind.env_var} or "
                f"{kind.section}.{kind.key} to a location outside it"
            )


def head_dir(root: Path, head_sha: str) -> Path:
    """The directory for *head_sha* under *root*. Raises StoreError when
    *head_sha* is not a full commit sha."""
    if not is_full_sha(head_sha):
        raise StoreError(f"{head_sha!r} is not a full commit sha (40 or 64 lowercase hex digits)")
    return root / head_sha


def write_into_head_dir(
    root: Path,
    head_sha: str,
    file_name: str,
    payload: bytes,
    *,
    kind: StoreKind,
    description: str,
    untrusted_roots: Sequence[Path] = (),
) -> Path:
    """Store *payload* as *file_name* under the directory of *head_sha* and
    return its path. *description* names the thing written in the failure
    message. Raises StoreError when the root is a refused location or the file
    cannot be written."""
    # Imported here, not at module level: the review package imports the review
    # verb, which writes records, so a module-level import would make importing
    # this store first (for example from the push verb) a circular import.
    from clagentic_loadout.review.atomic_io import write_bytes_atomic

    refuse_untrusted(root, untrusted_roots, kind)
    directory = head_dir(root, head_sha)
    try:
        if directory.is_symlink():
            raise StoreError(f"{str(directory)!r} is a symlink, refusing to write through it")
        # A root this call creates is made traversable for every user (the umask
        # could otherwise narrow it) and is single-user by design. A head
        # directory takes the permission bits of the root it is created under,
        # setgid included, so a root an administrator pre-created with a shared
        # group stays writable to every identity in that group.
        if not root.exists():
            root.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
            os.chmod(root, DIR_MODE)
        if not directory.exists():
            head_mode = stat.S_IMODE(root.stat().st_mode)
            directory.mkdir(mode=head_mode)
            os.chmod(directory, head_mode)
        target = directory / file_name
        write_bytes_atomic(target, payload, mode=FILE_MODE)
        os.chmod(target, FILE_MODE)
    except OSError as exc:
        raise StoreError(f"cannot write {description} under {str(root)!r}: {exc}") from exc
    return target


def list_head_dirs(root: Path, kind: StoreKind) -> list[Path]:
    """Every head directory under *root*. A root that does not exist holds
    none. Raises RootUnreadableError when the root cannot be listed."""
    try:
        return [
            entry
            for entry in root.iterdir()
            if is_full_sha(entry.name) and entry.is_dir() and not entry.is_symlink()
        ]
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RootUnreadableError(f"cannot read the {kind.label} root {str(root)!r}: {exc}") from exc


__all__ = [
    "DIR_MODE",
    "FILE_MODE",
    "RootUnreadableError",
    "StoreError",
    "StoreKind",
    "head_dir",
    "list_head_dirs",
    "refuse_untrusted",
    "resolve_root",
    "write_into_head_dir",
]
