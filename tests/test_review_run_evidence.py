"""Run evidence (commit range, engines) read from a `run` output, and the
fence-side validation and rendering of every evidence field."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.fence_state import (
    normalize_findings_state,
    render_engines,
    render_range,
)
from clagentic_loadout.merge.verdict import build_findings_verdict_body, parse_verdict_block
from clagentic_loadout.review.run_evidence import engines_of, evidence_from_document, range_of

BASE = "1" * 40
HEAD = "2" * 40
SINCE = "3" * 40


def _document(**extra):
    return {"mode": "full", "base_sha": BASE, "head_sha": HEAD, "since_head": None, "chunks": [], **extra}


def test_a_full_run_is_a_base_to_head_range():
    assert range_of(_document()) == {"basis": "base..head", "base": BASE, "head": HEAD}


def test_a_delta_run_is_a_since_range():
    document = _document(mode="delta", since_head=SINCE, base_sha=SINCE)

    assert range_of(document) == {"basis": "since", "since": SINCE, "head": HEAD}


@pytest.mark.parametrize(
    "document",
    [{}, {"head_sha": HEAD}, {"head_sha": "short", "base_sha": BASE}, {"head_sha": HEAD, "base_sha": "x"}],
)
def test_a_document_without_a_readable_range_yields_none(document):
    assert range_of(document) is None


def test_a_delta_without_a_readable_since_head_falls_back_to_base_to_head():
    document = _document(mode="delta", since_head="nope")

    assert range_of(document)["basis"] == "base..head"


def test_engines_are_counted_per_distinct_engine_model_and_reason():
    chunks = [
        {"engine": "carrier"},
        {"engine": "fallback", "engine_label": "m1", "carrier_unavailable_reason": "usage_limit"},
        {"engine": "carrier"},
        {"engine": "fallback", "engine_label": "m1", "carrier_unavailable_reason": "usage_limit"},
        {"engine": "fallback", "engine_label": "m1", "carrier_failure": {"detail": "boom"}},
    ]

    assert engines_of({"chunks": chunks}) == [
        {"engine": "carrier", "chunks": 2},
        {"engine": "fallback", "model": "m1", "reason": "usage_limit", "chunks": 2},
        {"engine": "fallback", "model": "m1", "reason": "carrier_failed", "chunks": 1},
    ]


def test_unknown_engines_and_unusable_labels_are_ignored_not_trusted():
    chunks = [
        {"engine": "mystery"},
        "not a record",
        {"engine": "fallback", "engine_label": "two\nlines", "carrier_unavailable_reason": "  "},
    ]

    assert engines_of({"chunks": chunks}) == [{"engine": "fallback", "chunks": 1}]


def test_a_document_without_chunk_records_has_no_engines():
    assert engines_of({}) == []
    assert evidence_from_document({}) == {}


def test_evidence_holds_only_what_the_document_supports():
    evidence = evidence_from_document(_document(chunks=[{"engine": "carrier"}]))

    assert set(evidence) == {"range", "engines"}


def test_rendering_names_the_range_and_the_engine():
    assert render_range({"basis": "base..head", "base": BASE, "head": HEAD}) == (
        f"range: {BASE[:12]}..{HEAD[:12]}"
    )
    assert render_range({"basis": "since", "since": SINCE, "head": HEAD}) == f"range: since {SINCE[:12]}"
    assert render_engines([{"engine": "carrier"}]) == "engine: carrier"
    assert render_engines(
        [{"engine": "carrier"}, {"engine": "fallback", "model": "m", "reason": "usage_limit"}]
    ) == "engine: carrier; fallback: m, reason usage_limit"
    assert render_engines([{"engine": "fallback"}]) == "engine: fallback"


@pytest.mark.parametrize(
    "state",
    [
        {"range": {"basis": "weird", "head": HEAD}},
        {"range": {"basis": "since", "since": "short", "head": HEAD}},
        {"range": {"basis": "base..head", "base": BASE, "head": HEAD, "extra": 1}},
        {"range": "base..head"},
        {"engines": [{"engine": "other"}]},
        {"engines": [{"engine": "carrier"}, {"engine": "carrier"}]},
        {"engines": [{"engine": "carrier", "chunks": 0}]},
        {"engines": [{"engine": "fallback", "model": "two\nlines"}]},
        {"dropped": [{"file": "a", "line": 1, "rule_id": "R", "message": "m"}]},
        {"dropped": [{"file": "a", "line": True, "rule_id": "R", "message": "m", "reason": "r"}]},
        {"dropped": "nope"},
    ],
)
def test_malformed_evidence_is_refused(state):
    with pytest.raises(ValueError):
        normalize_findings_state(state, head_sha=HEAD, review_status="clean")


def test_evidence_alone_does_not_raise_the_fence_schema_version():
    normalized = normalize_findings_state(
        {"engines": [{"engine": "carrier"}]}, head_sha=HEAD, review_status="clean"
    )

    assert normalized == {"engines": [{"engine": "carrier"}]}


def test_evidence_beside_state_keeps_the_state_version():
    normalized = normalize_findings_state(
        {"engines": [{"engine": "carrier"}], "scanners_run": [{"scanner": "s", "status": "ran"}]},
        head_sha=HEAD,
        review_status="clean",
    )

    assert normalized["fence_schema_version"] == 2


def test_the_body_states_range_engine_and_dropped_count_and_the_fence_agrees():
    dropped = [{"file": "b.py", "line": 2, "rule_id": "R2", "message": "m", "reason": "why"}]
    state = {
        "range": {"basis": "since", "since": SINCE, "head": HEAD},
        "engines": [{"engine": "fallback", "model": "m1", "reason": "usage_limit", "chunks": 3}],
        "dropped": dropped,
    }

    body = build_findings_verdict_body("reviewer", "clean", HEAD, 7, [], None, state)

    lines = body.splitlines()
    assert lines[0] == "REVIEWER — clean (0 finding(s), 1 dropped)"
    assert lines[1] == f"range: since {SINCE[:12]}"
    assert lines[2] == "engine: fallback: m1, reason usage_limit"
    assert "- b.py:2 [R2] m (reason: why)" in lines
    fence = parse_verdict_block(body)
    assert fence["range"] == state["range"]
    assert fence["engines"] == state["engines"]
    assert fence["dropped"] == dropped
    assert "fence_schema_version" not in fence
