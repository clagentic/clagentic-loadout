"""test_push_contention_check.py — unit tests for
clagentic_loadout.push.contention_check (lr-78a584).

Covers, against a REAL git repo (no mocked git, mirroring
test_push_cleanliness_check.py's own precedent):

  1. NO STATE: two independent calls against the same tree with no fixture
     state carried between them produce the same verdict — nothing to
     orphan, nothing to release.
  2. DEFAULT OFF: enabled=False never reads git state at all and never
     refuses, byte-identical-behavior contract.
  3. THE DIRTINESS ADJUDICATION: the verified counter-example from the task
     — a default-branch tree with stale dirty residue from an already-merged
     PR — PROCEEDS. A matching in-flight branch name refuses regardless of
     dirtiness; a matching branch name that is ALSO dirty still refuses (and
     reports the dirty detail), but dirtiness is never independently
     sufficient.
  4. THE OVERRIDE FLAG: mandatory, always honored, and reported.
"""

from __future__ import annotations

import subprocess

import pytest

from clagentic_loadout.push.contention_check import (
    WorkingTreeContentionError,
    check_working_tree_contention,
)
from clagentic_loadout.push.contention_config import DEFAULT_IN_FLIGHT_BRANCH_PATTERN

_PATTERN = DEFAULT_IN_FLIGHT_BRANCH_PATTERN


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


class TestDisabledIsANoOp:
    """DEFAULT OFF hard acceptance criterion: enabled=False never inspects
    git state and never refuses — byte-identical to no check existing."""

    def test_disabled_on_matching_dirty_branch_never_refuses(self, git_repo):
        _git(["checkout", "-b", "feat/lr-1-thing"], git_repo)
        (git_repo / "README.md").write_text("dirty\n")

        verdict = check_working_tree_contention(
            git_repo, enabled=False, branch_pattern=_PATTERN, override=False
        )
        assert verdict.in_flight is False
        assert verdict.reason == "contention check disabled"

    def test_disabled_does_not_shell_out_to_git(self, git_repo, monkeypatch):
        called = []
        real_run = subprocess.run

        def _spy(*args, **kwargs):
            called.append(args)
            return real_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", _spy)
        check_working_tree_contention(
            git_repo, enabled=False, branch_pattern=_PATTERN, override=False
        )
        assert called == []


class TestBranchNameIsThePrimarySignal:
    def test_default_branch_clean_proceeds(self, git_repo):
        verdict = check_working_tree_contention(
            git_repo, enabled=True, branch_pattern=_PATTERN, override=False
        )
        assert verdict.in_flight is False

    def test_matching_branch_clean_refuses(self, git_repo):
        _git(["checkout", "-b", "feat/lr-1-thing"], git_repo)
        with pytest.raises(WorkingTreeContentionError) as exc_info:
            check_working_tree_contention(
                git_repo, enabled=True, branch_pattern=_PATTERN, override=False
            )
        assert exc_info.value.branch == "feat/lr-1-thing"
        assert exc_info.value.dirty is False
        assert "feat/lr-1-thing" in str(exc_info.value)

    def test_non_matching_branch_clean_proceeds(self, git_repo):
        _git(["checkout", "-b", "some-random-branch"], git_repo)
        verdict = check_working_tree_contention(
            git_repo, enabled=True, branch_pattern=_PATTERN, override=False
        )
        assert verdict.in_flight is False


class TestDirtinessIsOnlyASecondarySignal:
    """The task's own verified counter-example, and the adjudicated rule
    that closes it: dirtiness is NEVER independently sufficient to refuse —
    it is only ever consulted once the branch-name signal has already
    matched."""

    def test_default_branch_dirty_proceeds(self, git_repo):
        # THE VERIFIED COUNTER-EXAMPLE FROM THE TASK: a tree on the default
        # branch with files still showing modified, stale residue from an
        # already-merged PR. This MUST proceed.
        (git_repo / "README.md").write_text("stale post-merge residue\n")

        verdict = check_working_tree_contention(
            git_repo, enabled=True, branch_pattern=_PATTERN, override=False
        )
        assert verdict.in_flight is False
        assert verdict.dirty is False  # never even consulted

    def test_non_matching_branch_dirty_still_proceeds(self, git_repo):
        _git(["checkout", "-b", "some-random-branch"], git_repo)
        (git_repo / "README.md").write_text("dirty but not an in-flight-shaped branch\n")

        verdict = check_working_tree_contention(
            git_repo, enabled=True, branch_pattern=_PATTERN, override=False
        )
        assert verdict.in_flight is False

    def test_matching_branch_dirty_refuses_and_reports_dirty(self, git_repo):
        _git(["checkout", "-b", "feat/lr-1-thing"], git_repo)
        (git_repo / "README.md").write_text("mid-edit\n")

        with pytest.raises(WorkingTreeContentionError) as exc_info:
            check_working_tree_contention(
                git_repo, enabled=True, branch_pattern=_PATTERN, override=False
            )
        assert exc_info.value.dirty is True
        assert "dirty" in str(exc_info.value)


class TestOverrideFlagIsMandatoryAndAlwaysHonored:
    def test_override_proceeds_on_a_refusing_state(self, git_repo):
        _git(["checkout", "-b", "feat/lr-1-thing"], git_repo)

        verdict = check_working_tree_contention(
            git_repo, enabled=True, branch_pattern=_PATTERN, override=True
        )
        assert verdict.in_flight is True
        assert verdict.overridden is True
        assert verdict.branch == "feat/lr-1-thing"

    def test_override_on_a_non_refusing_state_is_not_flagged_overridden(self, git_repo):
        # An override on a tree that would not have refused anyway carries
        # no meaningful "overridden" signal -- nothing was actually
        # overridden.
        verdict = check_working_tree_contention(
            git_repo, enabled=True, branch_pattern=_PATTERN, override=True
        )
        assert verdict.overridden is False


class TestNoStateBetweenInvocations:
    """Two independent calls against the same on-disk tree with no shared
    fixture state must each independently recompute their own verdict from
    live git state — this is the entire point of the module (no lock file,
    no TTL, no release step to forget)."""

    def test_two_calls_recompute_independently(self, git_repo):
        _git(["checkout", "-b", "feat/lr-1-thing"], git_repo)

        with pytest.raises(WorkingTreeContentionError):
            check_working_tree_contention(
                git_repo, enabled=True, branch_pattern=_PATTERN, override=False
            )

        # Simulate the branch landing (the ordinary NORMAL path the
        # crew-side predecessor self-blocked on): back on the default
        # branch, the very next invocation proceeds -- nothing was ever
        # "held" that needed releasing.
        _git(["checkout", "main"], git_repo)
        verdict = check_working_tree_contention(
            git_repo, enabled=True, branch_pattern=_PATTERN, override=False
        )
        assert verdict.in_flight is False


class TestGitReadFailureIsASoftFail:
    def test_not_a_git_repo_never_refuses(self, tmp_path, monkeypatch):
        # Not a git repo at all: _current_branch's `symbolic-ref` read fails,
        # which resolves to an empty branch name -- "does not match the
        # in-flight pattern" rather than an error. This check's own inability
        # to identify a repo must never refuse a write that would otherwise
        # be clean.
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        verdict = check_working_tree_contention(
            not_a_repo, enabled=True, branch_pattern=_PATTERN, override=False
        )
        assert verdict.in_flight is False

    def test_dirtiness_read_failure_soft_fails_and_proceeds(self, git_repo, monkeypatch):
        # A matching branch name (the check would otherwise proceed to the
        # dirtiness read) whose `git status` read itself fails -- this
        # check's own execution failure must never refuse a write on its
        # own; see push.contention_check.ContentionCheckUnavailableError's
        # own docstring.
        _git(["checkout", "-b", "feat/lr-1-thing"], git_repo)

        import subprocess as _subprocess

        real_run = _subprocess.run

        def _fake_run(cmd, **kwargs):
            if cmd[:2] == ["git", "status"]:
                return _subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="boom")
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(
            "clagentic_loadout.push.contention_check.subprocess.run", _fake_run
        )

        verdict = check_working_tree_contention(
            git_repo, enabled=True, branch_pattern=_PATTERN, override=False
        )
        assert verdict.in_flight is False
        assert "could not complete" in verdict.reason
