"""A branch's tracking remote (`branch.<name>.remote`) can itself be a URL carrying
a credential. No message built from it may echo it: the merge verbs name a
non-alias remote with a fixed placeholder, and the push verb refuses it outright.

Each case points the tracking remote at a closed local port, so a fetch fails
offline."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.commit_files import CommitFileReadError, ensure_commit_present
from clagentic_loadout.merge.tree_sync import (
    TreeSyncError,
    advance_repo_to_merged_sha,
    fetch_merged_sha_object,
)
from clagentic_loadout.push import git_coords
from clagentic_loadout.push.errors import RemoteResolutionError
from clagentic_loadout.push.verb import PushVerbError
from tests._gate_repo import git, seed_base_commit

TOKEN = "FAKETOKEN0123456789"
TRACKING_URLS = {
    "userinfo": f"http://user:{TOKEN}@127.0.0.1:1/o/r.git",
    "query": f"http://127.0.0.1:1/o/r.git?token={TOKEN}",
    "path": f"http://127.0.0.1:1/{TOKEN}/o/r.git",
}
MISSING_SHA = "f" * 40


@pytest.fixture(params=sorted(TRACKING_URLS))
def tree(request, tmp_path):
    path = tmp_path / "tree"
    seed_base_commit(path)
    git(path, "config", "branch.main.remote", TRACKING_URLS[request.param])
    return path


def _assert_no_token(excinfo, capsys):
    out, err = capsys.readouterr()
    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in out and TOKEN not in err


@pytest.mark.parametrize("known_sha", [None, MISSING_SHA])
def test_advance_repo_to_merged_sha_url_tracking_remote(tree, capsys, known_sha):
    with pytest.raises(TreeSyncError) as excinfo:
        advance_repo_to_merged_sha(tree, base_branch="main", known_merged_sha=known_sha)

    assert "exit" in str(excinfo.value)
    _assert_no_token(excinfo, capsys)


@pytest.mark.parametrize("known_sha", [None, MISSING_SHA])
def test_fetch_merged_sha_object_url_tracking_remote(tree, capsys, known_sha):
    with pytest.raises(TreeSyncError) as excinfo:
        fetch_merged_sha_object(tree, base_branch="main", known_merged_sha=known_sha)

    assert "exit" in str(excinfo.value)
    _assert_no_token(excinfo, capsys)


def test_ensure_commit_present_url_tracking_remote(tree, capsys):
    with pytest.raises(CommitFileReadError) as excinfo:
        ensure_commit_present(tree, MISSING_SHA, base_branch="main")

    assert MISSING_SHA in str(excinfo.value) and "exit" in str(excinfo.value)
    _assert_no_token(excinfo, capsys)


def test_remote_url_failure_url_tracking_remote(tree, capsys):
    remote = git_coords.tracking_remote("main", tree)

    with pytest.raises(RemoteResolutionError) as excinfo:
        git_coords.remote_url(remote, tree)

    _assert_no_token(excinfo, capsys)


def test_push_refuses_a_url_tracking_remote_without_echoing_it(tree, capsys):
    from clagentic_loadout.push.verb import _push_remote_alias

    with pytest.raises(PushVerbError) as excinfo:
        _push_remote_alias("main", tree)

    _assert_no_token(excinfo, capsys)


def test_display_remote_passes_a_configured_alias_and_masks_anything_else(tmp_path):
    path = tmp_path / "tree"
    seed_base_commit(path)
    git(path, "remote", "add", "mirror", "http://127.0.0.1:1/o/r.git")

    assert git_coords.display_remote("mirror", path) == "mirror"
    assert git_coords.display_remote("origin", path) == "origin"
    assert git_coords.display_remote(TRACKING_URLS["query"], path) == git_coords.NON_ALIAS_REMOTE_PLACEHOLDER
