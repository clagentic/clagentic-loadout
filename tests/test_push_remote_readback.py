"""test_push_remote_readback.py — unit tests for
clagentic_loadout.push.remote_readback (lr-4e8a43).

Coverage:
  - read_remote_head reads the ACTUAL remote (a real local bare-repo, no
    mocking of git itself) and returns a RemoteReadback whose
    remote_head_sha matches what `git ls-remote` independently reports.
  - A branch that was never pushed (the lr-60fac5 acceptance scenario: local
    commits exist, no push happened) is DETECTABLE -- read_remote_head
    raises RemoteReadbackError rather than inventing or falling back to a
    local value.
  - verify_remote_authorship: opt-in (no expected email -> no-op pass),
    matches/mismatches an actual commit author correctly.
  - RemoteReadback.source is always the fixed provenance tag, never
    caller-suppliable through ordinary use.
"""

from __future__ import annotations

import subprocess

import pytest

from clagentic_loadout.push.remote_readback import (
    REMOTE_READBACK_SOURCE_GIT_LS_REMOTE,
    RemoteReadbackError,
    read_remote_head,
    verify_remote_authorship,
)


def _git(args: list[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


@pytest.fixture
def repo_with_remote(tmp_path):
    """A local repo with a bare-repo 'origin' remote (real git, no network) —
    same shape as test_push_verb.py's own fixture, kept local to this file
    so this module's tests never depend on push.verb's test module."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(["init", "--bare", "-b", "main"], remote)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "author@example.com"], repo)
    _git(["config", "user.name", "Author"], repo)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "initial"], repo)
    _git(["remote", "add", "origin", str(remote)], repo)
    _git(["push", "origin", "main"], repo)

    _git(["checkout", "-b", "feature"], repo)
    (repo / "feature.txt").write_text("work\n")
    _git(["add", "feature.txt"], repo)
    _git(["commit", "-m", "feature work"], repo)

    return repo, remote


class TestReadRemoteHead:
    def test_reads_actual_remote_sha_after_push(self, repo_with_remote):
        repo, remote = repo_with_remote
        _git(["push", "origin", "feature"], repo)

        readback = read_remote_head("origin", "feature", repo)

        independent_check = subprocess.run(
            ["git", "log", "-1", "--format=%H", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert readback.remote_head_sha == independent_check
        assert readback.source == REMOTE_READBACK_SOURCE_GIT_LS_REMOTE
        assert readback.remote == "origin"
        assert readback.ref == "feature"

    def test_full_length_sha(self, repo_with_remote):
        repo, _remote = repo_with_remote
        _git(["push", "origin", "feature"], repo)
        readback = read_remote_head("origin", "feature", repo)
        assert len(readback.remote_head_sha) == 40

    def test_matches_helper(self, repo_with_remote):
        repo, _remote = repo_with_remote
        _git(["push", "origin", "feature"], repo)
        readback = read_remote_head("origin", "feature", repo)
        assert readback.matches(readback.remote_head_sha) is True
        assert readback.matches("0" * 40) is False

    def test_branch_never_pushed_is_detectable(self, repo_with_remote):
        """lr-60fac5 ACCEPTANCE SCENARIO, module level: local commits exist
        on 'feature' (see fixture), but `git push` to the remote was NEVER
        invoked. A caller asking this module for the remote's view of
        'feature' must not receive a fabricated/local value -- it must be
        structurally impossible to get a RemoteReadback back for a branch
        the remote does not have."""
        repo, _remote = repo_with_remote
        # Deliberately no `git push origin feature` here.

        with pytest.raises(RemoteReadbackError):
            read_remote_head("origin", "feature", repo)

    def test_stale_remote_sha_detectable_after_local_amend(self, repo_with_remote):
        """A push happened, then the caller amended locally WITHOUT pushing
        again -- the remote readback must report the OLD (still-remote)
        SHA, not the new local one, so a caller comparing against its own
        local HEAD can detect the drift itself."""
        repo, _remote = repo_with_remote
        _git(["push", "origin", "feature"], repo)
        pushed_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
        ).stdout.strip()

        (repo / "feature.txt").write_text("more work, never pushed\n")
        _git(["add", "feature.txt"], repo)
        _git(["commit", "-m", "unpushed follow-up"], repo)
        local_sha_after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
        ).stdout.strip()
        assert local_sha_after != pushed_sha

        readback = read_remote_head("origin", "feature", repo)
        assert readback.remote_head_sha == pushed_sha
        assert readback.remote_head_sha != local_sha_after

    def test_unreachable_remote_raises_not_falls_back(self, repo_with_remote, tmp_path):
        repo, _remote = repo_with_remote
        _git(["remote", "set-url", "origin", str(tmp_path / "does-not-exist.git")], repo)

        with pytest.raises(RemoteReadbackError):
            read_remote_head("origin", "feature", repo)


class TestVerifyRemoteAuthorship:
    def test_no_expected_email_is_a_noop_pass(self, repo_with_remote):
        repo, _remote = repo_with_remote
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
        ).stdout.strip()

        result = verify_remote_authorship(sha, None, repo)
        assert result.checked is False
        assert result.matches is True

    def test_matching_author_passes(self, repo_with_remote):
        repo, _remote = repo_with_remote
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
        ).stdout.strip()

        result = verify_remote_authorship(sha, "author@example.com", repo)
        assert result.checked is True
        assert result.matches is True
        assert result.actual_email == "author@example.com"

    def test_mismatched_author_is_detected_not_masked(self, repo_with_remote):
        """A readback must assert AUTHORSHIP, not merely ref-advance. This
        is the case a ref-advance-only check would pass cleanly: the commit
        landed, but under the wrong identity."""
        repo, _remote = repo_with_remote
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
        ).stdout.strip()

        result = verify_remote_authorship(sha, "expected-bot@example.com", repo)
        assert result.checked is True
        assert result.matches is False
        assert result.actual_email == "author@example.com"
        assert result.expected_email == "expected-bot@example.com"
