"""merge.pre_checks_sandbox -- the user-level command prefix around pre_checks.

A real launcher stands in for a sandbox: a script that records the argv it was
given and then executes the rest, so every assertion is about a real subprocess
and the argv it actually received. Only the git host's HTTP surface is a canned
double.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from clagentic_loadout.doctor.checks import check_pre_checks_sandbox
from clagentic_loadout.merge import verb
from clagentic_loadout.merge.post_merge import PostMergeStepFailedError, run_post_merge_steps
from clagentic_loadout.merge.pre_checks_sandbox import (
    CONFIG_KEY_PRE_CHECKS_SANDBOX,
    PreChecksSandboxConfigError,
    ignored_sandbox_warning,
    resolve_pre_checks_sandbox,
    validate_pre_checks_sandbox,
)
from clagentic_loadout.transport import provider_config
from tests._gate_repo import GateRepo, init_gate_repo
from tests._support.gate_merge import run_gate_merge

_PY = sys.executable
_SRC = Path(verb.__file__).resolve().parents[1]

_LAUNCHER = """#!{python}
import json, os, sys
args = sys.argv[1:]
with open({log!r}, "a") as handle:
    handle.write(json.dumps(args) + "\\n")
with open({log!r} + ".env", "a") as handle:
    handle.write(json.dumps(dict(os.environ)) + "\\n")
rest = args[args.index("--") + 1:]
os.execvp(rest[0], rest)
"""

_ENV_PROGRAM = "/usr/bin/env"


def _write_launcher(directory: Path) -> tuple[Path, Path]:
    log = directory / "launcher.log"
    launcher = directory / "sandbox-launcher"
    launcher.write_text(_LAUNCHER.format(python=_PY, log=str(log)), encoding="utf-8")
    launcher.chmod(0o755)
    return launcher, log


def _logged(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _launcher_envs(log: Path) -> list[dict[str, str]]:
    """The environment each launcher invocation actually started with."""
    env_log = Path(str(log) + ".env")
    return [json.loads(line) for line in env_log.read_text(encoding="utf-8").splitlines()]


def _inner(argv: list[str]) -> tuple[dict[str, str], list[str]]:
    """Split a logged launcher argv into the assignments delivered inside the
    sandbox and the command that follows them."""
    after = argv[argv.index("--") + 1:]
    assert after[0] == _ENV_PROGRAM, after
    assignments: dict[str, str] = {}
    index = 1
    while index < len(after) and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", after[index]):
        key, _, value = after[index].partition("=")
        assignments[key] = value
        index += 1
    return assignments, after[index:]


def _write_user_config(root: Path, merge_section: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(yaml.safe_dump({"merge": merge_section}), encoding="utf-8")


@pytest.fixture
def user_config_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "user-config-root"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", root)
    return root


@pytest.fixture
def launcher(tmp_path) -> tuple[Path, Path]:
    directory = tmp_path / "launcher-dir"
    directory.mkdir()
    return _write_launcher(directory)


def _prefix(launcher_path: Path) -> list[str]:
    return [str(launcher_path), "--clone", "{clone}", "--tmp", "{tmpdir}", "--"]


def _scratch(tmp_path: Path, name: str = "merger-tmp") -> Path:
    directory = tmp_path / name
    directory.mkdir()
    return directory


def _private_dir_under(parent: Path, argv: list[str]) -> Path:
    """The `{tmpdir}` the launcher was handed: a fresh child of *parent*."""
    private = Path(argv[argv.index("--tmp") + 1])
    assert private.parent == parent and private.name.startswith("pre-check-")
    return private


class TestWrappingEachCheckArgv:
    def test_list_form_step_is_wrapped_with_substituted_placeholders(self, tmp_path, launcher):
        launcher_path, log = launcher
        clone = tmp_path / "clone"
        clone.mkdir()
        merger_tmp = _scratch(tmp_path)
        marker = tmp_path / "ran.txt"
        step = {"cmd": [_PY, "-c", f"open({str(marker)!r}, 'w').write('x')"], "on_failure": "fail"}

        run_post_merge_steps(
            [step], clone, base_env={**os.environ, "TMPDIR": str(merger_tmp)},
            argv_prefix=_prefix(launcher_path),
        )

        [argv] = _logged(log)
        private = _private_dir_under(merger_tmp, argv)
        assert argv == [
            "--clone", str(clone), "--tmp", str(private), "--",
            _ENV_PROGRAM, f"TMPDIR={private}", *step["cmd"],
        ]
        assert marker.exists()

    def test_string_form_step_is_wrapped(self, tmp_path, launcher):
        launcher_path, log = launcher
        clone = tmp_path / "clone"
        clone.mkdir()
        merger_tmp = _scratch(tmp_path)
        run_post_merge_steps(
            [{"cmd": f"{_PY} -c 'import sys'", "on_failure": "fail"}],
            clone,
            base_env={**os.environ, "TMPDIR": str(merger_tmp)},
            argv_prefix=_prefix(launcher_path),
        )
        [argv] = _logged(log)
        _private_dir_under(merger_tmp, argv)
        assert argv[:3] == ["--clone", str(clone), "--tmp"]
        assert _inner(argv)[1] == [_PY, "-c", "import sys"]

    @pytest.mark.parametrize("as_list", [True, False])
    def test_inline_env_prefix_reaches_the_check_inside_the_sandbox_only(self, tmp_path, launcher, as_list):
        launcher_path, log = launcher
        clone = tmp_path / "clone"
        clone.mkdir()
        out = tmp_path / "seen.txt"
        code = f"import os; open({str(out)!r}, 'w').write(os.environ['SANDBOX_PROBE'])"
        cmd = ["SANDBOX_PROBE=from-step", _PY, "-c", code]
        step = {"cmd": cmd if as_list else " ".join(["SANDBOX_PROBE=from-step", _PY, "-c", repr(code)])}

        run_post_merge_steps(
            [{**step, "on_failure": "fail"}], clone, base_env=dict(os.environ),
            argv_prefix=_prefix(launcher_path),
        )

        argv = _logged(log)[0]
        assignments, command = _inner(argv)
        assert command == [_PY, "-c", code]
        assert assignments["SANDBOX_PROBE"] == "from-step"
        assert "SANDBOX_PROBE" not in _launcher_envs(log)[0]
        assert out.read_text(encoding="utf-8") == "from-step"

    @pytest.mark.parametrize("as_list", [True, False])
    def test_an_inline_tmpdir_never_chooses_the_bound_path_or_the_check_tmpdir(
        self, tmp_path, launcher, as_list
    ):
        launcher_path, log = launcher
        clone = tmp_path / "clone"
        clone.mkdir()
        merger_tmp = _scratch(tmp_path)
        elsewhere = tmp_path / "attacker-chosen"
        elsewhere.mkdir()
        seen = tmp_path / "seen-tmpdir.txt"
        code = f"import os; open({str(seen)!r}, 'w').write(os.environ['TMPDIR'])"
        inline = f"TMPDIR={elsewhere}"
        cmd = [inline, _PY, "-c", code] if as_list else f"{inline} {_PY} -c {code!r}"

        run_post_merge_steps(
            [{"cmd": cmd, "on_failure": "fail"}],
            clone,
            deployment_env_overrides={"TMPDIR": str(elsewhere)},
            base_env={**os.environ, "TMPDIR": str(merger_tmp)},
            argv_prefix=_prefix(launcher_path),
        )

        [argv] = _logged(log)
        private = _private_dir_under(merger_tmp, argv)
        assert str(elsewhere) not in argv
        assert seen.read_text(encoding="utf-8") == str(private)

    def test_the_private_directory_is_private_and_removed_after_the_check(self, tmp_path, launcher):
        launcher_path, log = launcher
        clone = tmp_path / "clone"
        clone.mkdir()
        merger_tmp = _scratch(tmp_path)
        mode_file = tmp_path / "mode.txt"
        code = (
            "import os, stat; d = os.environ['TMPDIR']; "
            "open(os.path.join(d, 'leftover'), 'w').write('x'); "
            f"open({str(mode_file)!r}, 'w').write(oct(stat.S_IMODE(os.stat(d).st_mode)))"
        )
        run_post_merge_steps(
            [{"cmd": [_PY, "-c", code], "on_failure": "fail"}] * 2,
            clone,
            base_env={**os.environ, "TMPDIR": str(merger_tmp)},
            argv_prefix=_prefix(launcher_path),
        )

        assert mode_file.read_text(encoding="utf-8") == "0o700"
        first, second = (_private_dir_under(merger_tmp, argv) for argv in _logged(log))
        assert first != second
        assert list(merger_tmp.iterdir()) == []

    def test_the_private_directory_is_removed_when_the_check_fails(self, tmp_path, launcher):
        launcher_path, _ = launcher
        clone = tmp_path / "clone"
        clone.mkdir()
        merger_tmp = _scratch(tmp_path)
        with pytest.raises(PostMergeStepFailedError):
            run_post_merge_steps(
                [{"cmd": [_PY, "-c", "raise SystemExit(3)"], "on_failure": "fail"}],
                clone,
                base_env={**os.environ, "TMPDIR": str(merger_tmp)},
                argv_prefix=_prefix(launcher_path),
            )
        assert list(merger_tmp.iterdir()) == []

    def test_an_unusable_merger_tmpdir_fails_the_step_rather_than_binding_another(
        self, tmp_path, launcher
    ):
        launcher_path, log = launcher
        clone = tmp_path / "clone"
        clone.mkdir()
        with pytest.raises(PostMergeStepFailedError):
            run_post_merge_steps(
                [{"cmd": [_PY, "-c", "pass"], "on_failure": "warn"}],
                clone,
                base_env={**os.environ, "TMPDIR": str(tmp_path / "missing")},
                argv_prefix=_prefix(launcher_path),
            )
        assert not log.exists()

    def test_a_substituted_value_is_not_expanded_again(self, tmp_path, launcher):
        launcher_path, log = launcher
        clone = tmp_path / "clone"
        clone.mkdir()
        merger_tmp = _scratch(tmp_path, "{clone}")
        run_post_merge_steps(
            [{"cmd": [_PY, "-c", "pass"], "on_failure": "fail"}],
            clone,
            base_env={**os.environ, "TMPDIR": str(merger_tmp)},
            argv_prefix=_prefix(launcher_path),
        )
        assert _private_dir_under(merger_tmp, _logged(log)[0])

    def test_verify_command_is_wrapped_too(self, tmp_path, launcher):
        launcher_path, log = launcher
        clone = tmp_path / "clone"
        clone.mkdir()
        run_post_merge_steps(
            [{"cmd": [_PY, "-c", "pass"], "verify": [_PY, "-c", "print('ok')"], "on_failure": "fail"}],
            clone,
            base_env={**os.environ, "TMPDIR": str(_scratch(tmp_path))},
            argv_prefix=_prefix(launcher_path),
        )
        assert [_inner(entry)[1] for entry in _logged(log)] == [
            [_PY, "-c", "pass"],
            [_PY, "-c", "print('ok')"],
        ]

    def test_a_verify_inline_tmpdir_cannot_repoint_the_check_tmpdir(self, tmp_path, launcher):
        launcher_path, log = launcher
        clone = tmp_path / "clone"
        clone.mkdir()
        merger_tmp = _scratch(tmp_path)
        seen = tmp_path / "verify-tmpdir.txt"
        verify = [
            "TMPDIR=/elsewhere", _PY, "-c",
            f"import os; open({str(seen)!r}, 'w').write(os.environ['TMPDIR']); print('ok')",
        ]
        run_post_merge_steps(
            [{"cmd": [_PY, "-c", "pass"], "verify": verify, "on_failure": "fail"}],
            clone,
            base_env={**os.environ, "TMPDIR": str(merger_tmp)},
            argv_prefix=_prefix(launcher_path),
        )
        verify_argv = _logged(log)[1]
        assert seen.read_text(encoding="utf-8") == str(_private_dir_under(merger_tmp, verify_argv))


class TestLauncherEnvironmentIsLoadoutControlled:
    """A repository declares a step's inline VAR=VALUE assignments, so none of
    them may reach the sandbox launcher: a loader variable would run
    repository code inside the launcher before any isolation."""

    _STEP_ASSIGNMENTS = ["LD_PRELOAD=x", "FOO=y"]

    def _run(self, tmp_path, launcher, step_extra=None, **kwargs):
        launcher_path, log = launcher
        clone = tmp_path / "clone"
        clone.mkdir()
        merger_tmp = _scratch(tmp_path)
        step = {"cmd": [*self._STEP_ASSIGNMENTS, _PY, "-c", "pass"], "on_failure": "fail", **(step_extra or {})}
        run_post_merge_steps(
            [step],
            clone,
            base_env={**os.environ, "TMPDIR": str(merger_tmp), "BASE_MARK": "base"},
            argv_prefix=_prefix(launcher_path),
            **kwargs,
        )
        return log, merger_tmp

    def test_step_assignments_reach_the_command_not_the_launcher(self, tmp_path, launcher):
        log, merger_tmp = self._run(tmp_path, launcher)

        [argv] = _logged(log)
        [launcher_env] = _launcher_envs(log)
        assert "LD_PRELOAD" not in launcher_env and "FOO" not in launcher_env
        assert launcher_env["BASE_MARK"] == "base"
        assert launcher_env["TMPDIR"] == str(_private_dir_under(merger_tmp, argv))
        assignments, command = _inner(argv)
        assert assignments["LD_PRELOAD"] == "x" and assignments["FOO"] == "y"
        assert command == [_PY, "-c", "pass"]
        assert argv.index("--") < argv.index(_ENV_PROGRAM) < argv.index("LD_PRELOAD=x")

    def test_deployment_overrides_stay_out_of_the_launcher_too(self, tmp_path, launcher):
        log, _ = self._run(tmp_path, launcher, deployment_env_overrides={"DEPLOY_ONLY": "d"})
        [launcher_env] = _launcher_envs(log)
        assert "DEPLOY_ONLY" not in launcher_env
        assert _inner(_logged(log)[0])[0]["DEPLOY_ONLY"] == "d"

    def test_verify_assignments_stay_out_of_the_launcher(self, tmp_path, launcher):
        log, _ = self._run(
            tmp_path, launcher, step_extra={"verify": ["LD_PRELOAD=v", "BAR=z", _PY, "-c", "print('ok')"]}
        )
        step_argv, verify_argv = _logged(log)
        for env in _launcher_envs(log):
            assert not {"LD_PRELOAD", "FOO", "BAR"} & set(env)
        assignments, command = _inner(verify_argv)
        assert command == [_PY, "-c", "print('ok')"]
        assert assignments["LD_PRELOAD"] == "v" and assignments["BAR"] == "z" and assignments["FOO"] == "y"
        assert _inner(step_argv)[0]["LD_PRELOAD"] == "x"

    def test_liveness_probe_and_detached_step_keep_assignments_out_of_the_launcher(
        self, tmp_path, launcher
    ):
        counter = tmp_path / "counter.txt"
        probe = [
            _PY, "-c",
            f"import os; p = {str(counter)!r}; "
            "n = int(open(p).read()) + 1 if os.path.exists(p) else 1; "
            "open(p, 'w').write(str(n)); print(n)",
        ]
        log, _ = self._run(
            tmp_path,
            launcher,
            step_extra={
                "detaches": True,
                "on_failure": "warn",
                "liveness_probe": {"cmd": probe, "poll_interval_seconds": 0.05, "max_polls": 3},
            },
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (
            Path(str(log) + ".env").exists() and len(_logged(log)) >= 3
        ):
            time.sleep(0.05)
        entries = _logged(log)
        assert len(entries) >= 3
        for env in _launcher_envs(log):
            assert not {"LD_PRELOAD", "FOO"} & set(env)
        for entry in entries:
            assert _inner(entry)[0]["LD_PRELOAD"] == "x"

    def test_a_program_name_the_env_would_misread_is_refused(self, tmp_path, launcher):
        launcher_path, log = launcher
        clone = tmp_path / "clone"
        clone.mkdir()
        with pytest.raises(PostMergeStepFailedError):
            run_post_merge_steps(
                [{"cmd": ["./odd=name"], "on_failure": "warn"}],
                clone,
                base_env={**os.environ, "TMPDIR": str(_scratch(tmp_path))},
                argv_prefix=_prefix(launcher_path),
            )
        assert not log.exists()


class TestAbsentPrefixChangesNothing:
    def test_an_inline_tmpdir_still_reaches_the_check_without_a_sandbox(self, tmp_path, monkeypatch):
        seen: list[dict] = []
        real_run = subprocess.run

        def _spy(argv, **kwargs):
            seen.append({"argv": list(argv), "env": kwargs.get("env")})
            return real_run(argv, **kwargs)

        monkeypatch.setattr(subprocess, "run", _spy)
        base_env = {**os.environ, "TMPDIR": "/merger/tmp"}
        run_post_merge_steps(
            [{"cmd": ["TMPDIR=/step/tmp", _PY, "-c", "pass"], "on_failure": "fail"}],
            tmp_path,
            base_env=base_env,
        )
        assert seen == [{"argv": [_PY, "-c", "pass"], "env": {**base_env, "TMPDIR": "/step/tmp"}}]

    def test_argv_env_and_cwd_are_exactly_what_they_were(self, tmp_path, monkeypatch):
        seen: list[dict] = []
        real_run = subprocess.run

        def _spy(argv, **kwargs):
            seen.append({"argv": list(argv), "cwd": kwargs.get("cwd"), "env": kwargs.get("env")})
            return real_run(argv, **kwargs)

        monkeypatch.setattr(subprocess, "run", _spy)
        base_env = {**os.environ, "TMPDIR": "/t"}
        run_post_merge_steps(
            [{"cmd": ["ONE=1", _PY, "-c", "pass"], "on_failure": "fail"}], tmp_path, base_env=base_env
        )
        assert seen == [
            {"argv": [_PY, "-c", "pass"], "cwd": str(tmp_path), "env": {**base_env, "ONE": "1"}}
        ]

    def test_an_empty_prefix_is_the_same_as_none(self, tmp_path, monkeypatch):
        seen: list[list[str]] = []
        real_run = subprocess.run

        def _spy(argv, **kwargs):
            seen.append(list(argv))
            return real_run(argv, **kwargs)

        monkeypatch.setattr(subprocess, "run", _spy)
        run_post_merge_steps([{"cmd": [_PY, "-c", "pass"]}], tmp_path, argv_prefix=())
        assert seen == [[_PY, "-c", "pass"]]


class TestValidation:
    def test_a_valid_argv_is_returned(self, launcher):
        launcher_path, _ = launcher
        assert validate_pre_checks_sandbox([str(launcher_path), "--x"]) == (str(launcher_path), "--x")

    @pytest.mark.parametrize(
        "value",
        [None, "/bin/true", [], [""], ["/bin/true", ""], ["/bin/true", 3], {"a": "b"}, ["true"],
         ["/nonexistent/sandbox"]],
    )
    def test_a_bad_value_is_refused(self, value):
        with pytest.raises(PreChecksSandboxConfigError) as excinfo:
            validate_pre_checks_sandbox(value)
        assert CONFIG_KEY_PRE_CHECKS_SANDBOX in str(excinfo.value)

    def test_a_non_executable_file_is_refused(self, tmp_path):
        plain = tmp_path / "plain"
        plain.write_text("x", encoding="utf-8")
        plain.chmod(0o644)
        with pytest.raises(PreChecksSandboxConfigError):
            validate_pre_checks_sandbox([str(plain)])

    def test_a_directory_is_refused(self, tmp_path):
        with pytest.raises(PreChecksSandboxConfigError):
            validate_pre_checks_sandbox([str(tmp_path)])

    def test_a_nul_in_the_launcher_is_a_controlled_refusal(self):
        with pytest.raises(PreChecksSandboxConfigError) as excinfo:
            validate_pre_checks_sandbox(["/bin/tr\x00ue"])
        assert "element 0" in str(excinfo.value) and "/bin" not in str(excinfo.value)

    def test_a_nul_in_a_later_element_is_a_controlled_refusal(self, launcher):
        launcher_path, _ = launcher
        with pytest.raises(PreChecksSandboxConfigError) as excinfo:
            validate_pre_checks_sandbox([str(launcher_path), "--ok", "--secret\x00tail"])
        assert "element 2" in str(excinfo.value) and "secret" not in str(excinfo.value)

    def test_an_unexpected_failure_while_inspecting_the_launcher_is_a_controlled_refusal(
        self, launcher, monkeypatch
    ):
        launcher_path, _ = launcher

        def _boom(_path):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(os.path, "isfile", _boom)
        with pytest.raises(PreChecksSandboxConfigError):
            validate_pre_checks_sandbox([str(launcher_path)])

    def test_nul_through_the_merge_verb_refuses_without_a_traceback(
        self, tmp_path, scratch_tmp, user_config_root, capsys
    ):
        _write_user_config(user_config_root, {CONFIG_KEY_PRE_CHECKS_SANDBOX: ["/bin/tr\x00ue"]})
        marker = tmp_path / "ran.txt"
        repo = _gate_repo(tmp_path, [_record_check(marker)])
        assert run_gate_merge(repo, []) == verb.EXIT_PRE_CHECKS_FAILED
        assert not marker.exists()
        assert "Traceback" not in capsys.readouterr().err

    def test_absent_key_resolves_to_nothing(self, user_config_root):
        assert resolve_pre_checks_sandbox() == ()
        _write_user_config(user_config_root, {"foreign_config_globs": []})
        assert resolve_pre_checks_sandbox() == ()

    def test_explicit_null_is_refused_not_read_as_absent(self, user_config_root):
        _write_user_config(user_config_root, {CONFIG_KEY_PRE_CHECKS_SANDBOX: None})
        with pytest.raises(PreChecksSandboxConfigError):
            resolve_pre_checks_sandbox()


def _gate_repo(tmp_path, steps, **kwargs) -> GateRepo:
    gate = {"required_reviewer_roles": [], "pre_checks": steps}
    return init_gate_repo(tmp_path / "shared", tracked_gate=gate, **kwargs)


def _record_check(marker: Path) -> dict:
    return {"cmd": [_PY, "-c", f"open({str(marker)!r}, 'w').write('ran')"], "on_failure": "fail"}


class TestTheMergeVerb:
    def test_the_configured_prefix_wraps_the_check_in_the_clone(
        self, tmp_path, scratch_tmp, monkeypatch, user_config_root, launcher
    ):
        launcher_path, log = launcher
        monkeypatch.setenv("TMPDIR", str(scratch_tmp))
        _write_user_config(user_config_root, {CONFIG_KEY_PRE_CHECKS_SANDBOX: _prefix(launcher_path)})
        marker = tmp_path / "ran.txt"
        repo = _gate_repo(tmp_path, [_record_check(marker)])
        calls: list[str] = []

        assert run_gate_merge(repo, calls) == verb.EXIT_OK

        [argv] = _logged(log)
        assert argv[0] == "--clone" and argv[2] == "--tmp" and argv[4] == "--"
        assert Path(argv[3]).parent == scratch_tmp and Path(argv[3]).name.startswith("pre-check-")
        assert not Path(argv[3]).exists()
        assert Path(argv[1]).name == "tree" and Path(argv[1]).parent.parent == scratch_tmp
        assert _inner(argv)[1] == [_PY, "-c", f"open({str(marker)!r}, 'w').write('ran')"]
        assert marker.read_text(encoding="utf-8") == "ran"
        assert len(calls) == 1

    def test_absent_key_runs_the_check_unwrapped(self, tmp_path, scratch_tmp, user_config_root, monkeypatch):
        seen: list[list[str]] = []
        real_run = subprocess.run

        def _spy(argv, **kwargs):
            if argv[0] == _PY:
                seen.append(list(argv))
            return real_run(argv, **kwargs)

        monkeypatch.setattr(subprocess, "run", _spy)
        marker = tmp_path / "ran.txt"
        step = _record_check(marker)
        repo = _gate_repo(tmp_path, [step])

        assert run_gate_merge(repo, []) == verb.EXIT_OK
        assert seen == [step["cmd"]]

    @pytest.mark.parametrize(
        "bad",
        ["not-a-list", [], [""], ["relative/sandbox"], ["/nonexistent/sandbox"], None],
    )
    def test_an_invalid_sandbox_refuses_the_merge_and_runs_no_check(
        self, tmp_path, scratch_tmp, user_config_root, capsys, bad
    ):
        _write_user_config(user_config_root, {CONFIG_KEY_PRE_CHECKS_SANDBOX: bad})
        marker = tmp_path / "ran.txt"
        repo = _gate_repo(tmp_path, [_record_check(marker)])
        calls: list[str] = []

        assert run_gate_merge(repo, calls) == verb.EXIT_PRE_CHECKS_FAILED

        assert not marker.exists()
        assert calls == []
        assert CONFIG_KEY_PRE_CHECKS_SANDBOX in capsys.readouterr().err

    def test_a_non_executable_sandbox_refuses_the_merge(self, tmp_path, scratch_tmp, user_config_root):
        plain = tmp_path / "plain"
        plain.write_text("x", encoding="utf-8")
        plain.chmod(0o644)
        _write_user_config(user_config_root, {CONFIG_KEY_PRE_CHECKS_SANDBOX: [str(plain)]})
        marker = tmp_path / "ran.txt"
        repo = _gate_repo(tmp_path, [_record_check(marker)])
        assert run_gate_merge(repo, []) == verb.EXIT_PRE_CHECKS_FAILED
        assert not marker.exists()

    def test_a_repo_level_key_is_ignored_and_warned(
        self, tmp_path, scratch_tmp, user_config_root, launcher, capsys
    ):
        launcher_path, log = launcher
        marker = tmp_path / "ran.txt"
        gate = {
            "required_reviewer_roles": [],
            "pre_checks": [_record_check(marker)],
            CONFIG_KEY_PRE_CHECKS_SANDBOX: _prefix(launcher_path),
        }
        repo = init_gate_repo(
            tmp_path / "shared",
            tracked_gate=gate,
            deployment_merge={CONFIG_KEY_PRE_CHECKS_SANDBOX: _prefix(launcher_path)},
        )

        assert run_gate_merge(repo, []) == verb.EXIT_OK

        assert marker.exists()
        assert not log.exists(), "a repository must not be able to set its own sandbox"
        err = capsys.readouterr().err
        assert err.count(f"merge.{CONFIG_KEY_PRE_CHECKS_SANDBOX}") >= 2
        assert "IGNORED" in err

    def test_a_repo_level_key_cannot_remove_the_user_level_sandbox(
        self, tmp_path, scratch_tmp, user_config_root, launcher
    ):
        launcher_path, log = launcher
        _write_user_config(user_config_root, {CONFIG_KEY_PRE_CHECKS_SANDBOX: _prefix(launcher_path)})
        marker = tmp_path / "ran.txt"
        gate = {
            "required_reviewer_roles": [],
            "pre_checks": [_record_check(marker)],
            CONFIG_KEY_PRE_CHECKS_SANDBOX: [],
        }
        repo = init_gate_repo(tmp_path / "shared", tracked_gate=gate)
        assert run_gate_merge(repo, []) == verb.EXIT_OK
        assert len(_logged(log)) == 1


class TestOneIgnoredWarningText:
    def test_both_repo_config_paths_emit_the_shared_text(self, tmp_path):
        from clagentic_loadout.merge.pre_checks_sandbox import ignored_repo_sandbox_warnings
        from clagentic_loadout.merge.repo_gate_runtime import gate_from_tracked_text

        declared = {"merge": {"required_reviewer_roles": [], CONFIG_KEY_PRE_CHECKS_SANDBOX: ["/x"]}}
        tracked = gate_from_tracked_text(yaml.safe_dump(declared), source="SRC")
        assert ignored_sandbox_warning("SRC") in tracked.warnings
        # An unloadable reviewer pair must not swallow the sandbox warning.
        unloadable = gate_from_tracked_text(
            yaml.safe_dump({"merge": {CONFIG_KEY_PRE_CHECKS_SANDBOX: ["/x"]}}), source="SRC"
        )
        assert ignored_sandbox_warning("SRC") in unloadable.warnings

        config = tmp_path / "repo" / ".clagentic" / "loadout" / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(yaml.safe_dump({"merge": {CONFIG_KEY_PRE_CHECKS_SANDBOX: ["/x"]}}), encoding="utf-8")
        [deployment] = ignored_repo_sandbox_warnings(tmp_path / "repo")
        assert deployment == ignored_sandbox_warning(config)

    def test_the_text_is_built_in_one_place(self):
        hits = [
            str(path.relative_to(_SRC))
            for path in sorted(_SRC.rglob("*.py"))
            if "is IGNORED -- the sandbox is" in path.read_text(encoding="utf-8")
        ]
        assert hits == ["merge/pre_checks_sandbox.py"]


class TestOnlyPreChecksAreWrapped:
    def test_the_prefix_is_passed_at_exactly_one_call_site(self):
        sites: list[str] = []
        for path in sorted(_SRC.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Call)
                    and any(kw.arg == "argv_prefix" for kw in node.keywords)
                    and getattr(node.func, "id", getattr(node.func, "attr", "")) == "run_post_merge_steps"
                ):
                    sites.append(f"{path.relative_to(_SRC)}:{node.lineno}")
        assert len(sites) == 1 and sites[0].startswith("merge/verb.py"), sites

    def test_post_merge_steps_run_without_a_prefix_by_default(self, tmp_path, monkeypatch):
        seen: list[list[str]] = []
        real_run = subprocess.run

        def _spy(argv, **kwargs):
            seen.append(list(argv))
            return real_run(argv, **kwargs)

        monkeypatch.setattr(subprocess, "run", _spy)
        run_post_merge_steps([{"cmd": [_PY, "-c", "pass"]}], tmp_path)
        assert seen == [[_PY, "-c", "pass"]]


class TestDoctor:
    def test_not_configured_is_ok_with_an_advisory(self, tmp_path):
        result = check_pre_checks_sandbox(config_root=tmp_path)
        assert result.ok
        assert "not configured" in result.summary
        assert result.resolved["configured"] is False

    def test_configured_reports_the_resolved_argv0(self, tmp_path, launcher):
        launcher_path, _ = launcher
        _write_user_config(tmp_path / "cfg", {CONFIG_KEY_PRE_CHECKS_SANDBOX: _prefix(launcher_path)})
        result = check_pre_checks_sandbox(config_root=tmp_path / "cfg")
        assert result.ok
        assert result.resolved["configured"] is True
        assert result.resolved["argv0"] == str(launcher_path)
        assert str(launcher_path) in result.summary

    _SECRET = "synthetic-secret-token-0123456789"

    def _assert_no_secret(self, result) -> None:
        assert self._SECRET not in result.summary
        assert self._SECRET not in json.dumps(result.resolved, default=str)
        assert set(result.resolved) == {"config_root", "configured", "argv0"}

    def test_a_secret_shaped_sandbox_argument_never_appears_in_the_report(
        self, tmp_path, launcher, capsys
    ):
        from clagentic_loadout.doctor.cli import main as doctor_main

        launcher_path, _ = launcher
        argv = [str(launcher_path), "--token", self._SECRET, f"--env=TOKEN={self._SECRET}"]
        _write_user_config(tmp_path / "cfg", {CONFIG_KEY_PRE_CHECKS_SANDBOX: argv})
        result = check_pre_checks_sandbox(config_root=tmp_path / "cfg")
        assert result.ok and result.resolved["configured"] is True
        self._assert_no_secret(result)
        doctor_main(["--config-root", str(tmp_path / "cfg")])
        captured = capsys.readouterr()
        assert self._SECRET not in captured.out + captured.err

    @pytest.mark.parametrize("shape", ["bad-argv0", "empty-element", "not-a-list"])
    def test_a_secret_in_an_invalid_value_never_appears_in_the_report(
        self, tmp_path, capsys, shape
    ):
        from clagentic_loadout.doctor.cli import main as doctor_main

        value = {
            "bad-argv0": ["/nonexistent/sandbox", self._SECRET],
            "empty-element": ["", self._SECRET],
            "not-a-list": self._SECRET,
        }[shape]
        _write_user_config(tmp_path / "cfg", {CONFIG_KEY_PRE_CHECKS_SANDBOX: value})
        result = check_pre_checks_sandbox(config_root=tmp_path / "cfg")
        assert not result.ok
        self._assert_no_secret(result)
        doctor_main(["--config-root", str(tmp_path / "cfg")])
        captured = capsys.readouterr()
        assert self._SECRET not in captured.out + captured.err

    def test_an_invalid_value_fails(self, tmp_path):
        _write_user_config(tmp_path / "cfg", {CONFIG_KEY_PRE_CHECKS_SANDBOX: ["/nonexistent/sandbox"]})
        result = check_pre_checks_sandbox(config_root=tmp_path / "cfg")
        assert not result.ok
        assert CONFIG_KEY_PRE_CHECKS_SANDBOX in result.summary
