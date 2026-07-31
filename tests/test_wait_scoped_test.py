"""test_wait_scoped_test.py — unit tests for clagentic_loadout.wait.scoped_test
(ported from an internal deployment's own scoped-test-wait script's inline
logic, Wave A slice 4).

Uses `python3 -c "..."` -style stand-ins are avoided (that shape is not in
the scoped-test allowlist by design); tests instead exercise real allowed
commands (python3 -m py_compile, make) against small fixture files so the
admission boundary itself stays under test, not just the plumbing.
"""

from __future__ import annotations

import sys

import pytest

from clagentic_loadout.wait.scoped_test import (
    NotScopedTestCommandError,
    scoped_test_wait,
)


class TestScopedTestWaitAdmissionBoundary:
    def test_non_scoped_command_rejected_without_executing(self, tmp_path):
        out = tmp_path / "out.txt"
        with pytest.raises(NotScopedTestCommandError):
            scoped_test_wait("pip install evil", str(out))
        # Never executed — no output file should have been created.
        assert not out.exists()

    def test_custom_patterns_override_default_admission(self, tmp_path):
        out = tmp_path / "out.txt"
        ok_file = tmp_path / "ok.py"
        ok_file.write_text("x = 1\n")
        custom = [rf"^{sys.executable}\s+-m\s+py_compile(\s|$)"]
        # A command NOT in the default set is admitted because it matches
        # the caller-supplied custom pattern instead.
        result = scoped_test_wait(
            f"{sys.executable} -m py_compile {ok_file}",
            str(out),
            patterns=custom,
        )
        assert result.timed_out is False
        assert result.returncode == 0


class TestScopedTestWaitExecution:
    def test_successful_command_streams_output_and_returns_zero(self, tmp_path):
        out = tmp_path / "out.txt"
        ok_file = tmp_path / "ok.py"
        ok_file.write_text("x = 1\n")
        result = scoped_test_wait(
            f"python3 -m py_compile {ok_file}",
            str(out),
        )
        assert result.timed_out is False
        assert result.returncode == 0
        assert out.exists()

    def test_failing_command_propagates_nonzero_returncode(self, tmp_path):
        out = tmp_path / "out.txt"
        bad_file = tmp_path / "bad.py"
        bad_file.write_text("this is not valid python (((\n")
        result = scoped_test_wait(
            f"python3 -m py_compile {bad_file}",
            str(out),
        )
        assert result.timed_out is False
        assert result.returncode != 0
        content = out.read_text()
        assert content  # stderr from the failed compile was captured

    def test_output_file_parent_directory_is_created(self, tmp_path):
        out = tmp_path / "nested" / "dir" / "out.txt"
        ok_file = tmp_path / "ok.py"
        ok_file.write_text("x = 1\n")
        result = scoped_test_wait(
            f"python3 -m py_compile {ok_file}",
            str(out),
        )
        assert result.timed_out is False
        assert out.exists()

    def test_timeout_kills_process_and_reports_timed_out(self, tmp_path):
        out = tmp_path / "out.txt"
        # `make` with no target and a nonexistent Makefile still matches the
        # scoped pattern; use an artificially tiny timeout against a command
        # that will not finish before it (python3 -m pytest with no tests
        # dir exits fast, so instead assert timeout plumbing directly via a
        # command that reliably outlives an effectively-zero-but-valid
        # timeout is impractical without a slow fixture — cover the
        # near-zero-timeout path against a real, if trivial, scoped command).
        ok_file = tmp_path / "ok.py"
        ok_file.write_text("x = 1\n")
        result = scoped_test_wait(
            f"python3 -m py_compile {ok_file}",
            str(out),
            timeout=0.000001,
        )
        assert result.timed_out is True
        assert result.returncode is None
