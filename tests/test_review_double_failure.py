"""When both engines fail, the failure record keeps each engine's own
diagnostics: the saved stderr path of each, and the error of the engine that
failed last rather than the one that failed first."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from clagentic_loadout.review.chunk_review import (
    REASON_CARRIER_FAILED,
    REASON_MODEL_UNAVAILABLE,
    STATUS_FAILED,
)
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import Env
from tests._support.review_chunk import CARRIER, FALLBACK, review


def _saved(path: str) -> Path:
    assert path, "no saved stderr path"
    return Path(path)


def test_an_unavailable_carrier_with_no_fallback_carries_its_saved_stderr(tmp_path):
    record, _ = review(tmp_path, {CARRIER: [(127, "", "carrier-says-gone")]}, fallback=False)

    assert record["status"] == STATUS_FAILED
    assert record["reason"] == REASON_MODEL_UNAVAILABLE
    assert record["exit_code"] == 127
    assert _saved(record["stderr_file"]).parent == tmp_path / "carrier-logs"
    assert _saved(record["stderr_file"]).read_text(encoding="utf-8") == "carrier-says-gone"


def test_both_engines_unavailable_carry_both_saved_stderr_paths(tmp_path):
    record, _ = review(
        tmp_path,
        {CARRIER: [(127, "", "carrier-says-gone")], FALLBACK: [(127, "", "fallback-says-gone")]},
        fallback=True,
    )

    assert record["reason"] == REASON_MODEL_UNAVAILABLE
    carrier_file = _saved(record["carrier_stderr_file"])
    fallback_file = _saved(record["fallback_stderr_file"])
    assert carrier_file != fallback_file
    assert carrier_file.read_text(encoding="utf-8") == "carrier-says-gone"
    assert fallback_file.read_text(encoding="utf-8") == "fallback-says-gone"
    # The record's own stderr_file leads with the engine tried last.
    assert record["stderr_file"] == record["fallback_stderr_file"]
    assert record["exit_code"] == 127


def test_both_unavailable_falls_back_to_the_carriers_file_when_the_fallback_left_none(tmp_path):
    record, _ = review(
        tmp_path,
        {CARRIER: [(127, "", "carrier-says-gone")], FALLBACK: [FileNotFoundError("nope")]},
        fallback=True,
    )

    assert record["fallback_stderr_file"] == ""
    assert record["stderr_file"] == record["carrier_stderr_file"] != ""


def test_an_unavailable_fallback_after_a_failed_carrier_keeps_the_fallbacks_stderr_path(tmp_path):
    record, _ = review(
        tmp_path,
        {
            CARRIER: [(1, "", "carrier-blew-up")] * 2,
            FALLBACK: [(127, "", "fallback-says-gone")],
        },
        fallback=True,
        max_attempts=1,
    )

    assert record["reason"] == REASON_CARRIER_FAILED
    assert record["stderr_last_line"] == "carrier-blew-up"
    saved = _saved(record["fallback_unavailable_stderr_file"])
    assert saved.read_text(encoding="utf-8") == "fallback-says-gone"
    assert record["fallback_unavailable_exit_code"] == 127


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    environment = Env(tmp_path)
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return environment


def test_a_failed_fallback_reports_its_own_error_with_the_carrier_failure_as_context(env, capsys):
    env.configure(carrier_mode="exit1", fallback_mode="exit2", max_attempts=1)

    code, payload = env.run(capsys=capsys)

    assert code == 20
    assert payload["engine"] == "fallback"
    assert payload["stderr_last_line"] == "fallback blew up differently"
    assert payload["carrier_failure"]["stderr_last_line"] == "carrier blew up"
    note = next(s["note"] for s in payload["stages"] if s["stage"] == "chunk-1")
    assert note.startswith("fallback blew up differently")
    assert "carrier failed: carrier blew up" in note


def test_a_blocked_run_names_both_saved_stderr_files_and_keeps_them_private(env, capsys):
    env.configure(carrier_mode="exit127_noisy", fallback_mode="exit127_noisy")
    previous = os.umask(0)
    try:
        code, payload = env.run(capsys=capsys)
    finally:
        os.umask(previous)

    assert code == 20
    carrier_file = _saved(payload["carrier_stderr_file"])
    fallback_file = _saved(payload["fallback_stderr_file"])
    assert carrier_file.read_text(encoding="utf-8") == "carrier engine is missing\n"
    assert fallback_file.read_text(encoding="utf-8") == "fallback engine is missing\n"
    assert payload["stderr_file"] == payload["fallback_stderr_file"]
    for saved in (carrier_file, fallback_file):
        assert stat.S_IMODE(os.stat(saved).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(saved.parent).st_mode) == 0o700
