"""The installed release records the source commit it was built from.

Runs the real installer (venv tier) against temp data dirs and reads the result
back through `clagentic-loadout --source-commit`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from test_install_atomic_venv import _Layout
from test_install_script import INSTALL_SH

from _install_support import git_source, source_copy
from clagentic_loadout._source_commit import (
    RECORD_FILENAME,
    SourceCommit,
    get_source_commit,
    parse_record,
)
from clagentic_loadout.cli import main as umbrella_main

SHA = "0123456789abcdef0123456789abcdef01234567"


def _head(src: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=src, check=True, capture_output=True, text=True
    ).stdout.strip()


def _porcelain(src: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=src, check=True, capture_output=True, text=True,
    ).stdout


def _read_installed(layout: _Layout) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(layout.live / "bin" / "clagentic-loadout"), "--source-commit"],
        capture_output=True, text=True,
    )


def test_clean_checkout_records_full_sha_and_clean(tmp_path: Path) -> None:
    layout = _Layout(tmp_path)
    src = git_source(tmp_path, "src")
    result = layout.install(src)
    assert result.returncode == 0, result.stderr

    read = _read_installed(layout)

    assert read.returncode == 0, read.stderr
    assert read.stdout == f"commit={_head(src)}\ndirty=false\n"
    assert len(_head(src)) == 40


def test_dirty_checkout_records_sha_and_dirty_marker(tmp_path: Path) -> None:
    layout = _Layout(tmp_path)
    src = git_source(tmp_path, "src")
    (src / "scratch-untracked.txt").write_text("x\n")
    result = layout.install(src)
    assert result.returncode == 0, result.stderr

    read = _read_installed(layout)

    assert read.returncode == 0, read.stderr
    assert read.stdout == f"commit={_head(src)}\ndirty=true\n"


def test_non_git_source_reports_unknown_and_exits_zero(tmp_path: Path) -> None:
    layout = _Layout(tmp_path)
    result = layout.install(source_copy(tmp_path, "plain"))
    assert result.returncode == 0, result.stderr

    read = _read_installed(layout)

    assert read.returncode == 0, read.stderr
    assert read.stdout == "commit=unknown\ndirty=unknown\n"


def test_editable_install_records_nothing_and_reports_unknown(tmp_path: Path) -> None:
    layout = _Layout(tmp_path)
    src = git_source(tmp_path, "src")
    result = subprocess.run(
        [
            "/bin/sh", str(INSTALL_SH), "--installer", "venv", "--editable",
            "--source", str(src), "--data-dir", str(layout.data),
            "--bin-dir", str(layout.bin), "--skills-dir", str(layout.skills),
            "--no-seed-config",
        ],
        capture_output=True, text=True, env=layout.env,
    )
    assert result.returncode == 0, result.stderr

    read = _read_installed(layout)

    assert read.returncode == 0, read.stderr
    assert read.stdout == "commit=unknown\ndirty=unknown\n"
    assert not (layout.live / RECORD_FILENAME).exists()


def test_install_leaves_source_checkout_status_unchanged(tmp_path: Path) -> None:
    layout = _Layout(tmp_path)
    src = git_source(tmp_path, "src")
    before = _porcelain(src)

    result = layout.install(src)

    assert result.returncode == 0, result.stderr
    assert _porcelain(src) == before == ""


def test_reinstall_of_clean_sha_keeps_the_record(tmp_path: Path) -> None:
    layout = _Layout(tmp_path)
    src = git_source(tmp_path, "src")
    assert layout.install(src).returncode == 0
    assert layout.install(src).returncode == 0

    assert _read_installed(layout).stdout == f"commit={_head(src)}\ndirty=false\n"


def test_reader_without_record_is_unknown_and_does_not_raise(tmp_path: Path) -> None:
    assert get_source_commit(tmp_path) == SourceCommit("unknown", "unknown")


def test_reader_rejects_malformed_records(tmp_path: Path) -> None:
    unknown = SourceCommit("unknown", "unknown")
    assert parse_record("") == unknown
    assert parse_record("commit=abc123\ndirty=false\n") == unknown
    assert parse_record(f"commit={SHA.upper()}\ndirty=false\n") == unknown
    assert parse_record(f"commit={SHA}\ndirty=maybe\n") == SourceCommit(SHA, "unknown")
    assert parse_record(f"commit={SHA}\ndirty=true\n") == SourceCommit(SHA, "true")


def test_umbrella_flag_prints_record_and_exits_zero(capsys) -> None:
    assert umbrella_main(["--source-commit"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("commit=") and "\ndirty=" in out
