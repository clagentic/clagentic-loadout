"""merge.merge_result_worktree -- a throwaway checkout of what a PR would merge to.

The pre_checks gate must judge the code that lands, not whatever the shared
working tree happens to hold: a tree sitting on the base branch is red for a
test the PR itself fixes, and a tree with unrelated edits would leak them into
the check. This module builds the merge result in a disposable detached
worktree and hands its path to the caller:

  1. `git worktree add --detach` at the PR base commit.
  2. The PR head is checked out when it is a fast-forward of base; otherwise it
     is merged onto base (`git merge --no-commit --no-ff`), leaving the merge
     result in the worktree without creating a commit.
  3. A head that conflicts with base raises `MergeResultConflictError`: the PR
     is not mergeable, so no check can be run against its result.

The worktree lives under the per-process temporary directory (`TMPDIR`), never
inside the repository, and removal is attempted (`git worktree remove --force`,
then `git worktree prune`) however the body exits. This module does not check
out, stash or reset the shared tree at *git_tree*; the git operations it runs
write there only the worktree's own administrative entry, which the cleanup
removes.

What this does NOT isolate: a linked worktree shares the repository's git
common directory with *git_tree*, so code running in the worktree can write
there (hooks, `config`, `info/`, refs, objects). This module does not prevent
that. The merge verb wraps the checks in `merge.common_dir_guard`, which
detects and undoes changes to `hooks/`, `info/`, `config` and `config.worktree`
only; the rest of the common directory is neither guarded nor restored. The
worktree is also not a process sandbox: code in it runs as the merger's user.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from clagentic_loadout.merge.commit_files import CommitFileReadError, ensure_commit_present

_TREE_NAME = "tree"
_STDERR_EXCERPT = 400


class MergeResultWorktreeError(Exception):
    """The merge result could not be materialised."""


class MergeResultConflictError(MergeResultWorktreeError):
    """The PR head does not merge cleanly onto its base commit."""


def _git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, errors="replace", cwd=str(cwd)
        )
    except OSError as exc:
        raise MergeResultWorktreeError(f"git {args[0]} could not run in {cwd}: {exc}") from exc


def _require(result: subprocess.CompletedProcess, what: str) -> None:
    if result.returncode != 0:
        raise MergeResultWorktreeError(
            f"{what} failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout).strip()[:_STDERR_EXCERPT]}"
        )


def _materialise(worktree: Path, git_tree: Path, base_sha: str, head_sha: str) -> None:
    _require(
        _git(["worktree", "add", "--detach", str(worktree), base_sha], cwd=git_tree),
        f"git worktree add at {base_sha[:12]}",
    )
    fast_forward = _git(["merge-base", "--is-ancestor", base_sha, head_sha], cwd=worktree)
    if fast_forward.returncode == 0:
        _require(
            _git(["checkout", "--detach", "--quiet", head_sha], cwd=worktree),
            f"checkout of PR head {head_sha[:12]}",
        )
        return
    merged = _git(
        ["merge", "--no-commit", "--no-ff", "--quiet", head_sha], cwd=worktree
    )
    if merged.returncode == 0:
        return
    unmerged = _git(["ls-files", "--unmerged"], cwd=worktree)
    if unmerged.returncode == 0 and unmerged.stdout.strip():
        raise MergeResultConflictError(
            f"PR head {head_sha[:12]} conflicts with base {base_sha[:12]}; the PR is not "
            f"mergeable, so its pre_checks cannot be run against a merge result"
        )
    _require(merged, f"merge of PR head {head_sha[:12]} onto base {base_sha[:12]}")


def _remove(worktree: Path, scratch: Path, git_tree: Path) -> None:
    removed = _git(["worktree", "remove", "--force", str(worktree)], cwd=git_tree)
    if removed.returncode != 0 and worktree.exists():
        print(
            f"merge: pre_checks worktree {str(worktree)!r} could not be removed by git "
            f"(exit {removed.returncode}): {removed.stderr.strip()[:_STDERR_EXCERPT]}",
            file=sys.stderr,
        )
    _git(["worktree", "prune"], cwd=git_tree)
    try:
        shutil.rmtree(scratch)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(
            f"merge: pre_checks scratch directory {str(scratch)!r} could not be removed: {exc}",
            file=sys.stderr,
        )


@contextmanager
def merge_result_worktree(
    git_tree: str | Path, base_sha: str, head_sha: str, *, base_branch: str = ""
) -> Iterator[Path]:
    """Yield the path of a detached worktree holding the result of merging
    *head_sha* onto *base_sha*, and remove it on exit however the body ends.

    Raises:
        MergeResultConflictError: the head conflicts with base.
        MergeResultWorktreeError: a commit is unavailable or git failed.
    """
    git_tree = Path(git_tree)
    try:
        for sha in (base_sha, head_sha):
            ensure_commit_present(git_tree, sha, base_branch=base_branch)
    except CommitFileReadError as exc:
        raise MergeResultWorktreeError(str(exc)) from exc
    scratch = Path(tempfile.mkdtemp(prefix="loadout-precheck-"))
    worktree = scratch / _TREE_NAME
    try:
        _materialise(worktree, git_tree, base_sha, head_sha)
        yield worktree
    finally:
        _remove(worktree, scratch, git_tree)


__all__ = ["MergeResultConflictError", "MergeResultWorktreeError", "merge_result_worktree"]
