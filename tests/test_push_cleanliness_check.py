"""test_push_cleanliness_check.py — unit tests for
clagentic_loadout.push.cleanliness_check (lr-d7a8).

Covers detection semantics against a REAL git repo (no mocked git): a
scratch-pattern match among untracked-and-unignored files is found; a
gitignored scratch file is silent (respects `--exclude-standard`); a clean
tree (or only tracked files) yields no matches; --strict raises
ScratchLitterFoundError carrying the same matches.
"""

from __future__ import annotations

import subprocess

import pytest

from clagentic_loadout.push.cleanliness_check import (
    CleanlinessCheckError,
    ScratchLitterFoundError,
    check_cleanliness,
    find_scratch_matches,
)
from clagentic_loadout.push.cleanliness_config import DEFAULT_SCRATCH_PATTERNS

_PATTERNS = tuple(DEFAULT_SCRATCH_PATTERNS)


def _git(args: list[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "author@example.com"], repo)
    _git(["config", "user.name", "Author"], repo)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "initial"], repo)
    return repo


class TestFindScratchMatches:
    def test_clean_tree_has_no_matches(self, git_repo):
        assert find_scratch_matches(git_repo, _PATTERNS) == []

    def test_untracked_tracked_only_file_has_no_matches(self, git_repo):
        # A tracked, ordinary file present -- not scratch-shaped, not
        # untracked either way.
        (git_repo / "src.py").write_text("x = 1\n")
        _git(["add", "src.py"], git_repo)
        _git(["commit", "-m", "add src"], git_repo)
        assert find_scratch_matches(git_repo, _PATTERNS) == []

    def test_untracked_unignored_scratch_file_matches(self, git_repo):
        (git_repo / "pr-body-lr-9999.txt").write_text("body text\n")
        matches = find_scratch_matches(git_repo, _PATTERNS)
        assert matches == [("pr-body-lr-9999.txt", "pr-body-*")]

    def test_gitignored_scratch_file_does_not_match(self, git_repo):
        (git_repo / ".gitignore").write_text("pr-body-*\n")
        _git(["add", ".gitignore"], git_repo)
        _git(["commit", "-m", "add gitignore"], git_repo)
        (git_repo / "pr-body-lr-9999.txt").write_text("body text\n")
        assert find_scratch_matches(git_repo, _PATTERNS) == []

    def test_untracked_non_scratch_file_does_not_match(self, git_repo):
        (git_repo / "notes.txt").write_text("just notes\n")
        assert find_scratch_matches(git_repo, _PATTERNS) == []

    def test_multiple_matches_all_returned(self, git_repo):
        (git_repo / "pr-body-lr-1.txt").write_text("a\n")
        (git_repo / "HANDOFF.md").write_text("b\n")
        matches = find_scratch_matches(git_repo, _PATTERNS)
        assert sorted(matches) == sorted(
            [("pr-body-lr-1.txt", "pr-body-*"), ("HANDOFF.md", "HANDOFF.md")]
        )

    def test_custom_pattern_list_honored(self, git_repo):
        (git_repo / "custom-litter.tmp").write_text("x\n")
        matches = find_scratch_matches(git_repo, ("custom-litter.*",))
        assert matches == [("custom-litter.tmp", "custom-litter.*")]

    def test_git_command_failure_raises(self, tmp_path, monkeypatch):
        # GIT_CEILING_DIRECTORIES stops git's repo-discovery walk from
        # escaping *tmp_path* to find some ancestor .git directory (the
        # sandbox may have one above /tmp) -- this test exercises the
        # underlying-git-command-failure path (not-a-repo, in this case)
        # deterministically, independent of the ambient filesystem.
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        with pytest.raises(CleanlinessCheckError):
            find_scratch_matches(not_a_repo, _PATTERNS)


class TestCheckCleanliness:
    def test_default_warns_and_does_not_raise(self, git_repo):
        (git_repo / "pr-body-lr-9999.txt").write_text("body text\n")
        matches = check_cleanliness(git_repo, _PATTERNS, strict=False)
        assert matches == [("pr-body-lr-9999.txt", "pr-body-*")]

    def test_strict_raises_with_same_matches(self, git_repo):
        (git_repo / "pr-body-lr-9999.txt").write_text("body text\n")
        with pytest.raises(ScratchLitterFoundError) as exc_info:
            check_cleanliness(git_repo, _PATTERNS, strict=True)
        assert exc_info.value.matches == [("pr-body-lr-9999.txt", "pr-body-*")]

    def test_strict_clean_tree_does_not_raise(self, git_repo):
        assert check_cleanliness(git_repo, _PATTERNS, strict=True) == []

    def test_clean_tree_returns_empty_regardless_of_strict(self, git_repo):
        assert check_cleanliness(git_repo, _PATTERNS, strict=False) == []
        assert check_cleanliness(git_repo, _PATTERNS, strict=True) == []
