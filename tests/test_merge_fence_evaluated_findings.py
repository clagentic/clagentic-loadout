"""The findings a version 3 fence carries so the merge gate can compute its
verdict again: validation, the all-or-nothing rule, and the round trip."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.fence_state import normalize_findings_state, state_from_fence
from clagentic_loadout.merge.verdict import build_verdict_block, parse_verdict_block
from clagentic_loadout.review.evaluation import evaluated_findings_for_fence, finding_state_id

HEAD = "b" * 40
FACTS = {"category": "correctness", "reachable": "yes"}


def _evaluation(**more):
    return {
        "verdict_outcome": "BLOCKED", "verdict_reasons": ["r"], "evaluator_version": "1", **more
    }


def _normalize(raw):
    return normalize_findings_state(raw, head_sha=HEAD, review_status="blocking")


def _entry(**more):
    return {"file": "a.py", "line": 1, "rule_id": "R1", "message": "m", **more}


def test_the_findings_round_trip_through_the_fence():
    state = _evaluation(evaluated_findings=[_entry(facts=FACTS)])

    parsed = parse_verdict_block(build_verdict_block("reviewer", "blocking", HEAD, 5, None, state))

    assert parsed["evaluated_findings"] == [_entry(facts=FACTS)]
    assert state_from_fence(parsed).evaluated_findings == (_entry(facts=FACTS),)


def test_a_fence_without_the_field_reads_as_not_carrying_findings():
    parsed = parse_verdict_block(build_verdict_block("reviewer", "blocking", HEAD, 5, None, _evaluation()))

    assert state_from_fence(parsed).evaluated_findings is None


def test_an_empty_list_is_a_fence_that_carries_no_findings():
    parsed = parse_verdict_block(
        build_verdict_block("reviewer", "blocking", HEAD, 5, None, _evaluation(evaluated_findings=[]))
    )

    assert state_from_fence(parsed).evaluated_findings == ()


def test_the_findings_need_the_rest_of_the_evaluation():
    with pytest.raises(ValueError, match="together"):
        _normalize({"evaluated_findings": [_entry()]})


@pytest.mark.parametrize(
    "entry",
    [
        _entry(severity="blocking"),
        _entry(fingerprint="fp2:" + "a" * 16),
        _entry(extra=1),
        _entry(facts="not an object"),
        _entry(facts={"evidence": "x ```review-result y"}),
        _entry(facts={"nested": {"deeper": ["~~~\nfence"]}}),
        _entry(facts={"object": object()}),
        _entry(line=0),
        _entry(message="has ``` fence"),
        {"file": "a.py", "line": 1, "rule_id": "R1"},
    ],
    ids=[
        "severity-not-praise", "fingerprint", "unknown-key", "facts-not-object",
        "fence-in-facts", "fence-nested-in-facts", "non-json-facts", "line-zero",
        "fence-in-message", "missing-message",
    ],
)
def test_a_malformed_or_fence_shaped_finding_is_refused(entry):
    with pytest.raises(ValueError):
        _normalize(_evaluation(evaluated_findings=[entry]))


def test_praise_is_the_only_severity_a_carried_finding_may_state():
    out = _normalize(_evaluation(evaluated_findings=[_entry(severity="praise")]))

    assert out["evaluated_findings"] == [_entry(severity="praise")]


def test_what_the_post_path_carries_is_what_the_evaluator_saw():
    seen = [
        {
            "file": "a.py", "line": 1, "rule_id": "R1", "message": "m", "facts": FACTS,
            "fingerprint": "fp2:" + "a" * 16, "severity": "blocking", "failure_sequence": "steps",
        },
        {"file": "b.py", "line": 2, "rule_id": "R2", "message": "n", "severity": "praise"},
    ]

    assert evaluated_findings_for_fence(seen) == [
        {"file": "a.py", "line": 1, "rule_id": "R1", "message": "m", "facts": FACTS},
        {"file": "b.py", "line": 2, "rule_id": "R2", "message": "n", "severity": "praise"},
    ]


def test_a_finding_state_id_is_stable_and_fits_the_id_grammar():
    first = finding_state_id("src/dir/a b.py", 10, "rule/with:odd chars")

    assert first == finding_state_id("src/dir/a b.py", 10, "rule/with:odd chars")
    assert first != finding_state_id("src/dir/a b.py", 11, "rule/with:odd chars")
    out = _normalize({"findings_open": [{"id": first, "rule_id": "R", "head": HEAD}]})
    assert out["findings_open"][0]["id"] == first
