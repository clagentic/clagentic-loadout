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

If the worktree cannot be made (old git, no disk, a leftover at the same path, a
submodule that will not check out) the tool raises `MergeTreeUnavailableError` before
any step runs and never falls back to moving ``--repo-path``.

THE TREE IS EPHEMERAL: it holds the merged commit's tracked content only (plus its
submodules), is removed once the steps finish, and so must never be the place a step
installs to or leaves a process running from. For that reason `refuse_detached_steps`
refuses a ``detaches: true`` step before any step runs. Untracked files in ``--repo-path``
(``.env``, ``node_modules``, a virtualenv) are the builder's state, not merged
content, and are deliberately not copied in.

A step that names ``--repo-path`` as an absolute path would act on the builder's
checkout, not on the merged content, and install stale code with a passing exit. So
`refuse_steps_naming_repo_path` refuses such a step before any step runs. Every step
is given the tree it runs in as ``CLAGENTIC_LOADOUT_MERGE_TREE`` (`merge_tree_env`).
"""

from __future__ import annotations

import hashlib
import os
import re
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

#: Outcome names carried at the front of every refusal message from this module.
OUTCOME_TREE_UNAVAILABLE = "merge-tree-unavailable"
OUTCOME_STEP_NAMES_REPO_PATH = "merge-tree-step-names-repo-path"
OUTCOME_DETACHED_STEP = "merge-tree-detached-step"

#: Exported to every post-merge step: the absolute path of the tree the steps run in
#: (the repository top-level when --repo-path is a clean base checkout, the tool tree
#: otherwise).
ENV_MERGE_TREE = "CLAGENTIC_LOADOUT_MERGE_TREE"

#: Appended to a step's failure message when it ran in the tool tree, because the
#: tree holds tracked content only and a step that needed an untracked file fails here.
TOOL_TREE_FAILURE_NOTE = (
    " [this step ran in a fresh tree of the merged commit's tracked content, so "
    "untracked files from --repo-path (.env, node_modules, a virtualenv) are not present]"
)

#: Step keys whose values are commands that run in the tree.
_COMMAND_KEYS = ("cmd", "verify", "liveness_probe")


class MergeTreeUnavailableError(TreeSyncError):
    """The tool-owned tree for the post-merge steps could not be made or used.
    Raised before any step runs; ``--repo-path`` has not been touched."""


class StepNamesRepoPathError(TreeSyncError):
    """A post-merge step names ``--repo-path``'s git top-level as an absolute path
    while the steps run in the tool tree. Raised before any step runs;
    ``--repo-path`` has not been touched."""


class DetachedStepInToolTreeError(TreeSyncError):
    """A post-merge step sets ``detaches: true`` while the steps run in the tool
    tree. Raised before any step runs; ``--repo-path`` has not been touched."""


def _git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(cwd))


def unsafe_tree_reason(
    git_tree: str | Path, base_branch: str, target: str
) -> str | None:
    """Why the checkout at *git_tree* must not be advanced, or None when it may.

    A checkout is advanced in place only when it is on *base_branch* (not another
    branch, not detached), has no changes to tracked files, and its HEAD is an
    ancestor of *target* (the commit it would be moved to). A HEAD with commits
    outside *target* would lose them from the branch when the branch is reset to
    *target*. Anything git cannot answer, including a *target* missing from the
    local object database, counts as unsafe: the tool tree is the side that cannot
    hurt.
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
    ancestry = _git(["merge-base", "--is-ancestor", "HEAD", target], cwd=tree)
    if ancestry.returncode == 1:
        return f"it has local commits not in {target}"
    if ancestry.returncode != 0:
        return (
            f"whether its HEAD is contained in {target} could not be read "
            f"(git exit {ancestry.returncode})"
        )
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


def _is_registered_worktree(tree: Path, path: Path) -> bool:
    """True when git lists exactly *path* as a linked worktree of *tree*'s repository."""
    listed = _git(["worktree", "list", "--porcelain"], cwd=tree)
    if listed.returncode != 0:
        return False
    wanted = {str(path), str(path.resolve())}
    return any(
        line.startswith("worktree ") and line[len("worktree "):] in wanted
        for line in listed.stdout.splitlines()
    )


def _clear_own_stale_registration(tree: Path, path: Path) -> None:
    """Drop a registration left at the tool tree's own *path* by a crash between
    add and remove. It acts on that one path only: a repository-wide prune would
    also delete registrations of the builder's own worktrees whose directories are
    missing (an unmounted disk), which this module must never touch."""
    if _is_registered_worktree(tree, path):
        _git(["worktree", "remove", "--force", str(path)], cwd=tree)


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
    _clear_own_stale_registration(tree, path)
    added = _git(["worktree", "add", "--detach", str(path), merged_sha], cwd=tree)
    if added.returncode != 0:
        raise MergeTreeUnavailableError(
            f"{OUTCOME_TREE_UNAVAILABLE}: git worktree add failed (exit "
            f"{added.returncode}) for {path}: {added.stderr.strip()[:400]}; "
            f"--repo-path was not touched"
        )
    try:
        if (path / ".gitmodules").is_file():
            # A submodule pin is merged content: a step that reads it needs it
            # checked out, and an empty directory would pass silently.
            submodules = _git(["submodule", "update", "--init", "--recursive"], cwd=path)
            if submodules.returncode != 0:
                raise MergeTreeUnavailableError(
                    f"{OUTCOME_TREE_UNAVAILABLE}: git submodule update failed (exit "
                    f"{submodules.returncode}) in {path}: "
                    f"{submodules.stderr.strip()[:400]}; no step ran and --repo-path "
                    f"was not touched"
                )
        yield path
    finally:
        removed = _git(["worktree", "remove", "--force", str(path)], cwd=tree)
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


def _command_strings(value: object) -> Iterator[str]:
    """Every string in a step's command value (a string, a list, or a mapping that
    holds either)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _command_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _command_strings(item)


def _repo_path_spellings(git_tree: Path, top: str) -> list[str]:
    """The ways *git_tree*'s top-level can be written as an absolute path: as given
    and by realpath, without a trailing slash. The filesystem root is never one."""
    candidates = [
        str(git_tree),
        top,
        os.path.realpath(top),
        os.path.realpath(git_tree),
    ]
    seen: list[str] = []
    for candidate in candidates:
        trimmed = candidate.rstrip("/")
        if trimmed and trimmed not in seen:
            seen.append(trimmed)
    return seen


def refuse_steps_naming_repo_path(steps: list[dict], git_tree: str | Path) -> None:
    """Raise `StepNamesRepoPathError` when any step's command names *git_tree*'s
    top-level as an absolute path, in a string command, a list command, or text
    inside a ``bash -c`` string. Also covers a step's ``verify`` and
    ``liveness_probe`` commands, which run in the same tree.

    The match is on a path boundary: ``/x/repo`` matches ``/x/repo`` and
    ``/x/repo/sub`` but not ``/x/repo-other`` or ``/y/x/repo``. A command spelled
    ``-C/x/repo`` (no separator before the path) is not recognised.
    """
    tree = Path(git_tree)
    top = _git(["rev-parse", "--show-toplevel"], cwd=tree)
    if top.returncode != 0:
        raise MergeTreeUnavailableError(
            f"{OUTCOME_TREE_UNAVAILABLE}: cannot read the top-level of {git_tree} "
            f"(git exit {top.returncode})"
        )
    for path in _repo_path_spellings(tree, top.stdout.strip()):
        pattern = re.compile(r"(?<![\w.~])" + re.escape(path) + r"(?![\w.-])")
        for index, step in enumerate(steps, start=1):
            for key in _COMMAND_KEYS:
                for text in _command_strings(step.get(key)):
                    if pattern.search(text):
                        label = step.get("description") or text
                        raise StepNamesRepoPathError(
                            f"{OUTCOME_STEP_NAMES_REPO_PATH}: post-merge step {index} "
                            f"({label!r}) names the checkout path {path} as an absolute "
                            f"path, but --repo-path is not a clean checkout of the base "
                            f"branch, so the steps run in a tool-owned tree and that path "
                            f"would act on the stale checkout instead of the merged "
                            f"content. Make the step relative to the repository "
                            f"top-level, use {ENV_MERGE_TREE}, or merge from a clean "
                            f"base checkout. No step ran and --repo-path was not touched"
                        )


def refuse_detached_steps(steps: list[dict]) -> None:
    """Raise `DetachedStepInToolTreeError` when any step sets ``detaches: true``.

    A detached step outlives the steps run, but the tool tree is removed as soon as
    they finish, which would delete the code and working directory of the process it
    started while the step still reported success. The tree does not support such a
    step; it must run from a clean base checkout.
    """
    for index, step in enumerate(steps, start=1):
        if step.get("detaches") is True:
            label = step.get("description") or step.get("cmd")
            raise DetachedStepInToolTreeError(
                f"{OUTCOME_DETACHED_STEP}: post-merge step {index} ({label!r}) sets "
                f"detaches: true, but --repo-path is not a clean checkout of the base "
                f"branch, so the steps run in a tool-owned tree that is removed when "
                f"they finish. A detached step must run from a clean base checkout, or "
                f"install outside the tree and detach from there. No step ran and "
                f"--repo-path was not touched"
            )


def merge_tree_env(
    overrides: Mapping[str, str] | None, tree: str | Path
) -> dict[str, str]:
    """*overrides* plus ``CLAGENTIC_LOADOUT_MERGE_TREE`` set to the absolute path of
    *tree*, the tree the steps run in. It wins over a deployment override of the
    same name, so a step always reads the tree it actually runs in."""
    return {**(overrides or {}), ENV_MERGE_TREE: str(Path(tree).resolve())}


__all__ = [
    "ENV_MERGE_TREE",
    "DetachedStepInToolTreeError",
    "MergeTreeUnavailableError",
    "OUTCOME_DETACHED_STEP",
    "OUTCOME_STEP_NAMES_REPO_PATH",
    "OUTCOME_TREE_UNAVAILABLE",
    "StepNamesRepoPathError",
    "TOOL_TREE_FAILURE_NOTE",
    "TREES_DIR_ENV_VAR",
    "merge_tree_env",
    "refuse_detached_steps",
    "refuse_steps_naming_repo_path",
    "steps_dir_in_tree",
    "tool_merge_tree",
    "trees_root",
    "unsafe_tree_reason",
]
