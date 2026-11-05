"""test_merge_premerge_steps.py -- the refusal decided before the server-side merge
judges the post_merge_steps of the commit that will be merged (the PR head merged
onto the base tip), not the ones in the --repo-path working tree.

A real repository with a bare origin stands in for the host. The base commit
tracks a deployment config; the PR head commit rewrites it; --repo-path sits on a
feature branch (so the steps would run in a tool-owned tree) with the base's
config in its working tree.
"""

from __future__ import annotations

import re
import subprocess
import sys

import pytest

from clagentic_loadout.merge import tool_merge_tree, verb
from clagentic_loadout.transport import provider_config
from tests import _merge_support as ms
from tests._merge_support import write_merge_config
from tests._gate_repo import CONFIRMABLE_GITHUB_ORIGIN, make_origin_confirmable

_PY = sys.executable
_PLATFORMS = ["forgejo", "github"]


def _git(args, cwd) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(cwd))
    assert result.returncode == 0, f"git {args!r}: {result.stderr}"
    return result.stdout.strip()


def _step(*extra) -> dict:
    return {"cmd": [_PY, "-c", "pass", *extra], "on_failure": "fail"}


def _benign() -> list[dict]:
    return [_step()]


def _naming(work) -> list[dict]:
    return [_step(), _step(f"{work}/scripts/install.sh")]


def _changed(steps: list[dict]) -> list[dict]:
    """The same steps plus one benign step, so the head commit differs from base."""
    return [*steps, _step("changed")]


def _origin(work) -> str:
    """The bare repository behind *work*; its `origin` remote is rewired to a host
    URL, so pushes name the path."""
    return str(work.parent / f"{work.name}-origin.git")


def _commit(work, message: str) -> str:
    _git(["commit", "-a", "-m", message], work)
    return _git(["rev-parse", "HEAD"], work)


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config-root")
    monkeypatch.setenv(tool_merge_tree.TREES_DIR_ENV_VAR, str(tmp_path / "trees"))
    ms.SEEDED_BASE["sha"] = ""
    yield
    ms.SEEDED_BASE["sha"] = ""


def _build(tmp_path, *, platform, base_steps, head_steps, base_advances=False):
    """Returns (work, base_sha, head_sha). *base_steps* / *head_steps* are
    callables of the work path."""
    work = tmp_path / "work"
    work.mkdir()
    ms.init_repo_with_origin(work)
    if platform == "github":
        make_origin_confirmable(work, url=CONFIRMABLE_GITHUB_ORIGIN)
    write_merge_config(work, base_steps(work))
    _git(["add", ".clagentic/loadout/config.yaml"], work)
    branch_point = _commit(work, "base config")
    _git(["push", _origin(work), "main:main"], work)
    _git(["checkout", "-q", "-b", "pr-head"], work)
    write_merge_config(work, head_steps(work))
    head_sha = _commit(work, "head config")
    _git(["checkout", "-q", "main"], work)
    base_sha = branch_point
    if base_advances:
        (work / "other.txt").write_text("base moved\n", encoding="utf-8")
        _git(["add", "other.txt"], work)
        base_sha = _commit(work, "base moves on")
        _git(["push", _origin(work), "main:main"], work)
    ms.record_base_sha(base_sha)
    _git(["checkout", "-q", "-b", "feature/x"], work)
    return work, base_sha, head_sha


def _run(work, head_sha, platform):
    pr_info = {"head": {"sha": head_sha}, "title": "feat: a change", "base": ms.default_base()}
    seen: list[tuple[str, str]] = []
    inner = (
        ms.make_merge_github_opener(pr_info=pr_info)
        if platform == "github"
        else ms.make_merge_opener(pr_info=pr_info)
    )

    def opener(req, timeout=15):
        seen.append((req.get_method(), req.full_url))
        return inner(req, timeout)

    argv = ms.merge_base_args(**{"--repo-path": str(work), "--platform": platform})
    code = verb.main(
        argv,
        token_provider=ms.RecordingTokenProvider(),
        authority_provider=ms.AllowingAuthorityProvider(),
        opener=opener,
    )
    merge_calls = [c for c in seen if c[0] in ("POST", "PUT") and c[1].endswith("/merge")]
    return code, merge_calls


@pytest.mark.parametrize("base_advances", [False, True], ids=["head-on-base", "base-moved-on"])
@pytest.mark.parametrize("platform", _PLATFORMS)
def test_a_pr_that_removes_a_repo_path_step_is_not_refused(
    tmp_path, capsys, platform, base_advances
):
    work, _, head_sha = _build(
        tmp_path, platform=platform, base_steps=_naming, head_steps=lambda w: _benign(),
        base_advances=base_advances,
    )
    _, merge_calls = _run(work, head_sha, platform)
    err = capsys.readouterr().err
    # The fake host lands nothing, so the post-merge steps that follow run the
    # base's config; only the decision before the merge is under test.
    assert "refused before merging" not in err
    assert "was NOT merged" not in err
    assert len(merge_calls) == 1


@pytest.mark.parametrize("base_advances", [False, True], ids=["head-on-base", "base-moved-on"])
@pytest.mark.parametrize("platform", _PLATFORMS)
def test_a_pr_that_adds_a_repo_path_step_is_refused(tmp_path, capsys, platform, base_advances):
    work, _, head_sha = _build(
        tmp_path, platform=platform, base_steps=lambda w: _benign(), head_steps=_naming,
        base_advances=base_advances,
    )
    code, merge_calls = _run(work, head_sha, platform)
    err = capsys.readouterr().err
    assert code == verb.EXIT_POST_MERGE_FAILED == 28
    assert tool_merge_tree.OUTCOME_STEP_NAMES_REPO_PATH in err
    assert merge_calls == []
    # The refusal names the commit the steps were read from.
    match = re.search(r"merged commit ([0-9a-f]{40})", err)
    assert match is not None
    if not base_advances:
        assert match.group(1) == head_sha


@pytest.mark.parametrize("platform", _PLATFORMS)
def test_a_step_present_on_both_sides_is_still_refused(tmp_path, capsys, platform):
    work, _, head_sha = _build(
        tmp_path, platform=platform, base_steps=_naming, head_steps=lambda w: _changed(_naming(w))
    )
    code, merge_calls = _run(work, head_sha, platform)
    assert code == verb.EXIT_POST_MERGE_FAILED
    assert tool_merge_tree.OUTCOME_STEP_NAMES_REPO_PATH in capsys.readouterr().err
    assert merge_calls == []


@pytest.mark.parametrize("platform", _PLATFORMS)
def test_steps_that_cannot_be_read_from_the_merged_commit_refuse(tmp_path, capsys, platform):
    work, _, head_sha = _build(
        tmp_path, platform=platform, base_steps=lambda w: _benign(), head_steps=lambda w: _changed(_benign())
    )
    # A head config whose post_merge_steps is not a list.
    broken = work / ".clagentic" / "loadout" / "config.yaml"
    _git(["checkout", "-q", "pr-head"], work)
    broken.write_text("merge:\n  post_merge_steps: not-a-list\n", encoding="utf-8")
    head_sha = _commit(work, "broken head config")
    _git(["checkout", "-q", "feature/x"], work)
    code, merge_calls = _run(work, head_sha, platform)
    err = capsys.readouterr().err
    assert code == verb.EXIT_POST_MERGE_FAILED
    assert "could not be read from merged commit" in err and "was NOT merged" in err
    assert merge_calls == []


@pytest.mark.parametrize("platform", _PLATFORMS)
def test_a_head_commit_that_cannot_be_built_refuses(tmp_path, capsys, platform):
    work, _, _ = _build(
        tmp_path, platform=platform, base_steps=lambda w: _benign(), head_steps=lambda w: _changed(_benign())
    )
    code, merge_calls = _run(work, "c" * 40, platform)
    err = capsys.readouterr().err
    assert code == verb.EXIT_POST_MERGE_FAILED
    assert "could not be built" in err and "was NOT merged" in err
    assert merge_calls == []


_CONFIG = ".clagentic/loadout/config.yaml"


def _head_that_deletes_the_config(work) -> str:
    _git(["checkout", "-q", "pr-head"], work)
    _git(["rm", "-q", _CONFIG], work)
    head_sha = _commit(work, "head drops the config")
    _git(["checkout", "-q", "feature/x"], work)
    return head_sha


@pytest.mark.parametrize("platform", _PLATFORMS)
def test_config_tracked_at_base_but_untracked_at_checkout_head_judges_working_tree(
    tmp_path, capsys, platform
):
    work, base_sha, _ = _build(
        tmp_path, platform=platform, base_steps=lambda w: _benign(), head_steps=lambda w: _changed(_benign())
    )
    head_sha = _head_that_deletes_the_config(work)
    # The checkout's own HEAD stops tracking the config, though the base does;
    # the file stays in the working tree, naming the repo path.
    _git(["rm", "-q", "--cached", _CONFIG], work)
    _commit(work, "checkout untracks the config")
    write_merge_config(work, _naming(work))
    assert _git(["rev-parse", "HEAD"], work) != base_sha
    code, merge_calls = _run(work, head_sha, platform)
    err = capsys.readouterr().err
    assert code == verb.EXIT_POST_MERGE_FAILED
    assert tool_merge_tree.OUTCOME_STEP_NAMES_REPO_PATH in err
    assert merge_calls == []


@pytest.mark.parametrize("platform", _PLATFORMS)
def test_config_untracked_at_base_but_tracked_at_checkout_head_is_zero_steps(
    tmp_path, capsys, platform
):
    work = tmp_path / "work"
    work.mkdir()
    ms.init_repo_with_origin(work)
    if platform == "github":
        make_origin_confirmable(work, url=CONFIRMABLE_GITHUB_ORIGIN)
    base_sha = _git(["rev-parse", "HEAD"], work)
    _git(["checkout", "-q", "-b", "pr-head"], work)
    (work / "head.txt").write_text("head\n", encoding="utf-8")
    _git(["add", "head.txt"], work)
    head_sha = _commit(work, "head change")
    _git(["checkout", "-q", "main"], work)
    ms.record_base_sha(base_sha)
    _git(["checkout", "-q", "-b", "feature/x"], work)
    # The checkout's HEAD tracks a config the base does not, and the merge result
    # (head onto base) has none: the merged commit declares zero steps.
    write_merge_config(work, _naming(work))
    _git(["add", _CONFIG], work)
    _commit(work, "checkout tracks the config")
    code, merge_calls = _run(work, head_sha, platform)
    err = capsys.readouterr().err
    assert "refused before merging" not in err
    assert tool_merge_tree.OUTCOME_STEP_NAMES_REPO_PATH not in err
    assert len(merge_calls) == 1
