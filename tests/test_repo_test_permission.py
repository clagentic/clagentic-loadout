"""The one optional allow rule for `test report`, and the installer step that
registers it."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from _install_support import CHECKOUT
from test_install_script import _run as run_install_sh  # noqa: F401  (shared installer runner)
from test_install_script import shared_venv_data_dir  # noqa: F401  (fixture)

from clagentic_loadout.repo_test import exits
from clagentic_loadout.repo_test.permission import (
    REPORT_RULE,
    SettingsError,
    default_settings_path,
    register_report_permission,
)
from clagentic_loadout.repo_test.permission import main as helper_main
from clagentic_loadout.repo_test.verb import main


def test_the_rule_is_exactly_the_approved_one() -> None:
    assert REPORT_RULE == "Bash(clagentic-loadout test report *)"


def test_a_missing_settings_file_is_created_with_only_that_rule(tmp_path: Path) -> None:
    path = tmp_path / "claude" / "settings.json"

    assert register_report_permission(path) == "added"

    assert json.loads(path.read_text()) == {"permissions": {"allow": [REPORT_RULE]}}


def test_existing_settings_are_preserved_and_only_the_rule_is_appended(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    original = {
        "model": "some-model",
        "permissions": {"allow": ["Bash(ls *)"], "deny": ["Bash(rm *)"], "defaultMode": "plan"},
        "hooks": {"PreToolUse": [{"matcher": "Bash"}]},
    }
    path.write_text(json.dumps(original))
    path.chmod(0o640)

    assert register_report_permission(path) == "added"

    after = json.loads(path.read_text())
    expected = json.loads(json.dumps(original))
    expected["permissions"]["allow"].append(REPORT_RULE)
    assert after == expected
    assert (path.stat().st_mode & 0o777) == 0o640


def test_registering_twice_changes_nothing_the_second_time(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    register_report_permission(path)
    first = path.read_bytes()

    assert register_report_permission(path) == "already-present"

    assert path.read_bytes() == first


def test_dry_run_reports_and_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"

    assert register_report_permission(path, dry_run=True) == "would-add"

    assert not path.exists()


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", '{"permissions": []}', '{"permissions": {"allow": "Bash(ls *)"}}'],
)
def test_a_file_that_cannot_be_updated_safely_is_refused_and_left_alone(tmp_path: Path, content: str) -> None:
    path = tmp_path / "settings.json"
    path.write_text(content)

    with pytest.raises(SettingsError, match="settings.json"):
        register_report_permission(path)

    assert path.read_text() == content


def test_a_symlinked_settings_file_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text("{}")
    link = tmp_path / "settings.json"
    link.symlink_to(real)

    with pytest.raises(SettingsError, match="symlink"):
        register_report_permission(link)

    assert real.read_text() == "{}"


def test_the_default_path_follows_the_override_then_the_config_dir_then_home(tmp_path: Path) -> None:
    assert default_settings_path({"CLAGENTIC_LOADOUT_CLAUDE_SETTINGS": str(tmp_path / "x.json")}) == tmp_path / "x.json"
    assert default_settings_path({"CLAUDE_CONFIG_DIR": str(tmp_path)}) == tmp_path / "settings.json"
    assert default_settings_path({}) == Path.home() / ".claude" / "settings.json"


def test_the_installer_helper_registers_and_reports_the_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "settings.json"

    code = helper_main(["--settings-file", str(path)])

    assert code == exits.EXIT_OK
    assert json.loads(capsys.readouterr().out) == {
        "result": "added",
        "rule": REPORT_RULE,
        "settings_file": str(path),
    }


def test_the_installer_helper_exits_with_its_own_code_on_an_unusable_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{")

    assert helper_main(["--settings-file", str(path)]) == exits.EXIT_SETTINGS
    assert "settings.json" in capsys.readouterr().err


def test_register_permission_is_not_a_subcommand(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "settings.json"

    code = main(["register-permission", "--settings-file", str(path)])

    assert code == exits.EXIT_USAGE
    assert not path.exists()
    assert "register-permission" in capsys.readouterr().err


def test_the_help_lists_only_run_and_report(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == exits.EXIT_OK

    help_text = capsys.readouterr().out
    assert "register-permission" not in help_text
    assert REPORT_RULE not in help_text
    assert "  run " in help_text
    assert "  report " in help_text


def _install(tmp_path: Path, data_dir: Path, *extra: str) -> tuple[int, Path]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {**os.environ, "HOME": str(home)}
    for var in ("CLAGENTIC_LOADOUT_CLAUDE_SETTINGS", "CLAUDE_CONFIG_DIR"):
        env.pop(var, None)
    result = run_install_sh(
        "--installer", "venv",
        "--source", str(CHECKOUT),
        "--data-dir", str(data_dir),
        "--bin-dir", str(tmp_path / "bin"),
        "--skills-dir", str(tmp_path / "skills"),
        "--no-seed-config",
        *extra,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.returncode, home


def test_installer_does_not_touch_permissions_by_default(tmp_path: Path, shared_venv_data_dir: Path) -> None:  # noqa: F811
    _, home = _install(tmp_path, shared_venv_data_dir)

    assert not (home / ".claude").exists()


def test_installer_registers_the_rule_only_when_asked(tmp_path: Path, shared_venv_data_dir: Path) -> None:  # noqa: F811
    settings = tmp_path / "chosen" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls *)"]}}))

    _install(tmp_path, shared_venv_data_dir, "--register-test-report-permission", "--claude-settings-file", str(settings))

    assert json.loads(settings.read_text()) == {"permissions": {"allow": ["Bash(ls *)", REPORT_RULE]}}
    assert not (tmp_path / "home" / ".claude").exists()


def test_installer_registration_is_idempotent(tmp_path: Path, shared_venv_data_dir: Path) -> None:  # noqa: F811
    settings = tmp_path / "settings.json"
    flags = ("--register-test-report-permission", "--claude-settings-file", str(settings))

    _install(tmp_path, shared_venv_data_dir, *flags)
    first = settings.read_bytes()
    _install(tmp_path, shared_venv_data_dir, *flags)

    assert settings.read_bytes() == first
    assert json.loads(first)["permissions"]["allow"] == [REPORT_RULE]


def _interpreter_of(script: Path) -> str:
    """Run the installer's own interpreter-discovery function, extracted verbatim."""
    source = (CHECKOUT / "scripts" / "install.sh").read_text().splitlines()
    start = source.index("_console_script_interpreter() {")
    end = source.index("}", start)
    body = "\n".join(source[start : end + 1])
    result = subprocess.run(
        ["sh", "-c", f'{body}\n_console_script_interpreter "$1"', "sh", str(script)],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.rstrip("\n")


def _write_script(path: Path, *lines: str) -> Path:
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.mark.parametrize(
    "python",
    [
        "/opt/tools/venv/bin/python",
        "/home/u/.local/share/pipx/venvs/clagentic-loadout/bin/python",
        "/home/u/.local/share/uv/tools/clagentic-loadout/bin/python",
        "/opt/my tools/venv/bin/python",
    ],
)
def test_a_shebang_yields_the_whole_interpreter_path(tmp_path: Path, python: str) -> None:
    script = _write_script(tmp_path / "tool", f"#!{python}", "import sys")

    assert _interpreter_of(script) == python


@pytest.mark.parametrize(
    ("exec_line", "expected"),
    [
        ("'''exec' /opt/venv/bin/python \"$0\" \"$@\"", "/opt/venv/bin/python"),
        ("'''exec' \"/opt/my tools/venv/bin/python\" \"$0\" \"$@\"", "/opt/my tools/venv/bin/python"),
        ("'''exec' '/opt/my tools/venv/bin/python' \"$0\" \"$@\"", "/opt/my tools/venv/bin/python"),
        (
            "'''exec' \"/home/u/.local/share/pipx/venvs/x y/bin/python\" \"$0\" \"$@\"",
            "/home/u/.local/share/pipx/venvs/x y/bin/python",
        ),
        (
            "'''exec' \"/home/u/.local/share/uv/tools/x y/bin/python\" \"$0\" \"$@\"",
            "/home/u/.local/share/uv/tools/x y/bin/python",
        ),
    ],
)
def test_a_pip_wrapper_yields_the_whole_interpreter_path(tmp_path: Path, exec_line: str, expected: str) -> None:
    script = _write_script(tmp_path / "tool", "#!/bin/sh", exec_line, "' '''")

    assert _interpreter_of(script) == expected


def test_a_wrapper_without_an_exec_line_yields_nothing_so_the_installer_fails_closed(tmp_path: Path) -> None:
    script = _write_script(tmp_path / "tool", "#!/bin/sh", "echo hello")

    assert _interpreter_of(script) == ""


def test_installer_registers_the_rule_when_the_install_path_contains_a_space(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "settings.json"
    spaced = tmp_path / "data dir"

    _install(tmp_path, spaced, "--register-test-report-permission", "--claude-settings-file", str(settings))

    assert json.loads(settings.read_text()) == {"permissions": {"allow": [REPORT_RULE]}}
