"""merge.premerge_steps -- the post_merge_steps a PR will run once merged.

Before the server-side merge there is no merged commit yet, but the steps that
run afterwards are the ones the merged commit declares (verb step 10 replaces
the working-tree list with them). A refusal decided before the merge has to
judge those same steps, or it refuses a PR for steps that PR removes. This
module builds the merge result in a private clone (merge.merge_result_clone,
the same construction the pre_checks gate uses) and reads the steps from it.

One case has no merged-commit source: a config file that is not git-tracked at
the base commit and absent from the merge result. The post-merge path keeps the
working-tree steps for exactly that case, so this module returns them too, and
only then; a tracked config that the merge result no longer carries is zero
steps, as it is after the merge.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.merge.fetch_transport import MergeFetchAuth
from clagentic_loadout.merge.merge_result_clone import (
    MergeResultCloneError,
    merge_result_clone,
)
from clagentic_loadout.merge.post_merge_config import (
    PostMergeConfigError,
    config_path_tracked_at_git_sha,
    load_post_merge_steps,
    load_post_merge_steps_from_git_sha,
    resolve_git_tree_relative_config_paths,
)

_STDERR_EXCERPT = 400


class MergedStepsUnreadableError(Exception):
    """The steps of the commit that would be merged could not be read."""


@dataclass(frozen=True)
class MergedCommitSteps:
    steps: list[dict]
    #: The commit the steps were read from: the PR head merged onto the base.
    commit: str


def _clone_head(clone: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=str(clone),
        )
    except OSError as exc:
        raise MergedStepsUnreadableError(f"git could not run in the merge-result clone: {exc}") from exc
    if result.returncode != 0:
        raise MergedStepsUnreadableError(
            f"the merge-result clone has no readable HEAD: {result.stderr.strip()[:_STDERR_EXCERPT]}"
        )
    return result.stdout.strip()


def read_merged_commit_steps(
    git_tree: str | Path,
    config_root: str | Path,
    *,
    base_sha: str,
    head_sha: str,
    base_branch: str,
    fetch_auth: MergeFetchAuth | None,
) -> MergedCommitSteps:
    """The post_merge_steps *head_sha* merged onto *base_sha* would declare.

    *git_tree* is the repository the merge result is built from, *config_root*
    the directory the repo's deployment config is resolved from (the two differ
    only in a wrapper layout).

    Raises:
        MergedStepsUnreadableError: the merge result cannot be built (a missing
            commit, a conflict, a git failure) or its config cannot be read.
            Never answered from the working tree.
        MergeFetchDeniedError: the tracking remote was refused before any
            network call (propagated unchanged).
    """
    relative, legacy = resolve_git_tree_relative_config_paths(str(config_root), git_tree)
    try:
        with merge_result_clone(
            git_tree, base_sha, head_sha, base_branch=base_branch, fetch_auth=fetch_auth
        ) as clone:
            commit = _clone_head(clone)
            try:
                steps = load_post_merge_steps_from_git_sha(
                    clone, commit, config_relative_path=relative, legacy_relative_path=legacy
                )
            except PostMergeConfigError as exc:
                raise MergedStepsUnreadableError(
                    f"post_merge_steps could not be read from merged commit {commit}: {exc}"
                ) from exc
    except MergeResultCloneError as exc:
        raise MergedStepsUnreadableError(
            f"the commit that would be merged (head {head_sha} onto base {base_sha}) "
            f"could not be built: {exc}"
        ) from exc
    if steps is None:
        tracked = config_path_tracked_at_git_sha(
            git_tree, base_sha, config_relative_path=relative, legacy_relative_path=legacy
        )
        if tracked:
            steps = []
        else:
            try:
                steps = load_post_merge_steps(str(config_root))
            except PostMergeConfigError as exc:
                raise MergedStepsUnreadableError(
                    f"post_merge_steps could not be read for merged commit {commit}: {exc}"
                ) from exc
    return MergedCommitSteps(steps=steps, commit=commit)


__all__ = ["MergedCommitSteps", "MergedStepsUnreadableError", "read_merged_commit_steps"]
