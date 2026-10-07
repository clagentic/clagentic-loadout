"""merge.commit_files and tree_sync.resolve_base_sha: reading a tracked file as
it exists at a commit, independent of the working tree."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.commit_files import CommitFileReadError, read_file_at_commit
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

    def test_an_option_shaped_sha_is_refused(self, tmp_path):
        repo = _repo(tmp_path / "r")
        commit_files(repo, {"a.txt": "a\n"})
        with pytest.raises(CommitFileReadError):
            read_file_at_commit(repo, "--upload-pack=x", "a.txt")


class TestResolveBaseSha:
    def test_reads_the_base_sha(self):
        assert resolve_base_sha({"base": {"ref": "main", "sha": "abc"}}) == "abc"

    @pytest.mark.parametrize("payload", [{}, {"base": "x"}, {"base": {}}, {"base": {"sha": None}}])
    def test_anything_else_is_empty(self, payload):
        assert resolve_base_sha(payload) == ""
