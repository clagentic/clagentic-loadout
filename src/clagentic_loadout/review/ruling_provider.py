"""review.ruling_provider — where the evaluator and the merge gate read rulings from.

THE SEAM. ``RulingProvider.rulings_for(repo, pr_number)`` is the one question the
evaluator asks: which rulings are on record for this pull request. A deployment
with its own authority service implements the protocol and is passed to the
evaluation context; loadout ships the protocol and a standalone fallback
(``FileRulingProvider``) and imports no service of its own.

TRUST. A ruling can lower a verdict, so its source must be one the pull request
under review can never write. The standalone provider reads a registry file
named by USER-LEVEL configuration (never the repository's own configuration,
which the PR can change) or by an environment variable, and refuses to use a
location the PR could reach:

  * a path inside a git work tree (a checkout the PR branch lives in);
  * a path inside the repository the evaluation was given;
  * a registry file, or any directory above it up to the root, that is writable
    by its group or by every user, or owned by someone other than this
    process's owner or root (a sticky shared directory such as a system
    temporary directory is allowed as an ancestor).

These checks run on every read and every write, whether or not the reviewed
repository is known. They do not stop code running as the same user as the
reader: a pre-merge check that executes pull-request code under that user can
write the registry unless the optional pre-checks sandbox is configured
(``merge.pre_checks_sandbox``; see docs/merge-authority.md).

A refused source applies no rulings and says why, which the evaluator reports
as a DEGRADED input: the only effect of "no rulings" is that nothing is demoted.

User-level configuration (``config.yaml`` under the loadout config root)::

    rulings:
      path: /var/lib/loadout/rulings.yaml   # absolute; omit to configure no registry
      reference_pattern: '^[A-Z]+-[0-9]+$'  # optional; work-item reference grammar

``CLAGENTIC_LOADOUT_RULINGS_FILE`` overrides ``path``, for hosts whose per-spawn
home directory hides the user-level file. With no path configured there is no
registry and behaviour is exactly as it is without this feature.

Which role may WRITE the registry is not configured here: the write verb
(review.ruling_verb) authorizes the roles that hold the ``ruling`` verb in the
effective role-to-verb mapping (review.ruling_authority).
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from clagentic_loadout.review.policy_reader import MAX_POLICY_BYTES
from clagentic_loadout.review.rulings import (
    Ruling,
    RulingFormatError,
    compile_reference_pattern,
    parse_rulings,
    rulings_for_scope,
)
from clagentic_loadout.transport.provider_config import load_user_config_section

CONFIG_SECTION_RULINGS = "rulings"
CONFIG_KEY_PATH = "path"
CONFIG_KEY_REFERENCE_PATTERN = "reference_pattern"
RULINGS_FILE_ENV_VAR = "CLAGENTIC_LOADOUT_RULINGS_FILE"

_WORLD_WRITABLE = stat.S_IWOTH
_GROUP_WRITABLE = stat.S_IWGRP


#: Starts every degraded reason this module reports, so a reader that wants only
#: the ruling-related reasons out of a verdict's degraded list can pick them out.
IGNORED_PREFIX = "rulings ignored"


class RulingsConfigError(ValueError):
    """The rulings configuration is present but unusable; the message names the
    key and the value it held."""


@dataclass(frozen=True)
class RulingScope:
    """The one repository and pull request an evaluation is for, and the
    directories the pull request under review can write (a registry inside one
    is refused)."""

    repo: str
    pr: int
    untrusted_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class RulingRead:
    """What a provider found for one scope: the rulings, and why any source was
    not used."""

    rulings: tuple[Ruling, ...] = ()
    degraded: tuple[str, ...] = ()


class RulingProvider(Protocol):
    def rulings_for(
        self, repo: str, pr_number: int, *, untrusted_roots: Sequence[Path] = ()
    ) -> RulingRead:
        """The rulings on record for *pr_number* in *repo*. *untrusted_roots*
        are directories the pull request under review can write; a source inside
        one must not be used."""
        ...


@dataclass(frozen=True)
class RulingsConfig:
    path: Path | None
    reference_pattern: str | None


def _section(config_root: str | Path | None) -> dict:
    return load_user_config_section(CONFIG_SECTION_RULINGS, config_root=config_root)


def load_rulings_config(
    config_root: str | Path | None = None, env: Mapping[str, str] | None = None
) -> RulingsConfig:
    """The registry location and reference grammar. Raises RulingsConfigError
    for a value that is present but unusable."""
    active_env = env if env is not None else os.environ
    section = _section(config_root)
    raw_path = (active_env.get(RULINGS_FILE_ENV_VAR) or "").strip() or section.get(CONFIG_KEY_PATH)
    path: Path | None = None
    if raw_path is not None:
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise RulingsConfigError(
                f"{CONFIG_SECTION_RULINGS}.{CONFIG_KEY_PATH} must be a non-empty path, "
                f"got {raw_path!r}"
            )
        path = Path(raw_path.strip())
        if not path.is_absolute():
            raise RulingsConfigError(
                f"the rulings registry path must be absolute (a relative path would resolve "
                f"against a directory the pull request may control), got {raw_path!r}"
            )
    pattern = section.get(CONFIG_KEY_REFERENCE_PATTERN)
    if pattern is not None:
        if not isinstance(pattern, str) or not pattern:
            raise RulingsConfigError(
                f"{CONFIG_SECTION_RULINGS}.{CONFIG_KEY_REFERENCE_PATTERN} must be a non-empty "
                f"regex string, got {pattern!r}"
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise RulingsConfigError(
                f"{CONFIG_SECTION_RULINGS}.{CONFIG_KEY_REFERENCE_PATTERN} is not a valid "
                f"regex ({pattern!r}): {exc}"
            ) from exc
    return RulingsConfig(path=path, reference_pattern=pattern)


def untrusted_reason(path: Path, untrusted_roots: Sequence[Path] = ()) -> str | None:
    """Why *path* is not a source the pull request cannot write, or None when it
    is acceptable. Judged on the resolved path, so a link does not hide a
    location."""
    resolved = path.resolve()
    for root in untrusted_roots:
        base = Path(root).resolve()
        if resolved == base or base in resolved.parents:
            return f"it is inside {base}, a repository the pull request under review can write"
    # Unconditional: holds whether or not the caller knows the reviewed repository.
    for parent in (resolved.parent, *resolved.parent.parents):
        if (parent / ".git").exists():
            return f"it is inside the git work tree at {parent}, which holds the pull request's branch"
    # The file and every directory above it: anyone who can replace one of them
    # can replace the registry. A path component that does not exist yet (a
    # first write) is skipped here and judged once it has been created.
    me = os.geteuid()
    for node in (resolved, *resolved.parents):
        label = "the file" if node == resolved else f"its directory {node}"
        try:
            info = node.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            return f"its permissions cannot be read: {exc}"
        if info.st_uid not in (0, me):
            return f"{label} is owned by user id {info.st_uid}, neither this process's owner nor root"
        if info.st_mode & _WORLD_WRITABLE and not _sticky_dir(info):
            return f"{label} is writable by every user on the host"
        if info.st_mode & _GROUP_WRITABLE and not _sticky_dir(info):
            return f"{label} is writable by the members of its group"
    return None


def _sticky_dir(info: os.stat_result) -> bool:
    """A shared directory with the sticky bit (a system temporary directory):
    other users can create entries there but cannot rename or remove ours."""
    return stat.S_ISDIR(info.st_mode) and bool(info.st_mode & stat.S_ISVTX)


class FileRulingProvider:
    """Standalone provider: a registry file on the host (see the module docstring
    for what makes its location acceptable)."""

    def __init__(self, path: Path, *, reference_pattern: str | None = None) -> None:
        self._path = Path(path)
        self._reference_pattern = reference_pattern

    @property
    def path(self) -> Path:
        return self._path

    def read_all(self, *, untrusted_roots: Sequence[Path] = ()) -> RulingRead:
        """Every ruling in the registry, whatever its scope."""
        reason = untrusted_reason(self._path, untrusted_roots)
        if reason is not None:
            return RulingRead(
                (),
                (
                    f"{IGNORED_PREFIX}: the registry {self._path} is not a trusted source: "
                    f"{reason}",
                ),
            )
        if not self._path.exists():
            return RulingRead()
        try:
            raw = self._path.read_bytes()
            if len(raw) > MAX_POLICY_BYTES:
                raise OSError(f"larger than {MAX_POLICY_BYTES} bytes")
            parsed = parse_rulings(
                yaml.safe_load(raw.decode("utf-8")),
                reference_pattern=compile_reference_pattern(self._reference_pattern),
            )
        except (OSError, UnicodeDecodeError, yaml.YAMLError, RulingFormatError) as exc:
            return RulingRead(
                (), (f"{IGNORED_PREFIX}: the registry {self._path} is unusable: {exc}",)
            )
        return RulingRead(parsed)

    def rulings_for(
        self, repo: str, pr_number: int, *, untrusted_roots: Sequence[Path] = ()
    ) -> RulingRead:
        everything = self.read_all(untrusted_roots=untrusted_roots)
        return RulingRead(rulings_for_scope(everything.rulings, repo, pr_number), everything.degraded)


class _UnavailableProvider:
    """Stands in for a registry whose configuration could not be used, so the
    evaluator reports the reason instead of silently applying nothing."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def rulings_for(
        self, repo: str, pr_number: int, *, untrusted_roots: Sequence[Path] = ()
    ) -> RulingRead:
        return RulingRead((), (f"{IGNORED_PREFIX}: {self._reason}",))


def resolve_ruling_provider(
    config_root: str | Path | None = None, env: Mapping[str, str] | None = None
) -> RulingProvider | None:
    """The configured provider, or None when no registry is configured."""
    try:
        config = load_rulings_config(config_root, env)
    except RulingsConfigError as exc:
        return _UnavailableProvider(str(exc))
    if config.path is None:
        return None
    return FileRulingProvider(config.path, reference_pattern=config.reference_pattern)


def read_scope_rulings(provider: RulingProvider | None, scope: RulingScope | None) -> RulingRead:
    """The rulings on record for *scope*. None are applied when no registry is
    configured or the scope is not known: a ruling that cannot be tied to one
    repository and pull request must not clear anything."""
    if provider is None or scope is None:
        return RulingRead()
    return provider.rulings_for(scope.repo, scope.pr, untrusted_roots=scope.untrusted_roots)


__all__ = [
    "IGNORED_PREFIX",
    "read_scope_rulings",
    "CONFIG_SECTION_RULINGS",
    "RULINGS_FILE_ENV_VAR",
    "FileRulingProvider",
    "RulingProvider",
    "RulingRead",
    "RulingScope",
    "RulingsConfig",
    "RulingsConfigError",
    "load_rulings_config",
    "resolve_ruling_provider",
    "untrusted_reason",
]
