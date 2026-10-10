"""merge.pre_checks_sandbox -- the deployment-tier command prefix that wraps
every pre_check in a process sandbox.

A pre_check executes code from the PR head before the PR is merged, as the
merger's user. The environment scrub (`merge.pre_check_env`) and the private
merge-result clone (`merge.merge_result_clone`) narrow what such a check can
reach, but a process running as the merger's user can still read that user's
files and the merger's own `/proc/<pid>/environ`, write outside the clone by
absolute path and leave daemons behind. Only process isolation closes that, and
loadout ships none: it composes with whatever the deployment provides.

`merge.pre_checks_sandbox` is an argv list, read ONLY from the user-level
config file, that is prepended to every pre_check command:

    merge:
      pre_checks_sandbox: [/usr/bin/some-sandbox, --flag, "{clone}", --, ]

Two placeholders are substituted per check, with no other expansion and no
shell: `{clone}` is the private merge-result clone the check runs in, and
`{tmpdir}` is a private mode-0700 directory loadout creates for that check
(under the merger's own `TMPDIR`) and removes afterwards. The check's `TMPDIR`
is forced to that same directory, so the sandbox bind and the environment agree
and a step's inline `TMPDIR=` cannot pick the read-write path the sandbox
binds. The prefix is applied after a step's inline `VAR=VALUE` env prefix has
been split off, so the rest of the environment still reaches the launched
process and the prefix is the outermost argv.

USER-LEVEL ONLY. The key is never read from a repo's config file, tracked or
not: a PR that could set or remove its own sandbox would defeat it. A repo
config that carries the key has it ignored, with a warning
(`ignored_repo_sandbox_warnings`).

FAIL CLOSED. A configured value that is not a usable argv raises
`PreChecksSandboxConfigError`, and the caller refuses the merge: a broken
sandbox configuration must never degrade into running the checks unsandboxed.
An absent key is the unconfigured default and changes nothing. Applies to
pre_checks only, never to post_merge_steps.
"""

from __future__ import annotations

import os
from pathlib import Path

from clagentic_loadout.merge.post_merge import PostMergeConfigError
from clagentic_loadout.merge.post_merge_config import CONFIG_SECTION_MERGE, read_repo_merge_section
from clagentic_loadout.repo_config import resolve_repo_config_path
from clagentic_loadout.transport.provider_config import load_user_config_section

#: Key within the user-level `merge:` section holding the sandbox argv prefix.
CONFIG_KEY_PRE_CHECKS_SANDBOX = "pre_checks_sandbox"


class PreChecksSandboxConfigError(PostMergeConfigError):
    """`merge.pre_checks_sandbox` is present but is not a usable argv prefix."""


def validate_pre_checks_sandbox(value: object, *, source: str = "user-level config") -> tuple[str, ...]:
    """The sandbox argv *value* describes, or a refusal.

    Raises:
        PreChecksSandboxConfigError: not a non-empty list of non-empty strings,
            or its first element is not an absolute path to an existing
            executable file.
    """
    label = f"{CONFIG_SECTION_MERGE}.{CONFIG_KEY_PRE_CHECKS_SANDBOX} in {source}"
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(part, str) and part for part in value)
    ):
        raise PreChecksSandboxConfigError(
            f"{label} must be a non-empty list of non-empty strings, got {value!r}."
        )
    launcher = value[0]
    if not os.path.isabs(launcher):
        raise PreChecksSandboxConfigError(
            f"{label}: the first element must be an absolute path to the sandbox "
            f"executable, got {launcher!r}."
        )
    if not (os.path.isfile(launcher) and os.access(launcher, os.X_OK)):
        raise PreChecksSandboxConfigError(
            f"{label}: {launcher!r} is not an existing executable file."
        )
    return tuple(value)


def resolve_pre_checks_sandbox(*, config_root: str | Path | None = None) -> tuple[str, ...]:
    """The configured sandbox argv prefix, `()` when the key is absent.

    Reads the user-level config file only (`config_root` defaults to the
    standard root). Any value present, including an explicit null, must
    validate.

    Raises:
        PreChecksSandboxConfigError: see `validate_pre_checks_sandbox`.
    """
    section = load_user_config_section(CONFIG_SECTION_MERGE, config_root=config_root)
    if CONFIG_KEY_PRE_CHECKS_SANDBOX not in section:
        return ()
    return validate_pre_checks_sandbox(section[CONFIG_KEY_PRE_CHECKS_SANDBOX])


def ignored_sandbox_warning(location: object) -> str:
    """The one warning text for a repo config *location* carrying the sandbox
    key, shared by every path that reads repo config so they cannot drift."""
    return (
        f"merge.{CONFIG_KEY_PRE_CHECKS_SANDBOX} in {location} is IGNORED -- the sandbox is "
        f"read only from the user-level config file, so a repository cannot set or remove "
        f"its own sandbox"
    )


def ignored_repo_sandbox_warnings(repo_path: str | Path | None) -> tuple[str, ...]:
    """A warning for the repo config file at *repo_path* when it carries the
    sandbox key, which is honoured from the user-level file only.

    A repo file that cannot be read yields nothing: the loaders that own it
    report that failure themselves.
    """
    if repo_path is None:
        return ()
    config_path = resolve_repo_config_path(repo_path, warn=False)
    try:
        merge_section = read_repo_merge_section(config_path)
    except PostMergeConfigError:
        return ()
    if CONFIG_KEY_PRE_CHECKS_SANDBOX not in merge_section:
        return ()
    return (ignored_sandbox_warning(config_path),)


__all__ = [
    "CONFIG_KEY_PRE_CHECKS_SANDBOX",
    "PreChecksSandboxConfigError",
    "ignored_repo_sandbox_warnings",
    "ignored_sandbox_warning",
    "resolve_pre_checks_sandbox",
    "validate_pre_checks_sandbox",
]
