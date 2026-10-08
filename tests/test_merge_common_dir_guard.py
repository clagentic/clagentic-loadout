"""A pre_check that changes the shared git directory is detected, undone, and
refuses the merge; a pre_check that leaves it alone is unaffected.

Real git throughout: the checks are real python child processes running in the
real merge-result worktree, writing into the real common directory of the
shared repository."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.common_dir_guard import (
    CommonDirGuardError,
    CommonDirModifiedError,
    CommonDirRestoreError,
    guard_common_dir,
)
from tests._gate_repo import GateRepo, git, init_gate_repo, seed_base_commit
from tests._support.gate_merge import run_gate_merge

_PY = sys.executable
_HOOK_BODY = "#!/bin/sh\nexit 0\n"


def _state(common_dir: Path) -> dict[str, tuple]:
    """Content and mode of every guarded location, for byte-identical checks."""
    found: dict[str, tuple] = {}
    for name in ("config", "config.worktree"):
        path = common_dir / name
        if path.exists():
            found[name] = (path.read_bytes(), path.stat().st_mode & 0o7777)
    for tree in ("hooks", "info"):
        for current, dirs, files in os.walk(common_dir / tree):
            for child in dirs + files:
                full = Path(current) / child
                rel = full.relative_to(common_dir).as_posix()
                if full.is_symlink():
                    found[rel] = ("symlink", os.readlink(full))
                elif full.is_dir():
                    found[rel] = ("dir", full.stat().st_mode & 0o7777)
                else:
                    found[rel] = (full.read_bytes(), full.stat().st_mode & 0o7777)
    return found


def _check(code: str) -> dict:
    return {"cmd": [_PY, "-c", code], "on_failure": "fail"}


def _seeded_repo(tmp_path, steps) -> GateRepo:
    """A repo declaring *steps* whose guarded locations are non-trivial: a
    hook, an info file and an extra config entry."""
    repo = init_gate_repo(
        tmp_path / "shared", tracked_gate={"required_reviewer_roles": [], "pre_checks": steps}
    )
    common = repo.path / ".git"
    (common / "hooks").mkdir(exist_ok=True)
    hook = common / "hooks" / "pre-commit"
    hook.write_text(_HOOK_BODY, encoding="utf-8")
    hook.chmod(0o755)
    (common / "info").mkdir(exist_ok=True)
    (common / "info" / "exclude").write_text("# keep\n", encoding="utf-8")
    git(repo.path, "config", "user.note", "seeded")
    return repo


class TestAPreCheckThatChangesSharedGitStateRefuses:
    def _refused(self, tmp_path, scratch_tmp, capsys, code, expected_path) -> Path:
        repo = _seeded_repo(tmp_path, [_check(code)])
        common = repo.path / ".git"
        before = _state(common)
        calls: list = []
        assert run_gate_merge(repo, calls) == verb.EXIT_PRE_CHECKS_FAILED
        assert calls == [], "the merge call must not be reached"
        assert _state(common) == before
        assert expected_path in capsys.readouterr().err
        return common

    def test_a_planted_hook_is_removed_and_the_merge_refused(self, tmp_path, scratch_tmp, capsys):
        hook = tmp_path / "shared" / ".git" / "hooks" / "post-merge"
        code = f"import os; p = {str(hook)!r}; open(p, 'w').write('#!/bin/sh\\ntouch /x\\n'); os.chmod(p, 0o755)"
        common = self._refused(tmp_path, scratch_tmp, capsys, code, "hooks/post-merge")
        assert not (common / "hooks" / "post-merge").exists()

    def test_core_hookspath_is_removed_from_the_shared_config(self, tmp_path, scratch_tmp, capsys):
        code = "import subprocess; subprocess.run(['git', 'config', 'core.hooksPath', '/evil'], check=True)"
        common = self._refused(tmp_path, scratch_tmp, capsys, code, "config")
        assert "hooksPath" not in (common / "config").read_text(encoding="utf-8")

    def test_an_info_attributes_file_is_removed(self, tmp_path, scratch_tmp, capsys):
        attributes = tmp_path / "shared" / ".git" / "info" / "attributes"
        code = f"open({str(attributes)!r}, 'w').write('* filter=x\\n')"
        self._refused(tmp_path, scratch_tmp, capsys, code, "info/attributes")
        assert not attributes.exists()

    def test_an_edited_existing_hook_and_its_mode_are_restored(self, tmp_path, scratch_tmp, capsys):
        hook = tmp_path / "shared" / ".git" / "hooks" / "pre-commit"
        code = f"import os; p = {str(hook)!r}; open(p, 'w').write('evil'); os.chmod(p, 0o600)"
        self._refused(tmp_path, scratch_tmp, capsys, code, "hooks/pre-commit")
        assert hook.read_text(encoding="utf-8") == _HOOK_BODY
        assert hook.stat().st_mode & 0o777 == 0o755

    def test_a_deleted_hook_comes_back(self, tmp_path, scratch_tmp, capsys):
        hook = tmp_path / "shared" / ".git" / "hooks" / "pre-commit"
        self._refused(tmp_path, scratch_tmp, capsys, f"import os; os.remove({str(hook)!r})", "hooks/pre-commit")
        assert hook.exists()

    def test_a_planted_hook_in_a_directory_made_read_only_is_restored_and_refused(
        self, tmp_path, scratch_tmp, capsys
    ):
        hooks = tmp_path / "shared" / ".git" / "hooks"
        code = (
            "import os; "
            f"p = {str(hooks / 'post-merge')!r}; "
            "open(p, 'w').write('#!/bin/sh\\ntouch /x\\n'); os.chmod(p, 0o755); "
            f"os.chmod({str(hooks)!r}, 0o555)"
        )
        repo = _seeded_repo(tmp_path, [_check(code)])
        common = repo.path / ".git"
        before = _state(common)
        mode_before = hooks.stat().st_mode & 0o7777
        calls: list = []
        assert run_gate_merge(repo, calls) == verb.EXIT_PRE_CHECKS_FAILED
        assert calls == []
        assert "hooks/post-merge" in capsys.readouterr().err
        assert not (hooks / "post-merge").exists()
        assert _state(common) == before
        assert hooks.stat().st_mode & 0o7777 == mode_before

    def test_a_read_only_directory_holding_a_planted_subtree_is_restored(
        self, tmp_path, scratch_tmp, capsys
    ):
        hooks = tmp_path / "shared" / ".git" / "hooks"
        sub = hooks / "nested"
        code = (
            "import os; "
            f"os.makedirs({str(sub)!r}); open({str(sub / 'h')!r}, 'w').write('x'); "
            f"os.chmod({str(sub)!r}, 0o500); os.chmod({str(hooks)!r}, 0o555)"
        )
        repo = _seeded_repo(tmp_path, [_check(code)])
        before = _state(repo.path / ".git")
        assert run_gate_merge(repo) == verb.EXIT_PRE_CHECKS_FAILED
        assert not sub.exists()
        assert _state(repo.path / ".git") == before

    def test_a_failing_check_that_also_changed_state_is_restored_and_refused(
        self, tmp_path, scratch_tmp, capsys
    ):
        attributes = tmp_path / "shared" / ".git" / "info" / "attributes"
        code = f"import sys; open({str(attributes)!r}, 'w').write('x'); sys.exit(3)"
        self._refused(tmp_path, scratch_tmp, capsys, code, "info/attributes")
        assert not attributes.exists()


class TestACheckThatTouchesNothingPasses:
    def test_the_merge_proceeds_and_the_state_is_unchanged(self, tmp_path, scratch_tmp):
        repo = _seeded_repo(tmp_path, [_check("import sys; sys.exit(0)")])
        before = _state(repo.path / ".git")
        assert run_gate_merge(repo) == verb.EXIT_OK
        assert _state(repo.path / ".git") == before


class TestTheGuardDirectly:
    @pytest.fixture
    def tree(self, tmp_path) -> Path:
        path = tmp_path / "repo"
        seed_base_commit(path)
        (path / ".git" / "hooks").mkdir(exist_ok=True)
        (path / ".git" / "hooks" / "keep").write_text("k", encoding="utf-8")
        return path

    def test_no_change_passes_silently(self, tree):
        with guard_common_dir(tree):
            pass

    def test_a_nested_added_directory_is_removed(self, tree):
        before = _state(tree / ".git")
        with pytest.raises(CommonDirModifiedError) as raised:
            with guard_common_dir(tree):
                nested = tree / ".git" / "hooks" / "sub" / "deeper"
                nested.mkdir(parents=True)
                (nested / "h").write_text("x", encoding="utf-8")
        assert "hooks/sub" in str(raised.value)
        assert _state(tree / ".git") == before

    def test_an_added_symlink_is_removed_without_following_it(self, tree, tmp_path):
        target = tmp_path / "outside"
        target.write_text("precious", encoding="utf-8")
        with pytest.raises(CommonDirModifiedError):
            with guard_common_dir(tree):
                os.symlink(target, tree / ".git" / "hooks" / "link")
        assert not os.path.lexists(tree / ".git" / "hooks" / "link")
        assert target.read_text(encoding="utf-8") == "precious"

    def test_a_replaced_config_is_restored(self, tree):
        config = tree / ".git" / "config"
        original = config.read_bytes()
        with pytest.raises(CommonDirModifiedError):
            with guard_common_dir(tree):
                config.write_text("[core]\n\tfsmonitor = /evil\n", encoding="utf-8")
        assert config.read_bytes() == original

    def test_an_added_config_worktree_is_removed(self, tree):
        extra = tree / ".git" / "config.worktree"
        with pytest.raises(CommonDirModifiedError):
            with guard_common_dir(tree):
                extra.write_text("[core]\n\thooksPath = /evil\n", encoding="utf-8")
        assert not extra.exists()

    def test_a_body_exception_is_re_raised_unchanged_when_nothing_was_touched(self, tree):
        with pytest.raises(ZeroDivisionError):
            with guard_common_dir(tree):
                1 / 0

    def test_a_body_exception_with_a_change_is_chained_and_named(self, tree):
        with pytest.raises(CommonDirModifiedError) as raised:
            with guard_common_dir(tree):
                (tree / ".git" / "hooks" / "new").write_text("x", encoding="utf-8")
                raise RuntimeError("check blew up")
        assert isinstance(raised.value.__cause__, RuntimeError)
        assert "check blew up" in str(raised.value)
        assert not (tree / ".git" / "hooks" / "new").exists()

    def test_a_read_only_hooks_directory_with_a_planted_file_is_restored_to_its_mode(self, tree):
        hooks = tree / ".git" / "hooks"
        hooks.chmod(0o755)
        before = _state(tree / ".git")
        with pytest.raises(CommonDirModifiedError):
            with guard_common_dir(tree):
                (hooks / "planted").write_text("x", encoding="utf-8")
                hooks.chmod(0o555)
        assert _state(tree / ".git") == before
        assert hooks.stat().st_mode & 0o7777 == 0o755

    def test_an_unrestorable_path_is_named_loudly(self, tree, monkeypatch):
        planted = tree / ".git" / "hooks" / "planted"

        def stuck(path):
            raise PermissionError(13, "cannot remove", str(path))

        monkeypatch.setattr("clagentic_loadout.merge.common_dir_guard._remove", stuck)
        with pytest.raises(CommonDirRestoreError) as raised:
            with guard_common_dir(tree):
                planted.write_text("x", encoding="utf-8")
        assert "hooks/planted" in str(raised.value)
        assert "hooks/planted" in raised.value.failures
        assert isinstance(raised.value, CommonDirGuardError)

    def test_a_path_that_is_not_a_git_tree_is_a_guard_error(self, tmp_path):
        with pytest.raises(CommonDirGuardError):
            with guard_common_dir(tmp_path / "missing"):
                pass
