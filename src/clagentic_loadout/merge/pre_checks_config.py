"""merge.pre_checks_config — repo-local `pre_checks` config surface (lr-0a03c3).

GAP THIS CLOSES: a repo migrating off a `.crew`-shaped deployment onto
loadout-native config had no home for its pre-merge validation commands (the
functional-inventory reference calls this `pre_checks` — ordered read-only
commands a merge gate runs BEFORE authorizing a merge, e.g. a lint pass with
no CI runner wired up). Without this module, that repo LOSES its pre-merge
checks the moment it migrates — `loadout-merge` had no config-driven way to
run them at all, only `post_merge_steps` (AFTER a merge already landed).

REPO-TIER, not deployment-tier (lr-0a03c3 design call #1): pre_checks is
"what does THIS repo want validated, in ITS OWN working tree, before *I*
(the merge gate) authorize landing a change" — the exact same trust boundary
`merge.post_merge_config`'s `post_merge_steps` already established for the
symmetric after-merge case (see that module's docstring for the full
rationale: no credential-minting or cross-repo escalation surface, unlike
`transport.provider_config`'s user-level-only `credentials:` tier). Lives in
the SAME repo-local, committed, public-safe `.clagentic/loadout/config.yaml`
file, under the `merge:` section this package's other merge-gate config
already owns (`merge.post_merge_config.CONFIG_SECTION_MERGE`) — NOT a new
top-level section, and NOT a new file.

REPLACE-NOT-MERGE (design call #2, consistent with `provisioning.roles`):
there is only one `pre_checks` list per repo — a repo either declares it (in
which case that IS the check list) or does not (in which case there are none
to run, mirroring `post_merge_steps`' own "absent -> []" contract). There is
no default list to override or merge against here, unlike `roles:`/
`model_routing:` (which have a REFERENCE default an omitted-section repo
falls back to) — an absent `pre_checks` key means "this repo runs no
pre-merge validation commands," a legitimate and common shape (e.g. a repo
gated purely by CI + reviewer verdicts), not "fall back to some baked-in
default check list" (loadout has no opinion on what a generic repo's own
pre-merge validation should run).

STEP SHAPE: reuses `merge.post_merge`'s existing step validator/executor
verbatim (`validate_post_merge_steps` / `run_post_merge_steps`) — same `cmd`
(str | list[str]), `description`, `on_failure` ("warn" default | "fail"),
`detaches` (bool, "false" default — lr-53556a) fields, same shell-operator-
token rejection, same `shell=False` execution contract. This is NOT a second
step-runner implementation: pre_checks and
post_merge_steps are structurally the SAME primitive ("an ordered list of
read-only-by-convention repo-local commands, gated by on_failure"), applied
at two different points in the merge gate's own lifecycle (before vs. after
the merge call). A caller with a `pre_checks: fail`-gated step failing MUST
refuse the merge before it is ever attempted — see `merge.verb`'s own gate
chain for where this plugs in as an ADDITIONAL, config-driven, opt-in link
ahead of step 9 (the merge call itself).

ROLE VOCABULARY: pre_checks entries name no role, no agent, no reviewer —
they are bare shell commands scoped to the repo's own working tree, same as
post_merge_steps. Nothing here is identity-bearing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.merge.post_merge import PostMergeConfigError, validate_post_merge_steps
from clagentic_loadout.merge.post_merge_config import CONFIG_SECTION_MERGE, read_repo_merge_section
from clagentic_loadout.merge.pre_check_env import is_git_location_name
from clagentic_loadout.merge.tracked_file import git_tracks_path
from clagentic_loadout.repo_config import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    resolve_repo_config_path,
)

#: Key within the `merge:` section holding the ordered pre-merge check list.
#: Sibling of CONFIG_KEY_POST_MERGE_STEPS within the SAME `merge:` section —
#: pre_checks run BEFORE the merge call, post_merge_steps run AFTER it.
CONFIG_KEY_PRE_CHECKS = "pre_checks"

#: Optional deployment-tier key in the same `merge:` section: environment
#: variable names kept in the pre_check child environment although the
#: default scrub (`merge.pre_check_env`) would remove them.
CONFIG_KEY_PRE_CHECKS_ENV_PASSTHROUGH = "pre_checks_env_passthrough"

#: Matched with `fullmatch`: a `$` anchor would accept a trailing newline, a
#: name that passes validation and then never equals the real variable.
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def load_pre_checks(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> list[dict]:
    """Resolve the pre_checks list for a repo.

    Reads `<repo_root>/<config_relative_path>` (default
    `.clagentic/loadout/config.yaml`, falling back to the legacy
    `.loadout/config.yaml` path when only that one exists — see
    `repo_config.resolve_repo_config_path`), expecting a `merge:` top-level
    section holding a `pre_checks` key (a list of step mappings — see
    `merge.post_merge.run_post_merge_steps` for the step shape; this module
    reuses that same validator/executor, see module docstring).

    Returns `[]` (no checks to run — a no-op, never an error) when
    *repo_root* is None, the config file is absent, the `merge:` section is
    absent, or the `pre_checks` key is absent within it. A repo that never
    opted into pre-merge validation is unaffected.

    Raises:
        PostMergeConfigError: the config file exists but is unreadable/
            malformed YAML, `pre_checks` is present but not a list, or any
            individual step fails `validate_post_merge_steps` (missing cmd,
            shell-operator token in a cmd string, invalid on_failure, etc.)
            — always at LOAD time, before any step executes.
    """
    if repo_root is None:
        return []

    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path
    )
    return pre_checks_from_section(read_repo_merge_section(config_path))


def pre_checks_from_section(merge_section: dict) -> list[dict]:
    """Validate and return the `pre_checks` list of an already-parsed `merge:`
    section (`[]` when the key is absent or null).

    Shared by `load_pre_checks` and the merge gate's base-commit reader, so a
    pre_check is validated by one rule wherever its text came from.

    Raises:
        PostMergeConfigError: `pre_checks` is not a list or a step is invalid.
    """
    steps = merge_section.get(CONFIG_KEY_PRE_CHECKS)
    if steps is None:
        return []

    validate_post_merge_steps(steps)
    return steps


def resolve_pre_checks_env_passthrough(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> tuple[str, ...]:
    """The environment variable names a deployment deliberately lets through
    to pre_check child processes (`merge.pre_checks_env_passthrough`).

    Deployment tier: read from the working-tree config file at *repo_root*,
    never from the tracked gate file, so the PR under review cannot widen what
    its own checks see. `()` when *repo_root* is None or the key is absent.

    Raises:
        PostMergeConfigError: the file cannot be read, or the key is not a list
            of valid environment variable names.
    """
    if repo_root is None:
        return ()
    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path, warn=False
    )
    value = read_repo_merge_section(config_path).get(CONFIG_KEY_PRE_CHECKS_ENV_PASSTHROUGH)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(name, str) and _ENV_NAME_RE.fullmatch(name) for name in value
    ):
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_KEY_PRE_CHECKS_ENV_PASSTHROUGH!r} must be a list of "
            f"environment variable names, got {value!r}."
        )
    selectors = [name for name in value if is_git_location_name(name)]
    if selectors:
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_KEY_PRE_CHECKS_ENV_PASSTHROUGH!r} names git "
            f"repository/location selector(s) {sorted(set(selectors))!r}, which are never "
            f"passed to a pre_check and cannot be passed through."
        )
    return tuple(dict.fromkeys(value))


@dataclass(frozen=True)
class PassthroughDecision:
    """The passthrough names the pre_check scrub may honour, and why not when
    the configured list was set aside.

    *names* is `()` whenever *ignored_file* is set. *ignored_file* is the config
    file that supplied the key and was not honoured; *reason* says why."""

    names: tuple[str, ...] = ()
    ignored_file: Path | None = None
    reason: str = ""


def decide_pre_checks_env_passthrough(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> PassthroughDecision:
    """`resolve_pre_checks_env_passthrough`, honoured only from an untracked file.

    The list widens what code from an unmerged PR can read from the merger's
    environment. A file tracked in *repo_root* is one a pull request can edit,
    so a list supplied by it is ignored (and named, with the reason). When git
    cannot say whether the file is tracked it is treated as tracked.

    Raises:
        PostMergeConfigError: as `resolve_pre_checks_env_passthrough`.
    """
    names = resolve_pre_checks_env_passthrough(repo_root, config_relative_path=config_relative_path)
    if not names or repo_root is None:
        return PassthroughDecision()
    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path, warn=False
    )
    tracked = git_tracks_path(repo_root, config_path)
    if tracked is False:
        return PassthroughDecision(names=names)
    why = "is tracked by git" if tracked else "could not be checked for git tracking"
    return PassthroughDecision(
        ignored_file=config_path,
        reason=(
            f"{CONFIG_KEY_PRE_CHECKS_ENV_PASSTHROUGH} is honoured only from an untracked file "
            f"and {config_path} {why}"
        ),
    )


__all__ = [
    "CONFIG_KEY_PRE_CHECKS",
    "CONFIG_KEY_PRE_CHECKS_ENV_PASSTHROUGH",
    "PassthroughDecision",
    "decide_pre_checks_env_passthrough",
    "resolve_pre_checks_env_passthrough",
    "CONFIG_SECTION_MERGE",
    "DEFAULT_CONFIG_RELATIVE_PATH",
    "load_pre_checks",
    "pre_checks_from_section",
]
