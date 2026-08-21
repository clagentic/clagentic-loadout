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
        HEAD already matches -- the overall call succeeds with no rewrite
        performed at all."""
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
        nothing to rewrite, and the post-rewrite verify step correctly
        refuses rather than silently reporting success for an unmatched
        HEAD."""
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


class TestReauthorCommitsNeverTouchesWorkingTree:
    """lr-ac7bb0: reauthor_commits' rewrite only ever reads/writes git
    objects, so working-tree content the caller has not yet committed --
    staged, unstaged, or untracked -- must survive a re-authoring rewrite
    byte-for-byte. The prior `git filter-branch` mechanism's terminal `git
    read-tree -u -m HEAD` checked out the newly rewritten tree over the
    working tree; this class is the regression coverage for the primitive
    swap that removes that step entirely."""

    def test_staged_but_uncommitted_change_survives_reauthoring(self, tmp_path):
        repo = _init_repo_with_base_and_branch(tmp_path, author_email="original@example.com")
        (repo / "feature.txt").write_text("staged edit, not yet committed\n")
        _git(["add", "feature.txt"], repo)

        rewritten = pin_commits_to_bot_identity(
            "Bot Name", "bot@example.com", "main", repo,
        )
        assert rewritten is True
        assert get_head_author_email(repo) == "bot@example.com"
        assert (repo / "feature.txt").read_text() == "staged edit, not yet committed\n"
        staged = _git(["diff", "--cached", "--name-only"], repo).stdout.strip()
        assert staged == "feature.txt"

    def test_untracked_file_survives_reauthoring(self, tmp_path):
        repo = _init_repo_with_base_and_branch(tmp_path, author_email="original@example.com")
        (repo / "untracked.txt").write_text("stray file, never added\n")

        rewritten = pin_commits_to_bot_identity(
            "Bot Name", "bot@example.com", "main", repo,
        )
        assert rewritten is True
        assert get_head_author_email(repo) == "bot@example.com"
        assert (repo / "untracked.txt").read_text() == "stray file, never added\n"
        untracked = _git(
            ["ls-files", "--others", "--exclude-standard"], repo
        ).stdout.strip()
        assert untracked == "untracked.txt"

    def test_merge_commit_inside_rewrite_range_is_rebuilt_correctly(self, tmp_path):
        """A merge commit that is ITSELF part of the rewrite range (not an
        already-landed one excluded by the floor) must be rebuilt with both
        parents remapped to their own rebuilt SHAs, its original tree
        (content) preserved, and its original message preserved -- exactly
        the geometry `git commit-tree`'s per-parent remap in reauthor_commits
        exists to get right without ever checking anything out."""
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

        _git(["checkout", "-b", "side"], repo)
        _git(["config", "user.email", "original@example.com"], repo)
        _git(["config", "user.name", "Feature Author"], repo)
        (repo / "side.txt").write_text("side work\n")
        _git(["add", "side.txt"], repo)
        _git(["commit", "-m", "side commit"], repo)

        _git(["checkout", "-b", "feature", "main"], repo)
        (repo / "feature.txt").write_text("feature work\n")
        _git(["add", "feature.txt"], repo)
        _git(["commit", "-m", "feature commit"], repo)
        _git(["merge", "--no-ff", "-m", "merge side into feature", "side"], repo)
        merge_tree_before = _git(["rev-parse", "HEAD^{tree}"], repo).stdout.strip()

        rewritten = pin_commits_to_bot_identity(
            "Bot Name", "bot@example.com", "main", repo,
        )
        assert rewritten is True

        head_after = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        parents_after = _git(["rev-parse", "HEAD^@"], repo).stdout.split()
        assert len(parents_after) == 2

        merge_subject = _git(["log", "-1", "--format=%s", head_after], repo).stdout.strip()
        assert merge_subject == "merge side into feature"
        merge_tree_after = _git(["rev-parse", f"{head_after}^{{tree}}"], repo).stdout.strip()
        assert merge_tree_after == merge_tree_before

        # Both parents (the rebuilt feature-branch tip and the rebuilt side
        # tip) must carry the bot identity -- neither is reachable from
        # main, so both are inside the rewrite range.
        for parent_sha in parents_after:
            assert (
                _git(["log", "-1", "--format=%ae", parent_sha], repo).stdout.strip()
                == "bot@example.com"
            )


def _commit_with_raw_message(
    repo: Path, message_bytes: bytes, *, env: dict[str, str] | None = None
) -> None:
    """Create a commit whose message is EXACTLY *message_bytes* -- bypasses
    `git commit -m`, which normalizes trailing whitespace, by piping raw
    bytes into `git commit -F -` (`text=False`, no decode/re-encode
    anywhere in this helper)."""
    subprocess.run(
        ["git", "commit", "-F", "-"],
        cwd=str(repo),
        input=message_bytes,
        capture_output=True,
        check=True,
        env=env,
    )


def _git_bytes(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Like _git, but never decodes stdout/stderr -- for a git subcommand
    (e.g. `checkout`) whose own output embeds a commit subject that may not
    be valid UTF-8, where this test helper has no reason to read the
    output as text at all."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=False, check=True
    )


def _commit_with_exact_object_message(repo: Path, message_bytes: bytes) -> None:
    """Create a commit on HEAD whose stored message is EXACTLY
    *message_bytes*, bypassing `git commit`'s own message normalization
    entirely (which collapses runs of trailing blank lines -- `git commit
    -F -` cannot produce a message with more than one trailing newline no
    matter what is piped into it, so this helper is what makes a test of
    "does an unusual-but-real stored message survive a rewrite unchanged"
    possible at all: it builds a real commit OBJECT directly with
    `git hash-object` + `git update-ref`, the same primitives
    `_rebuild_commit` itself uses, so the resulting HEAD carries genuinely
    non-normalized message bytes to rewrite)."""
    tree = subprocess.run(
        ["git", "write-tree"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    parent = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()
    name_email = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>"], cwd=str(repo),
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    header = f"tree {tree}\nparent {parent}\nauthor {name_email} 1700000000 +0000\ncommitter {name_email} 1700000000 +0000\n\n"
    new_object = header.encode("utf-8") + message_bytes
    new_sha = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
        cwd=str(repo), input=new_object, capture_output=True, check=True,
    ).stdout.strip().decode("ascii")
    subprocess.run(
        ["git", "update-ref", "HEAD", new_sha], cwd=str(repo), capture_output=True, check=True,
    )


class TestReauthorCommitsMessageFidelity:
    """lr-ac7bb0 follow-up (PR #27 review): reauthor_commits must
    preserve a commit message's exact original bytes, including whatever
    encoding it was written in and its own trailing-whitespace shape --
    never re-add a trailing newline (which made re-authoring
    NON-IDEMPOTENT: an already-bot-identity branch produced a new SHA on
    every re-authoring pass instead of being a stable no-op), and never
    force a UTF-8 decode that raises on a legacy-encoded message instead of
    returning (False, cause)."""

    def test_reauthoring_is_idempotent(self, tmp_path):
        """Re-authoring an already-bot-identity branch a second time must
        produce the IDENTICAL SHA, not a new one -- the direct regression
        test for the trailing-newline-accumulation defect: `git log
        --format=%B` appends its own newline on top of the message's own
        terminator, so a naive read-message/write-message round trip grows
        the message (and therefore changes the SHA) on every pass."""
        repo = _init_repo_with_base_and_branch(tmp_path, author_email="original@example.com")

        first_pass = pin_commits_to_bot_identity(
            "Bot Name", "bot@example.com", "main", repo,
        )
        assert first_pass is True
        sha_after_first = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        second_pass_ok, second_pass_cause = reauthor_commits(
            "main", "Bot Name", "bot@example.com", repo,
        )
        assert second_pass_ok is True
        assert second_pass_cause == ""
        sha_after_second = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        assert sha_after_second == sha_after_first

    def test_message_with_trailing_blank_line_survives_byte_exact(self, tmp_path):
        """An intentional trailing blank line in the original message must
        survive -- the fix must NOT `.rstrip()` the message (that would
        trade the newline-growth bug for a newline-loss bug on exactly this
        input). `git commit` itself normalizes away a message's own
        trailing blank lines at commit-creation time (it cannot produce
        this input), so the test commit is built directly via
        `_commit_with_exact_object_message` -- the same underlying
        primitives (`hash-object` + `update-ref`) `_rebuild_commit` itself
        uses -- to genuinely exercise a stored message with more than one
        trailing newline."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main"], repo)
        _git(["config", "user.email", "base@example.com"], repo)
        _git(["config", "user.name", "Base Author"], repo)
        (repo / "README.md").write_text("hello\n")
        _git(["add", "README.md"], repo)
        _git(["commit", "-m", "initial commit"], repo)

        _git(["checkout", "-b", "feature"], repo)
        _git(["config", "user.email", "original@example.com"], repo)
        _git(["config", "user.name", "Feature Author"], repo)
        (repo / "feature.txt").write_text("feature work\n")
        _git(["add", "feature.txt"], repo)
        _git(["commit", "-m", "feature commit"], repo)
        original_message = b"subject line\n\nbody paragraph one.\n\nbody paragraph two.\n\n"
        _commit_with_exact_object_message(repo, original_message)
        original_raw = subprocess.run(
            ["git", "cat-file", "commit", "HEAD"],
            cwd=str(repo), capture_output=True, check=True,
        ).stdout
        original_header, original_msg_bytes = original_raw.split(b"\n\n", 1)
        assert original_msg_bytes == original_message

        rewritten = pin_commits_to_bot_identity(
            "Bot Name", "bot@example.com", "main", repo,
        )
        assert rewritten is True

        rewritten_raw = subprocess.run(
            ["git", "cat-file", "commit", "HEAD"],
            cwd=str(repo), capture_output=True, check=True,
        ).stdout
        _rewritten_header, rewritten_msg_bytes = rewritten_raw.split(b"\n\n", 1)
        assert rewritten_msg_bytes == original_message

    def test_multi_paragraph_message_survives_byte_exact(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main"], repo)
        _git(["config", "user.email", "base@example.com"], repo)
        _git(["config", "user.name", "Base Author"], repo)
        (repo / "README.md").write_text("hello\n")
        _git(["add", "README.md"], repo)
        _git(["commit", "-m", "initial commit"], repo)

        _git(["checkout", "-b", "feature"], repo)
        _git(["config", "user.email", "original@example.com"], repo)
        _git(["config", "user.name", "Feature Author"], repo)
        (repo / "feature.txt").write_text("feature work\n")
        _git(["add", "feature.txt"], repo)
        original_message = (
            b"fix(thing): do the thing\n\n"
            b"First paragraph explains the defect in detail, across\n"
            b"multiple lines of prose.\n\n"
            b"Second paragraph explains the fix.\n\n"
            b"Task: lr-example\n"
        )
        _commit_with_raw_message(repo, original_message)

        rewritten = pin_commits_to_bot_identity(
            "Bot Name", "bot@example.com", "main", repo,
        )
        assert rewritten is True

        rewritten_raw = subprocess.run(
            ["git", "cat-file", "commit", "HEAD"],
            cwd=str(repo), capture_output=True, check=True,
        ).stdout
        _rewritten_header, rewritten_msg_bytes = rewritten_raw.split(b"\n\n", 1)
        assert rewritten_msg_bytes == original_message

    def test_non_utf8_message_rebuilds_with_encoding_header_intact(self, tmp_path):
        """A commit whose message is encoded per `i18n.commitEncoding`
        (here ISO-8859-1, carrying a byte -- 0xE9, 'e' + acute accent --
        that is not valid UTF-8 on its own) must rebuild successfully, with
        the exact original message bytes AND the commit object's own
        `encoding` header both preserved. A UTF-8 forced-decode anywhere in
        the rewrite path would raise UnicodeDecodeError on this input
        instead."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main"], repo)
        _git(["config", "user.email", "base@example.com"], repo)
        _git(["config", "user.name", "Base Author"], repo)
        _git(["config", "i18n.commitEncoding", "ISO-8859-1"], repo)
        (repo / "README.md").write_text("hello\n")
        _git(["add", "README.md"], repo)
        _git(["commit", "-m", "initial commit"], repo)

        _git(["checkout", "-b", "feature"], repo)
        _git(["config", "user.email", "original@example.com"], repo)
        _git(["config", "user.name", "Feature Author"], repo)
        (repo / "feature.txt").write_text("feature work\n")
        _git(["add", "feature.txt"], repo)
        # "caf\xe9" in ISO-8859-1 -- 0xE9 alone is not valid UTF-8.
        non_utf8_message = "café fix\n".encode("iso-8859-1")
        assert b"\xe9" in non_utf8_message
        with pytest.raises(UnicodeDecodeError):
            non_utf8_message.decode("utf-8")
        _commit_with_raw_message(repo, non_utf8_message)

        original_raw = subprocess.run(
            ["git", "cat-file", "commit", "HEAD"],
            cwd=str(repo), capture_output=True, check=True,
        ).stdout
        assert b"encoding ISO-8859-1" in original_raw

        rewritten = pin_commits_to_bot_identity(
            "Bot Name", "bot@example.com", "main", repo,
        )
        assert rewritten is True

        rewritten_raw = subprocess.run(
            ["git", "cat-file", "commit", "HEAD"],
            cwd=str(repo), capture_output=True, check=True,
        ).stdout
        assert b"encoding ISO-8859-1" in rewritten_raw
        rewritten_header, rewritten_msg_bytes = rewritten_raw.split(b"\n\n", 1)
        assert rewritten_msg_bytes == non_utf8_message

    def test_non_utf8_message_failure_path_returns_cause_not_raise(self, tmp_path):
        """A non-UTF-8 message on a detached-HEAD repo (a real,
        deterministic reauthor_commits failure -- see
        TestReauthorCommitsPropagatesStderr) must still return
        (False, cause), never raise -- confirms the byte-exact message path
        does not introduce a new way for a UTF-8 decode to escape the
        documented (False, cause) contract on ANY failure combination, not
        only the success path covered above."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main"], repo)
        _git(["config", "user.email", "base@example.com"], repo)
        _git(["config", "user.name", "Base Author"], repo)
        _git(["config", "i18n.commitEncoding", "ISO-8859-1"], repo)
        (repo / "README.md").write_text("hello\n")
        _git(["add", "README.md"], repo)
        _git(["commit", "-m", "initial commit"], repo)

        _git(["checkout", "-b", "feature"], repo)
        _git(["config", "user.email", "original@example.com"], repo)
        _git(["config", "user.name", "Feature Author"], repo)
        (repo / "feature.txt").write_text("feature work\n")
        _git(["add", "feature.txt"], repo)
        _commit_with_raw_message(repo, "café fix\n".encode("iso-8859-1"))
        _git_bytes(["checkout", "--detach", "HEAD"], repo)

        ok, cause = reauthor_commits("main", "Bot Name", "bot@example.com", repo)
        assert ok is False
        assert cause != ""
        assert "detached head" in cause.lower()


class TestRebuildCommitRejectsHeaderInjection:
    """lr-ac7bb0 follow-up (PR #27 security review, same defect
    independently found by two reviewers): `_rebuild_commit` f-string-
    interpolates bot_name/bot_email/dates directly into raw commit-object
    header bytes and hands them to `git hash-object`, which performs no
    validation. A newline embedded in any of the four values must be
    REFUSED via the documented (False, cause) surface -- never silently
    stripped, never raised uncaught, and never written as an actual commit
    object (asserted here by reading the object back afterward and
    confirming no injected header line landed)."""

    def _build_feature_branch(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main"], repo)
        _git(["config", "user.email", "base@example.com"], repo)
        _git(["config", "user.name", "Base Author"], repo)
        (repo / "README.md").write_text("hello\n")
        _git(["add", "README.md"], repo)
        _git(["commit", "-m", "initial commit"], repo)

        _git(["checkout", "-b", "feature"], repo)
        _git(["config", "user.email", "original@example.com"], repo)
        _git(["config", "user.name", "Feature Author"], repo)
        (repo / "feature.txt").write_text("feature work\n")
        _git(["add", "feature.txt"], repo)
        _git(["commit", "-m", "feature commit"], repo)
        return repo

    def test_newline_in_bot_name_is_refused(self, tmp_path):
        repo = self._build_feature_branch(tmp_path)
        head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        malicious_name = "Bot Name\ngpgsig -----BEGIN PGP SIGNATURE-----"

        with pytest.raises(AuthorMismatchError) as exc_info:
            pin_commits_to_bot_identity(malicious_name, "bot@example.com", "main", repo)
        message = str(exc_info.value)
        assert "Cause:" in message
        assert "newline" in message.lower()

        # HEAD must be completely untouched -- refused BEFORE any write.
        assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == head_before
        # No commit carrying an injected gpgsig line exists in the repo's
        # object store at all -- not just "HEAD didn't move", but "the
        # object was never created".
        fsck = subprocess.run(
            ["git", "fsck", "--unreachable", "--dangling"],
            cwd=str(repo), capture_output=True, text=True,
        )
        assert "gpgsig" not in fsck.stdout

    def test_newline_in_bot_email_is_refused(self, tmp_path):
        repo = self._build_feature_branch(tmp_path)
        head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        malicious_email = "bot@example.com\nparent 0000000000000000000000000000000000000000"

        with pytest.raises(AuthorMismatchError) as exc_info:
            pin_commits_to_bot_identity("Bot Name", malicious_email, "main", repo)
        message = str(exc_info.value)
        assert "Cause:" in message
        assert "newline" in message.lower()
        assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == head_before

    def test_carriage_return_in_bot_name_is_refused(self, tmp_path):
        repo = self._build_feature_branch(tmp_path)
        head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        with pytest.raises(AuthorMismatchError) as exc_info:
            pin_commits_to_bot_identity("Bot\rName", "bot@example.com", "main", repo)
        assert "carriage return" in str(exc_info.value).lower()
        assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == head_before

    def test_nul_in_bot_email_is_refused(self, tmp_path):
        repo = self._build_feature_branch(tmp_path)
        head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        with pytest.raises(AuthorMismatchError) as exc_info:
            pin_commits_to_bot_identity("Bot Name", "bot@ex\x00ample.com", "main", repo)
        assert "nul" in str(exc_info.value).lower()
        assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == head_before

    def test_leading_trailing_whitespace_in_bot_name_is_refused(self, tmp_path):
        repo = self._build_feature_branch(tmp_path)
        head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        with pytest.raises(AuthorMismatchError) as exc_info:
            pin_commits_to_bot_identity(" Bot Name", "bot@example.com", "main", repo)
        assert "whitespace" in str(exc_info.value).lower()
        assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == head_before

    def test_angle_bracket_in_bot_name_is_refused(self, tmp_path):
        repo = self._build_feature_branch(tmp_path)
        head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        with pytest.raises(AuthorMismatchError) as exc_info:
            pin_commits_to_bot_identity("Bot <Name", "bot@example.com", "main", repo)
        assert "'<' or '>'" in str(exc_info.value)
        assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == head_before

    def test_reauthor_commits_returns_false_cause_not_raise_on_injection(self, tmp_path):
        """The lower-level reauthor_commits (bypassing
        pin_commits_to_bot_identity's AuthorMismatchError wrapping) must
        itself return (False, cause) -- confirms the refusal is enforced
        at the actual write site, not merely re-framed one layer up."""
        repo = self._build_feature_branch(tmp_path)
        head_before = _git(["rev-parse", "HEAD"], repo).stdout.strip()

        ok, cause = reauthor_commits(
            "main", "Bot Name\nInjected", "bot@example.com", repo,
        )
        assert ok is False
        assert cause != ""
        assert "newline" in cause.lower()
        assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == head_before


class TestRejectDateInjection:
    """lr-ac7bb0 follow-up (PR #27 review): author_date/committer_date are
    interpolated into the same raw header bytes as bot_name/bot_email and
    are equally capable of injecting a newline -- covered directly against
    the private _reject_date_injection helper since a malformed date is
    not reachable through the public API (dates always come from
    `git log --date=raw` on a real commit, never from caller input),
    matching how _reject_ident_injection is exercised end-to-end (reachable
    via bot_name/bot_email) while its date counterpart is exercised as a
    unit."""

    def test_newline_in_date_is_rejected(self):
        from clagentic_loadout.push.identity import _reject_date_injection as reject

        cause = reject("author_date", "1700000000 +0000\ngpgsig evil")
        assert cause is not None
        assert "newline" in cause.lower()

    def test_carriage_return_in_date_is_rejected(self):
        from clagentic_loadout.push.identity import _reject_date_injection as reject

        cause = reject("committer_date", "1700000000 +0000\r")
        assert cause is not None
        assert "carriage return" in cause.lower()

    def test_nul_in_date_is_rejected(self):
        from clagentic_loadout.push.identity import _reject_date_injection as reject

        cause = reject("author_date", "1700000000\x00 +0000")
        assert cause is not None
        assert "nul" in cause.lower()

    def test_well_formed_raw_date_is_accepted(self):
        from clagentic_loadout.push.identity import _reject_date_injection as reject

        assert reject("author_date", "1700000000 +0000") is None


class TestRebuildCommitDropsExtraHeaders:
    """lr-ac7bb0 follow-up (PR #27 review): _rebuild_commit only
    ever reconstructs tree/parent(s)/author/committer/encoding -- every
    other header on the original object (gpgsig being the realistic case)
    is intentionally dropped, matching the prior git filter-branch
    mechanism's own behavior (a signature cannot remain valid over a
    rebuilt object's changed author/committer content regardless of which
    write mechanism performs the rewrite). This pins that as INTENDED
    behavior, not an oversight -- see _rebuild_commit's own docstring,
    "ONLY tree/parent(s).../ARE RECONSTRUCTED", for the full reasoning."""

    def test_commit_with_gpgsig_header_rebuilds_successfully_without_it(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(["init", "-b", "main"], repo)
        _git(["config", "user.email", "base@example.com"], repo)
        _git(["config", "user.name", "Base Author"], repo)
        (repo / "README.md").write_text("hello\n")
        _git(["add", "README.md"], repo)
        _git(["commit", "-m", "initial commit"], repo)

        _git(["checkout", "-b", "feature"], repo)
        _git(["config", "user.email", "original@example.com"], repo)
        _git(["config", "user.name", "Feature Author"], repo)
        (repo / "feature.txt").write_text("feature work\n")
        _git(["add", "feature.txt"], repo)
        _git(["commit", "-m", "feature commit"], repo)

        # Construct a commit object carrying a (fake, unverifiable --
        # correctness of the signature itself is irrelevant here, only
        # its PRESENCE as a header this rewrite must drop) multi-line
        # gpgsig header, using the same hash-object primitive the module
        # itself uses to build commits, so this test exercises a real
        # object shape rather than a synthetic string.
        tree = _git(["rev-parse", "HEAD^{tree}"], repo).stdout.strip()
        parent = _git(["rev-parse", "HEAD^"], repo).stdout.strip()
        ident = _git(["log", "-1", "--format=%an <%ae> %ad", "--date=raw", "HEAD"], repo).stdout.strip()
        fake_gpgsig = (
            "gpgsig -----BEGIN PGP SIGNATURE-----\n"
            " not a real signature, only shaped like one\n"
            " -----END PGP SIGNATURE-----"
        )
        header = (
            f"tree {tree}\n"
            f"parent {parent}\n"
            f"author {ident}\n"
            f"committer {ident}\n"
            f"{fake_gpgsig}\n"
            "\n"
        )
        new_object = header.encode("utf-8") + b"feature commit\n"
        new_sha = subprocess.run(
            ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
            cwd=str(repo), input=new_object, capture_output=True, check=True,
        ).stdout.strip().decode("ascii")
        _git(["update-ref", "HEAD", new_sha], repo)

        original_raw = subprocess.run(
            ["git", "cat-file", "commit", "HEAD"],
            cwd=str(repo), capture_output=True, check=True,
        ).stdout
        assert b"gpgsig -----BEGIN PGP SIGNATURE-----" in original_raw
        original_header, original_message = original_raw.split(b"\n\n", 1)

        rewritten = pin_commits_to_bot_identity(
            "Bot Name", "bot@example.com", "main", repo,
        )
        assert rewritten is True

        rewritten_raw = subprocess.run(
            ["git", "cat-file", "commit", "HEAD"],
            cwd=str(repo), capture_output=True, check=True,
        ).stdout
        rewritten_header, rewritten_message = rewritten_raw.split(b"\n\n", 1)

        # The signature is gone.
        assert b"gpgsig" not in rewritten_header
        assert b"PGP SIGNATURE" not in rewritten_raw
        # Everything else survived: tree unchanged, message byte-exact,
        # new identity applied.
        assert f"tree {tree}".encode("ascii") in rewritten_header
        assert rewritten_message == original_message
        assert get_head_author_email(repo) == "bot@example.com"


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
        the ambiguous floor must be refused BEFORE any rewrite ever runs
        (never a silent re-authoring on a guessed floor), and the
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

        # Refused BEFORE any rewrite ran -- HEAD and its author are
        # completely untouched.
        assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == head_before
        assert get_head_author_email(repo) == "original@example.com"

        with pytest.raises(AuthorMismatchError) as exc_info:
            pin_commits_to_bot_identity("Bot Name", "bot@example.com", "main", repo)
        assert "diverged" in str(exc_info.value)
        assert get_head_author_email(repo) == "original@example.com"


class TestCheckCleanWorkTree:
    """lr-4cd7ac (MILLER diagnosis lr-60781e; rationale updated lr-ac7bb0):
    a dirty tracked work tree is a LOCAL, RECOVERABLE condition and must be
    reported distinctly from a genuine identity mismatch, never folded into
    AuthorMismatchError's mis-attribution framing. This check is now a
    residual safety signal rather than a filter-branch precondition
    pre-empt -- see check_clean_work_tree's own docstring -- but its
    behavior (flags unstaged tracked changes, ignores untracked files) is
    unchanged."""

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
        """check_clean_work_tree only ever flags unstaged changes to
        TRACKED files -- an untracked file is a cleanliness_check concern
        (push.cleanliness_check), not this one."""
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
    return must carry a real, specific cause, and pin_commits_to_bot_identity
    must embed it in the raised AuthorMismatchError, rather than discarding
    it (the pre-fix behavior: the underlying rewrite's own failure detail
    was captured and never read anywhere).

    lr-ac7bb0 replaced the prior `git filter-branch` mechanism with a
    `git commit-tree`-based rewrite that never reads the working tree, so a
    dirty tracked working tree (this class's original failure trigger) no
    longer fails the rewrite at all -- see TestPinCommitsToBotIdentity /
    TestCheckCleanWorkTree elsewhere in this file for coverage that a dirty
    tree, while still guarded pre-flight by check_clean_work_tree in
    verb.py, no longer defeats reauthor_commits itself. The cause-
    propagation contract this class covers is exercised here instead via a
    detached HEAD -- a real, deterministic reauthor_commits failure the new
    mechanism raises on purpose (it must move a branch ref, and refuses to
    guess which one on a detached checkout)."""

    def test_reauthor_commits_returns_cause_on_detached_head(self, tmp_path):
        """reauthor_commits() itself is called directly here (bypassing
        both check_clean_work_tree and pin_commits_to_bot_identity) so this
        exercises the real detached-HEAD failure path end to end -- the
        cause must be specific and non-empty, never a generic message with
        no diagnostic content."""
        repo = _init_repo_with_base_and_branch(tmp_path, author_email="original@example.com")
        _git(["checkout", "--detach", "HEAD"], repo)

        ok, cause = reauthor_commits(
            "main", "Bot Name", "bot@example.com", repo,
        )
        assert ok is False
        assert cause != ""
        assert "detached head" in cause.lower()

    def test_pin_commits_error_message_embeds_the_real_cause(self, tmp_path):
        """Regression for lr-4cd7ac (MILLER diagnosis lr-60781e): the
        AuthorMismatchError raised by pin_commits_to_bot_identity must
        embed the real underlying cause, not a generic message with no
        diagnostic content. pin_commits_to_bot_identity does not itself run
        the check_clean_work_tree pre-flight (that is verb.py's job, see
        TestBotIdentity.test_dirty_work_tree_fails_with_a_distinct_message_
        before_reauthoring in test_push_verb.py for the pre-flight's own
        coverage) -- calling it directly here on a detached HEAD exercises
        the underlying failure-propagation fix in isolation."""
        repo = _init_repo_with_base_and_branch(tmp_path, author_email="original@example.com")
        _git(["checkout", "--detach", "HEAD"], repo)

        with pytest.raises(AuthorMismatchError) as exc_info:
            pin_commits_to_bot_identity(
                "Bot Name", "bot@example.com", "main", repo,
            )
        message = str(exc_info.value)
        assert "Cause:" in message
        assert "detached head" in message.lower()
        assert message.strip().endswith(
            "unrecoverable; fix the underlying failure and retry."
        )
