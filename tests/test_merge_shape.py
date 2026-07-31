"""test_merge_shape.py — unit coverage for clagentic_loadout.merge.merge_shape
(lr-14f704 item 3).

Proves the requested-vs-actual merge-shape mismatch is DETECTED via a genuine
local git readback (never inferred from the request), against REAL commits
built with actual `git merge`/`git commit` calls in a tmp_path repo -- no
platform API mocking needed here, since check_merge_shape only ever reads a
LOCAL object database (see module docstring, "SCOPE").
"""

from __future__ import annotations

import subprocess

import pytest

from clagentic_loadout.merge.merge_shape import (
    MULTI_PARENT_MERGE_METHODS,
    SINGLE_PARENT_MERGE_METHODS,
    MergeShapeCheckError,
    check_merge_shape,
    format_mismatch_message,
)


def _run_git(args: list[str], *, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(cwd))


def _git_ok(args: list[str], *, cwd) -> subprocess.CompletedProcess:
    result = _run_git(args, cwd=cwd)
    assert result.returncode == 0, f"git {args!r} failed: {result.stderr}"
    return result


def _head_sha(repo_dir) -> str:
    result = _run_git(["rev-parse", "HEAD"], cwd=repo_dir)
    assert result.returncode == 0
    return result.stdout.strip()


@pytest.fixture
def repo_with_real_merge_commit(tmp_path):
    """A repo whose HEAD is a genuine two-parent merge commit (`git merge
    --no-ff`) -- the real shape a --merge-method merge invocation should
    produce."""
    repo_dir = tmp_path / "repo"
    _git_ok(["init", "-b", "main"], cwd=tmp_path)
    _git_ok(["init", "-b", "main", str(repo_dir)], cwd=tmp_path)
    _git_ok(["config", "user.email", "test@example.com"], cwd=repo_dir)
    _git_ok(["config", "user.name", "test"], cwd=repo_dir)
    (repo_dir / "f.txt").write_text("v1\n", encoding="utf-8")
    _git_ok(["add", "f.txt"], cwd=repo_dir)
    _git_ok(["commit", "-m", "initial"], cwd=repo_dir)

    _git_ok(["checkout", "-b", "feature"], cwd=repo_dir)
    (repo_dir / "f.txt").write_text("v2\n", encoding="utf-8")
    _git_ok(["commit", "-am", "feature change"], cwd=repo_dir)

    _git_ok(["checkout", "main"], cwd=repo_dir)
    _git_ok(["merge", "--no-ff", "-m", "merge feature", "feature"], cwd=repo_dir)
    return repo_dir, _head_sha(repo_dir)


@pytest.fixture
def repo_with_single_parent_commit(tmp_path):
    """A repo whose HEAD is a genuine single-parent commit -- the real shape
    a --merge-method squash/rebase invocation should produce."""
    repo_dir = tmp_path / "repo"
    _git_ok(["init", "-b", "main"], cwd=tmp_path)
    _git_ok(["init", "-b", "main", str(repo_dir)], cwd=tmp_path)
    _git_ok(["config", "user.email", "test@example.com"], cwd=repo_dir)
    _git_ok(["config", "user.name", "test"], cwd=repo_dir)
    (repo_dir / "f.txt").write_text("v1\n", encoding="utf-8")
    _git_ok(["add", "f.txt"], cwd=repo_dir)
    _git_ok(["commit", "-m", "initial"], cwd=repo_dir)
    (repo_dir / "f.txt").write_text("v2\n", encoding="utf-8")
    _git_ok(["commit", "-am", "squashed change"], cwd=repo_dir)
    return repo_dir, _head_sha(repo_dir)


class TestMultiParentMethodMatchesRealMergeCommit:
    def test_merge_method_matches_real_two_parent_commit(self, repo_with_real_merge_commit):
        repo_dir, sha = repo_with_real_merge_commit
        result = check_merge_shape(sha, "merge", repo_dir)
        assert result.verified is True
        assert result.matches is True
        assert result.actual_parent_count == 2

    def test_rebase_merge_method_matches_real_two_parent_commit(self, repo_with_real_merge_commit):
        # Forgejo's rebase-merge Do value is the ONE Forgejo-only shape still
        # expected to land multi-parent -- see MULTI_PARENT_MERGE_METHODS.
        repo_dir, sha = repo_with_real_merge_commit
        result = check_merge_shape(sha, "rebase-merge", repo_dir)
        assert result.verified is True
        assert result.matches is True


class TestMultiParentMethodMismatchesSingleParentCommit:
    def test_merge_method_mismatches_single_parent_commit(self, repo_with_single_parent_commit):
        """THE REGRESSION LOCK for lr-14f704's originating incident: a
        --merge-method merge request that actually landed as a single-parent
        commit (exactly what happened when the flag was silently ignored and
        a squash was requested but a real-merge shape resulted -- or, as
        here, the inverse: merge requested, squash-shaped result) must be
        DETECTED, never silently accepted."""
        repo_dir, sha = repo_with_single_parent_commit
        result = check_merge_shape(sha, "merge", repo_dir)
        assert result.verified is True
        assert result.matches is False
        assert result.actual_parent_count == 1


class TestSingleParentMethodMatchesRealSquashCommit:
    def test_squash_method_matches_single_parent_commit(self, repo_with_single_parent_commit):
        repo_dir, sha = repo_with_single_parent_commit
        result = check_merge_shape(sha, "squash", repo_dir)
        assert result.verified is True
        assert result.matches is True
        assert result.actual_parent_count == 1

    def test_rebase_method_matches_single_parent_commit(self, repo_with_single_parent_commit):
        repo_dir, sha = repo_with_single_parent_commit
        result = check_merge_shape(sha, "rebase", repo_dir)
        assert result.verified is True
        assert result.matches is True


class TestSingleParentMethodMismatchesRealMergeCommit:
    def test_squash_method_mismatches_two_parent_commit(self, repo_with_real_merge_commit):
        """THE ORIGINATING INCIDENT'S EXACT SHAPE: --merge-method squash was
        requested but a real two-parent merge commit landed (because the
        flag was silently ignored before this fix) -- must be DETECTED."""
        repo_dir, sha = repo_with_real_merge_commit
        result = check_merge_shape(sha, "squash", repo_dir)
        assert result.verified is True
        assert result.matches is False
        assert result.actual_parent_count == 2


class TestUnverifiableMergeMethodIsANoOp:
    def test_manually_merged_is_unverified_no_op(self, repo_with_real_merge_commit):
        repo_dir, sha = repo_with_real_merge_commit
        result = check_merge_shape(sha, "manually-merged", repo_dir)
        assert result.verified is False
        assert result.matches is True  # no-op pass, never a spurious mismatch

    def test_unrecognized_merge_method_is_unverified_no_op(self, repo_with_real_merge_commit):
        repo_dir, sha = repo_with_real_merge_commit
        result = check_merge_shape(sha, "some-custom-integrator-value", repo_dir)
        assert result.verified is False
        assert result.matches is True


class TestReadFailureRaises:
    def test_nonexistent_sha_raises_merge_shape_check_error(self, tmp_path):
        repo_dir = tmp_path / "repo"
        _git_ok(["init", "-b", "main", str(repo_dir)], cwd=tmp_path)
        _git_ok(["config", "user.email", "test@example.com"], cwd=repo_dir)
        _git_ok(["config", "user.name", "test"], cwd=repo_dir)
        (repo_dir / "f.txt").write_text("v1\n", encoding="utf-8")
        _git_ok(["add", "f.txt"], cwd=repo_dir)
        _git_ok(["commit", "-m", "initial"], cwd=repo_dir)

        with pytest.raises(MergeShapeCheckError):
            check_merge_shape("f" * 40, "merge", repo_dir)

    def test_non_git_directory_raises(self, tmp_path):
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        with pytest.raises(MergeShapeCheckError):
            check_merge_shape("a" * 40, "merge", not_a_repo)


class TestConstantsAreDisjoint:
    def test_multi_and_single_parent_sets_never_overlap(self):
        assert MULTI_PARENT_MERGE_METHODS.isdisjoint(SINGLE_PARENT_MERGE_METHODS)


class TestFormatMismatchMessage:
    def test_names_sha_method_and_actual_count(self, repo_with_single_parent_commit):
        repo_dir, sha = repo_with_single_parent_commit
        result = check_merge_shape(sha, "merge", repo_dir)
        message = format_mismatch_message(result, pr_number=42, owner="some-owner", repo="some-repo")
        assert sha in message
        assert "merge" in message
        assert "1 parent" in message
        assert "#42" in message
        assert "some-owner/some-repo" in message
