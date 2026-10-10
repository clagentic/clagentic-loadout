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
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from clagentic_loadout.doctor.checks import check_pre_checks_sandbox
from clagentic_loadout.merge import verb
from clagentic_loadout.merge.post_merge import run_post_merge_steps
from clagentic_loadout.merge.pre_checks_sandbox import (
    CONFIG_KEY_PRE_CHECKS_SANDBOX,
    PreChecksSandboxConfigError,
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
rest = args[args.index("--") + 1:]
os.execvp(rest[0], rest)
"""


def _write_launcher(directory: Path) -> tuple[Path, Path]:
    log = directory / "launcher.log"
    launcher = directory / "sandbox-launcher"
    launcher.write_text(_LAUNCHER.format(python=_PY, log=str(log)), encoding="utf-8")
    launcher.chmod(0o755)
    return launcher, log


def _logged(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


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


class TestWrappingEachCheckArgv:
    def test_list_form_step_is_wrapped_with_substituted_placeholders(self, tmp_path, launcher):
        launcher_path, log = launcher
        clone = tmp_path / "clone"
        clone.mkdir()
        marker = tmp_path / "ran.txt"
        step = {"cmd": [_PY, "-c", f"open({str(marker)!r}, 'w').write('x')"], "on_failure": "fail"}

        run_post_merge_steps(
            [step], clone, base_env={**os.environ, "TMPDIR": "/the/check/tmp"},
            argv_prefix=_prefix(launcher_path),
        )

        assert _logged(log) == [
            ["--clone", str(clone), "--tmp", "/the/check/tmp", "--", *step["cmd"]]
        ]
        assert marker.exists()

    def test_string_form_step_is_wrapped(self, tmp_path, launcher):
        launcher_path, log = launcher
        clone = tmp_path / "clone"
        clone.mkdir()
        run_post_merge_steps(
            [{"cmd": f"{_PY} -c 'import sys'", "on_failure": "fail"}],
            clone,
            base_env={**os.environ, "TMPDIR": "/t"},
            argv_prefix=_prefix(launcher_path),
        )
        assert _logged(log) == [["--clone", str(clone), "--tmp", "/t", "--", _PY, "-c", "import sys"]]

    @pytest.mark.parametrize("as_list", [True, False])
    def test_inline_env_prefix_is_split_off_and_still_reaches_the_check(self, tmp_path, launcher, as_list):
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
        assert argv[argv.index("--") + 1:] == [_PY, "-c", code]
        assert not any(part.startswith("SANDBOX_PROBE=") for part in argv)
        assert out.read_text(encoding="utf-8") == "from-step"

    def test_step_tmpdir_assignment_is_what_tmpdir_expands_to(self, tmp_path, launcher):
        launcher_path, log = launcher
        clone = tmp_path / "clone"
        clone.mkdir()
        run_post_merge_steps(
            [{"cmd": ["TMPDIR=/step/tmp", _PY, "-c", "pass"], "on_failure": "fail"}],
            clone,
            base_env={**os.environ, "TMPDIR": "/base/tmp"},
            argv_prefix=_prefix(launcher_path),
        )
        assert _logged(log)[0][:4] == ["--clone", str(clone), "--tmp", "/step/tmp"]

    def test_a_substituted_value_is_not_expanded_again(self, tmp_path, launcher):
        launcher_path, log = launcher
        clone = tmp_path / "clone"
        clone.mkdir()
        run_post_merge_steps(
            [{"cmd": [_PY, "-c", "pass"], "on_failure": "fail"}],
            clone,
            base_env={**os.environ, "TMPDIR": "/t/{clone}"},
            argv_prefix=_prefix(launcher_path),
        )
        assert _logged(log)[0][3] == "/t/{clone}"

    def test_verify_command_is_wrapped_too(self, tmp_path, launcher):
        launcher_path, log = launcher
        clone = tmp_path / "clone"
        clone.mkdir()
        run_post_merge_steps(
            [{"cmd": [_PY, "-c", "pass"], "verify": [_PY, "-c", "print('ok')"], "on_failure": "fail"}],
            clone,
            base_env={**os.environ, "TMPDIR": "/t"},
            argv_prefix=_prefix(launcher_path),
        )
        assert [entry[entry.index("--") + 1:] for entry in _logged(log)] == [
            [_PY, "-c", "pass"],
            [_PY, "-c", "print('ok')"],
        ]


class TestAbsentPrefixChangesNothing:
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
        assert argv[0] == "--clone" and argv[2:5] == ["--tmp", str(scratch_tmp), "--"]
        assert Path(argv[1]).name == "tree" and Path(argv[1]).parent.parent == scratch_tmp
        assert argv[5:] == [_PY, "-c", f"open({str(marker)!r}, 'w').write('ran')"]
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

    def test_an_invalid_value_fails(self, tmp_path):
        _write_user_config(tmp_path / "cfg", {CONFIG_KEY_PRE_CHECKS_SANDBOX: ["/nonexistent/sandbox"]})
        result = check_pre_checks_sandbox(config_root=tmp_path / "cfg")
        assert not result.ok
        assert CONFIG_KEY_PRE_CHECKS_SANDBOX in result.summary
