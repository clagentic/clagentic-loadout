"""Atomic per-release venv switch in scripts/install.sh (venv tier).

Every release is installed into DATA_DIR/venvs/<id>, verified, and only then
pointed at by the DATA_DIR/venv symlink with one rename. These tests run the
real installer against temp HOMEs and temp data dirs: migration from the old
real-directory layout, atomic switching with previous-release retention,
pruning, failure isolation, concurrent execution during a reinstall, and the
default caller shape (HOME set, no flags).

Sources are copies of this checkout with no .git, so each install gets a fresh
timestamp-pid release id and therefore a genuinely new release.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

from test_install_script import (
    CHECKOUT,
    INSTALL_SH,
    _fake_externally_managed_python,
    _restricted_path_env,
)

_COPY_IGNORE = shutil.ignore_patterns(
    ".git", "tests", "docs", "__pycache__", "*.egg-info", "build", "dist", ".pytest_cache"
)


def _source_copy(tmp_path: Path, name: str = "src-copy") -> Path:
    dst = tmp_path / name
    shutil.copytree(CHECKOUT, dst, ignore=_COPY_IGNORE)
    return dst


class _Layout:
    """Data dir, bin dir, HOME and env for one isolated install target."""

    def __init__(self, tmp_path: Path) -> None:
        self.data = tmp_path / "data"
        self.bin = tmp_path / "bin"
        self.skills = tmp_path / "skills"
        self.home = tmp_path / "home"
        self.home.mkdir(exist_ok=True)
        self.env = {**os.environ, "HOME": str(self.home)}
        for var in ("PIP_CACHE_DIR", "CLAGENTIC_LOADOUT_HOME", "CLAGENTIC_LOADOUT_BIN_DIR"):
            self.env.pop(var, None)

    def install(self, source: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "/bin/sh", str(INSTALL_SH),
                "--installer", "venv",
                "--source", str(source),
                "--data-dir", str(self.data),
                "--bin-dir", str(self.bin),
                "--skills-dir", str(self.skills),
                "--no-seed-config",
            ],
            capture_output=True,
            text=True,
            env=self.env,
        )

    @property
    def live(self) -> Path:
        return self.data / "venv"

    def live_release(self) -> str:
        assert self.live.is_symlink(), f"{self.live} is not a symlink"
        target = os.readlink(self.live)
        assert target.startswith("venvs/"), target
        return target[len("venvs/"):]

    def releases(self) -> list[str]:
        venvs = self.data / "venvs"
        return sorted(p.name for p in venvs.iterdir()) if venvs.is_dir() else []

    def assert_bin_links_work(self) -> None:
        link = self.bin / "clagentic-loadout"
        assert link.is_symlink()
        assert os.readlink(link) == str(self.live / "bin" / "clagentic-loadout")
        done = subprocess.run([str(link), "--help"], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr


def test_first_install_migrates_real_directory_venv(tmp_path: Path) -> None:
    layout = _Layout(tmp_path)
    legacy = layout.data / "venv"
    subprocess.run(["python3", "-m", "venv", str(legacy)], check=True)
    (legacy / "legacy-marker").write_text("old\n")

    result = layout.install(_source_copy(tmp_path))

    assert result.returncode == 0, result.stderr
    assert "MIGRATION" in result.stderr
    new_release = layout.live_release()
    releases = layout.releases()
    assert new_release in releases
    legacy_dirs = [r for r in releases if r.startswith("legacy-")]
    assert len(legacy_dirs) == 1
    assert (layout.data / "venvs" / legacy_dirs[0] / "legacy-marker").is_file()
    layout.assert_bin_links_work()


def test_second_install_switches_atomically_keeps_previous_and_prunes(tmp_path: Path) -> None:
    layout = _Layout(tmp_path)
    legacy = layout.data / "venv"
    subprocess.run(["python3", "-m", "venv", str(legacy)], check=True)

    first = layout.install(_source_copy(tmp_path, "src-1"))
    assert first.returncode == 0, first.stderr
    release_1 = layout.live_release()

    second = layout.install(_source_copy(tmp_path, "src-2"))
    assert second.returncode == 0, second.stderr
    assert "MIGRATION" not in second.stderr
    release_2 = layout.live_release()
    assert release_2 != release_1
    # Current plus previous only: the migrated legacy directory is pruned.
    assert layout.releases() == sorted([release_1, release_2])
    layout.assert_bin_links_work()

    third = layout.install(_source_copy(tmp_path, "src-3"))
    assert third.returncode == 0, third.stderr
    release_3 = layout.live_release()
    assert layout.releases() == sorted([release_2, release_3])
    layout.assert_bin_links_work()


def test_failed_build_leaves_live_venv_untouched(tmp_path: Path) -> None:
    layout = _Layout(tmp_path)
    good = layout.install(_source_copy(tmp_path, "src-good"))
    assert good.returncode == 0, good.stderr
    live_before = layout.live_release()
    releases_before = layout.releases()

    broken = _source_copy(tmp_path, "src-broken")
    (broken / "pyproject.toml").write_text("[project\nname = \n")

    result = layout.install(broken)

    assert result.returncode == 3, result.stderr
    assert layout.live_release() == live_before
    assert layout.releases() == releases_before
    layout.assert_bin_links_work()


def test_failed_verification_leaves_live_venv_untouched(tmp_path: Path) -> None:
    layout = _Layout(tmp_path)
    good = layout.install(_source_copy(tmp_path, "src-good"))
    assert good.returncode == 0, good.stderr
    live_before = layout.live_release()
    releases_before = layout.releases()

    # Builds and installs fine, but every console script dies on import, so
    # --version exits non-zero and verification must refuse the release.
    unverifiable = _source_copy(tmp_path, "src-unverifiable")
    init = unverifiable / "src" / "clagentic_loadout" / "__init__.py"
    init.write_text(init.read_text() + "\nraise RuntimeError('broken release')\n")

    result = layout.install(unverifiable)

    assert result.returncode == 3, result.stderr
    assert "verification failed" in result.stderr
    assert layout.live_release() == live_before
    assert layout.releases() == releases_before
    layout.assert_bin_links_work()


def test_console_script_never_fails_during_a_reinstall(tmp_path: Path) -> None:
    layout = _Layout(tmp_path)
    first = layout.install(_source_copy(tmp_path, "src-1"))
    assert first.returncode == 0, first.stderr
    release_before = layout.live_release()

    stop = threading.Event()
    failures: list[str] = []
    runs: list[int] = []

    def hammer(script: str) -> None:
        while not stop.is_set():
            done = subprocess.run(
                [str(layout.bin / script), "--help"], capture_output=True, text=True
            )
            runs.append(done.returncode)
            if done.returncode != 0:
                failures.append(f"{script}: rc={done.returncode} {done.stderr[-300:]}")

    threads = [
        threading.Thread(target=hammer, args=(name,))
        for name in ("clagentic-loadout", "loadout-merge")
    ]
    for thread in threads:
        thread.start()
    try:
        second = layout.install(_source_copy(tmp_path, "src-2"))
    finally:
        stop.set()
        for thread in threads:
            thread.join()

    assert second.returncode == 0, second.stderr
    assert layout.live_release() != release_before
    assert len(runs) >= 10, "the loop barely ran; the test proves nothing"
    assert failures == []


def test_default_caller_shape_home_set_no_flags(tmp_path: Path) -> None:
    """HOME set, no flags, as the post-merge step invokes it: the venv tier is
    selected (no pipx/uv, PEP 668 interpreter) and the bin dir, data dir and
    console-script names are the documented defaults."""
    stub_python = _fake_externally_managed_python(tmp_path)
    env = _restricted_path_env(tmp_path, extra_bin_dirs=(stub_python.parent,))
    home = tmp_path / "home"
    home.mkdir()
    env["HOME"] = str(home)
    for var in ("CLAGENTIC_LOADOUT_HOME", "CLAGENTIC_LOADOUT_BIN_DIR", "PIP_CACHE_DIR"):
        env.pop(var, None)

    result = subprocess.run(
        ["/bin/sh", str(INSTALL_SH)], capture_output=True, text=True, env=env
    )

    assert result.returncode == 0, result.stderr
    data = home / ".local" / "share" / "clagentic" / "loadout"
    assert (data / "venv").is_symlink()
    assert os.readlink(data / "venv").startswith("venvs/")
    expected = {
        line.split("=")[0].strip()
        for line in (CHECKOUT / "pyproject.toml").read_text().split("[project.scripts]")[1]
        .split("[build-system]")[0].splitlines()
        if "=" in line
    }
    assert expected
    bin_dir = home / ".local" / "bin"
    for name in sorted(expected):
        link = bin_dir / name
        assert link.is_symlink(), name
        assert os.readlink(link) == str(data / "venv" / "bin" / name)
        done = subprocess.run([str(link), "--help"], capture_output=True, text=True)
        assert done.returncode == 0, f"{name}: {done.stderr}"
    assert (home / ".config" / "clagentic" / "loadout" / "config.yaml").is_file()
