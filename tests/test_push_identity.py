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

from clagentic_loadout.push.errors import AuthorMismatchError, DirtyWorkTreeError
from clagentic_loadout.push.identity import (
    AmbiguousExclusionRefError,
    check_clean_work_tree,
    get_head_author_email,
    pin_commits_to_bot_identity,
    reauthor_commits,
    resolve_exclusion_ref,
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


class TestResolveExclusionRefMergeBaseBleed:
    """lr-501695 (MILLER diagnosis, P1): resolve_exclusion_ref must return
    the TRUE MERGE BASE, never a branch ref directly -- a straight
    "prefer origin/<base> over local <base>" choice only guards a lagging
    LOCAL base ref; when the REMOTE-TRACKING ref is the stale one instead
    (local main merged/fast-forwarded ahead of origin/main without a
    subsequent fetch), the old logic let the rewrite range over-extend
    past the true merge base and re-stamp already-landed commits with new
    SHAs and bot-identity authorship.

    Both directions are covered here: the NEW failure mode this task
    fixes (remote-tracking ref stale, local ahead by a merge commit) and
    the ORIGINAL failure mode the pre-fix code was written to guard
    against (local ref stale) -- the fix must not regress the case it
    already handled correctly.
    """

    def test_local_base_ahead_of_origin_by_merge_commit_excludes_landed_commits(self, tmp_path):
        """THE REGRESSION THIS TASK REQUIRES (lr-501695 task description,
        OBSERVED GEOMETRY): local main is AHEAD of origin/main by an
        already-landed merge commit and another already-landed commit --
        origin/main is BEHIND but still an ancestor of local main/HEAD
        (the diagnosed shape exactly: "origin/main and local main differed
        by two commits; the range origin/main..HEAD contained THREE
        commits"). This is the geometry the task explicitly notes a bare
        `merge-base(origin/main, HEAD)` computation does NOT fix on its
        own, since origin/main's own merge-base with HEAD is itself when
        it is a plain ancestor -- the fix instead compares BOTH candidate
        refs' merge-base points and takes the more-advanced one (local
        main's, since it is the ref that actually observed the merge).

        The rewrite set (HEAD ^<resolved exclusion ref>) must contain ONLY
        the feature branch's own commit -- both already-landed commits
        must be excluded entirely, i.e. still reachable from the resolved
        exclusion ref, and must retain their original SHAs and authorship
        after the full re-authoring call."""
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

        # origin/main is frozen at this point -- simulating "this clone's
        # remote-tracking ref was never re-fetched" after local main
        # advances below. `git push` would otherwise auto-advance this
        # clone's own origin/main as a side effect, so the two landed
        # commits below are built directly on local main and NEVER pushed
        # from this clone -- honestly reproducing a remote-tracking ref
        # that genuinely never observed them (mirrors the diagnosed
        # incident: local main merged/fast-forwarded ahead of origin/main
        # without a subsequent fetch).
        stale_origin_main_sha = _git(["rev-parse", "origin/main"], repo).stdout.strip()

        # Two already-landed commits on local main: a real merge commit
        # (non-fast-forward, matching "this repo merges non-squash" from
        # the task description) plus a second plain commit -- matching the
        # OBSERVED GEOMETRY's "two commits" exactly.
        _git(["checkout", "-b", "landed-work"], repo)
        _git(["config", "user.email", "landed@example.com"], repo)
        _git(["config", "user.name", "Landed Author"], repo)
        (repo / "landed.txt").write_text("already landed\n")
        _git(["add", "landed.txt"], repo)
        _git(["commit", "-m", "already-landed commit"], repo)
        _git(["checkout", "main"], repo)
        _git(["config", "user.email", "base@example.com"], repo)
        _git(["config", "user.name", "Base Author"], repo)
        _git(["merge", "--no-ff", "-m", "merge already-landed work", "landed-work"], repo)
        landed_merge_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        (repo / "landed2.txt").write_text("also already landed\n")
        _git(["add", "landed2.txt"], repo)
        _git(["commit", "-m", "second already-landed commit"], repo)
        landed_second_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        _git(["checkout", "-b", "feature"], repo)
        _git(["config", "user.email", "original@example.com"], repo)
        _git(["config", "user.name", "Feature Author"], repo)
        (repo / "feature.txt").write_text("feature work\n")
        _git(["add", "feature.txt"], repo)
        _git(["commit", "-m", "feature commit"], repo)
        feature_sha_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        # Sanity: origin/main is indeed stale/behind, but still an ancestor
        # of local main/HEAD (the diagnosed shape, not a divergent ref).
        origin_main_sha = _git(["rev-parse", "origin/main"], repo).stdout.strip()
        assert origin_main_sha == stale_origin_main_sha
        assert origin_main_sha != landed_second_sha
        merge_base = _git(["merge-base", "origin/main", "main"], repo).stdout.strip()
        assert merge_base == origin_main_sha  # ancestor, not divergent

        exclusion_ref, _label = resolve_exclusion_ref("main", repo)
        assert exclusion_ref is not None

        rewrite_set = _git(
            ["log", "--format=%H", "HEAD", f"^{exclusion_ref}"], repo
        ).stdout.split()
        assert rewrite_set == [feature_sha_before]
        assert landed_merge_sha not in rewrite_set
        assert landed_second_sha not in rewrite_set

        # The full pin_commits_to_bot_identity path must leave both landed
        # commits' SHAs and authorship untouched.
        rewritten = pin_commits_to_bot_identity(
            "Bot Name", "bot@example.com", "main", repo,
        )
        assert rewritten is True
        merge_commit_after = _git(
            ["log", "-1", "--format=%H %ae", landed_merge_sha], repo
        ).stdout.strip()
        assert merge_commit_after == f"{landed_merge_sha} base@example.com"
        second_commit_after = _git(
            ["log", "-1", "--format=%H %ae", landed_second_sha], repo
        ).stdout.strip()
        assert second_commit_after == f"{landed_second_sha} base@example.com"
        assert get_head_author_email(repo) == "bot@example.com"

    def test_lagging_local_base_still_excludes_landed_commits(self, tmp_path):
        """ORIGINAL DIRECTION (the case the pre-fix code was written to
        guard against, must not regress): origin/main is CURRENT/ahead and
        the LOCAL main ref is stale (never fast-forwarded after a fetch).
        The rewrite set must still contain only the feature branch's own
        commit -- an already-landed commit reachable via the current
        origin/main must never enter the rewrite range."""
        origin = tmp_path / "origin.git"
        origin.mkdir()
        _git(["init", "--bare"], origin)

        seed = tmp_path / "seed"
        seed.mkdir()
        _git(["init", "-b", "main"], seed)
        _git(["config", "user.email", "base@example.com"], seed)
        _git(["config", "user.name", "Base Author"], seed)
        (seed / "README.md").write_text("hello\n")
        _git(["add", "README.md"], seed)
        _git(["commit", "-m", "initial commit"], seed)
        _git(["remote", "add", "origin", str(origin)], seed)
        _git(["push", "origin", "main"], seed)

        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main"], repo)
        _git(["config", "user.email", "base@example.com"], repo)
        _git(["config", "user.name", "Base Author"], repo)
        _git(["remote", "add", "origin", str(origin)], repo)
        _git(["fetch", "origin"], repo)
        _git(["checkout", "-b", "main", "origin/main"], repo)

        _git(["checkout", "-b", "feature"], repo)
        _git(["config", "user.email", "original@example.com"], repo)
        _git(["config", "user.name", "Feature Author"], repo)
        (repo / "feature.txt").write_text("feature work\n")
        _git(["add", "feature.txt"], repo)
        _git(["commit", "-m", "feature commit"], repo)
        feature_sha_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        # An already-landed commit pushed to origin AFTER this clone's local
        # main was created -- local main is now stale/behind origin/main,
        # and this clone's own local main ref is never advanced.
        _git(["checkout", "main"], seed)
        (seed / "landed.txt").write_text("already landed\n")
        _git(["add", "landed.txt"], seed)
        _git(["commit", "-m", "already-landed commit"], seed)
        landed_sha = _git(["rev-parse", "HEAD"], seed).stdout.strip()
        _git(["push", "origin", "main"], seed)

        _git(["fetch", "origin"], repo)
        origin_main_sha = _git(["rev-parse", "origin/main"], repo).stdout.strip()
        local_main_sha = _git(["rev-parse", "main"], repo).stdout.strip()
        assert origin_main_sha == landed_sha
        assert local_main_sha != landed_sha

        exclusion_ref, _label = resolve_exclusion_ref("main", repo)
        assert exclusion_ref is not None

        rewrite_set = _git(
            ["log", "--format=%H", "HEAD", f"^{exclusion_ref}"], repo
        ).stdout.split()
        assert rewrite_set == [feature_sha_before]
        assert landed_sha not in rewrite_set

        rewritten = pin_commits_to_bot_identity(
            "Bot Name", "bot@example.com", "main", repo,
        )
        assert rewritten is True
        landed_commit_after = _git(
            ["log", "-1", "--format=%H %ae", landed_sha], repo
        ).stdout.strip()
        assert landed_commit_after == f"{landed_sha} base@example.com"
        assert get_head_author_email(repo) == "bot@example.com"


class TestResolveExclusionRefDivergedBaseBranch:
    """lr-1cd30b (follow-up gap the lr-501695 security review named
    non-blocking): resolve_exclusion_ref's "more advanced of the two
    merge-bases" comparison assumes the two candidates' merge-base points
    are ancestor-comparable. Constructs the geometry where that assumption
    fails -- a merge commit on HEAD's own line joining two independently-
    evolved sides, one reachable only via the (diverged) remote-tracking
    ref's pre-divergence history, the other only via the local ref's --
    verified directly (git primitives, not by reasoning alone) to produce
    two merge-base points where NEITHER is an ancestor of the other.
    """

    def test_diverged_merge_base_points_raise_ambiguous_exclusion_ref(self, tmp_path):
        """The constructed geometry: root A, two independent lines (X off
        A, Y off A). Local `main` = X's tip. `origin/main` (remote-tracking)
        is force-pushed to Y's tip -- diverging it from local `main`, which
        shares only the root commit A with it. HEAD (`feature`) branches
        from local `main` (X), merges in line Y, then adds its own commit.

        merge-base(origin/main, HEAD) = Y (Y is reachable from HEAD only
        via the merge's second parent; X's line is not an ancestor of Y).
        merge-base(main, HEAD) = X (main IS X's tip, hence its own
        merge-base with anything reachable from it is itself).
        X != Y and neither is an ancestor of the other -- verified via `git
        merge-base --is-ancestor` in both directions before asserting
        against resolve_exclusion_ref, so this test fails loudly if the
        constructed geometry ever stops being genuinely diverged rather
        than silently asserting against a comparable pair.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main"], repo)
        _git(["config", "user.email", "base@example.com"], repo)
        _git(["config", "user.name", "Base Author"], repo)
        (repo / "root.txt").write_text("root\n")
        _git(["add", "root.txt"], repo)
        _git(["commit", "-m", "root commit"], repo)
        root_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        _git(["checkout", "-b", "line-x"], repo)
        (repo / "x.txt").write_text("x\n")
        _git(["add", "x.txt"], repo)
        _git(["commit", "-m", "commit X"], repo)
        x_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        _git(["checkout", "-b", "line-y", root_sha], repo)
        (repo / "y.txt").write_text("y\n")
        _git(["add", "y.txt"], repo)
        _git(["commit", "-m", "commit Y"], repo)
        y_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        _git(["checkout", "main"], repo)
        _git(["reset", "--hard", "line-x"], repo)

        _git(["checkout", "-b", "feature", "main"], repo)
        _git(["merge", "--no-ff", "-m", "merge line-y into feature", "line-y"], repo)
        (repo / "feature.txt").write_text("feature work\n")
        _git(["add", "feature.txt"], repo)
        _git(["commit", "-m", "feature commit"], repo)

        origin = tmp_path / "origin.git"
        origin.mkdir()
        _git(["init", "--bare"], origin)
        _git(["push", str(origin), "line-y:refs/heads/main"], repo)
        _git(["remote", "add", "origin", str(origin)], repo)
        _git(["fetch", "origin"], repo)

        assert _git(["rev-parse", "origin/main"], repo).stdout.strip() == y_sha
        assert _git(["rev-parse", "main"], repo).stdout.strip() == x_sha

        mb_origin = _git(["merge-base", "origin/main", "feature"], repo).stdout.strip()
        mb_local = _git(["merge-base", "main", "feature"], repo).stdout.strip()
        assert mb_origin == y_sha
        assert mb_local == x_sha
        assert mb_origin != mb_local

        is_ancestor_origin_of_local = subprocess.run(
            ["git", "merge-base", "--is-ancestor", mb_origin, mb_local],
            cwd=str(repo), capture_output=True, text=True,
        ).returncode
        is_ancestor_local_of_origin = subprocess.run(
            ["git", "merge-base", "--is-ancestor", mb_local, mb_origin],
            cwd=str(repo), capture_output=True, text=True,
        ).returncode
        assert is_ancestor_origin_of_local != 0
        assert is_ancestor_local_of_origin != 0

        with pytest.raises(AmbiguousExclusionRefError) as exc_info:
            resolve_exclusion_ref("main", repo)
        message = str(exc_info.value)
        assert "diverged" in message
        assert mb_origin in message
        assert mb_local in message

    def test_reauthor_commits_fails_closed_on_diverged_base_branch(self, tmp_path):
        """The same geometry via reauthor_commits/pin_commits_to_bot_identity:
        the ambiguous floor must be refused BEFORE `git filter-branch` ever
        runs (never a silent re-authoring on a guessed floor), and the
        cause must be visible -- reauthor_commits returns (False, cause)
        naming both candidate SHAs, and pin_commits_to_bot_identity embeds
        that cause in the raised AuthorMismatchError (the same fail-closed
        surface every other re-authoring failure uses, see
        TestReauthorCommitsPropagatesStderr below) -- so an operator or CI
        log sees exactly why the push was refused, matching PR #21's own
        stderr-visibility requirement for this failure path."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main"], repo)
        _git(["config", "user.email", "base@example.com"], repo)
        _git(["config", "user.name", "Base Author"], repo)
        (repo / "root.txt").write_text("root\n")
        _git(["add", "root.txt"], repo)
        _git(["commit", "-m", "root commit"], repo)
        root_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        _git(["checkout", "-b", "line-x"], repo)
        (repo / "x.txt").write_text("x\n")
        _git(["add", "x.txt"], repo)
        _git(["commit", "-m", "commit X"], repo)

        _git(["checkout", "-b", "line-y", root_sha], repo)
        (repo / "y.txt").write_text("y\n")
        _git(["add", "y.txt"], repo)
        _git(["commit", "-m", "commit Y"], repo)
        y_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        _git(["checkout", "main"], repo)
        _git(["reset", "--hard", "line-x"], repo)
        x_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        _git(["checkout", "-b", "feature", "main"], repo)
        _git(["merge", "--no-ff", "-m", "merge line-y into feature", "line-y"], repo)
        _git(["config", "user.email", "original@example.com"], repo)
        _git(["config", "user.name", "Feature Author"], repo)
        (repo / "feature.txt").write_text("feature work\n")
        _git(["add", "feature.txt"], repo)
        _git(["commit", "-m", "feature commit"], repo)
        head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        origin = tmp_path / "origin.git"
        origin.mkdir()
        _git(["init", "--bare"], origin)
        _git(["push", str(origin), "line-y:refs/heads/main"], repo)
        _git(["remote", "add", "origin", str(origin)], repo)
        _git(["fetch", "origin"], repo)

        ok, cause = reauthor_commits("main", "Bot Name", "bot@example.com", repo)
        assert ok is False
        assert "diverged" in cause
        assert x_sha in cause
        assert y_sha in cause

        # Refused BEFORE filter-branch ran -- HEAD and its author are
        # completely untouched.
        assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == head_before
        assert get_head_author_email(repo) == "original@example.com"

        with pytest.raises(AuthorMismatchError) as exc_info:
            pin_commits_to_bot_identity("Bot Name", "bot@example.com", "main", repo)
        assert "diverged" in str(exc_info.value)
        assert get_head_author_email(repo) == "original@example.com"


class TestCheckCleanWorkTree:
    """lr-4cd7ac (MILLER diagnosis lr-60781e): a dirty tracked work tree is
    a LOCAL, RECOVERABLE precondition failure -- `git filter-branch`
    refuses outright before it ever starts rewriting -- and must be
    reported distinctly from a genuine identity mismatch, never folded
    into AuthorMismatchError's mis-attribution framing."""

    def test_clean_tree_is_a_noop(self, tmp_path):
        repo = _init_repo_with_base_and_branch(tmp_path, author_email="original@example.com")
        check_clean_work_tree(repo)  # must not raise

    def test_dirty_tracked_file_raises_naming_the_file(self, tmp_path):
        repo = _init_repo_with_base_and_branch(tmp_path, author_email="original@example.com")
        (repo / "feature.txt").write_text("unstaged edit\n")

        with pytest.raises(DirtyWorkTreeError) as exc_info:
            check_clean_work_tree(repo)
        message = str(exc_info.value)
        assert "feature.txt" in message
        assert "unstaged changes" in message
        assert "LOCAL, RECOVERABLE" in message

    def test_untracked_file_alone_does_not_raise(self, tmp_path):
        """check_clean_work_tree mirrors filter-branch's own precondition
        (tracked working tree/index vs HEAD) -- an untracked file is a
        cleanliness_check concern (push.cleanliness_check), not this one."""
        repo = _init_repo_with_base_and_branch(tmp_path, author_email="original@example.com")
        (repo / "untracked.txt").write_text("stray file\n")
        check_clean_work_tree(repo)  # must not raise

    def test_non_repo_is_a_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        check_clean_work_tree(not_a_repo)  # must not raise


class TestReauthorCommitsPropagatesStderr:
    """lr-4cd7ac (MILLER diagnosis lr-60781e): reauthor_commits' failure
    return must carry the real `git filter-branch` stderr cause, and
    pin_commits_to_bot_identity must embed it in the raised
    AuthorMismatchError, rather than discarding it (the pre-fix behavior:
    result.stderr was captured and never read anywhere)."""

    def test_reauthor_commits_returns_cause_on_dirty_tree_filter_branch_failure(self, tmp_path):
        """reauthor_commits() itself is called directly here (bypassing
        both check_clean_work_tree and pin_commits_to_bot_identity) so this
        exercises the REAL `git filter-branch` failure path end to end --
        the exact discarded-stderr defect MILLER diagnosed: filter-branch's
        own precondition check ('You have unstaged changes') firing and its
        stderr making it all the way back to the caller."""
        repo = _init_repo_with_base_and_branch(tmp_path, author_email="original@example.com")
        (repo / "feature.txt").write_text("unstaged edit\n")

        ok, cause = reauthor_commits(
            "main", "Bot Name", "bot@example.com", repo,
        )
        assert ok is False
        assert cause != ""
        assert "unstaged changes" in cause.lower()

    def test_pin_commits_error_message_embeds_the_real_filter_branch_cause(self, tmp_path):
        """Regression for lr-4cd7ac (MILLER diagnosis lr-60781e): the
        AuthorMismatchError raised by pin_commits_to_bot_identity must
        embed the real filter-branch stderr, not a generic message with no
        diagnostic content. pin_commits_to_bot_identity does not itself run
        the check_clean_work_tree pre-flight (that is verb.py's job, see
        TestBotIdentity.test_dirty_work_tree_fails_with_a_distinct_message_
        before_reauthoring in test_push_verb.py for the pre-flight's own
        coverage) -- calling it directly here on a dirty tree exercises the
        underlying filter-branch-failure-propagation fix in isolation."""
        repo = _init_repo_with_base_and_branch(tmp_path, author_email="original@example.com")
        (repo / "feature.txt").write_text("unstaged edit\n")

        with pytest.raises(AuthorMismatchError) as exc_info:
            pin_commits_to_bot_identity(
                "Bot Name", "bot@example.com", "main", repo,
            )
        message = str(exc_info.value)
        assert "Cause:" in message
        assert "unstaged changes" in message.lower()
        assert message.strip().endswith(
            "unrecoverable; fix the underlying failure and retry."
        )
