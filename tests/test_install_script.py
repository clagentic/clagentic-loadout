"""test_install_script.py — syntax + smoke coverage for scripts/install.sh
(lr-0b45, lr-f43b, lr-e8cc).

install.sh is POSIX `/bin/sh`, not pytest-importable Python, so its
"conformance" coverage is: (1) a syntax check via `sh -n` (catches a typo
that would otherwise only surface at install time, in whatever environment
first runs it), (2) --help/--version exit 0, (3) --dry-run resolves an
installer plan without executing an actual install (safe to run in CI /
this test suite with no network access and no environment mutation), (4)
the venv fallback tier (lr-f43b): PEP-668-with-no-pipx-no-uv selects venv,
symlink refresh is idempotent, and the no-installer-found error message
reports resolved values rather than a stale/generic guess, and (5) the
empty/unset-HOME fail-fast (lr-e8cc): install.sh must refuse to silently
resolve `${HOME:-}/.local/...` down to root-relative paths.

Most of these scenarios are orthogonal to HOME and just need SOME writable
HOME present (this suite itself may run in an agent-spawn sandbox with HOME
empty/unset by design -- exactly the environment lr-e8cc's fix targets), so
`_run` defaults to a scratch HOME when the caller doesn't supply one.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parent.parent / "scripts" / "install.sh"


def _env_with_scratch_home(tmp_path: Path) -> dict[str, str]:
    """Ambient environment plus a real, writable, scratch HOME -- for tests
    exercising behavior that is orthogonal to HOME handling itself (--help,
    --dry-run plan resolution, usage errors, ...). Isolated per test via
    pytest's tmp_path so no test depends on (or mutates) the real runner's
    actual home directory."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["HOME"] = str(home)
    return env


def _run(
    *args: str, env: dict[str, str] | None = None, tmp_path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    if env is None:
        assert tmp_path is not None, "_run needs either an explicit env or tmp_path for a scratch HOME"
        env = _env_with_scratch_home(tmp_path)
    return subprocess.run(
        ["/bin/sh", str(INSTALL_SH), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _restricted_path_env(tmp_path: Path, extra_bin_dirs: tuple[Path, ...] = ()) -> dict[str, str]:
    """A PATH containing only /usr/bin:/bin (so pipx/uv are never found even
    if installed on the host running this suite) plus any caller-supplied
    stub bin dirs, layered in front. Used to deterministically exercise the
    venv-tier selection regardless of what happens to be on the real host's
    PATH."""
    stub_dirs = ":".join(str(d) for d in extra_bin_dirs)
    path = f"{stub_dirs}:/usr/bin:/bin" if stub_dirs else "/usr/bin:/bin"
    env = dict(os.environ)
    env["PATH"] = path
    return env


def _fake_externally_managed_python(tmp_path: Path) -> Path:
    """A python3 stub whose reported stdlib dir carries an
    EXTERNALLY-MANAGED marker file, and which supports the small surface
    install.sh actually calls: `-c '<sysconfig code>'` and `-m venv DIR`
    (delegates venv creation to the real interpreter so the resulting venv
    is genuinely usable by the venv/pip calls that follow)."""
    stdlib_dir = tmp_path / "fake-stdlib"
    stdlib_dir.mkdir()
    (stdlib_dir / "EXTERNALLY-MANAGED").write_text("[externally-managed]\n")

    real_python = shutil.which("python3") or shutil.which("python")
    assert real_python, "no python3/python on this host to back the venv stub"

    stub = tmp_path / "python3"
    stub.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  -c)\n"
        f"    echo '{stdlib_dir}'\n"
        "    ;;\n"
        "  -m)\n"
        "    shift\n"
        f"    exec '{real_python}' -m \"$@\"\n"
        "    ;;\n"
        "  *)\n"
        "    echo 'unsupported stub invocation' >&2\n"
        "    exit 1\n"
        "    ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    return stub


def test_install_sh_is_syntactically_valid_posix_sh() -> None:
    result = subprocess.run(
        ["/bin/sh", "-n", str(INSTALL_SH)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_install_sh_help_exits_ok(tmp_path: Path) -> None:
    result = _run("--help", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "Usage: install.sh" in result.stdout


def test_install_sh_version_exits_ok(tmp_path: Path) -> None:
    result = _run("--version", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "install.sh" in result.stdout


def test_install_sh_unknown_flag_is_usage_error(tmp_path: Path) -> None:
    result = _run("--not-a-real-flag", tmp_path=tmp_path)
    assert result.returncode == 1
    assert "unknown argument" in result.stderr


@pytest.mark.skipif(
    not (shutil.which("pipx") or shutil.which("uv") or shutil.which("pip3") or shutil.which("pip")),
    reason="no installer (pipx/uv/pip) available in this test environment",
)
def test_install_sh_dry_run_resolves_a_plan_without_installing(tmp_path: Path) -> None:
    result = _run("--dry-run", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "installer=" in result.stderr
    assert "--dry-run -- not executing." in result.stderr


def test_install_sh_dry_run_with_explicit_installer_and_source(tmp_path: Path) -> None:
    """--installer forces the choice even when other installers are also
    present, and --source points at an arbitrary path (never hardcoded to
    this checkout) -- proving the script is runnable against a different
    sdist/wheel/checkout, not just this repo's own tree."""
    fake_source = tmp_path / "some-other-checkout"
    fake_source.mkdir()
    result = _run("--installer", "pip", "--source", str(fake_source), "--dry-run", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "installer=pip" in result.stderr
    assert str(fake_source) in result.stderr


def test_install_sh_missing_source_path_is_usage_error(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    result = _run("--installer", "pip", "--source", str(missing), "--dry-run", tmp_path=tmp_path)
    assert result.returncode == 1
    assert "does not exist" in result.stderr


def test_install_sh_pep668_no_pipx_no_uv_selects_venv_tier(tmp_path: Path) -> None:
    """The lr-f43b repro: no pipx, no uv, and the interpreter reports a PEP
    668 EXTERNALLY-MANAGED marker. install.sh must fall through past pip
    --user to the self-managed venv tier rather than attempting (and
    failing) a bare `pip install --user`."""
    stub_python = _fake_externally_managed_python(tmp_path)
    stub_bin_dir = stub_python.parent
    env = _restricted_path_env(tmp_path, extra_bin_dirs=(stub_bin_dir,))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env["HOME"] = str(fake_home)

    fake_source = tmp_path / "some-checkout"
    fake_source.mkdir()

    result = _run("--source", str(fake_source), "--dry-run", env=env)
    assert result.returncode == 0, result.stderr
    assert "installer=venv" in result.stderr
    assert "externally-managed" in result.stderr


def test_install_sh_venv_tier_symlink_refresh_is_idempotent(tmp_path: Path) -> None:
    """Two consecutive real (non-dry-run) venv-tier installs of this
    checkout into the same data dir must both succeed, and the second run
    must recognize + reuse (not fail on, not duplicate) the venv and the
    symlinks the first run created. DEFAULT_BIN_DIR for the venv tier is
    derived from $HOME (mirroring pipx/uv's own ~/.local/bin convention),
    so HOME is pointed at a scratch dir for the duration of this test."""
    checkout = Path(__file__).resolve().parent.parent
    data_dir = tmp_path / "data"
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {**os.environ, "HOME": str(fake_home)}

    def _install() -> subprocess.CompletedProcess[str]:
        return _run(
            "--installer", "venv",
            "--source", str(checkout),
            "--data-dir", str(data_dir),
            env=env,
        )

    first = _install()
    assert first.returncode == 0, first.stderr
    assert "creating venv" in first.stderr

    installed_script = fake_home / ".local" / "bin" / "clagentic-loadout"
    assert installed_script.is_symlink()
    first_target = installed_script.resolve()

    second = _install()
    assert second.returncode == 0, second.stderr
    assert "reusing existing venv" in second.stderr
    assert installed_script.is_symlink()
    assert installed_script.resolve() == first_target


_MINIMAL_COREUTILS = ("dirname", "sh", "mkdir", "readlink", "rm", "ln", "awk", "cat")


def _minimal_path_without_installers(tmp_path: Path) -> str:
    """A PATH pointing at a scratch bin dir containing ONLY symlinks to the
    small set of real coreutils install.sh's own plumbing needs (dirname for
    --source default-resolution, etc.) -- with pipx/uv/pip3/pip/python3/
    python genuinely absent (not just stubbed-to-fail), so `command -v`
    reports them missing exactly like a real host without any of them."""
    minimal_bin = tmp_path / "minimal-bin"
    minimal_bin.mkdir()
    for _name in _MINIMAL_COREUTILS:
        real = shutil.which(_name)
        if real:
            (minimal_bin / _name).symlink_to(real)
    return str(minimal_bin)


def test_install_sh_no_installer_error_reports_probed_tiers(tmp_path: Path) -> None:
    """Conformance rule 4: the failure message must report which tiers were
    probed and why each was skipped -- never a generic, stale-guess error.
    Simulated with a PATH containing only the minimal coreutils install.sh's
    own plumbing needs, with pipx/uv/pip3/pip/python3/python genuinely
    absent."""
    env = _env_with_scratch_home(tmp_path)
    env["PATH"] = _minimal_path_without_installers(tmp_path)

    fake_source = tmp_path / "some-checkout"
    fake_source.mkdir()

    result = _run("--source", str(fake_source), "--dry-run", env=env)
    assert result.returncode == 2
    assert "pipx: not found" in result.stderr
    assert "uv: not found" in result.stderr
    assert "pip3/pip: not found" in result.stderr
    assert "venv: no python3/python interpreter found" in result.stderr


def _no_local_created(scratch_root: Path) -> bool:
    """True if nothing landed under a `.local` dir anywhere below
    scratch_root -- the lr-e8cc regression is that install.sh created
    /.local/bin and /.local/share/clagentic/loadout at the filesystem root
    when HOME was empty; this helper lets tests assert on a scratch stand-in
    for "root" without ever touching the real filesystem root."""
    return not any(scratch_root.rglob(".local"))


def test_install_sh_empty_home_fails_fast_and_creates_nothing(tmp_path: Path) -> None:
    """lr-e8cc repro: `HOME=` (set but empty) with no compensating override.
    install.sh must fail fast with a message reporting the resolved (empty)
    HOME and which overrides would fix it, and must create nothing -- not
    even a scratch stand-in for the root-relative /.local dirs the bug
    produced."""
    scratch_root = tmp_path / "scratch-root"
    scratch_root.mkdir()
    env = dict(os.environ)
    env["HOME"] = ""
    env.pop("CLAGENTIC_LOADOUT_HOME", None)
    env.pop("PIPX_BIN_DIR", None)
    env.pop("UV_TOOL_BIN_DIR", None)
    # Force cwd to the scratch root so any relative-to-"/" mkdir this bug
    # would have triggered lands (and is detectable) under scratch_root
    # instead of near the real filesystem root.
    env["PWD"] = str(scratch_root)

    fake_source = tmp_path / "some-checkout"
    fake_source.mkdir()

    result = subprocess.run(
        ["/bin/sh", str(INSTALL_SH), "--source", str(fake_source), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(scratch_root),
    )
    assert result.returncode == 1, result.stderr
    assert "HOME is empty or unset" in result.stderr
    assert "resolved: HOME=''" in result.stderr
    assert "--data-dir" in result.stderr
    assert "CLAGENTIC_LOADOUT_HOME" in result.stderr
    assert _no_local_created(scratch_root)


def test_install_sh_unset_home_fails_fast_and_creates_nothing(tmp_path: Path) -> None:
    """lr-e8cc repro via `env -u HOME` (HOME entirely absent from the
    environment, not just empty) -- same fail-fast, same nothing-created
    guarantee."""
    scratch_root = tmp_path / "scratch-root"
    scratch_root.mkdir()
    env = dict(os.environ)
    env.pop("HOME", None)
    env.pop("CLAGENTIC_LOADOUT_HOME", None)
    env.pop("PIPX_BIN_DIR", None)
    env.pop("UV_TOOL_BIN_DIR", None)

    fake_source = tmp_path / "some-checkout"
    fake_source.mkdir()

    result = subprocess.run(
        ["/bin/sh", str(INSTALL_SH), "--source", str(fake_source), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(scratch_root),
    )
    assert result.returncode == 1, result.stderr
    assert "HOME is empty or unset" in result.stderr
    assert _no_local_created(scratch_root)


def test_install_sh_empty_home_with_data_dir_override_succeeds(tmp_path: Path) -> None:
    """An explicit --data-dir override is the documented escape hatch for an
    agent-spawn environment that deliberately never sets HOME (rule 2 of
    lr-e8cc): the fail-fast must NOT fire when compensating overrides are
    present, even though HOME itself stays empty."""
    env = dict(os.environ)
    env["HOME"] = ""
    env.pop("CLAGENTIC_LOADOUT_HOME", None)
    env.pop("PIPX_BIN_DIR", None)
    env.pop("UV_TOOL_BIN_DIR", None)

    fake_source = tmp_path / "some-checkout"
    fake_source.mkdir()
    data_dir = tmp_path / "explicit-data-dir"

    result = subprocess.run(
        [
            "/bin/sh", str(INSTALL_SH),
            "--installer", "pip",
            "--source", str(fake_source),
            "--data-dir", str(data_dir),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "HOME is empty or unset" not in result.stderr


def test_install_sh_set_home_behavior_is_unchanged(tmp_path: Path) -> None:
    """No behavior change when HOME is set: a normal --dry-run with a real,
    writable HOME must resolve the venv-tier DATA_DIR default from HOME as
    before and must not trip the new fail-fast."""
    env = _env_with_scratch_home(tmp_path)

    fake_source = tmp_path / "some-checkout"
    fake_source.mkdir()

    result = _run("--installer", "pip", "--source", str(fake_source), "--dry-run", env=env)
    assert result.returncode == 0, result.stderr
    assert "HOME is empty or unset" not in result.stderr


# ---------------------------------------------------------------------------
# lr-d20f: venv-tier bin-dir override + escape-hatch guard completeness.
#
# BOBBIE nit on PR #20 (lr-e8cc): the venv tier's DEFAULT_BIN_DIR fell back to
# "${HOME:-}/.local/bin" with no compensating override, unlike pipx/uv (which
# have PIPX_BIN_DIR/UV_TOOL_BIN_DIR). --data-dir alone satisfied lr-e8cc's
# fail-fast guard even though the venv tier's symlink-target dir was still
# HOME-derived and un-compensated -- the same hazard class, on a different
# dir. This section covers: (1) the new --bin-dir/CLAGENTIC_LOADOUT_BIN_DIR
# override itself, (2) the escape hatches PEACHES flagged as untested
# (PIPX_BIN_DIR, UV_TOOL_BIN_DIR), and (3) the regression this task fixes --
# `--installer venv --data-dir X` with empty HOME and no bin-dir override
# must now fail fast instead of creating /.local/bin.
# ---------------------------------------------------------------------------


def test_install_sh_pipx_bin_dir_alone_compensates_empty_home(tmp_path: Path) -> None:
    """PIPX_BIN_DIR alone, with HOME empty and --installer pipx forced,
    satisfies the guard: pipx is provably the only tier reachable this run,
    and it never consults DATA_DIR/BIN_DIR."""
    env = dict(os.environ)
    env["HOME"] = ""
    env.pop("CLAGENTIC_LOADOUT_HOME", None)
    env.pop("CLAGENTIC_LOADOUT_BIN_DIR", None)
    env.pop("UV_TOOL_BIN_DIR", None)
    env["PIPX_BIN_DIR"] = str(tmp_path / "pipx-bin")

    fake_source = tmp_path / "some-checkout"
    fake_source.mkdir()

    result = subprocess.run(
        [
            "/bin/sh", str(INSTALL_SH),
            "--installer", "pipx",
            "--source", str(fake_source),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "HOME is empty or unset" not in result.stderr
    assert "installer=pipx" in result.stderr


def test_install_sh_uv_tool_bin_dir_alone_compensates_empty_home(tmp_path: Path) -> None:
    """UV_TOOL_BIN_DIR alone, with HOME empty and --installer uv forced,
    satisfies the guard, mirroring the pipx case above."""
    env = dict(os.environ)
    env["HOME"] = ""
    env.pop("CLAGENTIC_LOADOUT_HOME", None)
    env.pop("CLAGENTIC_LOADOUT_BIN_DIR", None)
    env.pop("PIPX_BIN_DIR", None)
    env["UV_TOOL_BIN_DIR"] = str(tmp_path / "uv-bin")

    fake_source = tmp_path / "some-checkout"
    fake_source.mkdir()

    result = subprocess.run(
        [
            "/bin/sh", str(INSTALL_SH),
            "--installer", "uv",
            "--source", str(fake_source),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "HOME is empty or unset" not in result.stderr
    assert "installer=uv" in result.stderr


def test_install_sh_venv_tier_data_dir_without_bin_dir_fails_fast(tmp_path: Path) -> None:
    """lr-d20f regression: `--installer venv --data-dir X` with empty HOME
    and no bin-dir override must fail fast (the DEFAULT_BIN_DIR="${HOME:-}/
    .local/bin" symlink-target hazard BOBBIE flagged), not silently create a
    root-relative /.local/bin the way it did before this fix."""
    scratch_root = tmp_path / "scratch-root"
    scratch_root.mkdir()
    env = dict(os.environ)
    env["HOME"] = ""
    env.pop("CLAGENTIC_LOADOUT_HOME", None)
    env.pop("CLAGENTIC_LOADOUT_BIN_DIR", None)
    env.pop("PIPX_BIN_DIR", None)
    env.pop("UV_TOOL_BIN_DIR", None)

    fake_source = tmp_path / "some-checkout"
    fake_source.mkdir()
    data_dir = tmp_path / "explicit-data-dir"

    result = subprocess.run(
        [
            "/bin/sh", str(INSTALL_SH),
            "--installer", "venv",
            "--source", str(fake_source),
            "--data-dir", str(data_dir),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(scratch_root),
    )
    assert result.returncode == 1, result.stderr
    assert "symlink-target bin dir" in result.stderr
    assert "--bin-dir" in result.stderr
    assert _no_local_created(scratch_root)


def test_install_sh_auto_detect_data_dir_without_bin_dir_fails_fast(tmp_path: Path) -> None:
    """Same regression as above, but via auto-detect (no --installer given)
    on a PATH restricted so the venv tier is what auto-detect would land on
    -- the guard must fire whenever the venv tier is REACHABLE, not only when
    it's explicitly forced."""
    scratch_root = tmp_path / "scratch-root"
    scratch_root.mkdir()
    stub_python = _fake_externally_managed_python(tmp_path)
    env = _restricted_path_env(tmp_path, extra_bin_dirs=(stub_python.parent,))
    env["HOME"] = ""
    env.pop("CLAGENTIC_LOADOUT_HOME", None)
    env.pop("CLAGENTIC_LOADOUT_BIN_DIR", None)
    env.pop("PIPX_BIN_DIR", None)
    env.pop("UV_TOOL_BIN_DIR", None)

    fake_source = tmp_path / "some-checkout"
    fake_source.mkdir()
    data_dir = tmp_path / "explicit-data-dir"

    result = subprocess.run(
        [
            "/bin/sh", str(INSTALL_SH),
            "--source", str(fake_source),
            "--data-dir", str(data_dir),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(scratch_root),
    )
    assert result.returncode == 1, result.stderr
    assert "symlink-target bin dir" in result.stderr
    assert _no_local_created(scratch_root)


def test_install_sh_venv_tier_data_dir_and_bin_dir_flags_succeed(tmp_path: Path) -> None:
    """Both --data-dir and --bin-dir given, HOME empty: the venv tier's base
    dir and symlink-target dir are both compensated, so the guard must not
    fire."""
    env = dict(os.environ)
    env["HOME"] = ""
    env.pop("CLAGENTIC_LOADOUT_HOME", None)
    env.pop("CLAGENTIC_LOADOUT_BIN_DIR", None)
    env.pop("PIPX_BIN_DIR", None)
    env.pop("UV_TOOL_BIN_DIR", None)

    fake_source = tmp_path / "some-checkout"
    fake_source.mkdir()
    data_dir = tmp_path / "explicit-data-dir"
    bin_dir = tmp_path / "explicit-bin-dir"

    result = subprocess.run(
        [
            "/bin/sh", str(INSTALL_SH),
            "--installer", "venv",
            "--source", str(fake_source),
            "--data-dir", str(data_dir),
            "--bin-dir", str(bin_dir),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "HOME is empty or unset" not in result.stderr


def test_install_sh_venv_tier_data_dir_and_clagentic_bin_dir_env_succeed(tmp_path: Path) -> None:
    """Same as above but via the CLAGENTIC_LOADOUT_BIN_DIR env-var form
    instead of --bin-dir, HOME empty."""
    env = dict(os.environ)
    env["HOME"] = ""
    env.pop("PIPX_BIN_DIR", None)
    env.pop("UV_TOOL_BIN_DIR", None)

    fake_source = tmp_path / "some-checkout"
    fake_source.mkdir()
    data_dir = tmp_path / "explicit-data-dir"
    bin_dir = tmp_path / "explicit-bin-dir-env"
    env["CLAGENTIC_LOADOUT_HOME"] = str(data_dir)
    env["CLAGENTIC_LOADOUT_BIN_DIR"] = str(bin_dir)

    result = subprocess.run(
        [
            "/bin/sh", str(INSTALL_SH),
            "--installer", "venv",
            "--source", str(fake_source),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "HOME is empty or unset" not in result.stderr


def test_install_sh_bin_dir_flag_overrides_venv_symlink_target(tmp_path: Path) -> None:
    """A real (non-dry-run) venv-tier install with HOME set but --bin-dir
    forced to a different directory: console_scripts must land in the
    --bin-dir target, not the HOME-derived default, proving the override is
    actually wired into DEFAULT_BIN_DIR (not just accepted by argument
    parsing)."""
    checkout = Path(__file__).resolve().parent.parent
    data_dir = tmp_path / "data"
    bin_dir = tmp_path / "custom-bin"
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {**os.environ, "HOME": str(fake_home)}

    result = _run(
        "--installer", "venv",
        "--source", str(checkout),
        "--data-dir", str(data_dir),
        "--bin-dir", str(bin_dir),
        env=env,
    )
    assert result.returncode == 0, result.stderr

    installed_script = bin_dir / "clagentic-loadout"
    assert installed_script.is_symlink()
    default_location = fake_home / ".local" / "bin" / "clagentic-loadout"
    assert not default_location.exists()


# ---------------------------------------------------------------------------
# lr-e570 (PEACHES/BOBBIE gate follow-up): the installer must WRITE
# ~/.config/clagentic/loadout/config.yaml's git_host.base_url key -- the
# exact section/key transport.git_host_api._resolve_git_host_base's config-file
# tier reads (GIT_HOST_CONFIG_SECTION / GIT_HOST_CONFIG_KEY_BASE_URL). This
# is what makes the config-file tier actually populated end to end for a
# released install, per lr-e570 comment #3.
# ---------------------------------------------------------------------------


def _config_yaml_path(fake_home: Path) -> Path:
    return fake_home / ".config" / "clagentic" / "loadout" / "config.yaml"


def _run_venv_install(tmp_path: Path, fake_home: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    """A real (non-dry-run) install via the venv tier -- avoids depending on
    pipx/uv/an installable pip in the CI/sandbox environment (which may be
    PEP-668-externally-managed with neither pipx nor uv present), mirroring
    how the pre-existing real-install tests in this file already install."""
    checkout = Path(__file__).resolve().parent.parent
    data_dir = tmp_path / "data"
    bin_dir = tmp_path / "bin"
    env = {**os.environ, "HOME": str(fake_home)}
    return _run(
        "--installer", "venv",
        "--source", str(checkout),
        "--data-dir", str(data_dir),
        "--bin-dir", str(bin_dir),
        *extra_args,
        env=env,
    )


def test_install_sh_seeds_git_host_base_url_when_supplied(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    result = _run_venv_install(tmp_path, fake_home, "--git-host-base-url", "https://git.example.com")
    assert result.returncode == 0, result.stderr

    config_path = _config_yaml_path(fake_home)
    assert config_path.is_file()
    content = config_path.read_text()
    assert "git_host:" in content
    assert "base_url: 'https://git.example.com'" in content
    assert "seeded git_host.base_url" in result.stderr


def test_install_sh_config_file_and_dir_have_safe_perms(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    result = _run_venv_install(tmp_path, fake_home, "--git-host-base-url", "https://git.example.com")
    assert result.returncode == 0, result.stderr

    config_path = _config_yaml_path(fake_home)
    assert oct(config_path.stat().st_mode)[-3:] == "600"
    assert oct(config_path.parent.stat().st_mode)[-3:] == "700"


def test_install_sh_writes_template_when_no_url_supplied(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    result = _run_venv_install(tmp_path, fake_home)
    assert result.returncode == 0, result.stderr

    content = _config_yaml_path(fake_home).read_text()
    assert "TEMPLATE" in content
    assert "# base_url:" in content
    # Never a dead localhost or a baked operator host as an ACTIVE value.
    assert "127.0.0.1" not in content
    assert "wrote a commented git_host.base_url TEMPLATE" in result.stderr


def test_install_sh_does_not_clobber_existing_real_value(tmp_path: Path) -> None:
    """Re-running the installer with no --git-host-base-url must NOT
    overwrite a previously-seeded (or hand-edited) real value."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    first = _run_venv_install(tmp_path, fake_home, "--git-host-base-url", "https://first.example.com")
    assert first.returncode == 0, first.stderr

    second = _run_venv_install(tmp_path, fake_home)
    assert second.returncode == 0, second.stderr
    assert "already has a git_host.base_url set" in second.stderr

    content = _config_yaml_path(fake_home).read_text()
    assert "https://first.example.com" in content
    assert "https://second" not in content


def test_install_sh_explicit_flag_replaces_existing_real_value(tmp_path: Path) -> None:
    """An explicit --git-host-base-url THIS run is consent to replace a
    prior real value -- only the no-flag re-run case is protected."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    first = _run_venv_install(tmp_path, fake_home, "--git-host-base-url", "https://first.example.com")
    assert first.returncode == 0, first.stderr

    second = _run_venv_install(tmp_path, fake_home, "--git-host-base-url", "https://second.example.com")
    assert second.returncode == 0, second.stderr

    content = _config_yaml_path(fake_home).read_text()
    assert "https://second.example.com" in content
    assert "https://first.example.com" not in content


def test_install_sh_git_host_base_url_env_var_override(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    checkout = Path(__file__).resolve().parent.parent
    data_dir = tmp_path / "data"
    bin_dir = tmp_path / "bin"
    env = {
        **os.environ,
        "HOME": str(fake_home),
        "CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL": "https://env.example.com",
    }

    result = _run(
        "--installer", "venv",
        "--source", str(checkout),
        "--data-dir", str(data_dir),
        "--bin-dir", str(bin_dir),
        env=env,
    )
    assert result.returncode == 0, result.stderr

    content = _config_yaml_path(fake_home).read_text()
    assert "https://env.example.com" in content


def test_install_sh_preserves_other_config_sections(tmp_path: Path) -> None:
    """Seeding git_host: must not clobber an unrelated pre-existing
    top-level section in the same config.yaml (e.g. credentials:)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    config_dir = fake_home / ".config" / "clagentic" / "loadout"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "credentials:\n  token_provider_forgejo: command\n"
    )

    result = _run_venv_install(tmp_path, fake_home, "--git-host-base-url", "https://git.example.com")
    assert result.returncode == 0, result.stderr

    content = _config_yaml_path(fake_home).read_text()
    assert "credentials:" in content
    assert "token_provider_forgejo: command" in content
    assert "git_host:" in content
    assert "https://git.example.com" in content


def test_install_sh_git_host_base_url_yaml_single_quote_escaped(tmp_path: Path) -> None:
    """A URL value is written as a single-quoted YAML scalar with an
    embedded single quote doubled per YAML escaping rules -- never
    interpolated unescaped, and never a shell-injection vector since it is
    passed through printf/sed, never eval'd."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    tricky_value = "https://example.com/it's-a-path"
    result = _run_venv_install(tmp_path, fake_home, "--git-host-base-url", tricky_value)
    assert result.returncode == 0, result.stderr

    content = _config_yaml_path(fake_home).read_text()
    assert "it''s-a-path" in content


def test_install_sh_dry_run_does_not_seed_config(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    result = _run_venv_install(tmp_path, fake_home, "--git-host-base-url", "https://git.example.com", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert not _config_yaml_path(fake_home).exists()


def test_install_sh_refuses_symlink_at_config_target_read_side(tmp_path: Path) -> None:
    """bobbie.sast.5: a symlink at the config.yaml target path pointing at
    a REGULAR file must be refused outright, before the file is read to
    preserve existing sections -- POSIX -f/-e follow a symlink, so an
    "-e && -f" style guard alone would silently read through it. The
    symlink target's content must never appear in the (refused) run's
    output, and the symlink itself must be left untouched (never followed,
    never unlinked as part of a refusal)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    config_dir = fake_home / ".config" / "clagentic" / "loadout"
    config_dir.mkdir(parents=True)

    # The attacker-controlled target: a regular file OUTSIDE the config
    # dir, containing a distinctive marker this test asserts never leaks
    # into loadout's own config file.
    attacker_target = tmp_path / "attacker-controlled-secret.yaml"
    attacker_target.write_text("credentials:\n  token_provider_forgejo: command\n  token_command_forgejo: ATTACKER_MARKER_SENTINEL\n")

    config_path = _config_yaml_path(fake_home)
    config_path.symlink_to(attacker_target)

    result = _run_venv_install(tmp_path, fake_home, "--git-host-base-url", "https://git.example.com")
    assert result.returncode == 0, result.stderr
    assert "is a symlink" in result.stderr
    assert "refusing to read through or write through it" in result.stderr

    # The symlink is left exactly as it was -- never followed, never
    # unlinked-and-replaced as a side effect of the refusal.
    assert config_path.is_symlink()
    assert config_path.resolve() == attacker_target.resolve()
    # The attacker-controlled content was never read into loadout's own
    # write path (no seeded-config success message, no attacker marker
    # anywhere in stderr).
    assert "seeded git_host.base_url" not in result.stderr
    assert "ATTACKER_MARKER_SENTINEL" not in result.stderr
    # The attacker's file itself is untouched.
    assert "ATTACKER_MARKER_SENTINEL" in attacker_target.read_text()


def test_install_sh_refuses_symlink_at_config_target_dangling(tmp_path: Path) -> None:
    """Same refusal for a DANGLING symlink (target does not exist) -- -L is
    checked independently of -e/-f, so this must refuse identically rather
    than falling through to "file does not exist, write a fresh one"."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    config_dir = fake_home / ".config" / "clagentic" / "loadout"
    config_dir.mkdir(parents=True)

    config_path = _config_yaml_path(fake_home)
    config_path.symlink_to(tmp_path / "does-not-exist.yaml")

    result = _run_venv_install(tmp_path, fake_home, "--git-host-base-url", "https://git.example.com")
    assert result.returncode == 0, result.stderr
    assert "is a symlink" in result.stderr
    assert config_path.is_symlink()
    assert not config_path.exists()  # still dangling -- never resolved/created-through


# ---------------------------------------------------------------------------
# lr-d0f3 nit (folded into lr-e570, comment #4): whitespace-only path-shaped
# values must be trimmed then treated as "not given," not passed through to
# mkdir -p / installer commands verbatim.
# ---------------------------------------------------------------------------


def test_install_sh_whitespace_only_data_dir_is_treated_as_unset(tmp_path: Path) -> None:
    scratch_root = tmp_path / "scratch-root"
    scratch_root.mkdir()
    env = dict(os.environ)
    env["HOME"] = ""
    env.pop("CLAGENTIC_LOADOUT_HOME", None)
    env.pop("PIPX_BIN_DIR", None)
    env.pop("UV_TOOL_BIN_DIR", None)

    fake_source = tmp_path / "some-checkout"
    fake_source.mkdir()

    result = subprocess.run(
        [
            "/bin/sh", str(INSTALL_SH),
            "--source", str(fake_source),
            "--data-dir", "   ",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(scratch_root),
    )
    # A whitespace-only --data-dir must behave exactly like an OMITTED
    # --data-dir: the empty-HOME fail-fast still fires (proving the value
    # was trimmed to empty before the guard's [ -z ... ] check ran), and
    # nothing whitespace-named is created anywhere.
    assert result.returncode == 1, result.stderr
    assert "HOME is empty or unset" in result.stderr
    assert _no_local_created(scratch_root)
    assert not any(p.name.strip() == "" for p in scratch_root.rglob("*"))


def test_install_sh_whitespace_only_git_host_base_url_writes_template(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    result = _run_venv_install(tmp_path, fake_home, "--git-host-base-url", "   ")
    assert result.returncode == 0, result.stderr

    content = _config_yaml_path(fake_home).read_text()
    assert "TEMPLATE" in content


# ---------------------------------------------------------------------------
# lr-c21507: global skill install (.claude/skills/loadout-init/ -> a
# HOME-derived skills dir, default ~/.claude/skills/<skill-name>/).
# ---------------------------------------------------------------------------


def test_install_sh_installs_loadout_init_skill_to_home_default(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    result = _run_venv_install(tmp_path, fake_home)
    assert result.returncode == 0, result.stderr

    skill_file = fake_home / ".claude" / "skills" / "loadout-init" / "SKILL.md"
    assert skill_file.is_file()
    assert "installed skill loadout-init" in result.stderr


def test_install_sh_skills_dir_flag_overrides_home_default(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    custom_skills_dir = tmp_path / "custom-skills"

    result = _run_venv_install(tmp_path, fake_home, "--skills-dir", str(custom_skills_dir))
    assert result.returncode == 0, result.stderr

    skill_file = custom_skills_dir / "loadout-init" / "SKILL.md"
    assert skill_file.is_file()
    default_location = fake_home / ".claude" / "skills" / "loadout-init"
    assert not default_location.exists()


def test_install_sh_skills_dir_env_var_override(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    checkout = Path(__file__).resolve().parent.parent
    data_dir = tmp_path / "data"
    bin_dir = tmp_path / "bin"
    custom_skills_dir = tmp_path / "env-skills"
    env = {
        **os.environ,
        "HOME": str(fake_home),
        "CLAGENTIC_LOADOUT_SKILLS_DIR": str(custom_skills_dir),
    }

    result = _run(
        "--installer", "venv",
        "--source", str(checkout),
        "--data-dir", str(data_dir),
        "--bin-dir", str(bin_dir),
        env=env,
    )
    assert result.returncode == 0, result.stderr

    assert (custom_skills_dir / "loadout-init" / "SKILL.md").is_file()


def test_install_sh_skill_install_is_idempotent(tmp_path: Path) -> None:
    """Re-running the installer replaces the skill's own subdirectory
    cleanly rather than erroring or duplicating content."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    first = _run_venv_install(tmp_path, fake_home)
    assert first.returncode == 0, first.stderr

    second = _run_venv_install(tmp_path, fake_home)
    assert second.returncode == 0, second.stderr

    skill_file = fake_home / ".claude" / "skills" / "loadout-init" / "SKILL.md"
    assert skill_file.is_file()


def test_install_sh_skill_install_does_not_touch_other_skills_in_same_dir(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    skills_dir = fake_home / ".claude" / "skills"
    other_skill_dir = skills_dir / "some-other-skill"
    other_skill_dir.mkdir(parents=True)
    (other_skill_dir / "SKILL.md").write_text("unrelated skill content\n")

    result = _run_venv_install(tmp_path, fake_home)
    assert result.returncode == 0, result.stderr

    assert (other_skill_dir / "SKILL.md").read_text() == "unrelated skill content\n"
    assert (skills_dir / "loadout-init" / "SKILL.md").is_file()


def test_install_sh_dry_run_does_not_install_skills(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    result = _run_venv_install(tmp_path, fake_home, "--dry-run")
    assert result.returncode == 0, result.stderr

    # --dry-run exits before the actual install command (and every step
    # after it, including skill install) ever runs.
    assert not (fake_home / ".claude" / "skills" / "loadout-init").exists()
    assert "--dry-run -- not executing." in result.stderr


def test_install_sh_skill_install_skipped_when_home_empty_and_no_override(tmp_path: Path) -> None:
    """HOME empty and no --skills-dir/CLAGENTIC_LOADOUT_SKILLS_DIR override:
    skill install is a soft skip (not a hard failure) as long as some OTHER
    override (e.g. --data-dir/--bin-dir for the venv tier itself) already
    satisfies install.sh's own empty-HOME fail-fast guard."""
    scratch_root = tmp_path / "scratch-root"
    scratch_root.mkdir()
    env = dict(os.environ)
    env["HOME"] = ""
    env.pop("CLAGENTIC_LOADOUT_HOME", None)
    env.pop("CLAGENTIC_LOADOUT_BIN_DIR", None)
    env.pop("CLAGENTIC_LOADOUT_SKILLS_DIR", None)
    env.pop("PIPX_BIN_DIR", None)
    env.pop("UV_TOOL_BIN_DIR", None)

    checkout = Path(__file__).resolve().parent.parent
    data_dir = tmp_path / "data"
    bin_dir = tmp_path / "bin"

    result = subprocess.run(
        [
            "/bin/sh", str(INSTALL_SH),
            "--installer", "venv",
            "--source", str(checkout),
            "--data-dir", str(data_dir),
            "--bin-dir", str(bin_dir),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(scratch_root),
    )
    assert result.returncode == 0, result.stderr
    assert "skipping skill install" in result.stderr
    assert _no_local_created(scratch_root)
