"""No hook and no fsmonitor helper executes while the merge-result worktree is
created, populated or torn down, and every git command the worktree helper runs
carries the caller's scrubbed environment.

Real git and real executable scripts: a script that fires writes a marker file,
so a test fails on observed execution, not on an inspected argument list. Each
scenario has a control that shows the same setup does fire when the protection
is switched off, so a green result cannot come from a hook git never runs."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from clagentic_loadout.merge import merge_result_worktree as worktree_module
from clagentic_loadout.merge import verb
from clagentic_loadout.merge.merge_result_worktree import merge_result_worktree
from tests._gate_repo import GateRepo, commit_files, git, init_gate_repo
from tests._support.gate_merge import run_gate_merge

_PY = sys.executable
_HOOK_NAMES = ("post-checkout", "post-merge", "pre-merge-commit", "reference-transaction")


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


def _plant_shared_hooks(repo: GateRepo, marker: Path) -> None:
    for name in _HOOK_NAMES:
        _script(repo.path / ".git" / "hooks" / name, marker)


def _plant_tracked_hooks_path(tmp_path, marker: Path) -> GateRepo:
    """A core.hooksPath in the shared config that points at a directory tracked
    in the PR head, so the hook only exists once the head is checked out."""
    scripts = {f"githooks/{name}": f"#!/bin/sh\necho \"$0\" >> {marker}\nexit 0\n" for name in _HOOK_NAMES}
    repo = _repo(tmp_path, head_files=scripts)
    git(repo.path, "checkout", "-q", "pr")
    for relative in scripts:
        (repo.path / relative).chmod(0o755)
        git(repo.path, "add", "--", relative)
    git(repo.path, "commit", "-q", "--amend", "--no-edit")
    repo.head_sha = git(repo.path, "rev-parse", "HEAD")
    git(repo.path, "checkout", "-q", "main")
    git(repo.path, "config", "core.hooksPath", "githooks")
    return repo


def _enter(repo: GateRepo, **kwargs) -> None:
    with merge_result_worktree(repo.path, repo.base_sha, repo.head_sha, **kwargs):
        pass


@pytest.fixture
def marker(tmp_path) -> Path:
    return tmp_path / "fired"


class TestSharedHooksNeverRun:
    @pytest.mark.parametrize("diverged", [False, True], ids=["fast-forward", "merge"])
    def test_no_hook_in_the_shared_hooks_directory_runs(self, tmp_path, scratch_tmp, marker, diverged):
        repo = _repo(tmp_path)
        if diverged:
            _diverge(repo)
        _plant_shared_hooks(repo, marker)
        _enter(repo)
        assert not marker.exists()

    def test_control_the_same_hooks_fire_when_the_override_is_removed(
        self, tmp_path, scratch_tmp, marker, monkeypatch
    ):
        monkeypatch.setattr(worktree_module, "_NO_EXECUTION_CONFIG", ())
        repo = _repo(tmp_path)
        _plant_shared_hooks(repo, marker)
        _enter(repo)
        assert marker.exists()


class TestAHooksPathIntoTheHeadNeverRuns:
    @pytest.mark.parametrize("diverged", [False, True], ids=["fast-forward", "merge"])
    def test_a_hook_tracked_in_the_pr_head_does_not_run(self, tmp_path, scratch_tmp, marker, diverged):
        repo = _plant_tracked_hooks_path(tmp_path, marker)
        if diverged:
            _diverge(repo)
        _enter(repo)
        assert not marker.exists()

    def test_control_the_same_hook_fires_when_the_override_is_removed(
        self, tmp_path, scratch_tmp, marker, monkeypatch
    ):
        monkeypatch.setattr(worktree_module, "_NO_EXECUTION_CONFIG", ())
        repo = _plant_tracked_hooks_path(tmp_path, marker)
        _enter(repo)
        assert marker.exists()


class TestAPlantedFsmonitorNeverRuns:
    def _plant(self, repo: GateRepo, marker: Path) -> None:
        helper = repo.path.parent / "fsmonitor-helper"
        _script(helper, marker)
        git(repo.path, "config", "core.fsmonitor", str(helper))

    @pytest.mark.parametrize("diverged", [False, True], ids=["fast-forward", "merge"])
    def test_it_does_not_run_during_add_checkout_merge_or_teardown(
        self, tmp_path, scratch_tmp, marker, diverged
    ):
        repo = _repo(tmp_path)
        if diverged:
            _diverge(repo)
        self._plant(repo, marker)
        _enter(repo)
        assert not marker.exists()

    def test_control_the_same_helper_fires_when_the_override_is_removed(
        self, tmp_path, scratch_tmp, marker, monkeypatch
    ):
        monkeypatch.setattr(worktree_module, "_NO_EXECUTION_CONFIG", ())
        repo = _repo(tmp_path)
        self._plant(repo, marker)
        _enter(repo)
        assert marker.exists()


class TestEveryGitCommandCarriesTheScrubbedEnvironment:
    @pytest.mark.parametrize("diverged", [False, True], ids=["fast-forward", "merge"])
    def test_add_checkout_merge_remove_and_prune_all_get_exactly_the_given_env(
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
        # availability probe that runs before the worktree exists is not.
        monkeypatch.setattr(worktree_module, "subprocess", SimpleNamespace(run=recording_run))
        scrubbed = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "ONLY_IN_SCRUBBED": "1"}
        _enter(repo, env=scrubbed)

        # argv is ["git", "-c", <cfg>, "-c", <cfg>, <subcommand>, ...]
        commands = {" ".join(argv[5:7]) if argv[5] == "worktree" else argv[5] for argv, _ in seen}
        assert {"worktree add", "worktree remove", "worktree prune"} <= commands
        assert ("merge" if diverged else "checkout") in commands
        for argv, env in seen:
            assert env == scrubbed
            assert "core.hooksPath=/dev/null" in argv and "core.fsmonitor=false" in argv


class TestTheGuardRestoresBeforeTeardown:
    def test_teardown_sees_the_restored_shared_config(self, tmp_path, scratch_tmp, monkeypatch):
        planted = "import subprocess; subprocess.run(['git', 'config', 'core.fsmonitor', '/evil'], check=True)"
        repo = init_gate_repo(
            tmp_path / "shared",
            tracked_gate={
                "required_reviewer_roles": [],
                "pre_checks": [{"cmd": [_PY, "-c", planted], "on_failure": "fail"}],
            },
        )
        config = repo.path / ".git" / "config"
        seen_at_teardown: list[str] = []
        real_remove = worktree_module._remove

        def spying_remove(*args, **kwargs):
            seen_at_teardown.append(config.read_text(encoding="utf-8"))
            return real_remove(*args, **kwargs)

        monkeypatch.setattr(worktree_module, "_remove", spying_remove)
        assert run_gate_merge(repo) == verb.EXIT_PRE_CHECKS_FAILED
        assert len(seen_at_teardown) == 1
        assert "fsmonitor" not in seen_at_teardown[0]
