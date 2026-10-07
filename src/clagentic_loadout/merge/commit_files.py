"""merge.commit_files -- read a tracked file as it exists at a given commit.

The merge gate must judge a PR by the repo's declaration at the PR's BASE
commit, never by whatever the shared working tree happens to hold: a tree
sitting on the PR head would otherwise let the PR relax its own gate. This
module is the git half of that rule: make sure the commit is present locally
(fetching it by SHA when it is not), then `git show <sha>:<path>`.

ABSENT IS NOT AN ERROR. A path that is not in the commit's tree returns None,
so a repo that has not yet landed the file reads as "declares nothing". Every
other failure (not a git tree, the commit cannot be fetched, git errors)
raises `CommitFileReadError` so the caller can degrade loudly instead of
treating an unreadable commit as an empty one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from clagentic_loadout.push.git_coords import tracking_remote


class CommitFileReadError(Exception):
    """A commit or a file at that commit could not be read."""


def _git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(cwd))
    except OSError as exc:
        raise CommitFileReadError(f"git {args[0]} could not run in {cwd}: {exc}") from exc


def ensure_commit_present(git_tree: str | Path, sha: str, *, base_branch: str = "") -> None:
    """Guarantee *sha* names a commit in *git_tree*'s object database.

    Fetches it from the base branch's tracking remote when it is missing.

    Raises:
        CommitFileReadError: *git_tree* is not a git tree, or the commit is
            absent locally and cannot be fetched.
    """
    git_tree = Path(git_tree)
    if not sha or sha.startswith("-"):
        raise CommitFileReadError(f"{sha!r} is not a usable commit SHA")
    probe = _git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=git_tree)
    if probe.returncode == 0:
        return
    remote = tracking_remote(base_branch, git_tree) if base_branch else "origin"
    fetched = _git(["fetch", remote, sha], cwd=git_tree)
    if fetched.returncode != 0:
        raise CommitFileReadError(
            f"commit {sha} is not present in {git_tree} and `git fetch {remote} {sha}` "
            f"failed (exit {fetched.returncode}): {fetched.stderr.strip()[:400]}"
        )
    recheck = _git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=git_tree)
    if recheck.returncode != 0:
        raise CommitFileReadError(
            f"commit {sha} is still not a commit in {git_tree} after fetching it from {remote}"
        )


def read_file_at_commit(
    git_tree: str | Path, sha: str, relative_path: str, *, base_branch: str = ""
) -> str | None:
    """Return *relative_path*'s text at commit *sha*, or None when the commit's
    tree does not contain it.

    *relative_path* is relative to the repository root.

    Raises:
        CommitFileReadError: see `ensure_commit_present`; or git could not list
            or show the path.
    """
    git_tree = Path(git_tree)
    ensure_commit_present(git_tree, sha, base_branch=base_branch)
    listed = _git(["ls-tree", "--full-tree", "--name-only", sha, "--", relative_path], cwd=git_tree)
    if listed.returncode != 0:
        raise CommitFileReadError(
            f"git ls-tree {sha} {relative_path} failed (exit {listed.returncode}) in "
            f"{git_tree}: {listed.stderr.strip()[:400]}"
        )
    if not listed.stdout.strip():
        return None
    shown = _git(["show", f"{sha}:{relative_path}"], cwd=git_tree)
    if shown.returncode != 0:
        raise CommitFileReadError(
            f"git show {sha}:{relative_path} failed (exit {shown.returncode}) in "
            f"{git_tree}: {shown.stderr.strip()[:400]}"
        )
    return shown.stdout


__all__ = ["CommitFileReadError", "ensure_commit_present", "read_file_at_commit"]
