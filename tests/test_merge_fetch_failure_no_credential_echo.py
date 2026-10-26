"""A failing `git fetch` in the merge verbs must never carry a credential from the
remote URL into the verb's output.

git quotes the remote URL (query string included) in its fetch diagnostics, so
the message raised for a failed fetch names only the remote and the exit code.
Each case points `origin` at a closed local port, so the fetch fails offline."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.commit_files import CommitFileReadError, ensure_commit_present
from clagentic_loadout.merge.merge_result_clone import MergeResultCloneError, merge_result_clone
from clagentic_loadout.merge.tree_sync import (
    TreeSyncError,
    advance_repo_to_merged_sha,
    fetch_merged_sha_object,
)
from tests._gate_repo import git, seed_base_commit

TOKEN = "FAKETOKEN0123456789"
ORIGINS = {
    "userinfo": f"http://user:{TOKEN}@127.0.0.1:1/o/r.git",
    "query": f"http://127.0.0.1:1/o/r.git?token={TOKEN}",
    "path": f"http://127.0.0.1:1/{TOKEN}/o/r.git",
}
MISSING_SHA = "f" * 40


@pytest.fixture(params=sorted(ORIGINS))
def tree(request, tmp_path):
    path = tmp_path / "tree"
    seed_base_commit(path)
    git(path, "remote", "add", "origin", ORIGINS[request.param])
    return path


def _assert_no_token(excinfo, capsys):
    out, err = capsys.readouterr()
    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in out and TOKEN not in err


@pytest.mark.parametrize("known_sha", [None, MISSING_SHA])
def test_advance_repo_to_merged_sha_fetch_failure_withholds_the_url(tree, capsys, known_sha):
    with pytest.raises(TreeSyncError) as excinfo:
        advance_repo_to_merged_sha(tree, base_branch="main", known_merged_sha=known_sha)

    assert "git fetch of" in str(excinfo.value) and "exit" in str(excinfo.value)
    _assert_no_token(excinfo, capsys)


@pytest.mark.parametrize("known_sha", [None, MISSING_SHA])
def test_fetch_merged_sha_object_fetch_failure_withholds_the_url(tree, capsys, known_sha):
    with pytest.raises(TreeSyncError) as excinfo:
        fetch_merged_sha_object(tree, base_branch="main", known_merged_sha=known_sha)

    assert "git fetch of" in str(excinfo.value) and "exit" in str(excinfo.value)
    _assert_no_token(excinfo, capsys)


def test_ensure_commit_present_fetch_failure_withholds_the_url(tree, capsys):
    with pytest.raises(CommitFileReadError) as excinfo:
        ensure_commit_present(tree, MISSING_SHA)

    assert MISSING_SHA in str(excinfo.value) and "exit" in str(excinfo.value)
    _assert_no_token(excinfo, capsys)


def test_merge_result_clone_fetch_failure_withholds_the_url(tree, capsys):
    with pytest.raises(MergeResultCloneError) as excinfo:
        with merge_result_clone(tree, MISSING_SHA, MISSING_SHA, base_branch="main"):
            pytest.fail("the clone must not be built when the commit cannot be fetched")

    _assert_no_token(excinfo, capsys)
