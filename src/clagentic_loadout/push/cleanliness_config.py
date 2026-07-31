"""push.cleanliness_config — per-repo scratch-pattern config for the
pre-push cleanliness check (lr-d7a8).

Reads the SAME single sectioned per-repo config file (default
`.clagentic/loadout/config.yaml` — see `repo_config.py`, lr-446c35, for the
shared path constant/legacy-fallback loader every section owner in this
package now resolves through) `wait.config` (its `wait:` section) and
`merge.post_merge_config` (its `merge:` section) already establish — this
module owns the `push:` section, key `scratch_patterns`. A repo overrides or
extends the shipped default pattern set entirely via config; there is no
second config mechanism here (explicit task direction: follow the existing
config-loading pattern, do not invent a new one).

This is REPO-LOCAL config, and that is the correct trust boundary for this
particular section (contrast with transport.provider_config's `credentials:`
tier, which is deliberately USER-LEVEL only, lr-0818): a scratch-pattern
list only ever decides which filenames in THIS repo's own working tree get
flagged before a push from THIS repo — there is no credential-minting or
cross-repo escalation surface analogous to the one that makes `credentials:`
user-level-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

import yaml

from clagentic_loadout.repo_config import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    resolve_repo_config_path,
)

#: Top-level section key this module owns within the repo-local config file.
CONFIG_SECTION_PUSH = "push"

#: Config key (within the `push:` section) holding the list of scratch
#: glob-pattern strings.
CONFIG_KEY_SCRATCH_PATTERNS = "scratch_patterns"

# Default scratch-file glob patterns — generic dev-scratch shapes, not tied
# to any one project, agent, or tracker (CLAUDE.md rule 1 / 6a: no internal
# identity in product code). A repo may override this set entirely via
# config; this is the baked-in fallback.
DEFAULT_SCRATCH_PATTERNS: list[str] = [
    "pr-body-*",
    "*scratch*",
    ".homecheck-*",
    "*.diff-check*",
    "HANDOFF.md",
]


class InvalidCleanlinessConfigError(ValueError):
    """Raised when a config file's scratch_patterns value is malformed (not
    a list of strings)."""


@dataclass(frozen=True)
class CleanlinessConfig:
    """Resolved pre-push cleanliness configuration: the pattern set plus
    where it came from, for diagnostics."""

    patterns: tuple[str, ...]
    source: str  # "default" or the config file path it was read from


def _default_config() -> CleanlinessConfig:
    return CleanlinessConfig(patterns=tuple(DEFAULT_SCRATCH_PATTERNS), source="default")


def load_scratch_patterns(
    repo_root: str | Path | None = None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> CleanlinessConfig:
    """Resolve the pre-push scratch-pattern set for a repo.

    Reads `<repo_root>/<config_relative_path>` (default
    `.clagentic/loadout/config.yaml`, falling back to the legacy
    `.loadout/config.yaml` path when only that one exists — see
    `repo_config.resolve_repo_config_path`), expecting a `push:` top-level
    section holding a `scratch_patterns` key (a list of glob-pattern strings,
    matched via fnmatch — see `find_scratch_matches`). Falls back to
    DEFAULT_SCRATCH_PATTERNS when the file, the `push:` section, or the
    `scratch_patterns` key is absent — a repo that never opted into
    overriding the pattern set keeps the shipped defaults.

    Args:
        repo_root: repo root to resolve the config path against. When None,
            the default pattern set is returned directly (no file lookup).
        config_relative_path: override the config file's relative path
            (mainly for tests).

    Raises:
        InvalidCleanlinessConfigError: the config file exists but
            scratch_patterns is malformed.
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
        raise InvalidCleanlinessConfigError(
            f"{config_path}: could not be read as YAML: {exc}."
        ) from exc

    if not isinstance(raw, dict):
        return _default_config()

    push_section = raw.get(CONFIG_SECTION_PUSH)
    if not isinstance(push_section, dict) or CONFIG_KEY_SCRATCH_PATTERNS not in push_section:
        return _default_config()

    raw_patterns = push_section[CONFIG_KEY_SCRATCH_PATTERNS]
    if not isinstance(raw_patterns, list) or not all(isinstance(p, str) for p in raw_patterns):
        raise InvalidCleanlinessConfigError(
            f"{config_path}: {CONFIG_SECTION_PUSH}.{CONFIG_KEY_SCRATCH_PATTERNS} must be "
            f"a list of strings, got {raw_patterns!r}."
        )

    return CleanlinessConfig(patterns=tuple(raw_patterns), source=str(config_path))


def match_scratch_pattern(filename: str, patterns: tuple[str, ...]) -> str | None:
    """Return the first pattern in *patterns* that matches *filename* (glob
    semantics via fnmatch, matched against the filename's final path
    component only — a pattern like `HANDOFF.md` matches a nested
    `docs/HANDOFF.md` the same as a root-level one), or None if no pattern
    matches."""
    basename = Path(filename).name
    for pattern in patterns:
        if fnmatch(basename, pattern):
            return pattern
    return None


__all__ = [
    "CONFIG_KEY_SCRATCH_PATTERNS",
    "CONFIG_SECTION_PUSH",
    "DEFAULT_CONFIG_RELATIVE_PATH",
    "DEFAULT_SCRATCH_PATTERNS",
    "CleanlinessConfig",
    "InvalidCleanlinessConfigError",
    "load_scratch_patterns",
    "match_scratch_pattern",
]
