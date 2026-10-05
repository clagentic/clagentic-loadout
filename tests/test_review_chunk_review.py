"""Tests for review.chunk_review's engine-absence handling, driven by a
scripted runner so no subprocess or real engine is involved."""

from __future__ import annotations

import json
import subprocess

from clagentic_loadout.review.chunk_review import (
    ENGINE_CARRIER,
    ENGINE_FALLBACK,
    REASON_MODEL_UNAVAILABLE,
    STATUS_FAILED,
    STATUS_OK,
    build_prompt,
    review_chunk,
)
from clagentic_loadout.review.chunking import plan_chunks
from clagentic_loadout.review.profile_config import ReviewProfile
from tests._review_cli_support import make_diff

_CARRIER = "carrier-engine"
_FALLBACK = "fallback-engine"
_ARRAY = json.dumps(
    [{"file": "a.py", "line": 1, "rule_id": "R1", "severity": "nit", "message": "m"}]
)


def _profile(*, fallback: bool) -> ReviewProfile:
    return ReviewProfile(
        name="reviewer",
        carrier=(_CARRIER,),
        fallback=(_FALLBACK,) if fallback else None,
        rulebook_text="",
        chunk_lines=600,
        timeout_seconds=5.0,
        fallback_timeout_seconds=5.0,
        max_attempts=3,
        parallel=1,
    )


def _runner(scripts: dict[str, list]):
    """Each call to an engine pops its next scripted step: an exception
    instance is raised, a (code, stdout, stderr) tuple is returned."""
    calls: list[str] = []

    def run(argv, *, input, capture_output, timeout, cwd):
        name = argv[0]
        calls.append(name)
        step = scripts[name].pop(0)
        if isinstance(step, BaseException):
            raise step
        code, stdout, stderr = step
        return subprocess.CompletedProcess(argv, code, stdout.encode(), stderr.encode())

    run.calls = calls
    return run


def _review(tmp_path, scripts, *, fallback: bool):
    chunk = plan_chunks(make_diff({"a.py": 3}), 600)[0]
    runner = _runner(scripts)
    record = review_chunk(
        chunk, 1, _profile(fallback=fallback), attempts_before=0, cwd=tmp_path, runner=runner
    )
    return record, runner


def test_a_carrier_that_vanishes_during_the_format_reprompt_hands_the_chunk_to_the_fallback(
    tmp_path,
):
    record, runner = _review(
        tmp_path,
        {
            _CARRIER: [(0, "not an array", ""), FileNotFoundError("gone")],
            _FALLBACK: [(0, _ARRAY, "")],
        },
        fallback=True,
    )

    assert record["status"] == STATUS_OK
    assert record["engine"] == ENGINE_FALLBACK
    assert runner.calls == [_CARRIER, _CARRIER, _FALLBACK]
    assert "gone" in record["carrier_unavailable"]
    assert "gone" in record[f"{ENGINE_CARRIER}_unavailable_detail"]


def test_a_carrier_that_vanishes_during_the_reprompt_without_a_fallback_is_unavailable(tmp_path):
    record, _ = _review(
        tmp_path,
        {_CARRIER: [(0, "not an array", ""), (127, "", "no such engine")]},
        fallback=False,
    )

    assert record["status"] == STATUS_FAILED
    assert record["reason"] == REASON_MODEL_UNAVAILABLE
    assert record["retriable"] is False
    assert "no such engine" not in record["detail"]
    assert "no fallback is configured" in record["detail"]
    assert record["stderr_excerpt"] == "no such engine"


def test_both_engines_absent_reports_both_diagnostics_and_prefers_the_fallback_stderr(tmp_path):
    record, _ = _review(
        tmp_path,
        {
            _CARRIER: [(127, "", "carrier-stderr")],
            _FALLBACK: [(127, "", "fallback-stderr")],
        },
        fallback=True,
    )

    assert record["status"] == STATUS_FAILED
    assert record["reason"] == REASON_MODEL_UNAVAILABLE
    assert record["retriable"] is False
    assert _CARRIER in record["detail"] and _FALLBACK in record["detail"]
    assert record["stderr_excerpt"] == "fallback-stderr"


def test_both_engines_absent_keeps_the_carrier_stderr_when_the_fallback_has_none(tmp_path):
    record, _ = _review(
        tmp_path,
        {_CARRIER: [(127, "", "carrier-stderr")], _FALLBACK: [FileNotFoundError("nope")]},
        fallback=True,
    )

    assert record["reason"] == REASON_MODEL_UNAVAILABLE
    assert record["stderr_excerpt"] == "carrier-stderr"


def test_a_delta_note_sits_between_the_rulebook_and_the_chunk():
    chunk = plan_chunks(make_diff({"a.py": 3}), 600)[0]

    prompt = build_prompt(chunk, 1, "rule text", "## Incremental review\nnote body")

    assert prompt.index("rule text") < prompt.index("## Incremental review")
    assert prompt.index("## Incremental review") < prompt.index("## Diff chunk 1 of 1")
    assert build_prompt(chunk, 1, "rule text") == build_prompt(chunk, 1, "rule text", "  ")
