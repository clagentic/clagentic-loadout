"""The optional failure_sequence on a blocking finding: asked for by the
output contract, preserved by validation and merging, rendered by the verdict
body builder, and copied into the fence."""

from __future__ import annotations

import json
from types import MappingProxyType

import pytest

from clagentic_loadout.merge.fence_state import (
    KEY_FAILURE_SEQUENCES,
    failure_sequences_of,
    normalize_findings_state,
)
from clagentic_loadout.merge.verdict import build_findings_verdict_body, parse_verdict_block
from clagentic_loadout.review.findings_contract import (
    MAX_FAILURE_SEQUENCE_CHARS,
    OUTPUT_CONTRACT,
    InvalidReplyError,
    merge_findings,
    parse_chunk_reply,
    validate_finding,
)

HEAD = "a" * 40


def _finding(**extra):
    return {
        "file": "a.py", "line": 3, "rule_id": "R1", "severity": "blocking",
        "message": "unchecked path", **extra,
    }


def test_the_output_contract_asks_for_an_optional_failure_sequence_on_blocking_findings():
    assert '"failure_sequence"' in OUTPUT_CONTRACT
    assert "OPTIONAL" in OUTPUT_CONTRACT
    assert "blocking" in OUTPUT_CONTRACT.split('"failure_sequence"')[1]


def test_a_non_empty_failure_sequence_is_preserved():
    assert validate_finding(_finding(failure_sequence="open() raises"), 1)[
        "failure_sequence"
    ] == "open() raises"


@pytest.mark.parametrize("value", [None, "", "   \n", 5, ["x"], {"a": 1}])
def test_anything_but_a_non_empty_string_is_left_out_not_rejected(value):
    validated = validate_finding(_finding(failure_sequence=value), 1)

    assert "failure_sequence" not in validated


def test_a_finding_without_the_field_validates_to_exactly_the_five_keys():
    assert set(validate_finding(_finding(), 1)) == {"file", "line", "rule_id", "severity", "message"}


def test_a_long_sequence_from_a_carrier_is_truncated_but_an_edited_one_is_kept_whole():
    long = "x" * (MAX_FAILURE_SEQUENCE_CHARS + 50)

    carrier = validate_finding(_finding(failure_sequence=long), 1)["failure_sequence"]
    edited = validate_finding(
        _finding(failure_sequence=long), 1, lenient_severity=True, truncate_message=False
    )["failure_sequence"]

    assert len(carrier) == MAX_FAILURE_SEQUENCE_CHARS and carrier.endswith("...")
    assert edited == long


def test_a_chunk_reply_keeps_the_failure_sequence():
    reply = json.dumps([_finding(failure_sequence="step then damage")])

    assert parse_chunk_reply(reply)[0]["failure_sequence"] == "step then damage"


def test_a_malformed_finding_is_still_rejected():
    with pytest.raises(InvalidReplyError):
        validate_finding({"file": "a.py", "failure_sequence": "x"}, 1)


def test_merging_keeps_the_sequence_of_the_surviving_finding():
    nit = {**_finding(severity="nit"), "failure_sequence": "from the nit copy"}
    blocking = _finding(failure_sequence="from the blocking copy")

    merged = merge_findings([(1, [nit]), (2, [blocking])])

    assert len(merged) == 1
    assert merged[0]["failure_sequence"] == "from the blocking copy"


def test_the_body_renders_the_sequence_under_its_bullet_and_the_fence_carries_a_copy():
    body = build_findings_verdict_body(
        "reviewer", "blocking", HEAD, 7, [_finding(failure_sequence="a\nb")]
    )

    assert "- a.py:3 [R1] unchecked path\n  failure sequence: a\n    b\n" in body
    fence = parse_verdict_block(body)
    assert fence[KEY_FAILURE_SEQUENCES] == [
        {"file": "a.py", "line": 3, "rule_id": "R1", "failure_sequence": "a\nb"}
    ]


def test_a_non_dict_mapping_state_still_gets_the_sequence_in_the_fence():
    state = MappingProxyType({})

    body = build_findings_verdict_body(
        "reviewer", "blocking", HEAD, 7, [_finding(failure_sequence="a\nb")], findings_state=state
    )

    assert "failure sequence: a" in body
    assert parse_verdict_block(body)[KEY_FAILURE_SEQUENCES] == [
        {"file": "a.py", "line": 3, "rule_id": "R1", "failure_sequence": "a\nb"}
    ]
    assert dict(state) == {}


def test_a_finding_without_one_builds_the_body_it_always_did():
    body = build_findings_verdict_body("reviewer", "blocking", HEAD, 7, [_finding()])

    assert body.startswith("REVIEWER — blocking (1 finding(s))\n- a.py:3 [R1] unchecked path\n")
    assert "failure sequence" not in body
    assert KEY_FAILURE_SEQUENCES not in parse_verdict_block(body)


def test_a_non_string_sequence_handed_to_the_builder_is_refused():
    with pytest.raises(ValueError, match="failure_sequence"):
        build_findings_verdict_body(
            "reviewer", "blocking", HEAD, 7, [_finding(failure_sequence=5)]
        )


def test_a_fence_shaped_sequence_is_refused_by_the_builder():
    with pytest.raises(ValueError, match="fence-delimiter"):
        build_findings_verdict_body(
            "reviewer", "blocking", HEAD, 7, [_finding(failure_sequence="```review-result")]
        )


def test_failure_sequences_are_listed_in_finding_order_for_findings_that_have_one():
    findings = [
        _finding(failure_sequence="one"),
        _finding(line=4),
        _finding(line=5, failure_sequence="  "),
        _finding(line=6, failure_sequence="two"),
    ]

    assert [e["line"] for e in failure_sequences_of(findings)] == [3, 6]


@pytest.mark.parametrize(
    "entry",
    [
        {"file": "a.py", "line": 0, "rule_id": "R", "failure_sequence": "x"},
        {"file": "a.py", "line": 1, "rule_id": "R", "failure_sequence": ""},
        {"file": "a.py", "line": 1, "rule_id": "R"},
        {"file": "a.py", "line": 1, "rule_id": "R", "failure_sequence": "x", "extra": 1},
    ],
)
def test_a_malformed_failure_sequence_entry_is_refused(entry):
    with pytest.raises(ValueError):
        normalize_findings_state(
            {KEY_FAILURE_SEQUENCES: [entry]}, head_sha=HEAD, review_status="blocking"
        )
