"""push.verify_config -- per-repo declaration of the verification commands
`loadout-push` runs before it opens or updates a PR.

Reads the SAME single sectioned per-repo config file every other `push:`
section owner resolves through (`repo_config.resolve_repo_config_path`); this
module owns the `push.verify` key: a list of `{name, argv, timeout_seconds}`
entries.

DEFAULT NO-OP: a repo with no file, no `push:` section, or no `verify` key
gets an empty entry tuple, and the push path is byte-identical to a build
without this feature.

TRUST MODEL: this is REPO-LOCAL config, like `push.scratch_patterns`, but
unlike those it names commands that EXECUTE. That is acceptable here because
the commands run in the pushing checkout, as the pushing user, against that
same checkout's own content -- the party who can edit this file can already
run arbitrary code there (a Makefile, a git hook, a test file). It is not a
credential-minting or cross-repo surface, so unlike the user-level-only
`credentials:` tier it stays repo-local. Hardening that DOES apply: `argv` is
an argument LIST executed without a shell (no expansion, no pipes, no
injection through a branch name or PR title), and loadout does not add the
minted push credential to the child's environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from clagentic_loadout.numeric_validation import is_finite_positive_number
from clagentic_loadout.repo_config import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    resolve_repo_config_path,
)

CONFIG_SECTION_PUSH = "push"
CONFIG_KEY_VERIFY = "verify"

#: Applied when an entry omits `timeout_seconds`. Generous on purpose: the
#: first consumers run real builds, and a too-tight silent default would turn
#: into spurious refusals.
DEFAULT_TIMEOUT_SECONDS = 600.0


class InvalidVerifyConfigError(ValueError):
    """Raised when `push.verify` is present but malformed."""


@dataclass(frozen=True)
class VerifyEntry:
    """One declared verification command."""

    name: str
    argv: tuple[str, ...]
    timeout_seconds: float


def _parse_entry(raw: object, index: int, config_path: Path) -> VerifyEntry:
    where = f"{config_path}: {CONFIG_SECTION_PUSH}.{CONFIG_KEY_VERIFY}[{index}]"
    if not isinstance(raw, dict):
        raise InvalidVerifyConfigError(f"{where} must be a mapping, got {raw!r}.")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise InvalidVerifyConfigError(f"{where}.name must be a non-empty string, got {name!r}.")

    argv = raw.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(a, str) for a in argv)
        or not argv[0]
    ):
        raise InvalidVerifyConfigError(
            f"{where}.argv must be a non-empty list of strings (an argument "
            f"list, not a shell string), got {argv!r}."
        )

    timeout = raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not is_finite_positive_number(timeout):
        raise InvalidVerifyConfigError(
            f"{where}.timeout_seconds must be a positive number, got {timeout!r}."
        )

    return VerifyEntry(name=name.strip(), argv=tuple(argv), timeout_seconds=float(timeout))


def load_verify_entries(
    repo_root: str | Path | None = None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> tuple[VerifyEntry, ...]:
    """Resolve the declared verification entries for a repo (empty tuple when
    none are configured).

    Raises:
        InvalidVerifyConfigError: the config file is unreadable YAML, or
            `push.verify` is not a list of well-formed entries with unique
            names.
    """
    if repo_root is None:
        return ()

    config_path = resolve_repo_config_path(repo_root, config_relative_path=config_relative_path)
    if not config_path.exists():
        return ()

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InvalidVerifyConfigError(f"{config_path}: could not be read as YAML: {exc}.") from exc

    if not isinstance(raw, dict):
        return ()
    push_section = raw.get(CONFIG_SECTION_PUSH)
    if not isinstance(push_section, dict) or CONFIG_KEY_VERIFY not in push_section:
        return ()

    raw_entries = push_section[CONFIG_KEY_VERIFY]
    if raw_entries is None:
        return ()
    if not isinstance(raw_entries, list):
        raise InvalidVerifyConfigError(
            f"{config_path}: {CONFIG_SECTION_PUSH}.{CONFIG_KEY_VERIFY} must be a list, "
            f"got {raw_entries!r}."
        )

    entries = tuple(_parse_entry(item, i, config_path) for i, item in enumerate(raw_entries))
    names = [e.name for e in entries]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise InvalidVerifyConfigError(
            f"{config_path}: {CONFIG_SECTION_PUSH}.{CONFIG_KEY_VERIFY} names must be unique, "
            f"duplicated: {duplicates!r}."
        )
    return entries


__all__ = [
    "CONFIG_KEY_VERIFY",
    "CONFIG_SECTION_PUSH",
    "DEFAULT_TIMEOUT_SECONDS",
    "InvalidVerifyConfigError",
    "VerifyEntry",
    "load_verify_entries",
]
