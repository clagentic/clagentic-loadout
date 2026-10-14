"""Tests for review.findings_contract: prose is never "no findings"."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.review.findings_contract import (
    InvalidReplyError,
    merge_findings,
    parse_chunk_output,
    parse_chunk_reply,
)

_FINDING = {"file": "a.py", "line": 3, "rule_id": "R1", "severity": "nit", "message": "m"}
# A reply states facts and never a severity: the severity above is what an older
# rulebook still sends, and it never survives parsing.
_PARSED = {k: v for k, v in _FINDING.items() if k != "severity"}
_FACTS = {
    "category": "correctness",
    "reachable": "yes",
    "attacker_precondition": "none",
    "impact": {"confidentiality": "none", "integrity": "partial", "availability": "none"},
    "durability": "durable",
}


def test_bare_array_parses():
    assert parse_chunk_reply(json.dumps([_FINDING])) == [_PARSED]


def test_empty_array_is_a_clean_chunk():
    assert parse_chunk_reply("[]") == []


def test_single_markdown_fence_is_tolerated():
    assert parse_chunk_reply("```json\n" + json.dumps([_FINDING]) + "\n```") == [_PARSED]


@pytest.mark.parametrize("severity", ["nit", "critical", "praise", 3, None, ""])
def test_a_severity_in_a_reply_is_discarded_whatever_it_holds(severity):
    reply = parse_chunk_output(json.dumps([{**_PARSED, "severity": severity}]))

    assert reply.findings == [_PARSED]
    assert reply.ignored_severity == 1


def test_a_reply_without_a_severity_reports_none_ignored():
    assert parse_chunk_output(json.dumps([{**_PARSED, "facts": _FACTS}])).ignored_severity == 0


def test_facts_are_carried_exactly_as_the_reply_wrote_them():
    reply = parse_chunk_output(json.dumps([{**_PARSED, "facts": {**_FACTS, "category": "nonsense"}}]))

    # Judging the facts is the evaluator's job; an out-of-vocabulary value is
    # not repaired or refused here.
    assert reply.findings[0]["facts"]["category"] == "nonsense"


def test_a_finding_without_facts_is_accepted_and_carries_none():
    assert "facts" not in parse_chunk_reply(json.dumps([_PARSED]))[0]


def test_a_failure_sequence_is_kept_without_a_severity():
    finding = {**_PARSED, "failure_sequence": "step then damage"}

    assert parse_chunk_reply(json.dumps([finding]))[0]["failure_sequence"] == "step then damage"


@pytest.mark.parametrize(
    "reply",
    [
        "Looks good to me.",
        "",
        "Here you go: []",
        '{"file": "a.py"}',
        "[not json",
        json.dumps([{**_FINDING, "line": "3"}]),
        json.dumps([{**_FINDING, "line": True}]),
        json.dumps([{**_FINDING, "message": ""}]),
        json.dumps(["not an object"]),
    ],
)
def test_anything_else_is_an_invalid_reply(reply):
    with pytest.raises(InvalidReplyError):
        parse_chunk_reply(reply)


@pytest.mark.parametrize("line", [0, -4])
def test_non_positive_line_is_an_invalid_reply(line):
    with pytest.raises(InvalidReplyError):
        parse_chunk_reply(json.dumps([{**_FINDING, "line": line}]))


def test_over_long_message_is_truncated_to_the_contract_bound():
    parsed = parse_chunk_reply(json.dumps([{**_FINDING, "message": "x" * 500}]))

    assert len(parsed[0]["message"]) == 200
    assert parsed[0]["message"].endswith("...")


def test_merge_orders_by_chunk_and_tags_the_chunk():
    other = {**_FINDING, "file": "b.py"}

    merged = merge_findings([(2, [other]), (1, [_FINDING])])

    assert [f["file"] for f in merged] == ["a.py", "b.py"]
    assert [f["chunk"] for f in merged] == [1, 2]


def test_merge_dedupes_but_keeps_the_most_severe_copy():
    blocking = {**_FINDING, "severity": "blocking"}

    merged = merge_findings([(1, [_FINDING]), (2, [blocking])])

    assert len(merged) == 1
    assert merged[0]["severity"] == "blocking"


def test_merge_drops_a_copy_that_states_the_same_facts():
    finding = {**_PARSED, "facts": _FACTS}

    merged = merge_findings([(1, [finding]), (2, [dict(finding)])])

    assert len(merged) == 1


def test_merge_keeps_every_copy_that_states_different_facts():
    # Which copy is stronger is the evaluator's call, from the computed severity;
    # the merge must not pick one by position and hide a blocking copy.
    mild = {**_PARSED, "facts": _FACTS}
    harsh = {**_PARSED, "facts": {**_FACTS, "impact": {**_FACTS["impact"], "confidentiality": "full"}}}

    merged = merge_findings([(1, [mild]), (2, [harsh])])

    assert [f["facts"] for f in merged] == [mild["facts"], harsh["facts"]]


def test_the_output_contract_offers_exactly_the_published_vocabulary():
    from clagentic_loadout.review import facts as vocab
    from clagentic_loadout.review.findings_contract import OUTPUT_CONTRACT

    assert '"severity"' not in OUTPUT_CONTRACT.split("Do not state a severity")[0]
    for values in (
        vocab.CATEGORIES,
        vocab.REACHABLE_VALUES,
        vocab.PRECONDITIONS,
        vocab.IMPACT_LEVELS,
        vocab.DURABILITY_VALUES,
    ):
        assert " | ".join(values) in OUTPUT_CONTRACT


_SEQ = "open the page, submit the form, the session is shared"


def _parse_one(finding):
    return parse_chunk_output(json.dumps([finding]))


def test_a_failure_sequence_inside_facts_moves_to_the_top_level():
    from clagentic_loadout.review.facts import STATE_COMPLETE, parse_facts

    reply = _parse_one({**_PARSED, "facts": {**_FACTS, "failure_sequence": f"  {_SEQ} "}})

    finding = reply.findings[0]
    assert finding["failure_sequence"] == _SEQ
    assert "failure_sequence" not in finding["facts"]
    assert parse_facts(finding["facts"]).state == STATE_COMPLETE
    assert len(reply.problems) == 1 and "moved" in reply.problems[0]


def test_a_top_level_failure_sequence_alone_records_no_problem():
    reply = _parse_one({**_PARSED, "facts": _FACTS, "failure_sequence": _SEQ})

    assert reply.findings[0]["failure_sequence"] == _SEQ
    assert reply.findings[0]["facts"] == _FACTS
    assert reply.problems == ()


def test_with_both_the_top_level_value_stands_and_the_nested_is_dropped():
    reply = _parse_one(
        {**_PARSED, "failure_sequence": _SEQ, "facts": {**_FACTS, "failure_sequence": "other"}}
    )

    assert reply.findings[0]["failure_sequence"] == _SEQ
    assert reply.findings[0]["facts"] == _FACTS
    assert len(reply.problems) == 1 and "dropped" in reply.problems[0]


@pytest.mark.parametrize("nested", [7, ["a"], {"a": 1}, "   ", None])
def test_a_nested_failure_sequence_that_fails_the_checks_stays_invalid(nested):
    from clagentic_loadout.review.facts import STATE_INVALID, parse_facts

    reply = _parse_one({**_PARSED, "facts": {**_FACTS, "failure_sequence": nested}})

    finding = reply.findings[0]
    assert "failure_sequence" not in finding
    assert parse_facts(finding["facts"]).state == STATE_INVALID
    assert reply.problems == ()


def test_another_unknown_fact_key_is_not_touched():
    from clagentic_loadout.review.facts import STATE_INVALID, parse_facts

    reply = _parse_one({**_PARSED, "facts": {**_FACTS, "note": "x"}})

    assert parse_facts(reply.findings[0]["facts"]).state == STATE_INVALID


def test_a_nested_failure_sequence_is_bounded_like_the_top_level_one():
    from clagentic_loadout.review.findings_contract import MAX_FAILURE_SEQUENCE_CHARS

    reply = _parse_one({**_PARSED, "facts": {**_FACTS, "failure_sequence": "x" * 900}})

    assert len(reply.findings[0]["failure_sequence"]) == MAX_FAILURE_SEQUENCE_CHARS
