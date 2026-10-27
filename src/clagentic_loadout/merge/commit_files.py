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

from clagentic_loadout.merge.fetch_transport import (
    MergeFetchAuth,
    MergeFetchDeniedError,
    MergeFetchFailedError,
    fetch_from_tracking_remote,
)


class CommitFileReadError(Exception):
    """A commit or a file at that commit could not be read."""


class CommitFetchDeniedError(MergeFetchDeniedError, CommitFileReadError):
    """The commit is absent and the fetch that would supply it was refused
    before any network call. Both kinds, so a caller reading "could not read the
    commit" still catches it while one that must not degrade a steered remote can
    tell it apart."""


def _git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    # errors="replace": git's own diagnostics must never raise while being read.
    # File content is fetched as bytes (`_git_bytes`) and decoded strictly.
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, errors="replace", cwd=str(cwd)
        )
    except OSError as exc:
        raise CommitFileReadError(f"git {args[0]} could not run in {cwd}: {exc}") from exc


def _git_bytes(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], capture_output=True, cwd=str(cwd))
    except OSError as exc:
        raise CommitFileReadError(f"git {args[0]} could not run in {cwd}: {exc}") from exc


def resolve_commit(git_tree: str | Path, ref: str) -> str | None:
    """The full SHA *ref* names in *git_tree*, or None when it names no commit
    there (not a git tree, an unborn branch, an unknown ref). Never fetches, so a
    read-only caller can tell an absent history from a readable one without
    touching the network."""
    if not ref or ref.startswith("-"):
        return None
    try:
        resolved = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=Path(git_tree))
    except CommitFileReadError:
        return None
    return resolved.stdout.strip() or None if resolved.returncode == 0 else None


def ensure_commit_present(
    git_tree: str | Path,
    sha: str,
    *,
    base_branch: str = "",
    fetch_auth: MergeFetchAuth | None = None,
) -> None:
    """Guarantee *sha* names a commit in *git_tree*'s object database.

    Fetches it from the base branch's tracking remote when it is missing, through
    `merge.fetch_transport` with *fetch_auth* (None allows local remotes only).

    Raises:
        CommitFileReadError: *git_tree* is not a git tree, or the commit is
            absent locally and cannot be fetched.
        CommitFetchDeniedError: the tracking remote's destination was refused
            before any network call. Also a MergeFetchDeniedError, which a caller
            that degrades an unreadable commit must re-raise instead (see
            `merge.repo_gate_runtime.load_gate_at_commit`).
    """
    git_tree = Path(git_tree)
    if not sha or sha.startswith("-"):
        raise CommitFileReadError(f"{sha!r} is not a usable commit SHA")
    probe = _git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=git_tree)
    if probe.returncode == 0:
        return
    # Outside a git tree no remote exists to judge or fetch from: that is an
    # unreadable commit (a caller may degrade it), not a refused destination.
    if _git(["rev-parse", "--git-dir"], cwd=git_tree).returncode != 0:
        raise CommitFileReadError(f"commit {sha} is not present: {git_tree} is not a git tree")
    try:
        fetch_from_tracking_remote(git_tree, base_branch, sha, fetch_auth)
    except MergeFetchDeniedError as exc:
        raise CommitFetchDeniedError(f"commit {sha} is not present in {git_tree}: {exc}") from exc
    except MergeFetchFailedError as exc:
        # git's fetch diagnostics quote the remote URL, query string included, and
        # the tracking remote is whatever config holds (a URL, or a name written as
        # `<secret>@host/path`), so neither is printed: the transport's message
        # carries only the exit code.
        raise CommitFileReadError(
            f"commit {sha} is not present in {git_tree} and fetching it from the "
            f"branch's tracking remote failed ({exc})"
        ) from exc
    recheck = _git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=git_tree)
    if recheck.returncode != 0:
        raise CommitFileReadError(
            f"commit {sha} is still not a commit in {git_tree} after fetching it from "
            f"the branch's tracking remote"
        )


def read_file_at_commit(
    git_tree: str | Path,
    sha: str,
    relative_path: str,
    *,
    base_branch: str = "",
    fetch_auth: MergeFetchAuth | None = None,
) -> str | None:
    """Return *relative_path*'s text at commit *sha*, or None when the commit's
    tree does not contain it.

    *relative_path* is relative to the repository root.

    Raises:
        CommitFileReadError: see `ensure_commit_present`; or git could not list
            or show the path; or the content is not valid UTF-8.
    """
    git_tree = Path(git_tree)
    ensure_commit_present(git_tree, sha, base_branch=base_branch, fetch_auth=fetch_auth)
    listed = _git(["ls-tree", "--full-tree", "--name-only", sha, "--", relative_path], cwd=git_tree)
    if listed.returncode != 0:
        raise CommitFileReadError(
            f"git ls-tree {sha} {relative_path} failed (exit {listed.returncode}) in "
            f"{git_tree}: {listed.stderr.strip()[:400]}"
        )
    if not listed.stdout.strip():
        return None
    shown = _git_bytes(["show", f"{sha}:{relative_path}"], cwd=git_tree)
    if shown.returncode != 0:
        raise CommitFileReadError(
            f"git show {sha}:{relative_path} failed (exit {shown.returncode}) in "
            f"{git_tree}: {shown.stderr.decode('utf-8', 'replace').strip()[:400]}"
        )
    try:
        return shown.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CommitFileReadError(
            f"{sha}:{relative_path} is not valid UTF-8 text in {git_tree}: {exc}"
        ) from exc


__all__ = [
    "CommitFetchDeniedError",
    "CommitFileReadError",
    "ensure_commit_present",
    "read_file_at_commit",
    "resolve_commit",
]
