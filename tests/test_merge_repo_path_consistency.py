"""test_merge_repo_path_consistency.py — unit coverage for
clagentic_loadout.merge.repo_path_consistency (lr-4522a3).

Covers:
  - a --repo-path whose origin remote matches --repo (HTTPS and SSH forms,
    with/without a trailing .git, case-insensitively) is reported CONSISTENT
  - the '.github' org-profile shape (directory basename diverges from the
    slug's own trailing segment) is CORRECT and never flagged -- the
    comparison is against the remote, never the directory name
  - a genuine slug/tree mismatch is refused (assert_repo_path_consistent
    raises MergeUsageError) naming both the --repo value and the tree's own
    remote-derived slug
  - explicit, non-silent handling of every degenerate case: absent
    --repo-path, a --repo-path that does not exist, a tree with no origin
    remote, and an unparseable remote URL -- all report checked=False with a
    reason, never silently treated as a pass
"""

from __future__ import annotations

import subprocess

import pytest

from clagentic_loadout.merge.errors import MergeUsageError
from clagentic_loadout.merge.repo_path_consistency import (
    RepoPathConsistencyResult,
    assert_repo_path_consistent,
    check_repo_path_consistency,
)


def _run_git(args: list[str], *, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(cwd))


def _git_ok(args: list[str], *, cwd) -> None:
    result = _run_git(args, cwd=cwd)
    assert result.returncode == 0, f"git {args!r} failed: {result.stderr}"


def _make_tree_with_remote(tmp_path, dirname: str, remote_url: str):
    tree_dir = tmp_path / dirname
    tree_dir.mkdir()
    _git_ok(["init", "-q", "-b", "main"], cwd=tree_dir)
    _git_ok(["remote", "add", "origin", remote_url], cwd=tree_dir)
    return tree_dir


class TestConsistentCases:
    def test_https_remote_matching_slug_is_consistent(self, tmp_path):
        tree = _make_tree_with_remote(
            tmp_path, "some-repo", "https://git-host.example.com/some-owner/some-repo.git"
        )
        result = check_repo_path_consistency("some-owner/some-repo", str(tree))
        assert result.checked
        assert result.matches

    def test_https_remote_without_dot_git_suffix_matches(self, tmp_path):
        tree = _make_tree_with_remote(
            tmp_path, "some-repo", "https://git-host.example.com/some-owner/some-repo"
        )
        result = check_repo_path_consistency("some-owner/some-repo", str(tree))
        assert result.checked
        assert result.matches

    def test_ssh_short_form_remote_matches(self, tmp_path):
        tree = _make_tree_with_remote(
            tmp_path, "some-repo", "git@git-host.example.com:some-owner/some-repo.git"
        )
        result = check_repo_path_consistency("some-owner/some-repo", str(tree))
        assert result.checked
        assert result.matches

    def test_ssh_long_form_remote_matches(self, tmp_path):
        tree = _make_tree_with_remote(
            tmp_path, "some-repo", "ssh://git@git-host.example.com/some-owner/some-repo.git"
        )
        result = check_repo_path_consistency("some-owner/some-repo", str(tree))
        assert result.checked
        assert result.matches

    def test_comparison_is_case_insensitive(self, tmp_path):
        tree = _make_tree_with_remote(
            tmp_path, "some-repo", "https://git-host.example.com/Some-Owner/Some-Repo.git"
        )
        result = check_repo_path_consistency("some-owner/some-repo", str(tree))
        assert result.checked
        assert result.matches

    def test_ssh_and_https_forms_cross_compare_as_equal(self, tmp_path):
        # An SSH-form remote must match the same owner/repo an HTTPS-form
        # remote would -- the comparison is owner/repo only, never the host
        # or scheme form.
        tree = _make_tree_with_remote(
            tmp_path, "some-repo", "git@git-host.example.com:some-owner/some-repo.git"
        )
        result = check_repo_path_consistency("some-owner/some-repo", str(tree))
        assert result.checked
        assert result.matches
        assert result.remote_owner_repo == "some-owner/some-repo"


class TestDotGithubShapeNeverFlagged:
    """The whole reason this check exists: clagentic/.github lives at a
    directory whose basename does NOT match the repo name -- this must be
    treated as CORRECT, never flagged, because the comparison is against the
    remote, never the directory name."""

    def test_dot_github_repo_at_differently_named_directory_is_consistent(self, tmp_path):
        # Directory is named "checkout-dir", nothing like ".github" or
        # "clagentic" -- only the remote matters.
        tree = _make_tree_with_remote(
            tmp_path, "checkout-dir", "https://git-host.example.com/clagentic/.github.git"
        )
        result = check_repo_path_consistency("clagentic/.github", str(tree))
        assert result.checked
        assert result.matches

    def test_dot_github_repo_at_clagentic_github_directory_name_is_consistent(self, tmp_path):
        # Mirrors the exact real-world shape named in the task: directory
        # basename "clagentic-github" (unrelated to the actual repo name
        # ".github") pointing at clagentic/.github's real remote.
        tree = _make_tree_with_remote(
            tmp_path, "clagentic-github", "https://git-host.example.com/clagentic/.github.git"
        )
        result = check_repo_path_consistency("clagentic/.github", str(tree))
        assert result.checked, result.reason
        assert result.matches


class TestMismatchRefused:
    def test_mismatched_slug_is_reported_not_matching(self, tmp_path):
        tree = _make_tree_with_remote(
            tmp_path, "some-repo", "https://git-host.example.com/some-owner/some-repo.git"
        )
        result = check_repo_path_consistency("other-owner/other-repo", str(tree))
        assert result.checked
        assert not result.matches
        assert result.remote_owner_repo == "some-owner/some-repo"

    def test_assert_raises_usage_error_naming_both_values(self, tmp_path):
        tree = _make_tree_with_remote(
            tmp_path, "some-repo", "https://git-host.example.com/some-owner/some-repo.git"
        )
        with pytest.raises(MergeUsageError) as exc_info:
            assert_repo_path_consistent("other-owner/other-repo", str(tree))
        message = str(exc_info.value)
        assert "other-owner/other-repo" in message
        assert "some-owner/some-repo" in message

    def test_assert_passes_silently_on_a_match(self, tmp_path):
        tree = _make_tree_with_remote(
            tmp_path, "some-repo", "https://git-host.example.com/some-owner/some-repo.git"
        )
        # Must not raise.
        assert_repo_path_consistent("some-owner/some-repo", str(tree))


class TestDegenerateCasesAreExplicitNeverSilent:
    """Every case here must report checked=False with a reason -- an absent
    acknowledgment is never treated as a pass (task's own explicit
    requirement)."""

    def test_no_repo_path_at_all(self):
        result = check_repo_path_consistency("some-owner/some-repo", None)
        assert not result.checked
        assert result.reason

    def test_empty_repo_path(self):
        result = check_repo_path_consistency("some-owner/some-repo", "")
        assert not result.checked
        assert result.reason

    def test_repo_path_does_not_exist(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        result = check_repo_path_consistency("some-owner/some-repo", str(missing))
        assert not result.checked
        assert "does not exist" in result.reason or "not a directory" in result.reason

    def test_repo_path_is_not_a_git_tree(self, tmp_path):
        not_git = tmp_path / "plain-dir"
        not_git.mkdir()
        result = check_repo_path_consistency("some-owner/some-repo", str(not_git))
        assert not result.checked
        assert result.reason

    def test_repo_path_has_no_origin_remote(self, tmp_path):
        tree = tmp_path / "no-remote"
        tree.mkdir()
        _git_ok(["init", "-q", "-b", "main"], cwd=tree)
        result = check_repo_path_consistency("some-owner/some-repo", str(tree))
        assert not result.checked
        assert "origin" in result.reason

    def test_unparseable_remote_url(self, tmp_path):
        tree = _make_tree_with_remote(tmp_path, "weird-remote", "not-a-real-remote-url")
        result = check_repo_path_consistency("some-owner/some-repo", str(tree))
        assert not result.checked
        assert "not-a-real-remote-url" in result.reason

    def test_assert_never_raises_on_a_checked_false_result(self, tmp_path):
        # A degenerate (unconfirmable) tree must never be treated as a
        # mismatch -- assert_repo_path_consistent only refuses a POSITIVELY
        # confirmed mismatch, never an absence of information.
        missing = tmp_path / "does-not-exist"
        assert_repo_path_consistent("some-owner/some-repo", str(missing))
        assert_repo_path_consistent("some-owner/some-repo", None)


class TestInvalidSlug:
    def test_malformed_repo_slug_reported_unchecked(self, tmp_path):
        tree = _make_tree_with_remote(
            tmp_path, "some-repo", "https://git-host.example.com/some-owner/some-repo.git"
        )
        result = check_repo_path_consistency("not-a-valid-slug", str(tree))
        assert not result.checked
        assert result.reason


def test_result_is_a_frozen_dataclass_instance(tmp_path):
    tree = _make_tree_with_remote(
        tmp_path, "some-repo", "https://git-host.example.com/some-owner/some-repo.git"
    )
    result = check_repo_path_consistency("some-owner/some-repo", str(tree))
    assert isinstance(result, RepoPathConsistencyResult)
