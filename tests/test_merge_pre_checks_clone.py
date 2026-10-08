"""The pre_checks gate runs against the merge result in a private clone.

Real git throughout: a shared tree sitting on base (where a check can be red
for something the PR itself fixes), a PR head that is either a fast-forward of
base or diverged from it, unrelated uncommitted edits in the shared tree, and
the cleanup guarantee on every way a check run can end. Only the git host's
HTTP surface is a canned double."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.merge_result_clone import (
    MergeResultConflictError,
    merge_result_clone,
)
from tests._gate_repo import GateRepo, commit_files, git, init_gate_repo
from tests._support.gate_merge import run_gate_merge as _merge

_PY = sys.executable

#: Reports what a check can see about its own repository, as JSON in argv[1].
_REPORT = """
import json, os, subprocess, sys
def g(*a):
    r = subprocess.run(['git', *a], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else 'ERR:' + r.stderr.strip()
out = {
    'head': g('rev-parse', 'HEAD'),
    'parents': g('rev-list', '--parents', '-n1', 'HEAD').split()[1:],
    'status': g('status', '--porcelain'),
    'diff': g('diff', '--name-only', 'origin/main..HEAD').splitlines(),
    'origin_main': g('rev-parse', 'refs/remotes/origin/main'),
    'origin_head': g('rev-parse', 'refs/remotes/origin/HEAD'),
    'origin_other': g('rev-parse', 'refs/remotes/origin/other'),
    'subject': g('log', '-1', '--format=%an <%ae>|%cn <%ce>'),
    'cwd': os.getcwd(),
}
open(sys.argv[1], 'w').write(json.dumps(out))
"""

#: Writes a hook, config, a ref and a commit into the check's own repository.
_TAMPER = """
import os, subprocess, sys
os.makedirs(os.path.join('.git', 'hooks'), exist_ok=True)
open(os.path.join('.git', 'hooks', 'post-checkout'), 'w').write('#!/bin/sh\\nexit 0\\n')
subprocess.run(['git', 'config', 'core.fsmonitor', '/evil'], check=True)
subprocess.run(['git', 'update-ref', 'refs/heads/evil', 'HEAD'], check=True)
subprocess.run(['git', '-c', 'user.name=x', '-c', 'user.email=x@example.invalid',
                'commit', '--allow-empty', '-qm', 'x'], check=True)
sys.exit(0 if 'evil' in open(os.path.join('.git', 'config')).read() else 1)
"""


def _check(code: str, *argv: str, **extra) -> dict:
    return {"cmd": [_PY, "-c", code, *argv], "on_failure": "fail", **extra}


def _exists(name: str) -> dict:
    return _check(f"import os, sys; sys.exit(0 if os.path.exists({name!r}) else 1)")


def _record_cwd(marker: Path, *, then: str = "") -> dict:
    return _check(f"import os; open({str(marker)!r}, 'w').write(os.getcwd()); {then}")


def _report(marker: Path) -> dict:
    return _check(_REPORT, str(marker))


def _repo(tmp_path, steps, **kwargs) -> GateRepo:
    gate = {"required_reviewer_roles": [], "pre_checks": steps}
    return init_gate_repo(tmp_path / "shared", tracked_gate=gate, **kwargs)


def _advance_base(repo: GateRepo, files: dict[str, str]) -> None:
    """Move `main` ahead of where the PR branched and make that the PR's base."""
    repo.base_sha = commit_files(repo.path, files, "base moves")


def _seed_remote_refs(repo: GateRepo, stale_main: str) -> None:
    """Remote-tracking refs as a fetched checkout has them, with `origin/main`
    behind the PR's base so the pin has something to correct."""
    git(repo.path, "update-ref", "refs/remotes/origin/main", stale_main)
    git(repo.path, "update-ref", "refs/remotes/origin/other", repo.head_sha)
    git(repo.path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


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

    def test_a_head_that_diverged_from_base_is_merged_onto_it_in_the_clone(self, tmp_path, scratch_tmp):
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
        assert list(scratch_tmp.iterdir()) == []


class TestHeadIsTheMergeResultAndTheTreeIsClean:
    """What a crew-style merge-gate check reads: a committed result, a clean tree
    and the true base under `origin/<base>`."""

    def _run(self, tmp_path, marker: Path, *, diverged: bool) -> tuple[GateRepo, dict]:
        repo = _repo(tmp_path, [_report(marker)], head_files={"README.md": "pr readme\n"})
        stale = repo.base_sha
        if diverged:
            _advance_base(repo, {"base-only.txt": "b\n"})
        _seed_remote_refs(repo, stale)
        assert _merge(repo) == verb.EXIT_OK
        return repo, json.loads(marker.read_text(encoding="utf-8"))

    def test_a_non_fast_forward_head_is_a_commit_with_two_parents_and_a_clean_tree(
        self, tmp_path, scratch_tmp
    ):
        repo, seen = self._run(tmp_path, tmp_path / "seen.json", diverged=True)
        assert seen["parents"] == [repo.base_sha, repo.head_sha]
        assert seen["head"] not in (repo.base_sha, repo.head_sha)
        assert seen["status"] == ""
        assert seen["origin_main"] == repo.base_sha
        assert seen["diff"] == ["README.md"]

    def test_the_merge_commit_has_the_synthetic_identity(self, tmp_path, scratch_tmp):
        _, seen = self._run(tmp_path, tmp_path / "seen.json", diverged=True)
        assert seen["subject"] == (
            "loadout pre_checks <pre-checks@loadout.invalid>|"
            "loadout pre_checks <pre-checks@loadout.invalid>"
        )

    def test_a_fast_forward_head_is_checked_out_as_it_is(self, tmp_path, scratch_tmp):
        repo, seen = self._run(tmp_path, tmp_path / "seen.json", diverged=False)
        assert seen["head"] == repo.head_sha
        assert seen["status"] == ""
        assert seen["origin_main"] == repo.base_sha
        assert seen["diff"] == ["README.md"]

    @pytest.mark.parametrize("diverged", [False, True], ids=["fast-forward", "merge"])
    def test_the_shared_remote_tracking_refs_are_copied_and_the_base_branch_is_pinned(
        self, tmp_path, scratch_tmp, diverged
    ):
        repo, seen = self._run(tmp_path, tmp_path / "seen.json", diverged=diverged)
        assert seen["origin_other"] == repo.head_sha
        assert seen["origin_head"] == repo.base_sha

    def test_the_clone_is_a_different_directory_with_its_own_git_dir(self, tmp_path, scratch_tmp):
        repo, seen = self._run(tmp_path, tmp_path / "seen.json", diverged=True)
        assert Path(seen["cwd"]).parent.parent == scratch_tmp
        assert Path(seen["cwd"]) != repo.path


class TestAGitSelectorInTheParentEnvironmentIsIgnored:
    @pytest.mark.parametrize("diverged", [False, True], ids=["fast-forward", "merge"])
    def test_git_dir_and_work_tree_do_not_redirect_the_check_or_the_clone(
        self, tmp_path, scratch_tmp, monkeypatch, diverged
    ):
        marker = tmp_path / "seen.json"
        repo = _repo(tmp_path, [_report(marker)])
        if diverged:
            _advance_base(repo, {"base-only.txt": "b\n"})
        monkeypatch.setenv("GIT_DIR", str(repo.path / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(repo.path))
        shared_head = git(repo.path, "rev-parse", "HEAD")
        assert _merge(repo) == verb.EXIT_OK
        seen = json.loads(marker.read_text(encoding="utf-8"))
        assert seen["head"] != shared_head
        if diverged:
            assert seen["parents"] == [repo.base_sha, repo.head_sha]
        else:
            assert seen["head"] == repo.head_sha
        assert git(repo.path, "rev-parse", "HEAD") == shared_head
        assert git(repo.path, "status", "--porcelain") == ""


class TestACheckThatWritesItsOwnRepositoryLeavesTheSharedOneUntouched:
    def test_hooks_config_refs_and_commits_in_the_clone_do_not_reach_the_shared_git_dir(
        self, tmp_path, scratch_tmp
    ):
        repo = _repo(tmp_path, [_check(_TAMPER)])
        before = _snapshot(repo.path / ".git")
        assert _merge(repo) == verb.EXIT_OK
        assert _snapshot(repo.path / ".git") == before


class TestTheCloneIsAlwaysRemoved:
    def _assert_gone(self, repo, marker: Path, scratch_tmp: Path) -> None:
        ran_in = Path(marker.read_text(encoding="utf-8"))
        assert scratch_tmp in ran_in.parents, "the clone must live under the per-process TMPDIR"
        assert repo.path not in ran_in.parents and ran_in != repo.path
        assert not ran_in.exists()
        assert not ran_in.parent.exists(), "the scratch directory is removed with it"
        assert list(scratch_tmp.iterdir()) == []

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

    def test_even_when_a_check_left_an_unreadable_directory(self, tmp_path, scratch_tmp):
        marker = tmp_path / "ran-in"
        locked = (
            "import os; os.makedirs('locked/inner'); "
            "open('locked/inner/f', 'w').write('x'); os.chmod('locked', 0)"
        )
        repo = _repo(tmp_path, [_record_cwd(marker, then=locked)])
        assert _merge(repo) == verb.EXIT_OK
        self._assert_gone(repo, marker, scratch_tmp)


class TestTheCloneHelper:
    def test_it_yields_the_head_checkout_for_a_fast_forward(self, tmp_path, scratch_tmp):
        repo = _repo(tmp_path, [])
        with merge_result_clone(repo.path, repo.base_sha, repo.head_sha) as tree:
            assert git(tree, "rev-parse", "HEAD") == repo.head_sha
            assert (tree / "change.txt").exists()
        assert not tree.exists()

    def test_a_conflict_raises_and_still_cleans_up(self, tmp_path, scratch_tmp):
        repo = _repo(tmp_path, [], head_files={"README.md": "pr\n"})
        _advance_base(repo, {"README.md": "base moves\n"})
        with pytest.raises(MergeResultConflictError):
            with merge_result_clone(repo.path, repo.base_sha, repo.head_sha):
                pytest.fail("no tree may be yielded for a conflicting head")
        assert list(scratch_tmp.iterdir()) == []

    def test_the_clone_borrows_objects_and_never_hardlinks_them(self, tmp_path, scratch_tmp):
        repo = _repo(tmp_path, [])
        with merge_result_clone(repo.path, repo.base_sha, repo.head_sha) as tree:
            alternates = (tree / ".git" / "objects" / "info" / "alternates").read_text(encoding="utf-8")
            assert Path(alternates.strip()) == (repo.path / ".git" / "objects").resolve()
            shared_inodes = {p.stat().st_ino for p in (repo.path / ".git" / "objects").rglob("*") if p.is_file()}
            clone_inodes = {p.stat().st_ino for p in (tree / ".git" / "objects").rglob("*") if p.is_file()}
            assert shared_inodes.isdisjoint(clone_inodes)

    def test_it_ignores_a_git_dir_in_the_environment_handed_to_it(self, tmp_path, scratch_tmp):
        repo = _repo(tmp_path, [])
        env = {**os.environ, "GIT_DIR": str(repo.path / ".git"), "GIT_WORK_TREE": str(repo.path)}
        with merge_result_clone(repo.path, repo.base_sha, repo.head_sha, env=env) as tree:
            assert git(tree, "rev-parse", "HEAD") == repo.head_sha
        assert git(repo.path, "rev-parse", "--abbrev-ref", "HEAD") == "main"
