"""merge.commit_files and tree_sync.resolve_base_sha: reading a tracked file as
it exists at a commit, independent of the working tree."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.commit_files import (
    CommitFileReadError,
    read_file_at_commit,
    read_file_at_commit_following_links,
)
from clagentic_loadout.merge.tree_sync import resolve_base_sha
from tests._gate_repo import commit_files, git


def _repo(path):
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    return path


class TestReadFileAtCommit:
    def test_returns_the_committed_text_not_the_working_tree_text(self, tmp_path):
        repo = _repo(tmp_path / "r")
        sha = commit_files(repo, {"dir/f.txt": "committed\n"})
        (repo / "dir" / "f.txt").write_text("edited\n", encoding="utf-8")
        assert read_file_at_commit(repo, sha, "dir/f.txt") == "committed\n"

    def test_a_path_absent_at_the_commit_is_none_not_an_error(self, tmp_path):
        repo = _repo(tmp_path / "r")
        sha = commit_files(repo, {"a.txt": "a\n"})
        (repo / "b.txt").write_text("untracked\n", encoding="utf-8")
        assert read_file_at_commit(repo, sha, "b.txt") is None

    def test_reads_relative_to_the_repo_root_from_a_subdirectory_tree(self, tmp_path):
        repo = _repo(tmp_path / "r")
        sha = commit_files(repo, {"sub/x.txt": "x\n", "top.txt": "top\n"})
        assert read_file_at_commit(repo / "sub", sha, "top.txt") == "top\n"

    def test_a_commit_missing_locally_is_fetched_from_the_remote(self, tmp_path):
        origin = _repo(tmp_path / "origin")
        commit_files(origin, {"a.txt": "a\n"})
        clone = tmp_path / "clone"
        git(tmp_path, "clone", "-q", str(origin), str(clone))
        later = commit_files(origin, {"gate.txt": "from origin\n"})
        assert read_file_at_commit(clone, later, "gate.txt", base_branch="main") == "from origin\n"

    def test_an_unfetchable_commit_raises_and_names_the_commit(self, tmp_path):
        repo = _repo(tmp_path / "r")
        commit_files(repo, {"a.txt": "a\n"})
        with pytest.raises(CommitFileReadError) as excinfo:
            read_file_at_commit(repo, "f" * 40, "a.txt")
        assert "f" * 40 in str(excinfo.value)

    def test_a_directory_that_is_not_a_git_tree_raises(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(CommitFileReadError):
            read_file_at_commit(plain, "a" * 40, "a.txt")

    def test_invalid_utf8_content_raises_the_read_error_not_a_decode_error(self, tmp_path):
        repo = _repo(tmp_path / "r")
        (repo / "bad.txt").write_bytes(b"\xff\xfe broken")
        git(repo, "add", "--", "bad.txt")
        git(repo, "commit", "-m", "bad")
        sha = git(repo, "rev-parse", "HEAD")
        with pytest.raises(CommitFileReadError, match="not valid UTF-8"):
            read_file_at_commit(repo, sha, "bad.txt")

    def test_an_option_shaped_sha_is_refused(self, tmp_path):
        repo = _repo(tmp_path / "r")
        commit_files(repo, {"a.txt": "a\n"})
        with pytest.raises(CommitFileReadError):
            read_file_at_commit(repo, "--upload-pack=x", "a.txt")


class TestReadFileAtCommitFollowingLinks:
    @pytest.mark.parametrize(
        ("link_path", "link_target", "read_path", "expected"),
        [
            ("cfg.yaml", "real.yaml", "cfg.yaml", "base\n"),
            ("sub/cfg.yaml", "../real.yaml", "sub/cfg.yaml", "base\n"),
            ("alias", "dir", "alias/f.txt", "in dir\n"),
            ("cfg.yaml", "missing.yaml", "cfg.yaml", None),
        ],
        ids=["file_link", "parent_relative_link", "directory_link", "dangling_link"],
    )
    def test_a_link_in_the_commit_is_followed_through_its_objects(
        self, tmp_path, link_path, link_target, read_path, expected
    ):
        repo = _repo(tmp_path / "r")
        commit_files(repo, {"real.yaml": "base\n", "dir/f.txt": "in dir\n", "sub/keep": "k\n"})
        (repo / link_path).symlink_to(link_target)
        git(repo, "add", "--", link_path)
        git(repo, "commit", "-q", "-m", "link")
        sha = git(repo, "rev-parse", "HEAD")
        # The working tree now points the same path somewhere else entirely.
        (repo / link_path).unlink()
        (repo / link_path).symlink_to("/dev/null")

        assert read_file_at_commit_following_links(repo, sha, read_path) == expected

    @pytest.mark.parametrize("link_target", ["/etc/hostname", "../../outside"], ids=["absolute", "leaves_repo"])
    def test_a_link_that_leaves_the_repository_raises(self, tmp_path, link_target):
        repo = _repo(tmp_path / "r")
        commit_files(repo, {"a.txt": "a\n"})
        (repo / "cfg.yaml").symlink_to(link_target)
        git(repo, "add", "--", "cfg.yaml")
        git(repo, "commit", "-q", "-m", "link")
        sha = git(repo, "rev-parse", "HEAD")

        with pytest.raises(CommitFileReadError):
            read_file_at_commit_following_links(repo, sha, "cfg.yaml")


class TestResolveBaseSha:
    def test_reads_the_base_sha(self):
        assert resolve_base_sha({"base": {"ref": "main", "sha": "abc"}}) == "abc"

    @pytest.mark.parametrize("payload", [{}, {"base": "x"}, {"base": {}}, {"base": {"sha": None}}])
    def test_anything_else_is_empty(self, payload):
        assert resolve_base_sha(payload) == ""
