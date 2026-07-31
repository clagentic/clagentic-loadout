"""test_push_branch_commit_check.py — unit tests for
clagentic_loadout.push.branch_commit_check (lr-dd1742).

Covers detection semantics against a REAL two-remote git setup (no mocked
git): a base branch is fetched for comparison ONLY (never a local branch
read), every commit in <fetched base>..HEAD is checked against the same
Conventional Commits grammar merge.commit_subjects already applies at merge
time; a stray, GitHub-shaped merge-commit subject on the branch is detected;
a clean branch passes; merge_method != 'merge' and skip=True both no-op
regardless of branch content; an unreachable remote/base is a
CommitCheckUnavailableError (check-execution failure), never a false pass or
a false refusal.
"""

from __future__ import annotations

import subprocess

import pytest

from clagentic_loadout.merge.commit_subjects import REAL_MERGE_METHOD
from clagentic_loadout.push.branch_commit_check import (
    CommitCheckUnavailableError,
    StrayMergeCommitError,
    check_branch_for_stray_merge_commits,
    find_non_conformant_branch_commits,
)


def _git(args: list[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


@pytest.fixture
def remote_and_clone(tmp_path):
    """A bare 'origin' repo with a `main` branch, plus a clone of it (the
    fixture's own working tree) already checked out on `main`. Tests advance
    the clone onto a feature branch and diverge origin's `main` independently
    to simulate "another PR already landed/opened on origin while this
    branch was being built."
    """
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", "-b", "main", str(origin)], tmp_path)

    clone = tmp_path / "clone"
    _git(["clone", str(origin), str(clone)], tmp_path)
    _git(["config", "user.email", "author@example.com"], clone)
    _git(["config", "user.name", "Author"], clone)
    (clone / "README.md").write_text("hello\n")
    _git(["add", "README.md"], clone)
    _git(["commit", "-m", "chore: initial commit"], clone)
    _git(["push", "origin", "main"], clone)

    return origin, clone


def _checkout_feature_branch(clone, name="feature") -> None:
    _git(["checkout", "-b", name], clone)


class TestFindNonConformantBranchCommits:
    def test_conformant_branch_has_no_offenders(self, remote_and_clone):
        _origin, clone = remote_and_clone
        _checkout_feature_branch(clone)
        (clone / "a.txt").write_text("a\n")
        _git(["add", "a.txt"], clone)
        _git(["commit", "-m", "feat: add a"], clone)

        offenders = find_non_conformant_branch_commits(clone, "main")
        assert offenders == []

    def test_stray_github_merge_commit_is_detected(self, remote_and_clone):
        _origin, clone = remote_and_clone
        _checkout_feature_branch(clone)
        # Simulate a stray, not-yet-landed merge commit already sitting on
        # the branch before this feature branch's own work was added -- the
        # exact shape a GitHub-generated merge commit has.
        _git(
            ["commit", "--allow-empty", "-m",
             "Merge pull request #377 from clagentic/fix/lr-f22787-x"],
            clone,
        )
        (clone / "b.txt").write_text("b\n")
        _git(["add", "b.txt"], clone)
        _git(["commit", "-m", "feat: add b"], clone)

        offenders = find_non_conformant_branch_commits(clone, "main")
        assert len(offenders) == 1
        sha, subject = offenders[0]
        assert subject == "Merge pull request #377 from clagentic/fix/lr-f22787-x"
        assert len(sha) == 40

    def test_comparison_is_against_fetched_origin_not_local_main(self, remote_and_clone):
        """The defect class this module closes: a stale LOCAL main must
        never hide an offending commit. Diverge origin's main (simulating
        another PR landing) so the fetched ref differs from whatever local
        `main` this working tree happens to have -- the check must range
        against the FRESHLY FETCHED ref, not a local one."""
        origin, clone = remote_and_clone
        _checkout_feature_branch(clone)
        (clone / "b.txt").write_text("b\n")
        _git(["add", "b.txt"], clone)
        _git(["commit", "-m", "feat: add b"], clone)

        # Advance origin/main independently (another PR landed) without
        # updating the clone's own local main ref at all.
        other_clone_dir = clone.parent / "other-clone"
        _git(["clone", str(origin), str(other_clone_dir)], clone.parent)
        _git(["config", "user.email", "author@example.com"], other_clone_dir)
        _git(["config", "user.name", "Author"], other_clone_dir)
        (other_clone_dir / "c.txt").write_text("c\n")
        _git(["add", "c.txt"], other_clone_dir)
        _git(["commit", "-m", "feat: unrelated change on main"], other_clone_dir)
        _git(["push", "origin", "main"], other_clone_dir)

        # The clone's local `main` ref is now STALE relative to origin -- the
        # check must still correctly find zero offenders (the feature
        # branch's own single conformant commit), proving it fetched fresh
        # rather than trusting a local ref.
        offenders = find_non_conformant_branch_commits(clone, "main")
        assert offenders == []

    def test_multiline_commit_message_checks_first_line_only(self, remote_and_clone):
        _origin, clone = remote_and_clone
        _checkout_feature_branch(clone)
        (clone / "a.txt").write_text("a\n")
        _git(["add", "a.txt"], clone)
        _git(
            ["commit", "-m", "feat: add a", "-m", "lr-9999: this is body text only"],
            clone,
        )
        offenders = find_non_conformant_branch_commits(clone, "main")
        assert offenders == []

    def test_unreachable_remote_raises_check_unavailable(self, remote_and_clone, monkeypatch):
        _origin, clone = remote_and_clone
        _checkout_feature_branch(clone)
        _git(["remote", "set-url", "origin", "https://127.0.0.1:1/nonexistent.git"], clone)
        with pytest.raises(CommitCheckUnavailableError):
            find_non_conformant_branch_commits(clone, "main")

    def test_nonexistent_base_branch_raises_check_unavailable(self, remote_and_clone):
        _origin, clone = remote_and_clone
        _checkout_feature_branch(clone)
        with pytest.raises(CommitCheckUnavailableError):
            find_non_conformant_branch_commits(clone, "does-not-exist")


class TestCheckBranchForStrayMergeCommits:
    def test_real_merge_method_refuses_on_stray_commit(self, remote_and_clone):
        _origin, clone = remote_and_clone
        _checkout_feature_branch(clone)
        _git(
            ["commit", "--allow-empty", "-m",
             "Merge pull request #378 from clagentic/fix/lr-f969fc-y"],
            clone,
        )
        with pytest.raises(StrayMergeCommitError) as exc_info:
            check_branch_for_stray_merge_commits(
                clone, "main", merge_method=REAL_MERGE_METHOD,
            )
        message = str(exc_info.value)
        assert "Merge pull request #378 from clagentic/fix/lr-f969fc-y" in message
        assert "git fetch origin main" in message
        assert "git rebase origin/main" in message
        assert exc_info.value.offenders[0][1] == (
            "Merge pull request #378 from clagentic/fix/lr-f969fc-y"
        )

    def test_conformant_branch_passes(self, remote_and_clone):
        _origin, clone = remote_and_clone
        _checkout_feature_branch(clone)
        (clone / "a.txt").write_text("a\n")
        _git(["add", "a.txt"], clone)
        _git(["commit", "-m", "feat: add a"], clone)
        check_branch_for_stray_merge_commits(
            clone, "main", merge_method=REAL_MERGE_METHOD,
        )  # no raise

    @pytest.mark.parametrize("merge_method", ["squash", "rebase", "anything-else"])
    def test_non_merge_method_is_noop_even_with_stray_commit(self, remote_and_clone, merge_method):
        _origin, clone = remote_and_clone
        _checkout_feature_branch(clone)
        _git(
            ["commit", "--allow-empty", "-m", "Merge pull request #1 from x/y"],
            clone,
        )
        check_branch_for_stray_merge_commits(
            clone, "main", merge_method=merge_method,
        )  # no raise -- no-op for any non-"merge" method

    def test_skip_bypasses_even_a_stray_commit_on_real_merge(self, remote_and_clone):
        _origin, clone = remote_and_clone
        _checkout_feature_branch(clone)
        _git(
            ["commit", "--allow-empty", "-m", "Merge pull request #1 from x/y"],
            clone,
        )
        check_branch_for_stray_merge_commits(
            clone, "main", merge_method=REAL_MERGE_METHOD, skip=True,
        )  # no raise

    def test_unreachable_remote_raises_check_unavailable_not_a_pass_or_refusal(
        self, remote_and_clone
    ):
        _origin, clone = remote_and_clone
        _checkout_feature_branch(clone)
        _git(["remote", "set-url", "origin", "https://127.0.0.1:1/nonexistent.git"], clone)
        with pytest.raises(CommitCheckUnavailableError):
            check_branch_for_stray_merge_commits(clone, "main", merge_method=REAL_MERGE_METHOD)
