"""`loadout-session launch`: the runtime argv, through the real console entry point
(`python -m clagentic_loadout session ...`) with a stub runtime on PATH and a
temporary HOME, so the config root and runtime dir resolve exactly as installed."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from clagentic_loadout.session.verb import EXIT_RUNTIME_NOT_FOUND, main

SRC = str(Path(__file__).resolve().parent.parent / "src")

SECTION = {
    "scratch_root": "/srv/scratch/{role}",
    "pin_same_host": False,
    "hosts": ["api.example.test:443"],
    "credential_paths": ["/etc/example/shared.env"],
    "mcp_servers": ["notes"],
}


def _home(tmp_path: Path, section: dict) -> Path:
    home = tmp_path / "home"
    cfg = home / ".config" / "clagentic" / "loadout"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text(yaml.safe_dump({"session_profile": section}), encoding="utf-8")
    return home


def _stub(tmp_path: Path, name: str = "stub-runtime") -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / name
    script.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$STUB_OUT"\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return bindir


def _run(tmp_path: Path, home: Path, bindir: Path | None, *argv: str):
    path = os.pathsep.join([str(bindir)] if bindir else [str(tmp_path / "empty")])
    env = {
        "HOME": str(home),
        "PATH": path,
        "PYTHONPATH": SRC,
        "STUB_OUT": str(tmp_path / "argv.txt"),
    }
    return subprocess.run(
        [sys.executable, "-m", "clagentic_loadout", "session", *argv],
        capture_output=True,
        text=True,
        env=env,
    )


def test_launch_execs_runtime_with_the_path_layer_write_prints(tmp_path):
    home = _home(tmp_path, {**SECTION, "runtime_command": "stub-runtime"})
    bindir = _stub(tmp_path)

    written = _run(tmp_path, home, bindir, "layer", "--role", "role-a", "--write")
    assert written.returncode == 0, written.stderr
    settings_path = written.stdout.strip()
    assert settings_path == str(home / ".clagentic" / "loadout" / "session" / "role-a.json")
    first = Path(settings_path).read_text(encoding="utf-8")

    launched = _run(tmp_path, home, bindir, "launch", "--role", "role-a", "--", "--resume", "x y")
    assert launched.returncode == 0, launched.stderr
    argv = (tmp_path / "argv.txt").read_text(encoding="utf-8").splitlines()
    assert argv == ["--settings", settings_path, "--resume", "x y"]
    assert Path(settings_path).read_text(encoding="utf-8") == first
    layer = json.loads(first)
    assert layer["permissions"]["additionalDirectories"] == ["/srv/scratch/role-a"]


def test_runtime_flag_overrides_config(tmp_path):
    home = _home(tmp_path, {**SECTION, "runtime_command": "not-installed"})
    bindir = _stub(tmp_path, "flag-runtime")
    result = _run(tmp_path, home, bindir, "launch", "--role", "role-a", "--runtime", "flag-runtime")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "argv.txt").read_text(encoding="utf-8").splitlines()[0] == "--settings"


@pytest.mark.parametrize(
    "section_extra,flag,command,source",
    [
        ({}, [], "claude", "the default"),
        ({"runtime_command": "cfg-runtime"}, [], "cfg-runtime", "session_profile.runtime_command"),
        ({"runtime_command": "cfg-runtime"}, ["--runtime", "flag-runtime"], "flag-runtime", "--runtime"),
    ],
    ids=["default", "config", "flag"],
)
def test_runtime_not_on_path_exits_in_range_naming_command_and_source(
    tmp_path, section_extra, flag, command, source
):
    home = _home(tmp_path, {**SECTION, **section_extra})
    result = _run(tmp_path, home, None, "launch", "--role", "role-a", *flag)
    assert result.returncode == EXIT_RUNTIME_NOT_FOUND
    assert repr(command) in result.stderr
    assert source in result.stderr
    assert not (home / ".clagentic").exists()


def test_launch_without_config_reports_and_starts_nothing(tmp_path, capsys):
    started = []
    rc = main(
        ["launch", "--role", "role-a"],
        config_root=tmp_path / "absent",
        runtime_base=tmp_path / "rt",
        exec_fn=lambda path, argv: started.append(argv),
    )
    assert rc == 0
    assert started == []
    assert "config.yaml" in capsys.readouterr().err
    assert not (tmp_path / "rt").exists()


def test_runtime_args_after_separator_are_refused_for_layer(tmp_path, capsys):
    rc = main(["layer", "--role", "role-a", "--", "x"], config_root=tmp_path)
    assert rc == 1
    assert "launch" in capsys.readouterr().err
