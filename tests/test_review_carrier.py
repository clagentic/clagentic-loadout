"""Tests for review.carrier: outcome classification of one engine call."""

from __future__ import annotations

import os
import sys

from clagentic_loadout.review.carrier import KIND_FAILED, KIND_UNAVAILABLE, run_engine


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
