"""Tests for review.findings_contract: prose is never "no findings"."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.review.findings_contract import (
    InvalidReplyError,
    merge_findings,
    parse_chunk_reply,
)

_FINDING = {"file": "a.py", "line": 3, "rule_id": "R1", "severity": "nit", "message": "m"}


def test_bare_array_parses():
    assert parse_chunk_reply(json.dumps([_FINDING])) == [_FINDING]


def test_empty_array_is_a_clean_chunk():
    assert parse_chunk_reply("[]") == []


def test_single_markdown_fence_is_tolerated():
    assert parse_chunk_reply("```json\n" + json.dumps([_FINDING]) + "\n```") == [_FINDING]


@pytest.mark.parametrize(
    "reply",
    [
        "Looks good to me.",
        "",
        "Here you go: []",
        '{"file": "a.py"}',
        "[not json",
        json.dumps([{**_FINDING, "severity": "critical"}]),
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
