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
import shutil
import subprocess
import sys
import time

import pytest
import yaml

from clagentic_loadout.merge import post_merge_verb, tool_merge_tree, verb
from clagentic_loadout.transport import provider_config
from tests import _merge_support as ms
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
    ms.SEEDED_BASE["sha"] = ""
    yield
    ms.SEEDED_BASE["sha"] = ""


def _add_values(clone) -> None:
    (clone / "data").mkdir()
    (clone / _VALUES).write_text(_MERGED_CONTENT, encoding="utf-8")
    _git(["add", _VALUES], clone)


def _push_merged_commit(work, tmp_path) -> str:
    """Push commit M (adds data/values.yaml) to the work repo's origin."""
    return _commit_to_origin(work, tmp_path, "other-clone", _add_values)


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


def _fetch_origin_objects(work) -> None:
    """Bring the origin's objects into *work*; its `origin` remote is rewired to a
    host URL, so the bare repository is named by path."""
    origin = work.parent / f"{work.name}-origin.git"
    _git(["fetch", str(origin), _BASE], work)


def _worktrees(work) -> list[str]:
    return [
        line for line in _git(["worktree", "list", "--porcelain"], work).splitlines()
        if line.startswith("worktree ")
    ]


def _scenario(tmp_path, *, shape: str, steps=True, platform="forgejo"):
    """shape: 'feature' (feature branch + uncommitted edit), 'dirty-base' (base
    branch + uncommitted edit), 'clean-base', 'local-commit' (base branch, clean
    tree, one commit that is not on origin)."""
    work = tmp_path / "work"
    work.mkdir()
    ms.init_repo_with_origin(work)
    if platform == "github":
        make_origin_confirmable(work, url=CONFIRMABLE_GITHUB_ORIGIN)
    merged = _push_merged_commit(work, tmp_path)
    marker = tmp_path / "marker.txt"
    if steps:
        ms.write_merge_config(work, [_step(marker)])
    if shape == "feature":
        _git(["checkout", "-b", "feature/x"], work)
    if shape == "local-commit":
        (work / "local.txt").write_text("local\n", encoding="utf-8")
        _git(["add", "local.txt"], work)
        _git(["commit", "-m", "local work"], work)
    if shape in ("feature", "dirty-base"):
        (work / "README.md").write_text("seed\nuncommitted edit\n", encoding="utf-8")
    return work, merged, marker


def _merge(work, merged, platform="forgejo", **extra):
    if platform == "github":
        opener = ms.make_merge_github_opener(merged_sha=merged)
    else:
        opener = ms.make_merge_opener()
    argv = ms.merge_base_args(**{"--repo-path": str(work), "--platform": platform, **extra})
    return verb.main(
        argv,
        token_provider=ms.RecordingTokenProvider(),
        authority_provider=ms.AllowingAuthorityProvider(),
        opener=opener,
    )


def _post_merge(work, merged, platform="forgejo"):
    sha = merged if platform == "github" else None
    opener = (
        ms.make_post_merge_github_opener(merge_commit_sha=sha)
        if platform == "github"
        else ms.make_post_merge_opener(merge_commit_sha=sha)
    )
    argv = ms.post_merge_base_args(str(work), **{"--platform": platform})
    return post_merge_verb.main(
        argv,
        token_provider=ms.RecordingTokenProvider(),
        authority_provider=ms.AllowingAuthorityProvider(),
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
        assert tool_merge_tree.unsafe_tree_reason(work, _BASE, "HEAD") is None

    def test_untracked_files_alone_do_not_make_it_unsafe(self, tmp_path):
        work, _, _ = _scenario(tmp_path, shape="clean-base")
        (work / "scratch.txt").write_text("x", encoding="utf-8")
        assert tool_merge_tree.unsafe_tree_reason(work, _BASE, "HEAD") is None

    def test_other_branch_detached_head_and_tracked_changes_are_unsafe(self, tmp_path):
        work, _, _ = _scenario(tmp_path, shape="feature")
        assert "feature/x" in tool_merge_tree.unsafe_tree_reason(work, _BASE, "HEAD")
        _git(["checkout", "--", "README.md"], work)
        _git(["checkout", "--detach"], work)
        assert "detached" in tool_merge_tree.unsafe_tree_reason(work, _BASE, "HEAD")
        _git(["checkout", _BASE], work)
        assert tool_merge_tree.unsafe_tree_reason(work, _BASE, "HEAD") is None
        (work / "README.md").write_text("changed\n", encoding="utf-8")
        assert "tracked files" in tool_merge_tree.unsafe_tree_reason(work, _BASE, "HEAD")


    def test_local_commit_outside_the_target_is_unsafe(self, tmp_path):
        work, merged, _ = _scenario(tmp_path, shape="local-commit")
        _fetch_origin_objects(work)
        reason = tool_merge_tree.unsafe_tree_reason(work, _BASE, merged)
        assert reason == f"it has local commits not in {merged}"

    def test_head_that_is_an_ancestor_of_the_target_is_safe(self, tmp_path):
        work, merged, _ = _scenario(tmp_path, shape="clean-base")
        _fetch_origin_objects(work)
        assert tool_merge_tree.unsafe_tree_reason(work, _BASE, merged) is None

    def test_target_missing_from_the_object_database_is_unsafe(self, tmp_path):
        work, _, _ = _scenario(tmp_path, shape="clean-base")
        reason = tool_merge_tree.unsafe_tree_reason(work, _BASE, "0" * 40)
        assert "could not be read" in reason


class TestMergeVerbLocalCommitOnBaseBranch:
    @pytest.mark.parametrize("platform", ["forgejo", "github"])
    def test_merge_runs_in_the_tool_tree_and_keeps_the_local_commit(self, tmp_path, platform):
        work, merged, marker = _scenario(tmp_path, shape="local-commit", platform=platform)
        before = _snapshot(work)
        local_commit = before["head"]
        assert _merge(work, merged, platform) == verb.EXIT_OK
        assert _snapshot(work) == before
        assert _git(["symbolic-ref", "--short", "HEAD"], work).strip() == _BASE
        assert _git(["rev-parse", _BASE], work).strip() == local_commit
        _assert_step_ran_in_tool_tree(marker, work, tmp_path)
        _assert_tool_tree_gone(work, tmp_path, merged)


class TestPostMergeVerbLocalCommitOnBaseBranch:
    @pytest.mark.parametrize("platform", ["forgejo", "github"])
    def test_steps_run_in_the_tool_tree_and_the_local_commit_stays(self, tmp_path, platform):
        work, merged, marker = _scenario(tmp_path, shape="local-commit", platform=platform)
        before = _snapshot(work)
        assert _post_merge(work, merged, platform) == 0
        assert _snapshot(work) == before
        assert _git(["rev-parse", _BASE], work).strip() == before["head"]
        _assert_step_ran_in_tool_tree(marker, work, tmp_path)
        _assert_tool_tree_gone(work, tmp_path, merged)


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
        ms.write_merge_config(
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
        ms.init_repo_with_origin(work)
        merged = _push_merged_commit(work, tmp_path)
        marker = tmp_path / "marker.txt"
        ms.write_merge_config(wrapper, [_step(marker)], git_working_tree="repo")
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
        assert result["post_merge_tree_removed"] is True
        assert not (tmp_path / "trees").exists() or not list((tmp_path / "trees").rglob(merged))

    def test_clean_base_branch_keeps_advancing_and_landing(self, tmp_path, capsys):
        work, merged, marker = _scenario(tmp_path, shape="clean-base")
        assert _post_merge(work, merged) == post_merge_verb.EXIT_OK
        assert marker.read_text().split("|")[1] == str(work.resolve())
        assert _git(["symbolic-ref", "--short", "HEAD"], work).strip() == _BASE
        assert _git(["rev-parse", "HEAD"], work).strip() == merged
        result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert "post_merge_tree" not in result and "post_merge_tree_removed" not in result


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


def _commit_to_origin(work, tmp_path, name, write) -> str:
    """Clone the work repo's origin to *name*, let *write(clone)* add files and
    stage them, commit and push; return the new tip."""
    origin = str(work.parent / f"{work.name}-origin.git")
    clone = tmp_path / name
    _git(["clone", "-b", _BASE, origin, str(clone)], tmp_path)
    _git(["config", "user.email", "t@example.com"], clone)
    _git(["config", "user.name", "t"], clone)
    write(clone)
    _git(["commit", "-m", f"{name} change"], clone)
    _git(["push", "origin", f"{_BASE}:{_BASE}"], clone)
    return _git(["rev-parse", "HEAD"], clone).strip()


def _cmd_step(cmd) -> dict:
    return {"cmd": cmd, "on_failure": "fail"}


def _record_env_step(marker) -> dict:
    code = (
        "import os, pathlib;"
        f"pathlib.Path(r'{marker}').write_text("
        f"os.environ.get('{tool_merge_tree.ENV_MERGE_TREE}', 'unset') + '|' + os.getcwd())"
    )
    return _cmd_step([_PY, "-c", code])


class TestRepoPathNamedByAStepIsRefused:
    """In the tool-tree case a step that names --repo-path's git top-level as an
    absolute path would act on the builder's checkout; it is refused before any step
    runs."""

    def _setup(self, tmp_path, naming_step):
        work, merged, marker = _scenario(tmp_path, shape="feature", steps=False)
        first = _step(marker)
        ms.write_merge_config(work, [first, naming_step(work)])
        return work, merged, marker

    @pytest.mark.parametrize(
        "naming_step",
        [
            pytest.param(lambda w: _cmd_step(f"{_PY} -c pass {w}/scripts/install.sh"), id="string"),
            pytest.param(lambda w: _cmd_step([_PY, "-c", "pass", f"--prefix={w}"]), id="list"),
            pytest.param(
                lambda w: _cmd_step(["bash", "-c", f"cd {w} && ./install.sh"]), id="bash-c"
            ),
            pytest.param(
                lambda w: {**_cmd_step([_PY, "-c", "pass"]), "verify": ["test", "-d", str(w)]},
                id="verify",
            ),
        ],
    )
    @pytest.mark.parametrize("verb_name", ["merge", "post-merge"])
    def test_refused_before_any_step_runs(
        self, tmp_path, capsys, naming_step, verb_name
    ):
        work, merged, marker = self._setup(tmp_path, naming_step)
        before = _snapshot(work)
        if verb_name == "merge":
            code = _merge(work, merged)
            expected = verb.EXIT_POST_MERGE_FAILED
        else:
            code = _post_merge(work, merged)
            expected = post_merge_verb.EXIT_POST_MERGE_FAILED
        assert code == expected
        err = capsys.readouterr().err
        assert tool_merge_tree.OUTCOME_STEP_NAMES_REPO_PATH in err
        assert "step 2" in err and str(work) in err
        assert "CLAGENTIC_LOADOUT_MERGE_TREE" in err and "clean base checkout" in err
        assert not marker.exists()
        assert _snapshot(work) == before
        _assert_tool_tree_gone(work, tmp_path, merged)

    def test_the_realpath_form_is_refused(self, tmp_path, capsys):
        work, merged, marker = self._setup(
            tmp_path, lambda w: _cmd_step([_PY, "-c", "pass", f"{w.resolve()}/x"])
        )
        link = tmp_path / "link"
        link.symlink_to(work)
        before = _snapshot(work)
        assert _merge(link, merged) == verb.EXIT_POST_MERGE_FAILED
        assert tool_merge_tree.OUTCOME_STEP_NAMES_REPO_PATH in capsys.readouterr().err
        assert not marker.exists()
        assert _snapshot(work) == before

    def test_the_symlink_spelling_is_refused_too(self, tmp_path, capsys):
        work, merged, marker = self._setup(
            tmp_path, lambda w: _cmd_step([_PY, "-c", "pass", f"{tmp_path}/link/x"])
        )
        (tmp_path / "link").symlink_to(work)
        assert _merge(tmp_path / "link", merged) == verb.EXIT_POST_MERGE_FAILED
        assert tool_merge_tree.OUTCOME_STEP_NAMES_REPO_PATH in capsys.readouterr().err
        assert not marker.exists()

    def test_a_sibling_path_that_shares_the_prefix_is_not_refused(self, tmp_path):
        work, merged, marker = self._setup(
            tmp_path, lambda w: _cmd_step([_PY, "-c", "pass", f"{w}-other/x", f"/y{w}"])
        )
        assert _merge(work, merged) == verb.EXIT_OK
        assert marker.exists()

    def test_a_clean_base_checkout_is_unchanged_even_when_a_step_names_its_path(self, tmp_path):
        work, merged, marker = _scenario(tmp_path, shape="clean-base", steps=False)
        ms.write_merge_config(work, [_step(marker), _cmd_step([_PY, "-c", "pass", str(work)])])
        assert _merge(work, merged) == verb.EXIT_OK
        assert marker.exists()


class TestDetachedStepIsRefusedInTheToolTree:
    """A `detaches: true` step would outlive the tool tree it starts from, so the
    tool tree refuses it before any step runs; a clean base checkout runs it."""

    @staticmethod
    def _detached(marker) -> dict:
        code = f"import pathlib; pathlib.Path(r'{marker}').write_text('ran')"
        return {"cmd": [_PY, "-c", code], "on_failure": "warn", "detaches": True,
                "description": "start the daemon"}

    @pytest.mark.parametrize("verb_name", ["merge", "post-merge"])
    def test_refused_before_any_step_runs(self, tmp_path, capsys, verb_name):
        work, merged, marker = _scenario(tmp_path, shape="feature", steps=False)
        detached_marker = tmp_path / "detached.txt"
        ms.write_merge_config(work, [_step(marker), self._detached(detached_marker)])
        before = _snapshot(work)
        if verb_name == "merge":
            code, expected = _merge(work, merged), verb.EXIT_POST_MERGE_FAILED
        else:
            code, expected = _post_merge(work, merged), post_merge_verb.EXIT_POST_MERGE_FAILED
        assert code == expected
        err = capsys.readouterr().err
        assert tool_merge_tree.OUTCOME_DETACHED_STEP in err
        assert "step 2" in err and "start the daemon" in err and "clean base checkout" in err
        assert not marker.exists() and not detached_marker.exists()
        assert _snapshot(work) == before
        _assert_tool_tree_gone(work, tmp_path, merged)

    def test_a_clean_base_checkout_runs_the_same_config(self, tmp_path):
        work, merged, marker = _scenario(tmp_path, shape="clean-base", steps=False)
        detached_marker = tmp_path / "detached.txt"
        ms.write_merge_config(work, [_step(marker), self._detached(detached_marker)])
        assert _merge(work, merged) == verb.EXIT_OK
        assert marker.exists()
        deadline = time.time() + 10
        while not detached_marker.exists() and time.time() < deadline:
            time.sleep(0.1)
        assert detached_marker.exists()


class TestRefusalStopsTheMerge:
    """The tool-tree refusals are decided before the server-side merge, so a refused
    step leaves the PR unmerged instead of merged with no deploy."""

    @staticmethod
    def _merge_recording(work, merged):
        seen: list[tuple[str, str]] = []
        inner = ms.make_merge_opener()

        def opener(req, timeout=15):
            seen.append((req.get_method(), req.full_url))
            return inner(req, timeout)

        argv = ms.merge_base_args(**{"--repo-path": str(work), "--platform": "forgejo"})
        code = verb.main(
            argv,
            token_provider=ms.RecordingTokenProvider(),
            authority_provider=ms.AllowingAuthorityProvider(),
            opener=opener,
        )
        merge_calls = [c for c in seen if c[0] in ("POST", "PUT") and c[1].endswith("/merge")]
        return code, merge_calls

    def _bad_steps(self, kind, work, tmp_path):
        if kind == "detaches":
            return [{"cmd": [_PY, "-c", "pass"], "on_failure": "warn", "detaches": True}]
        return [_cmd_step([_PY, "-c", "pass", f"{work}/scripts/install.sh"])]

    @pytest.mark.parametrize("kind", ["detaches", "repo-path"])
    def test_a_feature_branch_checkout_is_refused_with_nothing_merged(
        self, tmp_path, capsys, kind
    ):
        work, merged, marker = _scenario(tmp_path, shape="feature", steps=False)
        ms.write_merge_config(work, self._bad_steps(kind, work, tmp_path))
        before = _snapshot(work)
        code, merge_calls = self._merge_recording(work, merged)
        assert code == verb.EXIT_POST_MERGE_FAILED
        assert merge_calls == []
        err = capsys.readouterr().err
        outcome = (
            tool_merge_tree.OUTCOME_DETACHED_STEP
            if kind == "detaches"
            else tool_merge_tree.OUTCOME_STEP_NAMES_REPO_PATH
        )
        assert outcome in err and "was NOT merged" in err
        assert "all gates PASSED -- merging" not in err
        assert _snapshot(work) == before
        _assert_tool_tree_gone(work, tmp_path, merged)

    @pytest.mark.parametrize("kind", ["detaches", "repo-path"])
    def test_a_clean_base_checkout_merges_the_same_config(self, tmp_path, kind):
        work, merged, marker = _scenario(tmp_path, shape="clean-base", steps=False)
        ms.write_merge_config(work, self._bad_steps(kind, work, tmp_path))
        code, merge_calls = self._merge_recording(work, merged)
        assert code == verb.EXIT_OK
        assert len(merge_calls) == 1


class TestRefuseStepsUnit:
    def _repo(self, tmp_path):
        repo = tmp_path / "x" / "repo"
        repo.mkdir(parents=True)
        _git(["init", "-b", _BASE, str(repo)], tmp_path)
        return repo.resolve()

    @pytest.mark.parametrize(
        ("text", "refused"),
        [
            ("{r}", True),
            ("{r}/sub/file", True),
            ("cd {r}; make", True),
            ("--prefix={r}", True),
            ("{r}-other", False),
            ("{r}.git", False),
            ("/y{r}", False),
            ("{r}x/file", False),
            ("relative/path", False),
        ],
    )
    def test_path_boundary(self, tmp_path, text, refused):
        repo = self._repo(tmp_path)
        steps = [_cmd_step(["bash", "-c", text.format(r=repo)])]
        if refused:
            with pytest.raises(tool_merge_tree.StepNamesRepoPathError):
                tool_merge_tree.refuse_steps_naming_repo_path(steps, repo)
        else:
            tool_merge_tree.refuse_steps_naming_repo_path(steps, repo)

    def test_a_description_alone_is_not_scanned(self, tmp_path):
        repo = self._repo(tmp_path)
        step = {**_cmd_step(["true"]), "description": f"install from {repo}"}
        tool_merge_tree.refuse_steps_naming_repo_path([step], repo)


class TestMergeTreeEnv:
    @pytest.mark.parametrize("platform", ["forgejo", "github"])
    def test_tool_tree_case_exports_the_tree_root(self, tmp_path, platform):
        work, merged, marker = _scenario(tmp_path, shape="feature", steps=False, platform=platform)
        ms.write_merge_config(work, [_record_env_step(marker)])
        assert _merge(work, merged, platform) == verb.EXIT_OK
        value, cwd = marker.read_text().split("|")
        assert value.startswith(str(tmp_path / "trees")) and value.endswith(merged)
        assert cwd == value

    @pytest.mark.parametrize("platform", ["forgejo", "github"])
    def test_clean_case_exports_the_repository_top_level(self, tmp_path, platform):
        work, merged, marker = _scenario(tmp_path, shape="clean-base", steps=False, platform=platform)
        ms.write_merge_config(work, [_record_env_step(marker)])
        assert _merge(work, merged, platform) == verb.EXIT_OK
        assert marker.read_text().split("|")[0] == str(work.resolve())

    def test_post_merge_verb_exports_it_in_both_cases(self, tmp_path):
        work, merged, marker = _scenario(tmp_path, shape="feature", steps=False)
        ms.write_merge_config(work, [_record_env_step(marker)])
        assert _post_merge(work, merged) == post_merge_verb.EXIT_OK
        assert marker.read_text().split("|")[0].startswith(str(tmp_path / "trees"))

    def test_post_merge_verb_clean_case(self, tmp_path):
        work, merged, marker = _scenario(tmp_path, shape="clean-base", steps=False)
        ms.write_merge_config(work, [_record_env_step(marker)])
        assert _post_merge(work, merged) == post_merge_verb.EXIT_OK
        assert marker.read_text().split("|")[0] == str(work.resolve())

    def test_it_wins_over_a_deployment_override(self, tmp_path, monkeypatch):
        work, merged, marker = _scenario(tmp_path, shape="clean-base", steps=False)
        ms.write_merge_config(work, [_record_env_step(marker)])
        monkeypatch.setenv(f"CLAGENTIC_LOADOUT_POST_MERGE_ENV_{tool_merge_tree.ENV_MERGE_TREE}", "/elsewhere")
        assert _merge(work, merged) == verb.EXIT_OK
        assert marker.read_text().split("|")[0] == str(work.resolve())


class TestStepsLoadFromRepoPathConfig:
    def test_untracked_config_is_read_from_repo_path_not_the_tool_tree(self, tmp_path):
        work, merged, marker = _scenario(tmp_path, shape="feature", steps=False)
        code = (
            "import pathlib;"
            f"pathlib.Path(r'{marker}').write_text(str(pathlib.Path('.clagentic/loadout/config.yaml').exists()))"
        )
        ms.write_merge_config(work, [_cmd_step([_PY, "-c", code])])
        assert _merge(work, merged) == verb.EXIT_OK
        assert marker.read_text() == "False"

    def test_the_drift_check_prefers_git_tracked_steps_at_the_merged_commit(
        self, tmp_path, capsys
    ):
        work, _, marker = _scenario(tmp_path, shape="feature", steps=False)
        stale_marker = tmp_path / "stale.txt"
        ms.write_merge_config(work, [_cmd_step([_PY, "-c", f"open(r'{stale_marker}', 'w').close()"])])
        tracked = yaml.safe_dump(
            {"merge": {"post_merge_steps": [_cmd_step([_PY, "-c", f"open(r'{marker}', 'w').close()"])]}}
        )

        def write(clone):
            (clone / ".clagentic" / "loadout").mkdir(parents=True)
            (clone / ".clagentic" / "loadout" / "config.yaml").write_text(tracked, encoding="utf-8")
            _git(["add", "-f", ".clagentic/loadout/config.yaml"], clone)

        merged = _commit_to_origin(work, tmp_path, "config-clone", write)
        before = _snapshot(work)
        assert _merge(work, merged) == verb.EXIT_OK
        assert "DRIFT" in capsys.readouterr().err
        assert marker.exists() and not stale_marker.exists()
        assert _snapshot(work) == before


@pytest.fixture
def _allow_file_submodules(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")


class TestSubmodules:
    def _submodule_scenario(self, tmp_path, marker_code):
        work, _, marker = _scenario(tmp_path, shape="feature", steps=False)
        sub = tmp_path / "sub-source"
        sub.mkdir()
        _git(["init", "-b", _BASE, str(sub)], tmp_path)
        _git(["config", "user.email", "t@example.com"], sub)
        _git(["config", "user.name", "t"], sub)
        (sub / "s.txt").write_text("pinned\n", encoding="utf-8")
        _git(["add", "s.txt"], sub)
        _git(["commit", "-m", "sub"], sub)

        def write(clone):
            _git(["submodule", "add", str(sub), "vendor/sub"], clone)

        merged = _commit_to_origin(work, tmp_path, "submodule-clone", write)
        ms.write_merge_config(work, [_cmd_step([_PY, "-c", marker_code.format(marker=marker)])])
        return work, merged, marker, sub

    def test_the_pinned_submodule_is_checked_out_in_the_tool_tree(
        self, tmp_path, _allow_file_submodules
    ):
        code = "import pathlib; pathlib.Path(r'{marker}').write_text(pathlib.Path('vendor/sub/s.txt').read_text())"
        work, merged, marker, _ = self._submodule_scenario(tmp_path, code)
        before = _snapshot(work)
        assert _merge(work, merged) == verb.EXIT_OK
        assert marker.read_text() == "pinned\n"
        assert _snapshot(work) == before
        _assert_tool_tree_gone(work, tmp_path, merged)

    def test_a_submodule_that_will_not_check_out_refuses_before_any_step(
        self, tmp_path, capsys, _allow_file_submodules
    ):
        work, merged, marker, sub = self._submodule_scenario(tmp_path, "open(r'{marker}', 'w').close()")
        shutil.rmtree(sub)
        before = _snapshot(work)
        assert _merge(work, merged) == verb.EXIT_POST_MERGE_FAILED
        err = capsys.readouterr().err
        assert tool_merge_tree.OUTCOME_TREE_UNAVAILABLE in err and "submodule" in err
        assert not marker.exists()
        assert _snapshot(work) == before
        _assert_tool_tree_gone(work, tmp_path, merged)


class TestFailureMessageInTheToolTree:
    def _failing(self, tmp_path):
        work, merged, _ = _scenario(tmp_path, shape="feature", steps=False)
        ms.write_merge_config(work, [_cmd_step([_PY, "-c", "import sys; sys.exit(3)"])])
        return work, merged

    def test_merge_verb_failure_says_it_ran_in_a_fresh_tree(self, tmp_path, capsys):
        work, merged = self._failing(tmp_path)
        assert _merge(work, merged) == verb.EXIT_POST_MERGE_FAILED
        err = capsys.readouterr().err
        assert "failed (exit 3)" in err
        assert "fresh tree of the merged commit's tracked content" in err
        assert "untracked files from --repo-path" in err

    def test_post_merge_verb_failure_says_it_ran_in_a_fresh_tree(self, tmp_path, capsys):
        work, merged = self._failing(tmp_path)
        assert _post_merge(work, merged) == post_merge_verb.EXIT_POST_MERGE_FAILED
        assert "fresh tree of the merged commit's tracked content" in capsys.readouterr().err

    def test_a_clean_checkout_failure_has_no_such_note(self, tmp_path, capsys):
        work, merged, _ = _scenario(tmp_path, shape="clean-base", steps=False)
        ms.write_merge_config(work, [_cmd_step([_PY, "-c", "import sys; sys.exit(3)"])])
        assert _merge(work, merged) == verb.EXIT_POST_MERGE_FAILED
        assert "fresh tree" not in capsys.readouterr().err
