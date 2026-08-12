"""task_id_guard — config-driven refusal of an internal work-item identifier
appearing in a PR title or a branch commit subject.

WHY THIS MODULE EXISTS: an internal task-tracker id reaching a PR title or a
non-squash branch commit subject becomes PERMANENT public history the moment
it lands — neither surface is a Python string this package's own AST-based
anonymization guard (tests/test_anonymization_guard.py) can see, since a PR
title lives on the git host and a commit subject lives in git metadata, not
in `src/**/*.py`. Both surfaces depended entirely on a reviewer noticing.
This module is the mechanical enforcement that removes that dependency.

NOT A HARDCODED lr-XXXXXX GUARD (hard constraint — CLAUDE.md rule 1 "no
internal identity in product code," rule 6 "task_id is an opaque work-item
reference with a CONFIGURABLE PATTERN, loadout does not assume any particular
tracker"): the forbidden-pattern set is resolved EXCLUSIVELY through
TaskIdGuardConfig, loaded from the SAME `.clagentic/loadout/config.yaml`
seam every other section owner in this package reads through
(repo_config.resolve_repo_config_path). There is no source-literal pattern
anywhere in this module. A deployment on a DIFFERENT tracker convention
(JIRA-1234, PROJ-5678, anything) configures its own regex; a deployment that
never configures one gets a strict NO-OP (see NO-OP BY DEFAULT below).

NO-OP BY DEFAULT WHEN UNCONFIGURED (hard acceptance criterion): with no
`task_id_pattern` key set, `TaskIdGuardConfig.pattern is None` and every
check function in this module returns cleanly without inspecting anything —
never fails closed on an unconfigured deployment. This is also WHY block is a
defensible default (see DEFAULT MODE below): block only ever activates once a
deployment has already opted in by supplying a pattern.

DEFAULT MODE IS BLOCK (operator decision, pinned — do not weaken without a
fresh operator decision): once a pattern IS configured, `mode` defaults to
"block". This is deliberately NOT parallel to a feature that refuses on live
git state every checkout has (which correctly defaults off) — this guard is
inert with no pattern configured, so a block default only ever affects a
deployment that has already opted in. IF THIS NO-OP PROPERTY EVER CHANGES —
a built-in default pattern is added, or the guard becomes active with no
explicit configuration — this default must be revisited, because the
reasoning above no longer holds. See docs/verbs.md's own "Task-id guard"
section for the integrator-facing statement of this same dependency.

THREE MODES: "off" (nothing inspected, nothing blocked, nothing printed),
"warn" (a match prints a warning naming the field/value/config key and the
operation proceeds), "block" (a match raises TaskIdGuardViolation naming the
field/value/config key, the caller refuses).

WHAT IS EXEMPT, ALWAYS, REGARDLESS OF MODE:
  - The PR body's `Task: <id>` trailer (push.issue_link.normalize_task_trailer
    / release.dispatch._TASK_TRAILER_RE grammar) — this module never inspects
    a PR body at all; only a title or a commit SUBJECT (first line) is ever
    checked. The trailer is the sanctioned, provenance-carrying home for a
    task id (CLAUDE.md rule 8) and is out of scope for this guard by
    construction, not by a special-cased exemption.
  - The Conventional Commits trailing `(#NN)` PR-reference form: this
    module's pattern match is against the CALLER-SUPPLIED pattern only — a
    deployment's own pattern is responsible for not matching `(#NN)`; this
    module adds no additional stripping or special-casing on top of the
    configured regex, keeping the contract simple (the pattern IS the
    contract).

REUSE, NOT A SECOND GRAMMAR: this module has no opinion on Conventional
Commits shape at all (merge.title_gate / merge.commit_subjects already own
that, unchanged) — it is purely an ADDITIONAL, independent pattern-match
gate layered on the same two strings (PR title, branch commit subject) those
modules already validate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from clagentic_loadout.repo_config import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    resolve_repo_config_path,
)

#: Top-level section key. Shared with every other push:-owning module
#: (push.contention_config, push.cleanliness_config) — one sectioned file,
#: one section per verb/feature area, this package's established convention.
CONFIG_SECTION_PUSH = "push"

#: Config key (within the `push:` section): the caller's own internal
#: work-item-identifier pattern, e.g. r"\bJIRA-\d+\b". Absent -> the guard is
#: a strict no-op (see module docstring, "NO-OP BY DEFAULT").
CONFIG_KEY_TASK_ID_PATTERN = "task_id_guard_pattern"

#: Config key (within the `push:` section): enforcement mode for this
#: guard. One of MODE_OFF / MODE_WARN / MODE_BLOCK. Only consulted when
#: CONFIG_KEY_TASK_ID_PATTERN is also set — see load_task_id_guard_config.
CONFIG_KEY_TASK_ID_MODE = "task_id_guard_mode"

MODE_OFF = "off"
MODE_WARN = "warn"
MODE_BLOCK = "block"

_VALID_MODES = frozenset({MODE_OFF, MODE_WARN, MODE_BLOCK})

#: The shipped default mode once a pattern IS configured (operator decision,
#: pinned — see module docstring "DEFAULT MODE IS BLOCK"). Never consulted
#: when no pattern is configured at all.
DEFAULT_MODE_WHEN_PATTERN_CONFIGURED = MODE_BLOCK


class InvalidTaskIdGuardConfigError(ValueError):
    """Raised when a config file's task-id-guard keys are malformed
    (task_id_guard_pattern not a string / not a valid regex, or
    task_id_guard_mode not one of off/warn/block)."""


class TaskIdGuardViolation(Exception):
    """Raised (mode="block") when a PR title or branch commit subject
    matches the configured task_id_guard_pattern. Carries the offending
    field name, the matched value, and the config key that enabled the
    check, so the message and any programmatic caller can both name the
    exact settings knob to adjust — see _format_violation."""

    def __init__(self, message: str, *, field: str, value: str, matched: str) -> None:
        super().__init__(message)
        self.field = field
        self.value = value
        self.matched = matched


@dataclass(frozen=True)
class TaskIdGuardConfig:
    """Resolved task-id-guard configuration."""

    pattern: str | None  # None means "no pattern configured" -- strict no-op
    mode: str  # one of MODE_OFF / MODE_WARN / MODE_BLOCK
    source: str  # "default" or the config file path it was read from


def _default_config() -> TaskIdGuardConfig:
    return TaskIdGuardConfig(pattern=None, mode=MODE_OFF, source="default")


def load_task_id_guard_config(
    repo_root: str | Path | None = None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> TaskIdGuardConfig:
    """Resolve the task-id-guard configuration for a repo.

    Reads `<repo_root>/<config_relative_path>` (default
    `.clagentic/loadout/config.yaml`), expecting a `push:` top-level section
    holding `task_id_guard_pattern` (a regex string) and, optionally,
    `task_id_guard_mode` (one of "off"/"warn"/"block").

    ABSENCE SEMANTICS: no repo_root, no config file, no `push:` section, or
    no `task_id_guard_pattern` key -> `_default_config()`
    (pattern=None, mode="off") -- the guard is a strict no-op. This is the
    hard acceptance criterion "no configured pattern means the guard is a
    no-op, it must never fail closed on an unconfigured deployment."

    `task_id_guard_mode` is consulted ONLY when a pattern IS configured; when
    the pattern is present but the mode key is absent, the resolved mode is
    DEFAULT_MODE_WHEN_PATTERN_CONFIGURED ("block" — operator-pinned default,
    see module docstring). An explicit mode always wins over that default,
    including an explicit "off" (a deployment that wants the pattern
    recorded but the check inert on this surface is a legitimate
    configuration).

    Raises:
        InvalidTaskIdGuardConfigError: the config file exists but
            task_id_guard_pattern is not a non-empty valid-regex string, or
            task_id_guard_mode is not one of "off"/"warn"/"block".
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
        raise InvalidTaskIdGuardConfigError(
            f"{config_path}: could not be read as YAML: {exc}."
        ) from exc

    if not isinstance(raw, dict):
        return _default_config()

    push_section = raw.get(CONFIG_SECTION_PUSH)
    if not isinstance(push_section, dict) or CONFIG_KEY_TASK_ID_PATTERN not in push_section:
        return _default_config()

    raw_pattern = push_section[CONFIG_KEY_TASK_ID_PATTERN]
    if not isinstance(raw_pattern, str) or not raw_pattern:
        raise InvalidTaskIdGuardConfigError(
            f"{config_path}: {CONFIG_SECTION_PUSH}.{CONFIG_KEY_TASK_ID_PATTERN} "
            f"must be a non-empty string, got {raw_pattern!r}."
        )
    try:
        re.compile(raw_pattern)
    except re.error as exc:
        raise InvalidTaskIdGuardConfigError(
            f"{config_path}: {CONFIG_SECTION_PUSH}.{CONFIG_KEY_TASK_ID_PATTERN} "
            f"is not a valid regex ({raw_pattern!r}): {exc}."
        ) from exc

    raw_mode = push_section.get(
        CONFIG_KEY_TASK_ID_MODE, DEFAULT_MODE_WHEN_PATTERN_CONFIGURED
    )
    if raw_mode not in _VALID_MODES:
        raise InvalidTaskIdGuardConfigError(
            f"{config_path}: {CONFIG_SECTION_PUSH}.{CONFIG_KEY_TASK_ID_MODE} "
            f"must be one of {sorted(_VALID_MODES)!r}, got {raw_mode!r}."
        )

    return TaskIdGuardConfig(pattern=raw_pattern, mode=raw_mode, source=str(config_path))


def _format_violation(*, field: str, value: str, matched: str) -> str:
    """Build the TaskIdGuardViolation message. Names the offending field,
    the full offending value, the exact matched substring, and the config
    key that enabled the check (so the docs are reachable from the refusal
    itself, per this task's own documentation acceptance criteria) -- CLI
    hygiene rule 4, never a collapsed guess."""
    return (
        f"Task-id guard FAILED -- {field} contains a string matching the "
        f"configured internal-identifier pattern: {matched!r} (in {value!r}).\n"
        f"WHY THIS MATTERS: an internal work-item identifier in a PR title "
        f"or a non-squash branch commit subject becomes permanent public "
        f"history the moment it lands -- neither surface is reachable by "
        f"this package's own src/-scoped anonymization guard.\n"
        f"RESOLUTION: remove the internal identifier from {field}. The "
        f"opaque work-item reference belongs in the PR body's 'Task: <id>' "
        f"trailer instead (never a title or commit subject) -- see "
        f"--task-id on the push verb.\n"
        f"Enabled by: `.clagentic/loadout/config.yaml` "
        f"`{CONFIG_SECTION_PUSH}.{CONFIG_KEY_TASK_ID_PATTERN}` "
        f"(mode `{CONFIG_SECTION_PUSH}.{CONFIG_KEY_TASK_ID_MODE}`, default "
        f"{DEFAULT_MODE_WHEN_PATTERN_CONFIGURED!r} once a pattern is set). "
        f"See docs/verbs.md for the full contract, including how to change "
        f"the pattern or turn this check off."
    )


def check_task_id_guard(
    value: str | None,
    *,
    field: str,
    pattern: str | None,
    mode: str,
) -> None:
    """Check *value* (a PR title, or a single commit subject -- first line
    only) against *pattern*.

    A strict no-op whenever *pattern* is None (no configured pattern -- see
    module docstring "NO-OP BY DEFAULT") or *value* is None, REGARDLESS of
    *mode*. A strict no-op whenever *mode* is MODE_OFF, even with a pattern
    configured.

    ONE SIMPLE CONTRACT FOR EVERY CALLER: mode="warn" never raises -- it
    returns the formatted warning message instead, so the caller controls
    where/how it is surfaced (mirrors this package's own "surfacing a bypass
    is the caller's job" convention elsewhere, e.g.
    merge.title_gate.check_pr_title's own skip logging). mode="block" always
    raises TaskIdGuardViolation on a match.

    Returns:
        The formatted warning message string when mode="warn" and a match
        was found; None otherwise (no match, mode="off", no pattern, or no
        value).

    Raises:
        TaskIdGuardViolation: mode="block" and *value* matches *pattern*.
    """
    if pattern is None or value is None or mode == MODE_OFF:
        return None

    match = re.search(pattern, value)
    if match is None:
        return None

    message = _format_violation(field=field, value=value, matched=match.group(0))

    if mode == MODE_BLOCK:
        raise TaskIdGuardViolation(
            message, field=field, value=value, matched=match.group(0)
        )

    # mode == MODE_WARN
    return message


__all__ = [
    "CONFIG_KEY_TASK_ID_MODE",
    "CONFIG_KEY_TASK_ID_PATTERN",
    "CONFIG_SECTION_PUSH",
    "DEFAULT_MODE_WHEN_PATTERN_CONFIGURED",
    "InvalidTaskIdGuardConfigError",
    "MODE_BLOCK",
    "MODE_OFF",
    "MODE_WARN",
    "TaskIdGuardConfig",
    "TaskIdGuardViolation",
    "check_task_id_guard",
    "load_task_id_guard_config",
]
