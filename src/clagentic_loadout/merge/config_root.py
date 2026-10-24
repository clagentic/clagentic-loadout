"""merge.config_root -- the one place the merge verbs find the config file of
``--repo-path``.

Every merge-verb reader of the deployment config (post_merge_steps and its
siblings, the fallback gate keys, merge_requirements, the working-tree
declaration, the timeout and flag resolvers, the pre_checks readers) resolves
its file through `resolve_merge_config_path` / `resolve_merge_config_root`
here, which apply `repo_config.resolve_bound_config_root` with this verb's
vocabulary. Rules in that function: a config at the git tree wins; a config in
the directory holding the git tree counts only when it declares
``merge.git_working_tree`` as the one segment naming that tree; any other
config above the tree that holds post_merge_steps, gate keys or
merge_requirements is refused, never read as empty.

The vocabulary is imported at call time because the modules that own it import
this one.
"""

from __future__ import annotations

from pathlib import Path

from clagentic_loadout.merge.post_merge import PostMergeConfigError
from clagentic_loadout.repo_config import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    LEGACY_CONFIG_RELATIVE_PATH,
    ConfigRootRefusedError,
    resolve_bound_config_path,
    resolve_bound_config_root,
)


class MergeConfigRootRefusedError(PostMergeConfigError):
    """A config file above the git tree of ``--repo-path`` cannot be used. The
    message names the resolved tree, the config root and the tree's origin."""


def _vocabulary() -> dict:
    from clagentic_loadout.merge.gate_config import CONFIG_KEY_MERGE_REQUIREMENTS
    from clagentic_loadout.merge.post_merge_config import (
        CONFIG_KEY_GIT_WORKING_TREE,
        CONFIG_KEY_POST_MERGE_STEPS,
        CONFIG_SECTION_MERGE,
    )
    from clagentic_loadout.merge.repo_gate_runtime import GATE_KEYS

    return {
        "section": CONFIG_SECTION_MERGE,
        "declaration_key": CONFIG_KEY_GIT_WORKING_TREE,
        "guarded_keys": (CONFIG_KEY_POST_MERGE_STEPS, CONFIG_KEY_MERGE_REQUIREMENTS, *GATE_KEYS),
    }


def resolve_merge_config_root(
    repo_root: str | Path,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
    legacy_relative_path: str = LEGACY_CONFIG_RELATIVE_PATH,
) -> Path:
    """The directory whose config file the merge verbs read for *repo_root*.

    Raises:
        MergeConfigRootRefusedError: a config above the git tree is not usable.
    """
    try:
        return resolve_bound_config_root(
            repo_root,
            config_relative_path,
            legacy_relative_path,
            **_vocabulary(),
        )
    except ConfigRootRefusedError as exc:
        raise MergeConfigRootRefusedError(str(exc)) from exc


def resolve_merge_config_path(
    repo_root: str | Path,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
    legacy_relative_path: str = LEGACY_CONFIG_RELATIVE_PATH,
    warn: bool = True,
) -> Path:
    """The config file the merge verbs read for *repo_root*, with the legacy-path
    fallback of `repo_config.resolve_repo_config_path`.

    Raises:
        MergeConfigRootRefusedError: a config above the git tree is not usable.
    """
    try:
        return resolve_bound_config_path(
            repo_root,
            config_relative_path=config_relative_path,
            legacy_relative_path=legacy_relative_path,
            warn=warn,
            **_vocabulary(),
        )
    except ConfigRootRefusedError as exc:
        raise MergeConfigRootRefusedError(str(exc)) from exc


__all__ = [
    "MergeConfigRootRefusedError",
    "resolve_merge_config_path",
    "resolve_merge_config_root",
]
