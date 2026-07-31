"""test_push_identity.py — tests for clagentic_loadout.push.identity
(lr-09ca, Wave B slice 3).

Uses real (local, filesystem-only) git repos in tmp_path -- no real push,
no real network, no Date-dependence (all commits use git's own clock via
subprocess, never asserted against wall-clock values).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clagentic_loadout.push.errors import AuthorMismatchError
from clagentic_loadout.push.identity import (
    get_head_author_email,
    pin_commits_to_bot_identity,
    verify_head_author,
)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _init_repo_with_base_and_branch(tmp_path: Path, *, author_email: str = "someone@example.com") -> Path:
    """Build a local repo: main has one commit, then origin/main is faked by
    tagging main as an 'origin/main'-shaped ref via a second local clone
    acting as 'origin' (so resolve_exclusion_ref's remote-tracking-ref
    preference has something real to resolve), then a feature branch with
    one commit under *author_email*."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(["init", "--bare"], origin)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "base@example.com"], repo)
    _git(["config", "user.name", "Base Author"], repo)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "initial commit"], repo)
    _git(["remote", "add", "origin", str(origin)], repo)
    _git(["push", "origin", "main"], repo)
    _git(["fetch", "origin"], repo)

    _git(["checkout", "-b", "feature"], repo)
    _git(["config", "user.email", author_email], repo)
    _git(["config", "user.name", "Feature Author"], repo)
    (repo / "feature.txt").write_text("feature work\n")
    _git(["add", "feature.txt"], repo)
    _git(["commit", "-m", "feature commit"], repo)

    return repo


class TestGetHeadAuthorEmail:
    def test_returns_head_author_email(self, tmp_path):
        repo = _init_repo_with_base_and_branch(tmp_path, author_email="original@example.com")
        assert get_head_author_email(repo) == "original@example.com"

    def test_returns_empty_string_on_non_repo(self, tmp_path, monkeypatch):
        # GIT_CEILING_DIRECTORIES stops git's upward repo-search at tmp_path
        # so this assertion is not sensitive to whatever git repo (if any)
        # happens to contain the test runner's own filesystem root.
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        assert get_head_author_email(not_a_repo) == ""


class TestVerifyHeadAuthor:
    def test_matches(self, tmp_path):
        repo = _init_repo_with_base_and_branch(tmp_path, author_email="original@example.com")
        assert verify_head_author("original@example.com", repo) is True

    def test_mismatch(self, tmp_path):
        repo = _init_repo_with_base_and_branch(tmp_path, author_email="original@example.com")
        assert verify_head_author("someone-else@example.com", repo) is False


class TestPinCommitsToBotIdentity:
    def test_reauthors_and_verifies(self, tmp_path):
        repo = _init_repo_with_base_and_branch(tmp_path, author_email="original@example.com")
        rewritten = pin_commits_to_bot_identity(
            "Bot Name", "bot@example.com", "main", repo,
        )
        assert rewritten is True
        assert get_head_author_email(repo) == "bot@example.com"

    def test_base_reachable_commits_are_never_rewritten(self, tmp_path):
        """The base commit's author (base@example.com) must survive
        re-authoring unchanged -- only the feature branch's own commits are
        rewritten, matching the source module's exclusion-ref contract."""
        repo = _init_repo_with_base_and_branch(tmp_path, author_email="original@example.com")
        pin_commits_to_bot_identity("Bot Name", "bot@example.com", "main", repo)
        r = subprocess.run(
            ["git", "log", "--format=%ae", "main"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == "base@example.com"

    def test_missing_identity_skips_reauthoring_by_default(self, tmp_path):
        repo = _init_repo_with_base_and_branch(tmp_path, author_email="original@example.com")
        rewritten = pin_commits_to_bot_identity(None, None, "main", repo)
        assert rewritten is False
        assert get_head_author_email(repo) == "original@example.com"

    def test_missing_identity_fails_closed_when_required(self, tmp_path):
        repo = _init_repo_with_base_and_branch(tmp_path, author_email="original@example.com")
        with pytest.raises(AuthorMismatchError):
            pin_commits_to_bot_identity(None, None, "main", repo, fail_closed_on_missing=True)

    def test_branch_already_at_bot_identity_is_a_noop_success(self, tmp_path):
        """Branch at base with no new commits, and HEAD is ALREADY authored
        under the target bot identity: reauthor_commits() has nothing to
        rewrite (True, no-op) and the subsequent verify step passes because
        HEAD already matches -- the overall call succeeds without a
        filter-branch rewrite."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main"], repo)
        _git(["config", "user.email", "bot@example.com"], repo)
        _git(["config", "user.name", "Bot Name"], repo)
        (repo / "README.md").write_text("hello\n")
        _git(["add", "README.md"], repo)
        _git(["commit", "-m", "initial commit"], repo)

        rewritten = pin_commits_to_bot_identity("Bot Name", "bot@example.com", "main", repo)
        assert rewritten is True
        assert get_head_author_email(repo) == "bot@example.com"

    def test_branch_at_base_with_different_author_fails_closed(self, tmp_path):
        """Branch at base (nothing ahead of base to rewrite) but HEAD's
        existing author does not match the target bot identity: there is
        nothing for filter-branch to rewrite, and the post-rewrite verify
        step correctly refuses rather than silently reporting success for
        an unmatched HEAD."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main"], repo)
        _git(["config", "user.email", "base@example.com"], repo)
        _git(["config", "user.name", "Base Author"], repo)
        (repo / "README.md").write_text("hello\n")
        _git(["add", "README.md"], repo)
        _git(["commit", "-m", "initial commit"], repo)

        with pytest.raises(AuthorMismatchError):
            pin_commits_to_bot_identity("Bot Name", "bot@example.com", "main", repo)
