"""test_push_git_coords.py — tests for clagentic_loadout.push.git_coords
(lr-09ca, Wave B slice 3).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clagentic_loadout.push.errors import RemoteResolutionError
from clagentic_loadout.push.git_coords import (
    current_branch,
    parse_forgejo_coords,
    parse_owner_repo,
    read_remote_url_best_effort,
    remote_url,
    tracking_remote,
)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


class TestParseForgejoCoords:
    def test_https_url(self):
        api_base, owner, repo = parse_forgejo_coords("https://git-host.example.com/some-owner/some-repo.git")
        assert api_base == "https://git-host.example.com"
        assert owner == "some-owner"
        assert repo == "some-repo"

    def test_http_url_with_port(self):
        api_base, owner, repo = parse_forgejo_coords("http://git-host.example.com:3000/some-owner/some-repo")
        assert api_base == "http://git-host.example.com:3000"
        assert owner == "some-owner"
        assert repo == "some-repo"

    def test_userinfo_stripped(self):
        api_base, owner, repo = parse_forgejo_coords(
            "http://x-access-token:secret-tok@git-host.example.com:3000/some-owner/some-repo.git"
        )
        assert api_base == "http://git-host.example.com:3000"
        assert "secret-tok" not in api_base

    def test_wrong_scheme_raises(self):
        with pytest.raises(RemoteResolutionError):
            parse_forgejo_coords("git@git-host.example.com:some-owner/some-repo.git")

    def test_wrong_path_shape_raises(self):
        with pytest.raises(RemoteResolutionError):
            parse_forgejo_coords("https://git-host.example.com/only-one-segment")


class TestParseOwnerRepo:
    def test_valid(self):
        assert parse_owner_repo("some-owner/some-repo") == ("some-owner", "some-repo")

    def test_malformed_raises(self):
        with pytest.raises(RemoteResolutionError):
            parse_owner_repo("not-a-valid-owner-repo")

    def test_empty_segment_raises(self):
        with pytest.raises(RemoteResolutionError):
            parse_owner_repo("owner/")


class TestGitHelpersAgainstRealRepo:
    def test_current_branch_and_tracking_remote_and_remote_url(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "feature"], repo)
        _git(["config", "user.email", "a@example.com"], repo)
        _git(["config", "user.name", "A"], repo)
        (repo / "f.txt").write_text("x\n")
        _git(["add", "f.txt"], repo)
        _git(["commit", "-m", "c"], repo)
        _git(["remote", "add", "origin", "https://git-host.example.com/some-owner/some-repo.git"], repo)

        assert current_branch(repo) == "feature"
        assert tracking_remote("feature", repo) == "origin"
        assert remote_url("origin", repo) == "https://git-host.example.com/some-owner/some-repo.git"

    def test_remote_url_raises_on_missing_remote(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main"], repo)
        with pytest.raises(RemoteResolutionError):
            remote_url("origin", repo)

    def test_read_remote_url_best_effort_returns_empty_on_failure(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        assert read_remote_url_best_effort(not_a_repo) == ""

    def test_read_remote_url_best_effort_returns_url_on_success(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main"], repo)
        _git(["config", "user.email", "a@example.com"], repo)
        _git(["config", "user.name", "A"], repo)
        (repo / "f.txt").write_text("x\n")
        _git(["add", "f.txt"], repo)
        _git(["commit", "-m", "c"], repo)
        _git(["remote", "add", "origin", "https://git-host.example.com/some-owner/some-repo.git"], repo)

        assert read_remote_url_best_effort(repo) == "https://git-host.example.com/some-owner/some-repo.git"
