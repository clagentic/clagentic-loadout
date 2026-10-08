"""No hook and no fsmonitor helper executes while the merge-result clone is
created, populated or torn down, and every git command the clone helper runs
carries the caller's scrubbed environment.

Real git and real executable scripts: a script that fires writes a marker file,
so a test fails on observed execution, not on an inspected argument list. Each
scenario has a control that shows the same setup does fire when the protection
is switched off, so a green result cannot come from a hook git never runs.

The hooks come from places a private clone still consults: a template directory
copied into the clone's `.git/hooks`, and a user-level `core.hooksPath`
(absolute, or relative so that it resolves into the PR head's own tree)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from clagentic_loadout.merge import merge_result_clone as clone_module
from clagentic_loadout.merge.merge_result_clone import merge_result_clone
from tests._gate_repo import GateRepo, commit_files, git, init_gate_repo

_HOOK_NAMES = ("post-checkout", "reference-transaction")


def _script(path: Path, marker: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho \"$0\" >> {marker}\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def _repo(tmp_path, head_files=None) -> GateRepo:
    return init_gate_repo(
        tmp_path / "shared",
        tracked_gate={"required_reviewer_roles": [], "pre_checks": []},
        head_files=head_files,
    )


def _diverge(repo: GateRepo) -> None:
    """Move base ahead so the head must be merged, not fast-forwarded."""
    repo.base_sha = commit_files(repo.path, {"base-only.txt": "b\n"}, "base moves")


def _home(tmp_path, config: str) -> dict[str, str]:
    """An environment whose HOME carries a user-level git config."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(config, encoding="utf-8")
    return {"PATH": os.environ["PATH"], "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"}


def _enter(repo: GateRepo, **kwargs) -> None:
    with merge_result_clone(repo.path, repo.base_sha, repo.head_sha, **kwargs):
        pass


@pytest.fixture
def marker(tmp_path) -> Path:
    return tmp_path / "fired"


class TestTemplateHooksNeverRun:
    def _env(self, tmp_path, marker: Path) -> dict[str, str]:
        for name in _HOOK_NAMES:
            _script(tmp_path / "template" / "hooks" / name, marker)
        env = _home(tmp_path, "")
        env["GIT_TEMPLATE_DIR"] = str(tmp_path / "template")
        return env

    @pytest.mark.parametrize("diverged", [False, True], ids=["fast-forward", "merge"])
    def test_no_hook_copied_into_the_clone_runs(self, tmp_path, scratch_tmp, marker, diverged):
        repo = _repo(tmp_path)
        if diverged:
            _diverge(repo)
        _enter(repo, env=self._env(tmp_path, marker))
        assert not marker.exists()

    def test_control_the_same_hooks_fire_when_the_override_is_removed(
        self, tmp_path, scratch_tmp, marker, monkeypatch
    ):
        monkeypatch.setattr(clone_module, "_NO_EXECUTION_CONFIG", ())
        repo = _repo(tmp_path)
        _enter(repo, env=self._env(tmp_path, marker))
        assert marker.exists()


class TestAUserLevelHooksPathNeverRuns:
    def _absolute(self, tmp_path, marker: Path) -> dict[str, str]:
        for name in _HOOK_NAMES:
            _script(tmp_path / "user-hooks" / name, marker)
        return _home(tmp_path, f"[core]\n\thooksPath = {tmp_path / 'user-hooks'}\n")

    @pytest.mark.parametrize("diverged", [False, True], ids=["fast-forward", "merge"])
    def test_an_absolute_hooks_path_does_not_run(self, tmp_path, scratch_tmp, marker, diverged):
        repo = _repo(tmp_path)
        if diverged:
            _diverge(repo)
        _enter(repo, env=self._absolute(tmp_path, marker))
        assert not marker.exists()

    def test_control_the_absolute_hooks_fire_when_the_override_is_removed(
        self, tmp_path, scratch_tmp, marker, monkeypatch
    ):
        monkeypatch.setattr(clone_module, "_NO_EXECUTION_CONFIG", ())
        repo = _repo(tmp_path)
        _enter(repo, env=self._absolute(tmp_path, marker))
        assert marker.exists()

    def _into_the_head(self, tmp_path, marker: Path) -> tuple[GateRepo, dict[str, str]]:
        """A relative hooks path resolves into the checked-out tree, where the
        PR head tracks the hook."""
        scripts = {
            f"githooks/{name}": f"#!/bin/sh\necho \"$0\" >> {marker}\nexit 0\n" for name in _HOOK_NAMES
        }
        repo = _repo(tmp_path, head_files=scripts)
        git(repo.path, "checkout", "-q", "pr")
        for relative in scripts:
            (repo.path / relative).chmod(0o755)
            git(repo.path, "add", "--", relative)
        git(repo.path, "commit", "-q", "--amend", "--no-edit")
        repo.head_sha = git(repo.path, "rev-parse", "HEAD")
        git(repo.path, "checkout", "-q", "main")
        return repo, _home(tmp_path, "[core]\n\thooksPath = githooks\n")

    @pytest.mark.parametrize("diverged", [False, True], ids=["fast-forward", "merge"])
    def test_a_hook_tracked_in_the_pr_head_does_not_run(self, tmp_path, scratch_tmp, marker, diverged):
        repo, env = self._into_the_head(tmp_path, marker)
        if diverged:
            _diverge(repo)
        _enter(repo, env=env)
        assert not marker.exists()

    def test_control_the_tracked_hook_fires_when_the_override_is_removed(
        self, tmp_path, scratch_tmp, marker, monkeypatch
    ):
        monkeypatch.setattr(clone_module, "_NO_EXECUTION_CONFIG", ())
        repo, env = self._into_the_head(tmp_path, marker)
        _enter(repo, env=env)
        assert marker.exists()


class TestAPlantedFsmonitorNeverRuns:
    def _env(self, tmp_path, marker: Path) -> dict[str, str]:
        helper = tmp_path / "fsmonitor-helper"
        _script(helper, marker)
        return _home(tmp_path, f"[core]\n\tfsmonitor = {helper}\n")

    @pytest.mark.parametrize("diverged", [False, True], ids=["fast-forward", "merge"])
    def test_it_does_not_run_while_the_clone_is_built_or_removed(
        self, tmp_path, scratch_tmp, marker, diverged
    ):
        repo = _repo(tmp_path)
        if diverged:
            _diverge(repo)
        _enter(repo, env=self._env(tmp_path, marker))
        assert not marker.exists()

    def test_control_the_same_helper_fires_when_the_override_is_removed(
        self, tmp_path, scratch_tmp, marker, monkeypatch
    ):
        monkeypatch.setattr(clone_module, "_NO_EXECUTION_CONFIG", ())
        repo = _repo(tmp_path)
        _enter(repo, env=self._env(tmp_path, marker))
        assert marker.exists()


class TestEveryGitCommandCarriesTheScrubbedEnvironment:
    @pytest.mark.parametrize("diverged", [False, True], ids=["fast-forward", "merge"])
    def test_every_command_gets_the_given_env_without_repository_selectors(
        self, tmp_path, scratch_tmp, monkeypatch, diverged
    ):
        repo = _repo(tmp_path)
        if diverged:
            _diverge(repo)
        seen: list[tuple[list[str], dict | None]] = []
        real_run = subprocess.run

        def recording_run(argv, *args, **kwargs):
            if argv and argv[0] == "git":
                seen.append((list(argv), kwargs.get("env")))
            return real_run(argv, *args, **kwargs)

        # Only this module's own subprocess calls are recorded; the commit
        # availability probe that runs before the clone exists is not.
        monkeypatch.setattr(clone_module, "subprocess", SimpleNamespace(run=recording_run))
        given = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "ONLY_IN_GIVEN": "1",
            "GIT_DIR": str(repo.path / ".git"),
            "GIT_WORK_TREE": str(repo.path),
            "GIT_INDEX_FILE": str(tmp_path / "index"),
        }
        _enter(repo, env=given)

        # argv is ["git", "-c", <cfg>, "-c", <cfg>, "-c", <cfg>, <subcommand>, ...]
        commands = {argv[7] for argv, _ in seen}
        assert {"clone", "for-each-ref", "update-ref", "checkout"} <= commands
        assert ("commit-tree" in commands) is diverged
        for argv, env in seen:
            assert env is not None and env["ONLY_IN_GIVEN"] == "1"
            assert not {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"} & set(env)
            assert "core.hooksPath=/dev/null" in argv and "core.fsmonitor=false" in argv
            assert "commit.gpgsign=false" in argv
            assert ("GIT_AUTHOR_NAME" in env) is (argv[7] == "commit-tree")
