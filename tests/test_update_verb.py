"""`clagentic-loadout update`: table-driven coverage over synthetic source
checkouts and temp data/bin/home dirs. Nothing here touches a real data dir,
bin dir, HOME or network-hosted source."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from _install_support import git_source
from clagentic_loadout.update import verb as update_verb
from clagentic_loadout.update.verb import (
    EXIT_INSTALL_FAILED,
    EXIT_INSTALLER_ERROR,
    EXIT_NO_SOURCE,
    EXIT_OK,
)


class _Host:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.data = tmp_path / "data"
        self.bin = tmp_path / "bin"
        self.home = tmp_path / "home"
        self.config = tmp_path / "config"
        self.home.mkdir()
        self.config.mkdir()
        for var in ("PIP_CACHE_DIR", "CLAGENTIC_LOADOUT_HOME", "CLAGENTIC_LOADOUT_BIN_DIR",
                    "CLAGENTIC_LOADOUT_SOURCE", "CLAGENTIC_LOADOUT_SKILLS_DIR"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("CLAGENTIC_LOADOUT_SKILLS_DIR", str(tmp_path / "skills"))

    def run(self, capsys, *extra: str, source: Path | None = None):
        argv = ["--data-dir", str(self.data), "--bin-dir", str(self.bin),
                "--config-root", str(self.config), *extra]
        if source is not None:
            argv += ["--source", str(source)]
        rc = update_verb.main(argv)
        cap = capsys.readouterr()
        try:
            result = json.loads(cap.out) if cap.out.strip() else None
        except json.JSONDecodeError:
            result = None
        return rc, result, cap.err

    def live(self) -> str | None:
        link = self.data / "venv"
        return os.readlink(link).removeprefix("venvs/") if link.is_symlink() else None

    def releases(self) -> list[str]:
        venvs = self.data / "venvs"
        return sorted(p.name for p in venvs.iterdir()) if venvs.is_dir() else []

    def snapshot(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.tmp))
            for root in (self.data, self.bin)
            if root.exists()
            for p in root.rglob("*")
        )


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Host:
    return _Host(tmp_path, monkeypatch)


def _commit_marker(src: Path, text: str) -> None:
    (src / "release-marker.txt").write_text(text)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(["git", "add", "release-marker.txt"], cwd=src, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "marker"], cwd=src, check=True)


def _head(src: Path) -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, check=True,
                         capture_output=True, text=True)
    return out.stdout.strip()[:12]


def test_ac1_switch_keeps_previous_release(host: _Host, tmp_path: Path, capsys) -> None:
    src_a = git_source(tmp_path, "src-a")
    rc, first, err = host.run(capsys, source=src_a)
    assert rc == EXIT_OK, err
    assert host.live() == _head(src_a)
    assert first["switched"] is True and first["verified"] is True

    src_b = git_source(tmp_path, "src-b")
    _commit_marker(src_b, "b")
    rc, result, err = host.run(capsys, source=src_b)

    assert rc == EXIT_OK, err
    assert host.live() == _head(src_b)
    assert result["live_release"] == _head(src_b)
    assert result["previous_release"] == _head(src_a)
    assert result["switched"] is True and result["verified"] is True
    assert host.releases() == sorted([_head(src_a), _head(src_b)])
    done = subprocess.run([str(host.bin / "clagentic-loadout"), "--help"], capture_output=True)
    assert done.returncode == 0


def test_ac2_failed_verification_leaves_live_release(host: _Host, tmp_path: Path, capsys) -> None:
    src_a = git_source(tmp_path, "src-a")
    assert host.run(capsys, source=src_a)[0] == EXIT_OK
    releases_before = host.releases()

    broken = git_source(tmp_path, "src-broken")
    init = broken / "src" / "clagentic_loadout" / "__init__.py"
    init.write_text(init.read_text() + "\nraise RuntimeError('broken release')\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(["git", "add", str(init)], cwd=broken, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "break"], cwd=broken, check=True)

    rc, result, err = host.run(capsys, source=broken)

    assert rc == EXIT_INSTALL_FAILED
    assert result is None
    assert "failed" in err and _head(src_a) in err
    assert host.live() == _head(src_a)
    assert host.releases() == releases_before


def test_ac3_no_source_refuses_before_any_write(host: _Host, capsys) -> None:
    rc, result, err = host.run(capsys)

    assert rc == EXIT_NO_SOURCE
    assert "--source" in err and "update.source" in err
    assert str(host.config / "config.yaml") in err
    assert host.snapshot() == []


def test_configured_source_is_used(host: _Host, tmp_path: Path, capsys) -> None:
    src = git_source(tmp_path, "src-cfg")
    (host.config / "config.yaml").write_text(f"update:\n  source: '{src}'\n")

    rc, result, err = host.run(capsys)

    assert rc == EXIT_OK, err
    assert result["source"] == str(src.resolve())
    assert host.live() == _head(src)


def test_ac4_dry_run_changes_nothing(host: _Host, tmp_path: Path, capsys) -> None:
    src_a = git_source(tmp_path, "src-a")
    assert host.run(capsys, source=src_a)[0] == EXIT_OK
    before = host.snapshot()
    src_b = git_source(tmp_path, "src-b")
    _commit_marker(src_b, "b")

    rc, result, err = host.run(capsys, "--dry-run", source=src_b)

    assert rc == EXIT_OK, err
    assert result["dry_run"] is True and result["switched"] is False
    assert result["previous_release"] == _head(src_a)
    assert result["target_release"] == _head(src_b)
    assert result["data_dir"] == str(host.data) and result["bin_dir"] == str(host.bin)
    assert host.snapshot() == before
    assert host.live() == _head(src_a)


def test_ac5_same_release_is_nothing_to_switch(host: _Host, tmp_path: Path, capsys) -> None:
    src = git_source(tmp_path, "src-a")
    assert host.run(capsys, source=src)[0] == EXIT_OK
    before = host.snapshot()

    rc, result, err = host.run(capsys, source=src)

    assert rc == EXIT_OK, err
    assert result["switched"] is False
    assert "nothing to switch" in err
    assert "creating venv" not in err and "already built" not in err
    assert host.snapshot() == before


def test_source_without_installer_is_refused(host: _Host, tmp_path: Path, capsys) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()

    rc, _result, err = host.run(capsys, source=bare)

    assert rc == EXIT_NO_SOURCE
    assert str(bare) in err
    assert host.snapshot() == []


def test_missing_source_path_reports_resolved_value(host: _Host, tmp_path: Path, capsys) -> None:
    missing = tmp_path / "nope"

    rc, _result, err = host.run(capsys, source=missing)

    assert rc == EXIT_NO_SOURCE
    assert str(missing) in err


def test_empty_home_without_compensation_is_installer_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    src = git_source(tmp_path, "src-a")
    monkeypatch.setenv("HOME", "")
    for var in ("CLAGENTIC_LOADOUT_HOME", "CLAGENTIC_LOADOUT_BIN_DIR"):
        monkeypatch.delenv(var, raising=False)

    rc = update_verb.main(["--source", str(src), "--config-root", str(tmp_path / "cfg")])

    assert rc == EXIT_INSTALLER_ERROR
    assert "HOME" in capsys.readouterr().err
