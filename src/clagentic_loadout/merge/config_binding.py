"""merge.config_binding -- bind a ``--repo-path`` tree to the repository a merge
verb was asked to act on, before the tree supplies anything that executes.

Two inputs are read from the deployment file of ``--repo-path`` and are not
content-addressed: the gate keys used when the base commit has no tracked gate
file, and ``post_merge_steps`` (commands the verb then runs). Both are bound
here by one read of the tree's ``origin`` (git_origin): the host must equal the
host the platform is configured for, and owner/name must equal ``--repo``,
case-insensitively. The SSH and HTTPS remotes of one host both pass. A tree
whose origin cannot be read or parsed is refused rather than passed.

The tracked gate file is read at the PR base commit and is not bound here.
"""

from __future__ import annotations

from clagentic_loadout.git_origin import ConfigTreeBinding, OriginBindingError
from clagentic_loadout.merge.errors import MergeUsageError
from clagentic_loadout.merge.post_merge_config import (
    PostMergeConfigError,
    load_post_merge_steps,
)
from clagentic_loadout.platform_detect import DEFAULT_GITHUB_HOSTNAME, PLATFORM_GITHUB


def config_binding_for(platform: str, git_host_base: str, repo_slug: str) -> ConfigTreeBinding:
    """The binding for *repo_slug* on *platform*: github.com for GitHub, the
    host of the resolved git-host base URL for Forgejo."""
    host = DEFAULT_GITHUB_HOSTNAME if platform == PLATFORM_GITHUB else git_host_base
    return ConfigTreeBinding(expected_host=host, slug=repo_slug)


def require_confirmed_tree(binding: ConfigTreeBinding, repo_path: str, *, reading: str) -> None:
    """Raise MergeUsageError, naming the resolved tree, remote and host, when
    *repo_path* is not a confirmed clone for *binding*. *reading* names what
    would have been read from it.

    The tree confirmed is the git top-level of *repo_path* itself, the same
    tree the config is read from. A config file never gets to name a different
    tree to be confirmed in its place."""
    try:
        binding.confirm(repo_path)
    except OriginBindingError as exc:
        raise MergeUsageError(
            f"--repo-path {repo_path!r} is refused as the source of {reading}: {exc}. "
            "Point --repo-path at a clone of the repository on the configured host."
        ) from exc


def require_confirmed_tree_for_steps(binding: ConfigTreeBinding, repo_path: str) -> None:
    """Refuse, before anything is merged or checked out, a *repo_path* that
    declares ``post_merge_steps`` but is not a confirmed clone.

    A tree that declares no steps runs nothing, so it is not asked to prove
    where it came from. A declaration this cannot read is left to the loader
    that reports it with its own exit code."""
    try:
        steps = load_post_merge_steps(repo_path, wrapper_hop=False)
    except PostMergeConfigError:
        return
    if not steps:
        return
    require_confirmed_tree(binding, repo_path, reading="post_merge_steps")


__all__ = [
    "config_binding_for",
    "require_confirmed_tree",
    "require_confirmed_tree_for_steps",
]
