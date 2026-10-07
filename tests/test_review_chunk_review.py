"""Tests for review.chunk_review's engine-absence handling, driven by a
scripted runner so no subprocess or real engine is involved."""

from __future__ import annotations

from clagentic_loadout.review.chunk_review import (
    ENGINE_CARRIER,
    ENGINE_FALLBACK,
    REASON_CARRIER_FAILED,
    REASON_MODEL_UNAVAILABLE,
    STATUS_FAILED,
    STATUS_OK,
    build_prompt,
    review_chunk,
)
from clagentic_loadout.review.chunking import plan_chunks
from clagentic_loadout.review.engine_breaker import EngineBreaker
from tests._review_cli_support import make_diff
from tests._support.review_chunk import (
    ARRAY as _ARRAY,
    CARRIER as _CARRIER,
    FALLBACK as _FALLBACK,
    profile as _profile,
    review as _review,
    scripted_runner as _runner,
)


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


_LIMIT_STDERR = "prompt echo\n" * 3000 + "ERROR: You've hit your usage limit.\n"


def _review_with(tmp_path, scripts, *, fallback, attempts_before=0, breaker=None):
    chunk = plan_chunks(make_diff({"a.py": 3}), 600)[0]
    runner = _runner(scripts)
    record = review_chunk(
        chunk,
        1,
        _profile(fallback=fallback),
        attempts_before=attempts_before,
        cwd=tmp_path,
        runner=runner,
        breaker=breaker,
    )
    return record, runner


def test_a_usage_limit_goes_straight_to_the_fallback_with_no_carrier_retry(tmp_path):
    record, runner = _review_with(
        tmp_path,
        {_CARRIER: [(1, "", _LIMIT_STDERR)], _FALLBACK: [(0, _ARRAY, "")]},
        fallback=True,
    )

    assert record["status"] == STATUS_OK
    assert record["engine"] == ENGINE_FALLBACK
    assert runner.calls == [_CARRIER, _FALLBACK]
    assert record["carrier_unavailable_reason"] == "usage_limit"
    assert "usage_limit" in record["carrier_unavailable"]


def test_after_a_usage_limit_the_breaker_skips_the_carrier_for_later_chunks(tmp_path):
    breaker = EngineBreaker()
    _review_with(
        tmp_path,
        {_CARRIER: [(1, "", _LIMIT_STDERR)], _FALLBACK: [(0, _ARRAY, "")]},
        fallback=True,
        breaker=breaker,
    )

    record, runner = _review_with(
        tmp_path, {_CARRIER: [], _FALLBACK: [(0, _ARRAY, "")]}, fallback=True, breaker=breaker
    )

    assert runner.calls == [_FALLBACK]
    assert record["engine"] == ENGINE_FALLBACK
    assert record["carrier_unavailable_reason"] == "usage_limit"


def test_a_usage_limit_without_a_fallback_blocks_with_the_error_line(tmp_path):
    record, _ = _review_with(
        tmp_path, {_CARRIER: [(1, "", _LIMIT_STDERR)]}, fallback=False
    )

    assert record["status"] == STATUS_FAILED
    assert record["reason"] == REASON_MODEL_UNAVAILABLE
    assert record["retriable"] is False
    assert "usage_limit" in record["detail"]
    assert record["stderr_last_line"] == "ERROR: You've hit your usage limit."


def test_exhausted_carrier_failures_run_the_fallback_and_record_why(tmp_path):
    record, runner = _review_with(
        tmp_path,
        {
            _CARRIER: [(1, "", "prompt echo\n" * 3000 + "ERROR: login expired\n")] * 2,
            _FALLBACK: [(0, _ARRAY, "")],
        },
        fallback=True,
        attempts_before=2,
    )

    assert record["status"] == STATUS_OK
    assert record["engine"] == ENGINE_FALLBACK
    assert runner.calls == [_CARRIER, _CARRIER, _FALLBACK]
    failure = record["carrier_failure"]
    assert failure["exit_code"] == 1
    assert failure["stderr_last_line"] == "ERROR: login expired"
    assert "ERROR: login expired" in failure["stderr_excerpt"]


def test_a_carrier_failure_before_the_last_attempt_stays_retriable(tmp_path):
    record, runner = _review_with(
        tmp_path,
        {_CARRIER: [(1, "", "ERROR: flaky\n")] * 2},
        fallback=True,
        attempts_before=0,
    )

    assert record["status"] == STATUS_FAILED
    assert record["reason"] == REASON_CARRIER_FAILED
    assert record["retriable"] is True
    assert runner.calls == [_CARRIER, _CARRIER]


def test_exhausted_carrier_failures_without_a_fallback_block_with_the_tail(tmp_path):
    record, _ = _review_with(
        tmp_path,
        {_CARRIER: [(1, "", "prompt echo\n" * 3000 + "ERROR: login expired\n")] * 2},
        fallback=False,
        attempts_before=2,
    )

    assert record["status"] == STATUS_FAILED
    assert record["reason"] == REASON_CARRIER_FAILED
    assert "ERROR: login expired" in record["stderr_excerpt"]
    assert record["stderr_last_line"] == "ERROR: login expired"


def test_a_fallback_record_carries_the_carriers_stderr_file_and_last_line(tmp_path):
    record, _ = _review_with(
        tmp_path,
        {_CARRIER: [(1, "", _LIMIT_STDERR)], _FALLBACK: [(0, _ARRAY, "")]},
        fallback=True,
    )

    assert record["carrier_unavailable_stderr_last_line"] == "ERROR: You've hit your usage limit."
    assert record["carrier_unavailable_exit_code"] == 1
    assert record["carrier_unavailable_stderr_file"].startswith(str(tmp_path))


def test_a_chunk_that_skips_a_tripped_carrier_still_names_the_carriers_error(tmp_path):
    breaker = EngineBreaker()
    _review_with(
        tmp_path,
        {_CARRIER: [(1, "", _LIMIT_STDERR)], _FALLBACK: [(0, _ARRAY, "")]},
        fallback=True,
        breaker=breaker,
    )

    record, _ = _review_with(
        tmp_path, {_CARRIER: [], _FALLBACK: [(0, _ARRAY, "")]}, fallback=True, breaker=breaker
    )

    assert record["carrier_unavailable_stderr_last_line"] == "ERROR: You've hit your usage limit."
    assert record["carrier_unavailable_stderr_file"]


def test_a_breaker_persists_across_instances_and_expires(tmp_path):
    path = tmp_path / "breaker.json"
    now = [1000.0]
    first = EngineBreaker(path, ttl_seconds=60, clock=lambda: now[0])
    first.trip("carrier", "usage_limit", {"stderr_last_line": "x"})

    assert EngineBreaker(path, ttl_seconds=60, clock=lambda: now[0]).reason("carrier") == "usage_limit"
    assert EngineBreaker(path, ttl_seconds=60, clock=lambda: now[0]).evidence("carrier") == {
        "stderr_last_line": "x"
    }
    now[0] += 61
    assert EngineBreaker(path, ttl_seconds=60, clock=lambda: now[0]).reason("carrier") == ""
    first.clear()
    assert not path.exists()


def test_a_corrupt_breaker_file_is_ignored(tmp_path):
    path = tmp_path / "breaker.json"
    path.write_text("{not json", encoding="utf-8")

    assert EngineBreaker(path).reason("carrier") == ""
