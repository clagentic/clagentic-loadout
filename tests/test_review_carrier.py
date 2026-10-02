"""Tests for review.carrier: outcome classification of one engine call."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from clagentic_loadout.review.carrier import (
    KIND_FAILED,
    KIND_TIMEOUT,
    KIND_UNAVAILABLE,
    run_engine,
)


def test_missing_executable_is_unavailable(tmp_path):
    result = run_engine([str(tmp_path / "absent")], "p", 5, cwd=tmp_path)

    assert result.kind == KIND_UNAVAILABLE


def test_present_but_non_executable_file_is_a_failure_not_unavailable(tmp_path):
    script = tmp_path / "engine"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(script, 0o644)

    result = run_engine([str(script)], "p", 5, cwd=tmp_path)

    assert result.kind == KIND_FAILED
    assert "cannot run" in result.detail


def test_exit_127_is_unavailable(tmp_path):
    result = run_engine([sys.executable, "-c", "raise SystemExit(127)"], "p", 5, cwd=tmp_path)

    assert result.kind == KIND_UNAVAILABLE


def test_timeout_kills_a_grandchild_that_holds_stdout_open(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "engine.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "sys.stdout.write('partial'); sys.stdout.flush()\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    started = time.monotonic()
    result = run_engine([sys.executable, str(script)], "p", 2, cwd=tmp_path)
    elapsed = time.monotonic() - started

    assert result.kind == KIND_TIMEOUT
    assert "partial" in result.stdout_excerpt
    # Without a group kill the grandchild keeps the pipe open and the call
    # blocks for its full 60s sleep.
    assert elapsed < 30
    grandchild = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _is_running(grandchild):
        time.sleep(0.05)
    assert not _is_running(grandchild)


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed child of a dead parent is reparented and reaped by init, but a
    # zombie still answers signal 0; its state file says so.
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


def test_missing_working_directory_is_a_failure_not_an_absent_engine(tmp_path):
    result = run_engine([sys.executable, "-c", "pass"], "p", 5, cwd=tmp_path / "gone")

    assert result.kind == KIND_FAILED
    assert "working directory" in result.detail
