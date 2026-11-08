"""review.changeset_ratio_source — the per-file counts the ratio is computed from.

Two sources, one answer shape (the object of review.changeset_ratio):

  local  ``git diff --numstat`` in a repository, with the profile read at the
         merge base so a change cannot loosen the budget or the test globs it
         is judged by;
  host   the host's compare between two commits, through the same
         range-diff transport the churn report reads; the profile is the one
         the caller already read at the base.

Neither raises: a count that cannot be read is the ``unavailable`` object with
its reason, because the ratio is advice and never a reason to fail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clagentic_loadout.review import changeset_ratio
from clagentic_loadout.review.churn_delta import FileDelta, read_net_delta
from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReadError
from clagentic_loadout.review.policy_source import read_repo_profile
from clagentic_loadout.review.repo_profile import RepoProfile


def from_host(
    backend: Any, profile: RepoProfile | None, *, owner: str, repo: str, base_sha: str, head_sha: str
) -> dict[str, Any]:
    """The ratio object for the host's compare of *base_sha* to *head_sha*."""
    settings = changeset_ratio.settings_from_profile(profile)
    delta, reason = read_net_delta(
        backend, owner=owner, repo=repo, from_head=base_sha, to_head=head_sha
    )
    if delta is None:
        return changeset_ratio.unavailable(reason or "the host compare could not be read", settings)
    return changeset_ratio.compute(delta.files, settings, changeset_ratio.extra_test_globs(profile))


def from_local(repo_path: str | Path, base: str, head: str) -> dict[str, Any]:
    """The ratio object for *head* against *base* in the repository at *repo_path*."""
    reader = GitPolicyReader(repo_path)
    try:
        base_sha = reader.resolve_commit(base)
        head_sha = reader.resolve_commit(head)
        merge_base = reader.merge_base(base_sha, head_sha)
        present = set(reader.list_paths(merge_base))
        counts = reader.numstat(merge_base, head_sha)
    except PolicyReadError as exc:
        return changeset_ratio.unavailable(str(exc))
    profile, _state, _problem = read_repo_profile(reader, merge_base, present)
    settings = changeset_ratio.settings_from_profile(profile)
    return changeset_ratio.compute(
        [FileDelta(path, added, removed) for path, added, removed in counts],
        settings,
        changeset_ratio.extra_test_globs(profile),
    )


__all__ = ["from_host", "from_local"]
