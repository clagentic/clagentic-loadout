"""merge.tool_merge_tree -- a tool-owned tree for post-merge steps, so a merge never
moves a builder's checkout.

`merge.tree_sync.advance_repo_to_merged_sha` puts ``--repo-path`` on the merged
commit with ``git checkout --detach`` and `land_on_base_branch` then repoints the
base branch at it. That is right for a checkout that sits clean on the PR's base
branch (a shared deploy clone): nothing else lives there. It is wrong for a clone a
builder is working in: its branch, HEAD and uncommitted edits would move under
them. `unsafe_tree_reason` tells the two apart, and for the second kind the steps
run in a detached ``git worktree`` of the same repository under the loadout state
directory instead, keyed by repository and merged commit, removed when the steps
finish whether they passed or failed. The worktree is a second checkout of the
same object database, so it never touches ``--repo-path``'s branch, HEAD, index or
working tree (git does keep a small record of it under the common git directory
while it exists).

WHY A TOOL TREE AND NOT A REFUSAL: refusing would stop every merge whose
``--repo-path`` is a builder's clone, and a repository cannot declare a separate
tree to avoid that (``merge.git_working_tree`` must name ``--repo-path``'s own git
top-level). A tool tree meets the requirement with no per-repository change.

If the worktree cannot be made (old git, no disk, a leftover at the same path) the
tool raises `MergeTreeUnavailableError` before any step runs and never falls back to
moving ``--repo-path``.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from clagentic_loadout.artifact_store import StoreKind, resolve_root
from clagentic_loadout.merge.tree_sync import TreeSyncError

TREES_DIR_ENV_VAR = "CLAGENTIC_LOADOUT_MERGE_TREES_DIR"
CONFIG_SECTION = "merge"
CONFIG_KEY_TREES_DIR = "trees_dir"

_TREES_KIND = StoreKind(
    label="merge trees",
    env_var=TREES_DIR_ENV_VAR,
    section=CONFIG_SECTION,
    key=CONFIG_KEY_TREES_DIR,
    default_subdir="merge-trees",
)

#: Outcome name carried at the front of every refusal message from this module.
OUTCOME_TREE_UNAVAILABLE = "merge-tree-unavailable"


class MergeTreeUnavailableError(TreeSyncError):
    """The tool-owned tree for the post-merge steps could not be made or used.
    Raised before any step runs; ``--repo-path`` has not been touched."""


def _git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(cwd))


def unsafe_tree_reason(git_tree: str | Path, base_branch: str) -> str | None:
    """Why the checkout at *git_tree* must not be advanced, or None when it may.

    A checkout is advanced in place only when it is on *base_branch* (not another
    branch, not detached) and has no changes to tracked files. Anything git cannot
    answer counts as unsafe: the tool tree is the side that cannot hurt.
    """
    tree = Path(git_tree)
    head = _git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=tree)
    if head.returncode == 0:
        branch = head.stdout.strip()
        if branch != base_branch:
            return f"it is on branch {branch!r}, not the base branch {base_branch!r}"
    elif head.returncode == 1:
        return f"its HEAD is detached, not on the base branch {base_branch!r}"
    else:
        return f"its current branch could not be read (git exit {head.returncode})"
    status = _git(["status", "--porcelain", "--untracked-files=no"], cwd=tree)
    if status.returncode != 0:
        return f"its status could not be read (git exit {status.returncode})"
    if status.stdout.strip():
        return "it has changes to tracked files"
    return None


def trees_root(env: Mapping[str, str] | None = None) -> Path:
    """The directory tool trees are made under (not created here)."""
    return resolve_root(_TREES_KIND, env)


def _repo_key(git_tree: Path) -> str:
    """A stable, anonymous key for the repository *git_tree* belongs to: all
    checkouts and worktrees of one repository share a git common directory."""
    common = _git(["rev-parse", "--git-common-dir"], cwd=git_tree)
    if common.returncode != 0 or not common.stdout.strip():
        raise MergeTreeUnavailableError(
            f"{OUTCOME_TREE_UNAVAILABLE}: cannot identify the repository at {git_tree} "
            f"(git exit {common.returncode}); --repo-path was not touched"
        )
    resolved = (git_tree / common.stdout.strip()).resolve()
    return hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]


@contextmanager
def tool_merge_tree(
    git_tree: str | Path,
    merged_sha: str,
    *,
    env: Mapping[str, str] | None = None,
) -> Iterator[Path]:
    """Make a detached worktree of *git_tree*'s repository at *merged_sha* under the
    state directory, yield its path, and remove it afterwards (pass or fail).

    *merged_sha* must already be in the local object database (the caller fetched
    and verified it). Raises `MergeTreeUnavailableError`, having changed nothing in
    *git_tree*, when the tree cannot be made.
    """
    tree = Path(git_tree)
    root = trees_root(env)
    path = root / _repo_key(tree) / merged_sha
    if path.exists() or path.is_symlink():
        raise MergeTreeUnavailableError(
            f"{OUTCOME_TREE_UNAVAILABLE}: {path} already exists (a leftover from an "
            f"interrupted run, or a concurrent run on the same commit); remove it and "
            f"retry. --repo-path was not touched"
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MergeTreeUnavailableError(
            f"{OUTCOME_TREE_UNAVAILABLE}: cannot create {path.parent}: {exc}; "
            f"--repo-path was not touched"
        ) from exc
    _git(["worktree", "prune"], cwd=tree)
    added = _git(["worktree", "add", "--detach", str(path), merged_sha], cwd=tree)
    if added.returncode != 0:
        raise MergeTreeUnavailableError(
            f"{OUTCOME_TREE_UNAVAILABLE}: git worktree add failed (exit "
            f"{added.returncode}) for {path}: {added.stderr.strip()[:400]}; "
            f"--repo-path was not touched"
        )
    try:
        yield path
    finally:
        removed = _git(["worktree", "remove", "--force", str(path)], cwd=tree)
        _git(["worktree", "prune"], cwd=tree)
        if removed.returncode != 0 or path.exists():
            print(
                f"merge: WARNING -- tool tree {path} could not be removed (git exit "
                f"{removed.returncode}): {removed.stderr.strip()[:400]}",
                file=sys.stderr,
            )


def steps_dir_in_tree(tool_tree: Path, git_tree: str | Path, steps_dir: str | Path) -> Path:
    """Where in *tool_tree* a step that would run in *steps_dir* of the original
    checkout *git_tree* runs: the same path relative to the repository top-level, so
    relative paths in a step resolve to the merged content.

    Raises `MergeTreeUnavailableError` when the merged commit has no such directory.
    """
    top = _git(["rev-parse", "--show-toplevel"], cwd=Path(git_tree))
    if top.returncode != 0:
        raise MergeTreeUnavailableError(
            f"{OUTCOME_TREE_UNAVAILABLE}: cannot read the top-level of {git_tree} "
            f"(git exit {top.returncode})"
        )
    relative = os.path.relpath(Path(steps_dir).resolve(), Path(top.stdout.strip()).resolve())
    mapped = tool_tree / relative
    if not mapped.is_dir():
        raise MergeTreeUnavailableError(
            f"{OUTCOME_TREE_UNAVAILABLE}: the merged commit has no directory "
            f"{relative!r} to run the post-merge steps in"
        )
    return mapped


__all__ = [
    "MergeTreeUnavailableError",
    "OUTCOME_TREE_UNAVAILABLE",
    "TREES_DIR_ENV_VAR",
    "steps_dir_in_tree",
    "tool_merge_tree",
    "trees_root",
    "unsafe_tree_reason",
]
