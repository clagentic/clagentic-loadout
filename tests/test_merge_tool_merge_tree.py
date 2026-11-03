"""test_merge_tool_merge_tree.py -- loadout-merge and loadout-post-merge never move
a checkout that is not a clean checkout of the PR's base branch.

A real local git repository with a bare origin stands in for the host. The merged
commit M is pushed to origin by a second clone, so the work checkout under test is
behind it and M carries a file the work checkout does not have. A post-merge step
reads that file through a relative path and records what it read plus its working
directory, which proves the step ran against the merged content and where.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
import yaml

from clagentic_loadout.merge import post_merge_verb, tool_merge_tree, verb
from clagentic_loadout.transport import provider_config
from tests import test_merge_post_merge_verb as pmv
from tests import test_merge_verb_post_merge as mvp
from tests._gate_repo import CONFIRMABLE_GITHUB_ORIGIN, make_origin_confirmable

_PY = sys.executable
_BASE = "main"
_VALUES = "data/values.yaml"
_MERGED_CONTENT = "merged-content\n"


def _git(args, cwd) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(cwd))
    assert result.returncode == 0, f"git {args!r}: {result.stderr}"
    return result.stdout


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config-root")
    monkeypatch.setenv(tool_merge_tree.TREES_DIR_ENV_VAR, str(tmp_path / "trees"))
    mvp._SEEDED_BASE["sha"] = ""
    yield
    mvp._SEEDED_BASE["sha"] = ""


def _push_merged_commit(work, tmp_path) -> str:
    """Push commit M (adds data/values.yaml) to the work repo's origin from a
    second clone; return M."""
    origin = str(work.parent / f"{work.name}-origin.git")
    other = tmp_path / "other-clone"
    _git(["clone", "-b", _BASE, origin, str(other)], tmp_path)
    _git(["config", "user.email", "t@example.com"], other)
    _git(["config", "user.name", "t"], other)
    (other / "data").mkdir()
    (other / _VALUES).write_text(_MERGED_CONTENT, encoding="utf-8")
    _git(["add", _VALUES], other)
    _git(["commit", "-m", "merged change"], other)
    _git(["push", "origin", f"{_BASE}:{_BASE}"], other)
    return _git(["rev-parse", "HEAD"], other).strip()


def _snapshot(work) -> dict:
    return {
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], work).strip(),
        "head": _git(["rev-parse", "HEAD"], work).strip(),
        "status": _git(["status", "--porcelain"], work),
        "readme": (work / "README.md").read_bytes(),
        "files": sorted(p.name for p in work.iterdir() if p.name != ".git"),
    }


def _step(marker) -> dict:
    code = (
        "import pathlib;"
        f"pathlib.Path(r'{marker}').write_text("
        f"pathlib.Path('{_VALUES}').read_text() + '|' + str(pathlib.Path.cwd()))"
    )
    return {"cmd": [_PY, "-c", code], "on_failure": "fail"}


def _worktrees(work) -> list[str]:
    return [
        line for line in _git(["worktree", "list", "--porcelain"], work).splitlines()
        if line.startswith("worktree ")
    ]


def _scenario(tmp_path, *, shape: str, steps=True, platform="forgejo"):
    """shape: 'feature' (feature branch + uncommitted edit), 'dirty-base' (base
    branch + uncommitted edit), 'clean-base'."""
    work = tmp_path / "work"
    work.mkdir()
    mvp._init_repo_with_origin(work)
    if platform == "github":
        make_origin_confirmable(work, url=CONFIRMABLE_GITHUB_ORIGIN)
    merged = _push_merged_commit(work, tmp_path)
    marker = tmp_path / "marker.txt"
    if steps:
        mvp._write_merge_config(work, [_step(marker)])
    if shape == "feature":
        _git(["checkout", "-b", "feature/x"], work)
    if shape in ("feature", "dirty-base"):
        (work / "README.md").write_text("seed\nuncommitted edit\n", encoding="utf-8")
    return work, merged, marker


def _merge(work, merged, platform="forgejo", **extra):
    if platform == "github":
        opener = mvp._make_github_opener(merged_sha=merged)
    else:
        opener = mvp._make_opener()
    argv = mvp._base_args(**{"--repo-path": str(work), "--platform": platform, **extra})
    return verb.main(
        argv,
        token_provider=mvp._RecordingTokenProvider(),
        authority_provider=mvp._AllowingAuthorityProvider(),
        opener=opener,
    )


def _post_merge(work, merged, platform="forgejo"):
    sha = merged if platform == "github" else None
    opener = (
        pmv._make_github_opener(merge_commit_sha=sha)
        if platform == "github"
        else pmv._make_opener(merge_commit_sha=sha)
    )
    argv = pmv._base_args(str(work), **{"--platform": platform})
    return post_merge_verb.main(
        argv,
        token_provider=pmv._RecordingTokenProvider(),
        authority_provider=pmv._AllowingAuthorityProvider(),
        opener=opener,
    )


def _assert_step_ran_in_tool_tree(marker, work, tmp_path):
    content, cwd = marker.read_text().split("|")
    assert content == _MERGED_CONTENT
    assert str(tmp_path / "trees") in cwd
    assert str(work) not in cwd.replace(str(tmp_path / "trees"), "")


def _assert_tool_tree_gone(work, tmp_path, merged):
    assert _worktrees(work) == [f"worktree {work}"]
    assert not list((tmp_path / "trees").rglob(merged))


class TestUnsafeTreeReason:
    def test_clean_checkout_on_the_base_branch_is_safe(self, tmp_path):
        work, _, _ = _scenario(tmp_path, shape="clean-base")
        assert tool_merge_tree.unsafe_tree_reason(work, _BASE) is None

    def test_untracked_files_alone_do_not_make_it_unsafe(self, tmp_path):
        work, _, _ = _scenario(tmp_path, shape="clean-base")
        (work / "scratch.txt").write_text("x", encoding="utf-8")
        assert tool_merge_tree.unsafe_tree_reason(work, _BASE) is None

    def test_other_branch_detached_head_and_tracked_changes_are_unsafe(self, tmp_path):
        work, _, _ = _scenario(tmp_path, shape="feature")
        assert "feature/x" in tool_merge_tree.unsafe_tree_reason(work, _BASE)
        _git(["checkout", "--", "README.md"], work)
        _git(["checkout", "--detach"], work)
        assert "detached" in tool_merge_tree.unsafe_tree_reason(work, _BASE)
        _git(["checkout", _BASE], work)
        assert tool_merge_tree.unsafe_tree_reason(work, _BASE) is None
        (work / "README.md").write_text("changed\n", encoding="utf-8")
        assert "tracked files" in tool_merge_tree.unsafe_tree_reason(work, _BASE)


class TestMergeVerbFeatureBranchWithUncommittedEdit:
    @pytest.mark.parametrize("platform", ["forgejo", "github"])
    def test_checkout_is_untouched_and_steps_run_on_merged_content(self, tmp_path, platform):
        work, merged, marker = _scenario(tmp_path, shape="feature", platform=platform)
        before = _snapshot(work)
        assert _merge(work, merged, platform) == verb.EXIT_OK
        assert _snapshot(work) == before
        _assert_step_ran_in_tool_tree(marker, work, tmp_path)
        _assert_tool_tree_gone(work, tmp_path, merged)

    def test_the_tool_tree_is_named_on_stderr(self, tmp_path, capsys):
        work, merged, _ = _scenario(tmp_path, shape="feature")
        assert _merge(work, merged) == verb.EXIT_OK
        err = capsys.readouterr().err
        assert "tool tree" in err and str(tmp_path / "trees") in err

    def test_failing_step_still_removes_the_tree_and_leaves_the_checkout(self, tmp_path):
        work, merged, marker = _scenario(tmp_path, shape="feature", steps=False)
        mvp._write_merge_config(
            work, [{"cmd": [_PY, "-c", "import sys; sys.exit(3)"], "on_failure": "fail"}]
        )
        before = _snapshot(work)
        assert _merge(work, merged) == verb.EXIT_POST_MERGE_FAILED
        assert _snapshot(work) == before
        _assert_tool_tree_gone(work, tmp_path, merged)


class TestMergeVerbDirtyBaseBranch:
    @pytest.mark.parametrize("platform", ["forgejo", "github"])
    def test_tracked_changes_on_the_base_branch_are_never_moved(self, tmp_path, platform):
        work, merged, marker = _scenario(tmp_path, shape="dirty-base", platform=platform)
        before = _snapshot(work)
        assert _merge(work, merged, platform) == verb.EXIT_OK
        assert _snapshot(work) == before
        _assert_step_ran_in_tool_tree(marker, work, tmp_path)
        _assert_tool_tree_gone(work, tmp_path, merged)


class TestMergeVerbCleanBaseBranchIsUnchanged:
    @pytest.mark.parametrize("platform", ["forgejo", "github"])
    def test_steps_run_in_repo_path_and_the_tree_lands_on_the_base_branch(self, tmp_path, platform):
        work, merged, marker = _scenario(tmp_path, shape="clean-base", platform=platform)
        assert _merge(work, merged, platform) == verb.EXIT_OK
        content, cwd = marker.read_text().split("|")
        assert content == _MERGED_CONTENT
        assert cwd == str(work.resolve())
        assert _git(["symbolic-ref", "--short", "HEAD"], work).strip() == _BASE
        assert _git(["rev-parse", "HEAD"], work).strip() == merged
        assert not (tmp_path / "trees").exists()


class TestSyncTreeAfterMergeFalseIsUnchanged:
    def test_nothing_is_fetched_run_or_moved(self, tmp_path):
        work, merged, marker = _scenario(tmp_path, shape="feature", steps=False)
        config = work / ".clagentic" / "loadout"
        config.mkdir(parents=True)
        (config / "config.yaml").write_text(
            yaml.safe_dump({"merge": {"sync_tree_after_merge": False, "post_merge_steps": [_step(marker)]}}),
            encoding="utf-8",
        )
        before = _snapshot(work)
        assert _merge(work, merged) == verb.EXIT_OK
        assert _snapshot(work) == before
        assert not marker.exists()
        assert not (tmp_path / "trees").exists()


class TestWrapperLayout:
    def test_declared_git_working_tree_runs_in_the_tool_tree(self, tmp_path):
        wrapper = tmp_path / "wrapper"
        wrapper.mkdir()
        work = wrapper / "repo"
        work.mkdir()
        mvp._init_repo_with_origin(work)
        merged = _push_merged_commit(work, tmp_path)
        marker = tmp_path / "marker.txt"
        mvp._write_merge_config(wrapper, [_step(marker)], git_working_tree="repo")
        _git(["checkout", "-b", "feature/x"], work)
        (work / "README.md").write_text("seed\nuncommitted edit\n", encoding="utf-8")
        before = _snapshot(work)
        assert _merge(work, merged) == verb.EXIT_OK
        assert _snapshot(work) == before
        _assert_step_ran_in_tool_tree(marker, work, tmp_path)
        _assert_tool_tree_gone(work, tmp_path, merged)


class TestToolTreeCannotBeMade:
    def _break_worktree_add(self, monkeypatch):
        real = tool_merge_tree._git

        def failing(args, *, cwd):
            if args[:2] == ["worktree", "add"]:
                return subprocess.CompletedProcess(args, 128, "", "simulated failure")
            return real(args, cwd=cwd)

        monkeypatch.setattr(tool_merge_tree, "_git", failing)

    def test_merge_refuses_before_running_a_step_and_never_moves_repo_path(
        self, tmp_path, monkeypatch, capsys
    ):
        work, merged, marker = _scenario(tmp_path, shape="feature")
        before = _snapshot(work)
        self._break_worktree_add(monkeypatch)
        assert _merge(work, merged) == verb.EXIT_POST_MERGE_FAILED
        assert _snapshot(work) == before
        assert not marker.exists()
        assert tool_merge_tree.OUTCOME_TREE_UNAVAILABLE in capsys.readouterr().err

    def test_post_merge_refuses_the_same_way(self, tmp_path, monkeypatch, capsys):
        work, merged, marker = _scenario(tmp_path, shape="feature")
        before = _snapshot(work)
        self._break_worktree_add(monkeypatch)
        assert _post_merge(work, merged) == post_merge_verb.EXIT_POST_MERGE_FAILED
        assert _snapshot(work) == before
        assert not marker.exists()
        assert tool_merge_tree.OUTCOME_TREE_UNAVAILABLE in capsys.readouterr().err

    def test_a_leftover_at_the_tree_path_is_refused(self, tmp_path):
        work, merged, _ = _scenario(tmp_path, shape="clean-base")
        key = tool_merge_tree._repo_key(work)
        leftover = tmp_path / "trees" / key / merged
        leftover.mkdir(parents=True)
        with pytest.raises(tool_merge_tree.MergeTreeUnavailableError):
            with tool_merge_tree.tool_merge_tree(work, merged):
                pytest.fail("must not yield")
        assert leftover.exists()


class TestPostMergeVerb:
    @pytest.mark.parametrize("platform", ["forgejo", "github"])
    @pytest.mark.parametrize("shape", ["feature", "dirty-base"])
    def test_checkout_is_untouched_and_steps_run_on_merged_content(
        self, tmp_path, capsys, platform, shape
    ):
        work, merged, marker = _scenario(tmp_path, shape=shape, platform=platform)
        before = _snapshot(work)
        assert _post_merge(work, merged, platform) == post_merge_verb.EXIT_OK
        assert _snapshot(work) == before
        _assert_step_ran_in_tool_tree(marker, work, tmp_path)
        _assert_tool_tree_gone(work, tmp_path, merged)
        result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert result["steps_run"] == 1
        assert str(tmp_path / "trees") in result["post_merge_tree"]

    def test_clean_base_branch_keeps_advancing_and_landing(self, tmp_path, capsys):
        work, merged, marker = _scenario(tmp_path, shape="clean-base")
        assert _post_merge(work, merged) == post_merge_verb.EXIT_OK
        assert marker.read_text().split("|")[1] == str(work.resolve())
        assert _git(["symbolic-ref", "--short", "HEAD"], work).strip() == _BASE
        assert _git(["rev-parse", "HEAD"], work).strip() == merged
        assert "post_merge_tree" not in json.loads(capsys.readouterr().out.strip().splitlines()[-1])


class TestStepsDirInTree:
    def test_a_subdirectory_maps_to_the_same_relative_path(self, tmp_path):
        work, merged, _ = _scenario(tmp_path, shape="clean-base", steps=False)
        sub = work / "sub"
        sub.mkdir()
        tree = tmp_path / "tree"
        (tree / "sub").mkdir(parents=True)
        assert tool_merge_tree.steps_dir_in_tree(tree, work, sub) == tree / "sub"

    def test_a_directory_missing_from_the_merged_commit_is_refused(self, tmp_path):
        work, _, _ = _scenario(tmp_path, shape="clean-base", steps=False)
        sub = work / "sub"
        sub.mkdir()
        tree = tmp_path / "tree"
        tree.mkdir()
        with pytest.raises(tool_merge_tree.MergeTreeUnavailableError):
            tool_merge_tree.steps_dir_in_tree(tree, work, sub)
