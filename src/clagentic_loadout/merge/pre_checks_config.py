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

from pathlib import Path

import yaml

from clagentic_loadout.merge.post_merge import PostMergeConfigError, validate_post_merge_steps
from clagentic_loadout.merge.post_merge_config import CONFIG_SECTION_MERGE
from clagentic_loadout.repo_config import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    resolve_repo_config_path,
)

#: Key within the `merge:` section holding the ordered pre-merge check list.
#: Sibling of CONFIG_KEY_POST_MERGE_STEPS within the SAME `merge:` section —
#: pre_checks run BEFORE the merge call, post_merge_steps run AFTER it.
CONFIG_KEY_PRE_CHECKS = "pre_checks"


def _read_yaml_mapping(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PostMergeConfigError(f"{path}: could not be read as YAML: {exc}.") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PostMergeConfigError(
            f"{path}: top-level document must be a mapping, got {type(raw).__name__}."
        )
    return raw


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
    raw = _read_yaml_mapping(config_path)

    merge_section = raw.get(CONFIG_SECTION_MERGE)
    if merge_section is None:
        return []
    if not isinstance(merge_section, dict):
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_SECTION_MERGE!r} section must be a mapping, "
            f"got {type(merge_section).__name__}."
        )

    steps = merge_section.get(CONFIG_KEY_PRE_CHECKS)
    if steps is None:
        return []

    validate_post_merge_steps(steps)
    return steps


__all__ = [
    "CONFIG_KEY_PRE_CHECKS",
    "CONFIG_SECTION_MERGE",
    "DEFAULT_CONFIG_RELATIVE_PATH",
    "load_pre_checks",
]
