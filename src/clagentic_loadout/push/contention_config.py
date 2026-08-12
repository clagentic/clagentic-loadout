"""push.contention_config — per-repo config for the optional working-tree
contention pre-flight check (lr-78a584).

Reads the SAME single sectioned per-repo config file
(`.clagentic/loadout/config.yaml`, via `repo_config.resolve_repo_config_path`)
every other section owner in this package already resolves through — this
module owns the `push:` section, keys `contention_check` and
`in_flight_branch_pattern`, sharing the `push:` top-level section with
`push.cleanliness_config`'s own `scratch_patterns` key rather than inventing
a second config location for a related, adjacent feature.

DEFAULT OFF, EXPLICIT OPT-IN (hard acceptance criterion, task description +
comment #1): a repo that never sets `push: contention_check: true` gets
`ContentionConfig.enabled is False` and the check never runs at all — see
`push.contention_check.check_working_tree_contention`, which short-circuits
on `enabled=False` before reading any git state. Absent config is byte-
identical to today's behavior.

PER-REPO GRANULARITY: this is repo-local config (mirrors
`push.cleanliness_config`'s own trust-boundary reasoning exactly — a
contention decision only ever concerns THIS repo's own working tree, no
credential-minting or cross-repo escalation surface is involved), never a
global/user-level toggle.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from clagentic_loadout.repo_config import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    resolve_repo_config_path,
)

#: Top-level section key this module shares with push.cleanliness_config.
CONFIG_SECTION_PUSH = "push"

#: Config key (within the `push:` section): explicit opt-in switch. Absent
#: or false -> disabled (today's behavior, unchanged).
CONFIG_KEY_CONTENTION_CHECK = "contention_check"

#: Config key (within the `push:` section): the regex a checked-out branch
#: name must match to count as "in-flight work" (the PRIMARY signal — see
#: push.contention_check's own module docstring for the full dirtiness-vs-
#: branch-name adjudication).
CONFIG_KEY_IN_FLIGHT_BRANCH_PATTERN = "in_flight_branch_pattern"

#: Default branch-name regex: the common feature/fix/chore-branch shapes this
#: package's own contributor conventions already use (see this repo's own
#: CLAUDE.md "Commit convention" / branch_conventions precedent) — a repo
#: that enables the feature without overriding the pattern gets a sane
#: baked-in default rather than matching nothing.
DEFAULT_IN_FLIGHT_BRANCH_PATTERN = r"^(feat|fix|chore|build|ci|docs|perf|refactor|style|test)/"


class InvalidContentionConfigError(ValueError):
    """Raised when a config file's contention-check keys are malformed
    (contention_check not a bool, or in_flight_branch_pattern not a string /
    not a valid regex)."""


@dataclass(frozen=True)
class ContentionConfig:
    """Resolved working-tree contention-check configuration."""

    enabled: bool
    branch_pattern: str
    source: str  # "default" or the config file path it was read from


def _default_config() -> ContentionConfig:
    return ContentionConfig(
        enabled=False, branch_pattern=DEFAULT_IN_FLIGHT_BRANCH_PATTERN, source="default"
    )


def load_contention_config(
    repo_root: str | Path | None = None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> ContentionConfig:
    """Resolve the working-tree contention-check config for a repo.

    Reads `<repo_root>/<config_relative_path>` (default
    `.clagentic/loadout/config.yaml`, with the same legacy-path fallback
    every other section-owning loader in this package applies — see
    `repo_config.resolve_repo_config_path`), expecting a `push:` top-level
    section holding `contention_check` (bool) and, optionally,
    `in_flight_branch_pattern` (a regex string). Falls back to
    `_default_config()` (disabled) when the file, the `push:` section, or the
    `contention_check` key is absent — a repo that never opted in keeps
    today's behavior, unchanged.

    Args:
        repo_root: repo root to resolve the config path against. When None,
            the default (disabled) config is returned directly (no file
            lookup).
        config_relative_path: override the config file's relative path
            (mainly for tests).

    Raises:
        InvalidContentionConfigError: the config file exists but
            contention_check or in_flight_branch_pattern is malformed.
    """
    if repo_root is None:
        return _default_config()

    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path
    )
    if not config_path.exists():
        return _default_config()

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InvalidContentionConfigError(
            f"{config_path}: could not be read as YAML: {exc}."
        ) from exc

    if not isinstance(raw, dict):
        return _default_config()

    push_section = raw.get(CONFIG_SECTION_PUSH)
    if not isinstance(push_section, dict) or CONFIG_KEY_CONTENTION_CHECK not in push_section:
        return _default_config()

    enabled = push_section[CONFIG_KEY_CONTENTION_CHECK]
    if not isinstance(enabled, bool):
        raise InvalidContentionConfigError(
            f"{config_path}: {CONFIG_SECTION_PUSH}.{CONFIG_KEY_CONTENTION_CHECK} "
            f"must be a boolean, got {enabled!r}."
        )

    raw_pattern = push_section.get(
        CONFIG_KEY_IN_FLIGHT_BRANCH_PATTERN, DEFAULT_IN_FLIGHT_BRANCH_PATTERN
    )
    if not isinstance(raw_pattern, str) or not raw_pattern:
        raise InvalidContentionConfigError(
            f"{config_path}: {CONFIG_SECTION_PUSH}.{CONFIG_KEY_IN_FLIGHT_BRANCH_PATTERN} "
            f"must be a non-empty string, got {raw_pattern!r}."
        )
    try:
        import re

        re.compile(raw_pattern)
    except re.error as exc:
        raise InvalidContentionConfigError(
            f"{config_path}: {CONFIG_SECTION_PUSH}.{CONFIG_KEY_IN_FLIGHT_BRANCH_PATTERN} "
            f"is not a valid regex ({raw_pattern!r}): {exc}."
        ) from exc

    return ContentionConfig(
        enabled=enabled, branch_pattern=raw_pattern, source=str(config_path)
    )


__all__ = [
    "CONFIG_KEY_CONTENTION_CHECK",
    "CONFIG_KEY_IN_FLIGHT_BRANCH_PATTERN",
    "CONFIG_SECTION_PUSH",
    "DEFAULT_CONFIG_RELATIVE_PATH",
    "DEFAULT_IN_FLIGHT_BRANCH_PATTERN",
    "ContentionConfig",
    "InvalidContentionConfigError",
    "load_contention_config",
]
