"""Tests for review.carrier: outcome classification of one engine call."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
from pathlib import Path

import pytest

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


needs_proc = pytest.mark.skipif(
    sys.platform != "linux", reason="reads process state from /proc"
)


@needs_proc
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

    try:
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
    finally:
        # A failed assertion must not leave a 60s sleeper behind.
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)


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


def test_a_local_setup_failure_is_not_repeated(tmp_path):
    from clagentic_loadout.review.carrier import run_engine_with_retry

    calls: list[int] = []

    def refusing_runner(argv, **kwargs):
        calls.append(1)
        raise PermissionError("not executable")

    result = run_engine_with_retry(["engine"], "p", 5, cwd=tmp_path, runner=refusing_runner)

    assert result.kind == KIND_FAILED
    assert result.deterministic
    assert len(calls) == 1


def test_a_nonzero_exit_triggers_exactly_one_retry(tmp_path):
    import subprocess

    from clagentic_loadout.review.carrier import run_engine_with_retry

    calls: list[int] = []

    def failing_runner(argv, **kwargs):
        calls.append(1)
        return subprocess.CompletedProcess(argv, 3, b"", b"boom")

    run_engine_with_retry(["engine"], "p", 5, cwd=tmp_path, runner=failing_runner)

    assert len(calls) == 2


@needs_proc
def test_the_direct_child_is_reaped_when_a_descendant_outlives_the_group(tmp_path, monkeypatch):
    from clagentic_loadout.review import carrier

    monkeypatch.setattr(carrier, "_DRAIN_SECONDS", 1)
    pid_file = tmp_path / "child.pid"
    escapee_pid_file = tmp_path / "escapee.pid"
    script = tmp_path / "engine.py"
    # The escapee starts its own session, so the group kill cannot reach it and
    # it keeps stdout open past the drain window.
    script.write_text(
        "import os, subprocess, sys, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "escapee = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
        " start_new_session=True)\n"
        f"open({str(escapee_pid_file)!r}, 'w').write(str(escapee.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    try:
        result = run_engine([sys.executable, str(script)], "p", 1, cwd=tmp_path)
        child = int(pid_file.read_text(encoding="utf-8"))

        assert result.kind == KIND_TIMEOUT
        # A reaped child has no /proc entry; an unreaped one lingers as a zombie.
        assert not Path(f"/proc/{child}").exists()
    finally:
        if escapee_pid_file.exists():
            try:
                os.kill(int(escapee_pid_file.read_text(encoding="utf-8")), 9)
            except ProcessLookupError:
                pass
