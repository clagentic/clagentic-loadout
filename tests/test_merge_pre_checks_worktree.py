"""The pre_checks gate runs against the merge result in a throwaway worktree.

Real git throughout: a shared tree sitting on base (where a check can be red
for something the PR itself fixes), a PR head that is either a fast-forward of
base or diverged from it, unrelated uncommitted edits in the shared tree, and
the cleanup guarantee on every way a check run can end. Only the git host's
HTTP surface is a canned double."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.merge_result_worktree import (
    MergeResultConflictError,
    merge_result_worktree,
)
from tests._gate_repo import GateRepo, commit_files, git, init_gate_repo
from tests._support.gate_merge import run_gate_merge as _merge

_PY = sys.executable


def _check(code: str, **extra) -> dict:
    return {"cmd": [_PY, "-c", code], "on_failure": "fail", **extra}


def _exists(name: str) -> dict:
    return _check(f"import os, sys; sys.exit(0 if os.path.exists({name!r}) else 1)")


def _record_cwd(marker: Path, *, then: str = "") -> dict:
    return _check(f"import os; open({str(marker)!r}, 'w').write(os.getcwd()); {then}")


def _repo(tmp_path, steps, **kwargs) -> GateRepo:
    gate = {"required_reviewer_roles": [], "pre_checks": steps}
    return init_gate_repo(tmp_path / "shared", tracked_gate=gate, **kwargs)


def _worktrees(repo: GateRepo) -> list[str]:
    return git(repo.path, "worktree", "list").splitlines()


def _advance_base(repo: GateRepo, files: dict[str, str]) -> None:
    """Move `main` ahead of where the PR branched and make that the PR's base."""
    repo.base_sha = commit_files(repo.path, files, "base moves")


class TestTheCheckSeesTheMergeResultNotTheSharedTree:
    def test_a_check_red_on_base_that_the_pr_fixes_passes(self, tmp_path, scratch_tmp):
        repo = _repo(tmp_path, [_exists("fixed.txt")], head_files={"fixed.txt": "x\n"})
        assert not (repo.path / "fixed.txt").exists()
        calls: list[str] = []
        assert _merge(repo, calls) == verb.EXIT_OK
        assert len(calls) == 1

    def test_a_check_red_on_the_result_refuses_before_the_merge_call(self, tmp_path, scratch_tmp):
        repo = _repo(tmp_path, [_exists("never.txt")])
        calls: list[str] = []
        assert _merge(repo, calls) == verb.EXIT_PRE_CHECKS_FAILED
        assert calls == []

    def test_unrelated_uncommitted_edits_do_not_leak_and_are_left_untouched(self, tmp_path, scratch_tmp):
        leak_probe = _check(
            "import os, sys; "
            "sys.exit(0 if (not os.path.exists('scratch.txt') "
            "and open('README.md').read() == 'base\\n') else 1)"
        )
        repo = _repo(tmp_path, [leak_probe])
        (repo.path / "README.md").write_text("locally edited\n", encoding="utf-8")
        (repo.path / "scratch.txt").write_text("untracked\n", encoding="utf-8")
        head_before = git(repo.path, "rev-parse", "HEAD")
        status_before = git(repo.path, "status", "--porcelain")
        branch_before = git(repo.path, "rev-parse", "--abbrev-ref", "HEAD")

        assert _merge(repo) == verb.EXIT_OK

        assert git(repo.path, "rev-parse", "HEAD") == head_before
        assert git(repo.path, "rev-parse", "--abbrev-ref", "HEAD") == branch_before
        assert git(repo.path, "status", "--porcelain") == status_before
        assert (repo.path / "README.md").read_text(encoding="utf-8") == "locally edited\n"
        assert (repo.path / "scratch.txt").read_text(encoding="utf-8") == "untracked\n"
        assert git(repo.path, "stash", "list") == ""

    def test_a_head_that_diverged_from_base_is_merged_onto_it_in_the_worktree(self, tmp_path, scratch_tmp):
        repo = _repo(tmp_path, [_exists("change.txt"), _exists("base-only.txt")])
        _advance_base(repo, {"base-only.txt": "b\n"})
        assert not (repo.path / "change.txt").exists()
        assert _merge(repo) == verb.EXIT_OK
        assert not (repo.path / "change.txt").exists()

    def test_a_head_that_conflicts_with_base_refuses_before_the_merge_call(
        self, tmp_path, scratch_tmp, capsys
    ):
        repo = _repo(tmp_path, [_check("pass")], head_files={"README.md": "from the pr\n"})
        _advance_base(repo, {"README.md": "from base\n"})
        calls: list[str] = []
        assert _merge(repo, calls) == verb.EXIT_PRE_CHECKS_FAILED
        assert calls == []
        err = capsys.readouterr().err
        assert "conflicts with base" in err and "not mergeable" in err
        assert len(_worktrees(repo)) == 1


class TestTheWorktreeIsAlwaysRemoved:
    def _assert_gone(self, repo, marker: Path, scratch_tmp: Path) -> None:
        ran_in = Path(marker.read_text(encoding="utf-8"))
        assert scratch_tmp in ran_in.parents, "the worktree must live under the per-process TMPDIR"
        assert repo.path not in ran_in.parents and ran_in != repo.path
        assert not ran_in.exists()
        assert not ran_in.parent.exists(), "the scratch directory is removed with it"
        assert len(_worktrees(repo)) == 1
        assert git(repo.path, "worktree", "prune", "--dry-run") == ""

    def test_after_a_pass(self, tmp_path, scratch_tmp):
        marker = tmp_path / "ran-in"
        repo = _repo(tmp_path, [_record_cwd(marker)])
        assert _merge(repo) == verb.EXIT_OK
        self._assert_gone(repo, marker, scratch_tmp)

    def test_after_a_failure(self, tmp_path, scratch_tmp):
        marker = tmp_path / "ran-in"
        repo = _repo(tmp_path, [_record_cwd(marker, then="raise SystemExit(1)")])
        assert _merge(repo) == verb.EXIT_PRE_CHECKS_FAILED
        self._assert_gone(repo, marker, scratch_tmp)

    def test_after_a_timeout(self, tmp_path, scratch_tmp):
        marker = tmp_path / "ran-in"
        repo = _repo(
            tmp_path,
            [_record_cwd(marker, then="import time; time.sleep(30)") | {"timeout_seconds": 1}],
        )
        assert _merge(repo) == verb.EXIT_PRE_CHECKS_FAILED
        self._assert_gone(repo, marker, scratch_tmp)

    def test_after_an_unexpected_exception(self, tmp_path, scratch_tmp, monkeypatch):
        marker = tmp_path / "ran-in"
        repo = _repo(tmp_path, [_check("pass")])

        def boom(steps, project_root, **kwargs):
            marker.write_text(str(project_root), encoding="utf-8")
            assert Path(project_root).is_dir()
            raise RuntimeError("runner blew up")

        monkeypatch.setattr(verb, "run_post_merge_steps", boom)
        with pytest.raises(RuntimeError, match="runner blew up"):
            _merge(repo)
        self._assert_gone(repo, marker, scratch_tmp)


class TestTheWorktreeHelper:
    def test_it_yields_the_head_checkout_for_a_fast_forward(self, tmp_path, scratch_tmp):
        repo = _repo(tmp_path, [])
        with merge_result_worktree(repo.path, repo.base_sha, repo.head_sha) as tree:
            assert git(tree, "rev-parse", "HEAD") == repo.head_sha
            assert (tree / "change.txt").exists()
        assert not tree.exists()

    def test_a_conflict_raises_and_still_cleans_up(self, tmp_path, scratch_tmp):
        repo = _repo(tmp_path, [], head_files={"README.md": "pr\n"})
        _advance_base(repo, {"README.md": "base moved\n"})
        with pytest.raises(MergeResultConflictError):
            with merge_result_worktree(repo.path, repo.base_sha, repo.head_sha):
                pytest.fail("no tree may be yielded for a conflicting head")
        assert list(scratch_tmp.iterdir()) == []
        assert len(_worktrees(repo)) == 1
