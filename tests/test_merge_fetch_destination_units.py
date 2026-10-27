# ruff: noqa: F811
# (the shared fixtures are imported into this namespace for pytest to register)
"""The merge-path fetch primitives judge their destination and carry no ambient credential.

Library-level cases for the fetch sites behind loadout-merge and loadout-post-merge
(`tests/test_merge_fetch_destination.py` drives the entrypoints): the merge-result
clone, the tracked-gate read, the tree-sync fetches. Real git repos, a throwaway
HOME whose global config holds a credential helper, and a local HTTP listener that
records every request and Authorization header.

  M6  the merge-result clone and the tracked-gate read refuse a steered remote
  M7  an anchored private host that requires the minted token: every fetch site
      succeeds with the minted token and never presents the ambient one
  M8  a local / file:// remote: the fetch carries no askpass and no token
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from clagentic_loadout.merge.commit_files import (
    CommitFetchDeniedError,
    CommitFileReadError,
    ensure_commit_present,
    read_file_at_commit,
)
from clagentic_loadout.merge.fetch_transport import MergeFetchAuth, MergeFetchDeniedError
from clagentic_loadout.merge.merge_result_clone import MergeResultCloneError, merge_result_clone
from clagentic_loadout.merge.repo_gate_runtime import load_gate_at_commit
from clagentic_loadout.merge.tree_sync import (
    TreeSyncError,
    advance_repo_to_merged_sha,
    fetch_merged_sha_object,
)
from tests._gate_repo import commit_files, git, seed_base_commit
from tests.test_push_guard_fetch_hermetic import (  # noqa: F401
    AMBIENT,
    TOKEN,
    _AuthenticatedGitServer,
    _http_backend_available,
    ambient_home,
    listener,
)

MISSING_SHA = "f" * 40
_REPO_PATH_SUFFIX = "some-owner/some-repo.git"


def _no_credential_reached(listener) -> bool:
    return listener.requests == [] and listener.authorizations == []


def _tree_with_remote(tmp_path, url: str):
    tree = tmp_path / "tree"
    seed_base_commit(tree)
    git(tree, "remote", "add", "origin", url)
    git(tree, "config", "branch.main.remote", "origin")
    return tree


@pytest.mark.parametrize("kind", ["url", "insteadof", "branch-remote"])
def test_m6_the_merge_result_clone_and_the_tracked_gate_read_refuse_a_steered_remote(
    kind, tmp_path, listener, ambient_home
):
    tree = _tree_with_remote(tmp_path, f"https://github.com/{_REPO_PATH_SUFFIX}")
    auth = MergeFetchAuth(anchor_url="https://github.com", token=TOKEN)
    if kind == "url":
        git(tree, "config", "remote.origin.url", listener.url)
    elif kind == "insteadof":
        git(tree, "config", f"url.{listener.base}.insteadOf", "https://github.com/")
    else:
        git(tree, "config", "branch.main.remote", listener.url)

    with pytest.raises(CommitFetchDeniedError):
        ensure_commit_present(tree, MISSING_SHA, base_branch="main", fetch_auth=auth)
    # A refused destination propagates as such; it is not an unbuildable merge result.
    with pytest.raises(CommitFetchDeniedError) as denied:
        with merge_result_clone(
            tree, MISSING_SHA, MISSING_SHA, base_branch="main", fetch_auth=auth
        ):
            pytest.fail("no clone may be built from a refused destination")
    assert not isinstance(denied.value, MergeResultCloneError)
    # The gate reader must not degrade a steered remote into "nothing readable".
    with pytest.raises(MergeFetchDeniedError):
        load_gate_at_commit(tree, MISSING_SHA, base_branch="main", fetch_auth=auth)
    with pytest.raises(TreeSyncError, match="refusing before any git network call"):
        advance_repo_to_merged_sha(
            tree, base_branch="main", known_merged_sha=MISSING_SHA, fetch_auth=auth
        )
    with pytest.raises(TreeSyncError, match="refusing before any git network call"):
        fetch_merged_sha_object(tree, base_branch="main", fetch_auth=auth)

    assert _no_credential_reached(listener)


@pytest.mark.parametrize(
    "url",
    ["ssh://git@github.com/some-owner/some-repo.git", "git@github.com:some-owner/some-repo.git",
     "git://github.com/some-owner/some-repo.git", "ext::sh -c true"],
    ids=["ssh", "scp", "git", "ext"],
)
def test_other_transports_are_refused_even_on_the_platform_host(url, tmp_path):
    tree = _tree_with_remote(tmp_path, url)
    auth = MergeFetchAuth(anchor_url="https://github.com", token=TOKEN)

    with pytest.raises(CommitFetchDeniedError):
        ensure_commit_present(tree, MISSING_SHA, base_branch="main", fetch_auth=auth)


def test_a_call_with_no_fetch_auth_reaches_local_remotes_only(tmp_path, listener, ambient_home):
    remote = tmp_path / "remote.git"
    tree = tmp_path / "tree"
    seed_base_commit(tree)
    git(tmp_path, "clone", "-q", "--bare", str(tree), str(remote))
    git(tree, "remote", "add", "origin", str(remote))
    git(tree, "config", "branch.main.remote", "origin")
    later = commit_files(tree, {"a.txt": "a\n"})
    git(tree, "push", "-q", "origin", "main")
    git(tree, "reset", "-q", "--hard", "HEAD~1")
    git(tree, "reflog", "expire", "--expire=now", "--all")
    git(tree, "gc", "-q", "--prune=now")

    ensure_commit_present(tree, later, base_branch="main")
    assert read_file_at_commit(tree, later, "a.txt", base_branch="main") == "a\n"

    git(tree, "remote", "set-url", "origin", listener.url)
    with pytest.raises(CommitFileReadError):
        ensure_commit_present(tree, MISSING_SHA, base_branch="main")
    assert _no_credential_reached(listener)


@pytest.mark.skipif(
    not _http_backend_available(), reason="git http-backend is not installed on this host"
)
def test_m7_a_private_anchored_host_is_fetched_with_the_minted_token_never_the_ambient_one(
    tmp_path, ambient_home
):
    bare = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed_base_commit(seed)
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    git(seed, "remote", "add", "origin", str(bare))
    trees = []
    for name in ("advance", "ensure", "object"):
        clone = tmp_path / name
        git(tmp_path, "clone", "-q", str(bare), str(clone))
        trees.append(clone)
    merged = commit_files(seed, {"merged.txt": "m\n"})
    git(seed, "push", "-q", "origin", "main")
    served = tmp_path / "served" / "some-owner" / "some-repo.git"
    shutil.copytree(bare, served)
    git(served, "config", "uploadpack.allowAnySHA1InWant", "true")

    with _AuthenticatedGitServer(tmp_path / "served", TOKEN) as server:
        for clone in trees:
            git(clone, "remote", "set-url", "origin", f"http://{server.authority}/{_REPO_PATH_SUFFIX}")
        auth = MergeFetchAuth(anchor_url=f"http://{server.authority}", token=TOKEN)
        advance, ensure, fetch_only = trees

        assert advance_repo_to_merged_sha(
            advance, base_branch="main", known_merged_sha=merged, fetch_auth=auth
        ) == merged
        ensure_commit_present(ensure, merged, base_branch="main", fetch_auth=auth)
        assert fetch_merged_sha_object(fetch_only, base_branch="main", fetch_auth=auth) == merged

        passwords = server.passwords_presented()

    assert git(ensure, "cat-file", "-t", merged) == "commit"
    assert TOKEN in passwords
    assert set(passwords) == {TOKEN}
    assert AMBIENT not in passwords


@pytest.mark.skipif(
    not _http_backend_available(), reason="git http-backend is not installed on this host"
)
def test_m7_the_ambient_credential_in_home_is_not_consulted_when_the_token_is_wrong(
    tmp_path, ambient_home
):
    """The ambient helper in HOME would answer the 401; the neutralized fetch
    must not consult it, so the fetch fails rather than authenticating."""
    bare = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed_base_commit(seed)
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(bare), str(clone))
    served = tmp_path / "served" / "some-owner" / "some-repo.git"
    shutil.copytree(bare, served)

    with _AuthenticatedGitServer(tmp_path / "served", AMBIENT) as server:
        git(clone, "remote", "set-url", "origin", f"http://{server.authority}/{_REPO_PATH_SUFFIX}")
        auth = MergeFetchAuth(anchor_url=f"http://{server.authority}", token=TOKEN)

        with pytest.raises(TreeSyncError):
            fetch_merged_sha_object(clone, base_branch="main", fetch_auth=auth)

        passwords = server.passwords_presented()

    assert AMBIENT not in passwords


@pytest.mark.parametrize("scheme", ["path", "file"])
def test_m8_a_local_remote_fetch_carries_no_askpass_and_no_token(
    scheme, tmp_path, monkeypatch, ambient_home
):
    bare = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed_base_commit(seed)
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(bare), str(clone))
    git(seed, "remote", "add", "origin", str(bare))
    merged = commit_files(seed, {"m.txt": "m\n"})
    git(seed, "push", "-q", "origin", "main")
    git(clone, "remote", "set-url", "origin", bare.as_uri() if scheme == "file" else str(bare))
    seen: list[dict] = []
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        if cmd[0] == "git" and "fetch" in cmd:
            env = kwargs["env"]
            seen.append({"env": dict(env), "home_entries": os.listdir(env["HOME"])})
        return real_run(cmd, **kwargs)

    monkeypatch.setattr("clagentic_loadout.push.git_push.subprocess.run", spy)
    auth = MergeFetchAuth(anchor_url="https://github.com", token=TOKEN)

    assert advance_repo_to_merged_sha(
        clone, base_branch="main", known_merged_sha=merged, fetch_auth=auth
    ) == merged

    assert len(seen) == 1
    env = seen[0]["env"]
    assert "GIT_ASKPASS" not in env
    assert not any(TOKEN in value for value in env.values())
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert seen[0]["home_entries"] == []
