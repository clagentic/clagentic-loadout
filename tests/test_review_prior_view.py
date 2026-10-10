"""Where a prior finding's line went, what the reviewer is shown for it, and how
a reply names findings as resolved."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.review.chunking import plan_chunks
from clagentic_loadout.review.delta import (
    MAX_LISTED_FINDINGS,
    DeltaContext,
    render_delta_note,
    reviewer_resolved_positions,
)
from clagentic_loadout.review.findings_contract import InvalidReplyError, parse_chunk_output
from clagentic_loadout.review.prior_view import (
    MAX_EXCERPT_CHARS,
    locate_prior_line,
    new_head_excerpt,
    prior_line_text,
)

_FINDING = {"file": "a.py", "line": 3, "rule_id": "R1", "severity": "blocking", "message": "m"}


def _chunks(*hunks: list[str], name: str = "a.py"):
    header = [f"diff --git a/{name} b/{name}", "index 1..2 100644", f"--- a/{name}", f"+++ b/{name}"]
    return plan_chunks("\n".join(header + [line for hunk in hunks for line in hunk]) + "\n", 600)


def test_a_line_below_an_insertion_moves_down_and_one_above_it_does_not():
    chunks = _chunks(["@@ -10,2 +10,5 @@", " keep", "+new 1", "+new 2", "+new 3", " tail"])

    assert locate_prior_line(chunks, "a.py", 4).mapped == 4
    assert locate_prior_line(chunks, "a.py", 40).mapped == 43


def test_a_context_line_inside_a_hunk_maps_exactly_and_a_removed_line_is_unmapped():
    chunks = _chunks(["@@ -10,3 +10,3 @@", " keep", "-gone", "+replacement", " tail"])

    assert locate_prior_line(chunks, "a.py", 10).mapped == 10
    assert locate_prior_line(chunks, "a.py", 12).mapped == 12
    removed = locate_prior_line(chunks, "a.py", 11)
    assert removed.mapped is None and removed.anchor == 11


def test_a_pure_deletion_moves_later_lines_up():
    chunks = _chunks(["@@ -5,3 +4,0 @@", "-x", "-y", "-z"])

    assert locate_prior_line(chunks, "a.py", 20).mapped == 17
    assert locate_prior_line(chunks, "a.py", 6).mapped is None


def test_the_prior_line_text_comes_from_the_old_side_of_the_diff():
    chunks = _chunks(["@@ -10,3 +10,3 @@", " keep", "-gone", "+replacement", " tail"])

    assert prior_line_text(chunks, "a.py", 11) == "gone"
    assert prior_line_text(chunks, "a.py", 10) == "keep"
    assert prior_line_text(chunks, "a.py", 99) is None


def test_the_excerpt_is_bounded_numbered_and_marks_gaps():
    hunk = ["@@ -1,60 +1,60 @@", *[f" row {n}" for n in range(1, 61)]]
    chunks = _chunks(hunk)

    excerpt = new_head_excerpt(chunks, "a.py", 30)

    assert excerpt[0] == "10: row 10" and excerpt[-1] == "50: row 50" and len(excerpt) == 41
    tiny = new_head_excerpt(chunks, "a.py", 30, max_chars=60)
    assert tiny[-1] == "..." and sum(len(e) for e in tiny) < 100
    assert sum(len(e) for e in excerpt) < MAX_EXCERPT_CHARS


def test_a_listed_finding_shows_its_prior_line_and_the_new_head_around_it():
    chunks = _chunks(["@@ -2,3 +2,4 @@", " before", "-bad call()", "+good call()", "+helper()", " after"])
    context = DeltaContext("1" * 40, (_FINDING | {"line": 3},))

    note = render_delta_note(context, None, chunks)

    assert "that line then read: bad call()" in note
    assert "not present in the new head; the nearest new-head position is line 3" in note
    assert "3: good call()" in note and "id: a.py:3:R1" in note
    assert 'name its id in the "resolved" list' in note


def test_only_the_owner_chunk_can_resolve_a_finding_and_only_by_its_id():
    chunks = _chunks(["@@ -1,2 +1,2 @@", " a", "-b", "+c"])
    context = DeltaContext("1" * 40, (_FINDING, _FINDING | {"line": 9, "rule_id": "R2"}))

    assert reviewer_resolved_positions(context, chunks, {1: ["a.py:3:R1"]}) == {0}
    assert reviewer_resolved_positions(context, chunks, {2: ["a.py:3:R1"]}) == frozenset()
    assert reviewer_resolved_positions(context, chunks, {1: ["a.py:3"]}) == frozenset()


def test_a_finding_past_the_listing_cap_cannot_be_resolved_by_a_reply():
    chunks = _chunks(["@@ -1,2 +1,2 @@", " a", "-b", "+c"])
    many = tuple(_FINDING | {"line": n + 1, "rule_id": f"R{n}"} for n in range(MAX_LISTED_FINDINGS + 1))
    context = DeltaContext("1" * 40, many)
    ids = [f"a.py:{f['line']}:{f['rule_id']}" for f in many]

    positions = reviewer_resolved_positions(context, chunks, {1: ids})

    assert positions == frozenset(range(MAX_LISTED_FINDINGS))


@pytest.mark.parametrize(
    "resolved",
    ["everything", [1], [""], ["ok", None], {"a": 1}],
    ids=["string", "number", "empty", "mixed", "object"],
)
def test_a_malformed_resolved_value_resolves_nothing_but_keeps_the_findings(resolved):
    reply = json.dumps({"findings": [_FINDING], "resolved": resolved})

    parsed = parse_chunk_output(reply)

    assert parsed.resolved == [] and [f["rule_id"] for f in parsed.findings] == ["R1"]


def test_the_reply_forms_are_a_bare_array_or_an_object_in_a_fence():
    assert parse_chunk_output(json.dumps([_FINDING])).resolved == []
    fenced = "```json\n" + json.dumps({"findings": [], "resolved": [" a.py:3:R1 "]}) + "\n```"
    assert parse_chunk_output(fenced).resolved == ["a.py:3:R1"]
    with pytest.raises(InvalidReplyError):
        parse_chunk_output(json.dumps({"resolved": ["a.py:3:R1"]}))
