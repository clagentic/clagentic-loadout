"""merge.config_binding -- bind a ``--repo-path`` tree to the repository a merge
verb was asked to act on, before the tree supplies anything that executes.

Two inputs are read from the deployment file of ``--repo-path`` and are not
content-addressed: the gate keys used when the base commit has no tracked gate
file, and ``post_merge_steps`` (commands the verb then runs). Both are bound
here by one read of the tree's ``origin`` (git_origin): the host must equal the
host the platform is configured for, and owner/name must equal ``--repo``,
case-insensitively. The SSH and HTTPS remotes of one host both pass. A tree
whose origin cannot be read or parsed is refused rather than passed.

``--repo-path`` must itself be inside a git working tree (`require_repo_path_in_git_tree`):
the one git top-level is the tree that is confirmed, read the gate from and synced.

Which file is read is decided by ``merge.config_root`` (the tree's own, or the
direct parent's when it declares the tree), so the file read is always one the
confirmed tree vouches for.

The tracked gate file is read at the PR base commit and is not bound here.
"""

from __future__ import annotations

from pathlib import Path

from clagentic_loadout.git_origin import ConfigTreeBinding, OriginBindingError
from clagentic_loadout.merge.config_root import (
    MergeConfigRootRefusedError,
    resolve_merge_config_path,
    resolve_merge_config_root,
)
from clagentic_loadout.merge.errors import MergeUsageError
from clagentic_loadout.merge.post_merge_config import (
    CONFIG_KEY_GIT_WORKING_TREE,
    PostMergeConfigError,
    load_post_merge_steps,
    read_repo_merge_section,
)
from clagentic_loadout.platform_detect import DEFAULT_GITHUB_HOSTNAME, PLATFORM_GITHUB
from clagentic_loadout.repo_config import find_git_top_level


def config_binding_for(platform: str, git_host_base: str, repo_slug: str) -> ConfigTreeBinding:
    """The binding for *repo_slug* on *platform*: github.com for GitHub, the
    host of the resolved git-host base URL for Forgejo."""
    host = DEFAULT_GITHUB_HOSTNAME if platform == PLATFORM_GITHUB else git_host_base
    return ConfigTreeBinding(expected_host=host, slug=repo_slug)


def _declared_tree_segment(repo_path: str) -> str | None:
    """The single path segment *repo_path*'s own config declares as
    ``merge.git_working_tree``, or None when it declares none or cannot be read.
    Used only to word a hint, so a file that cannot be read is simply no hint."""
    try:
        config_path = resolve_merge_config_path(repo_path, warn=False)
        declared = read_repo_merge_section(config_path).get(CONFIG_KEY_GIT_WORKING_TREE)
    except (PostMergeConfigError, OSError):
        return None
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    return None


def require_repo_path_in_git_tree(repo_path: str) -> None:
    """Raise MergeUsageError when *repo_path* is not inside a git working tree.

    ``--repo-path`` names the git tree itself: the tree the origin is confirmed
    for, the gate is read from, the tree sync advances and the post-merge steps
    run in. A directory that only holds the tree (a wrapper) is no longer
    accepted in its place; its config is still found, one level up, from the
    tree. When the directory declares ``merge.git_working_tree``, the message
    names ``<directory>/<that segment>`` as the path to pass.

    Reads no remote, so no remote text can reach the message. Run first, before
    the origin binding and before any credential or network call, whatever the
    skip flags say.
    """
    if find_git_top_level(repo_path) is not None:
        return
    resolved = Path(repo_path).resolve()
    segment = _declared_tree_segment(repo_path)
    hint = (
        f" This directory declares merge.git_working_tree {segment!r}: pass "
        f"{str(resolved / segment)!r} instead."
        if segment is not None
        else ""
    )
    raise MergeUsageError(
        f"--repo-path {str(resolved)!r} is not inside a git working tree. "
        "--repo-path must be the git tree itself, not a directory that holds it."
        f"{hint}"
    )


def require_confirmed_tree(binding: ConfigTreeBinding, repo_path: str, *, reading: str) -> None:
    """Raise MergeUsageError, naming the resolved tree, the remote name origin
    and the parsed host and owner/name (never the remote's URL text), when
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


def require_resolvable_config_root(repo_path: str) -> None:
    """Raise MergeUsageError when a config file above the git tree of
    *repo_path* holds merge settings that cannot be accepted (see
    `merge.config_root`). The message names the resolved tree, the config root
    and the parsed origin host and owner/name. Run before anything is merged or checked out, so the
    refusal is never a half-finished merge."""
    try:
        resolve_merge_config_root(repo_path)
    except MergeConfigRootRefusedError as exc:
        raise MergeUsageError(f"--repo-path {repo_path!r} is refused: {exc}") from exc


def require_confirmed_tree_for_steps(binding: ConfigTreeBinding, repo_path: str) -> None:
    """Refuse, before anything is merged or checked out, a *repo_path* whose
    config cannot be accepted, or that declares ``post_merge_steps`` but is not
    a confirmed clone.

    A tree that declares no steps runs nothing, so it is not asked to prove
    where it came from. A declaration this cannot read is left to the loader
    that reports it with its own exit code."""
    require_resolvable_config_root(repo_path)
    try:
        steps = load_post_merge_steps(repo_path)
    except PostMergeConfigError:
        return
    if not steps:
        return
    require_confirmed_tree(binding, repo_path, reading="post_merge_steps")


__all__ = [
    "config_binding_for",
    "require_confirmed_tree",
    "require_confirmed_tree_for_steps",
    "require_repo_path_in_git_tree",
    "require_resolvable_config_root",
]
