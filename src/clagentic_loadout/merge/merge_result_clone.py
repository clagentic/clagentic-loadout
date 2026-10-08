"""merge.merge_result_clone -- a private clone holding what a PR would merge to.

The pre_checks gate must judge the code that lands, not whatever the shared
working tree happens to hold: a tree sitting on the base branch is red for a
test the PR itself fixes, and a tree with unrelated edits would leak them into
the check. This module builds the merge result in a throwaway clone of the
shared repository and hands its path to the caller. The clone is private: it has
its own `.git` (config, hooks, refs, index, objects), so nothing the check writes
in its own repository reaches the shared one.

  1. `git clone --shared --no-checkout` of the shared repository into a
     directory under the per-process temporary directory (`TMPDIR`). `--shared`
     borrows the shared object store read-only through an alternates file and
     writes new objects only into the clone; it is never a hardlinking clone.
  2. The shared repository's remote-tracking refs (`refs/remotes/origin/*`, and
     `origin/HEAD` when present) replace the clone's own, and
     `refs/remotes/origin/<base_branch>` is pinned to the PR base commit, so a
     check that diffs against `origin/<base>` sees the true base.
  3. The merge result is built before anything is checked out. A head that is a
     fast-forward of base is checked out as it is. Otherwise the merge is
     computed with `git merge-tree --write-tree` and COMMITTED in the clone (fixed
     synthetic author and committer, hooks off, no signing), so `HEAD` is the
     merge result and the tree is clean. A head that conflicts with base raises
     `MergeResultConflictError`: the PR is not mergeable, so no check can be run
     against its result.

The clone is removed in `finally` however the body exits. This module never
checks out, stashes, resets or otherwise touches the shared tree at *git_tree*;
it only reads from it.

Every git command run here carries `core.hooksPath=/dev/null` and
`core.fsmonitor=false` on the command line, so no hook, no `core.hooksPath` from
any config file and no fsmonitor helper executes, and runs with the git
repository/location selectors (`GIT_DIR`, `GIT_WORK_TREE`, ...) removed from the
environment so the repository is resolved from the working directory.

What this does NOT isolate: the shared repository and tree are no longer handed
to the checks, but a check runs as the merger's user and can still write to any
path it can name, such as the shared `.git` by absolute path or `~/.gitconfig`
through `HOME`, and can start daemons. Closing that needs a process sandbox,
which loadout does not provide.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping

from clagentic_loadout.merge.commit_files import CommitFileReadError, ensure_commit_present
from clagentic_loadout.merge.pre_check_env import without_git_location

_TREE_NAME = "tree"
_STDERR_EXCERPT = 400
_REMOTE = "origin"
_REMOTE_REFS = f"refs/remotes/{_REMOTE}/"
_REMOTE_HEAD = f"{_REMOTE_REFS}HEAD"
_MERGE_CONFLICT_EXIT = 1

#: Author and committer of the commit that records the merge result. The
#: address uses the reserved `.invalid` TLD so it can never be a real mailbox.
_SYNTHETIC_IDENTITY = {
    "GIT_AUTHOR_NAME": "loadout pre_checks",
    "GIT_AUTHOR_EMAIL": "pre-checks@loadout.invalid",
    "GIT_COMMITTER_NAME": "loadout pre_checks",
    "GIT_COMMITTER_EMAIL": "pre-checks@loadout.invalid",
}


class MergeResultCloneError(Exception):
    """The merge result could not be materialised."""


class MergeResultConflictError(MergeResultCloneError):
    """The PR head does not merge cleanly onto its base commit."""


#: Per-command configuration for every git command run here. A `core.hooksPath`
#: in any config file (which may resolve into the PR head's own tree), a hook in
#: a template directory and a `core.fsmonitor` helper would otherwise execute
#: during clone/checkout. Command-line `-c` outranks every config file.
#: Signing is off so the synthetic commit never asks for a key.
_NO_EXECUTION_CONFIG = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "commit.gpgsign=false",
)


def _command_env(env: Mapping[str, str] | None, *, identity: bool = False) -> dict[str, str]:
    base = without_git_location(os.environ if env is None else env)
    return {**base, **_SYNTHETIC_IDENTITY} if identity else base


def _git(
    args: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
    identity: bool = False,
    stdin: str | None = None,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *_NO_EXECUTION_CONFIG, *args],
            capture_output=True,
            text=True,
            errors="replace",
            cwd=str(cwd),
            env=_command_env(env, identity=identity),
            input=stdin,
        )
    except OSError as exc:
        raise MergeResultCloneError(f"git {args[0]} could not run in {cwd}: {exc}") from exc


def _require(result: subprocess.CompletedProcess, what: str) -> str:
    if result.returncode != 0:
        raise MergeResultCloneError(
            f"{what} failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout).strip()[:_STDERR_EXCERPT]}"
        )
    return result.stdout.strip()


def _source_remote_refs(git_tree: Path, env: Mapping[str, str] | None) -> tuple[dict[str, str], str]:
    """The shared repo's `refs/remotes/origin/*` (name -> object) and the target
    of its `origin/HEAD` symbolic ref ("" when absent)."""
    listed = _require(
        _git(
            ["for-each-ref", "--format=%(objectname) %(refname)", _REMOTE_REFS],
            cwd=git_tree,
            env=env,
        ),
        f"listing the remote-tracking refs of {git_tree}",
    )
    refs: dict[str, str] = {}
    for line in listed.splitlines():
        sha, _, name = line.partition(" ")
        if name and name != _REMOTE_HEAD:
            refs[name] = sha
    head = _git(["symbolic-ref", "--quiet", _REMOTE_HEAD], cwd=git_tree, env=env)
    target = head.stdout.strip() if head.returncode == 0 else ""
    return refs, target if target.startswith(_REMOTE_REFS) else ""


def _install_remote_refs(
    clone: Path,
    source_refs: dict[str, str],
    source_head: str,
    base_branch: str,
    base_sha: str,
    env: Mapping[str, str] | None,
) -> None:
    """Make the clone's remote-tracking refs the shared repo's, with
    `origin/<base_branch>` pinned to *base_sha*."""
    _git(["symbolic-ref", "--delete", "--quiet", _REMOTE_HEAD], cwd=clone, env=env)
    stale = _require(
        _git(["for-each-ref", "--format=%(refname)", _REMOTE_REFS], cwd=clone, env=env),
        "listing the clone's remote-tracking refs",
    )
    wanted = dict(source_refs)
    if base_branch:
        wanted[f"{_REMOTE_REFS}{base_branch}"] = base_sha
    commands = [
        f"delete {name}" for name in stale.splitlines() if name != _REMOTE_HEAD and name not in wanted
    ]
    commands += [f"update {name} {sha}" for name, sha in sorted(wanted.items())]
    if commands:
        _require(
            _git(["update-ref", "--stdin"], cwd=clone, env=env, stdin="\n".join(commands) + "\n"),
            "installing the remote-tracking refs in the clone",
        )
    if source_head:
        _require(
            _git(["symbolic-ref", _REMOTE_HEAD, source_head], cwd=clone, env=env),
            f"pointing {_REMOTE_HEAD} at {source_head}",
        )


def _checkout(clone: Path, commit: str, env: Mapping[str, str] | None) -> None:
    # --force: the clone was made with --no-checkout, so its index is empty and
    # an ordinary checkout would treat every file as a local change.
    _require(
        _git(["checkout", "--force", "--detach", "--quiet", commit], cwd=clone, env=env),
        f"checkout of {commit[:12]}",
    )


def _build_merge_result(
    clone: Path, base_sha: str, head_sha: str, env: Mapping[str, str] | None
) -> None:
    ancestry = _git(["merge-base", "--is-ancestor", base_sha, head_sha], cwd=clone, env=env)
    if ancestry.returncode == 0:
        _checkout(clone, head_sha, env)
        return
    if ancestry.returncode != 1:
        _require(ancestry, f"ancestry check of {base_sha[:12]} against {head_sha[:12]}")
    merged = _git(
        ["merge-tree", "--write-tree", "--no-messages", base_sha, head_sha], cwd=clone, env=env
    )
    if merged.returncode == _MERGE_CONFLICT_EXIT:
        raise MergeResultConflictError(
            f"PR head {head_sha[:12]} conflicts with base {base_sha[:12]}; the PR is not "
            f"mergeable, so its pre_checks cannot be run against a merge result"
        )
    tree = _require(merged, f"merge of PR head {head_sha[:12]} onto base {base_sha[:12]}").splitlines()[0]
    commit = _require(
        _git(
            [
                "commit-tree",
                tree,
                "-p",
                base_sha,
                "-p",
                head_sha,
                "-m",
                f"pre_checks merge result: {head_sha} onto {base_sha}",
            ],
            cwd=clone,
            env=env,
            identity=True,
        ),
        "committing the merge result",
    )
    _checkout(clone, commit, env)


def _materialise(
    clone: Path,
    git_tree: Path,
    base_sha: str,
    head_sha: str,
    base_branch: str,
    env: Mapping[str, str] | None,
) -> None:
    _require(
        _git(
            ["clone", "--shared", "--no-checkout", "--quiet", "--", str(git_tree.resolve()), str(clone)],
            cwd=clone.parent,
            env=env,
        ),
        f"git clone of {git_tree}",
    )
    refs, source_head = _source_remote_refs(git_tree, env)
    _install_remote_refs(clone, refs, source_head, base_branch, base_sha, env)
    _build_merge_result(clone, base_sha, head_sha, env)


def _make_removable(scratch: Path) -> None:
    """Give the owner full access to every directory under *scratch*: a check
    may have made one read-only or unreadable, which would stop its removal.
    Symbolic links are skipped so nothing outside *scratch* is changed."""
    for root, dirs, _files in os.walk(scratch):
        for directory in [Path(root), *(Path(root) / name for name in dirs)]:
            if not directory.is_symlink():
                try:
                    os.chmod(directory, 0o700)
                except OSError:
                    pass


def _remove(scratch: Path) -> None:
    _make_removable(scratch)
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
def merge_result_clone(
    git_tree: str | Path,
    base_sha: str,
    head_sha: str,
    *,
    base_branch: str = "",
    env: Mapping[str, str] | None = None,
) -> Iterator[Path]:
    """Yield the path of a private clone whose `HEAD` is the result of merging
    *head_sha* onto *base_sha*, and remove it on exit however the body ends.

    *base_branch* names the branch whose `refs/remotes/origin/<base_branch>` is
    pinned to *base_sha* in the clone. Every git command this runs has hooks,
    fsmonitor and signing disabled, has the repository/location selector
    variables removed, and, when *env* is given, runs with that environment
    instead of the inherited one.

    Raises:
        MergeResultConflictError: the head conflicts with base.
        MergeResultCloneError: a commit is unavailable or git failed.
    """
    git_tree = Path(git_tree)
    try:
        for sha in (base_sha, head_sha):
            ensure_commit_present(git_tree, sha, base_branch=base_branch)
    except CommitFileReadError as exc:
        raise MergeResultCloneError(str(exc)) from exc
    scratch = Path(tempfile.mkdtemp(prefix="loadout-precheck-"))
    try:
        clone = scratch / _TREE_NAME
        _materialise(clone, git_tree, base_sha, head_sha, base_branch, env)
        yield clone
    finally:
        _remove(scratch)


__all__ = ["MergeResultCloneError", "MergeResultConflictError", "merge_result_clone"]
