"""The evaluation fields of the verdict fence (fence_schema_version 3): they
validate, round-trip through the reader, and an older fence reads as not
evaluated rather than as clean."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.merge.errors import VerdictMalformedError
from clagentic_loadout.merge.fence_state import (
    EVALUATED_FENCE_SCHEMA_VERSION,
    normalize_findings_state,
    raw_mentions_findings_state,
    state_from_fence,
)
from clagentic_loadout.merge.verdict import (
    build_findings_verdict_body,
    build_verdict_block,
    parse_verdict_block,
)

HEAD = "b" * 40
BASE = "a" * 40


def _evaluation(**overrides) -> dict:
    fields = {
        "verdict_outcome": "BLOCKED",
        "verdict_reasons": ["src/a.py:10:R1 blocks (computed severity high)"],
        "evaluator_version": "1",
        "policy_base_sha": BASE,
    }
    fields.update(overrides)
    return fields


def _normalize(raw: dict, status: str = "blocking") -> dict:
    return normalize_findings_state(raw, head_sha=HEAD, review_status=status)


def test_evaluation_fields_raise_the_version_to_three():
    out = _normalize(_evaluation())

    assert out["fence_schema_version"] == EVALUATED_FENCE_SCHEMA_VERSION == 3
    assert out["verdict_outcome"] == "BLOCKED"
    assert out["policy_base_sha"] == BASE


def test_the_base_revision_is_optional():
    out = _normalize({k: v for k, v in _evaluation().items() if k != "policy_base_sha"})

    assert "policy_base_sha" not in out


def test_evaluation_over_state_is_version_three_and_keeps_the_state():
    out = _normalize({**_evaluation(), "supersedes": 7})

    assert out["fence_schema_version"] == 3
    assert out["supersedes"] == 7


@pytest.mark.parametrize("missing", ["verdict_outcome", "verdict_reasons", "evaluator_version"])
def test_the_evaluation_fields_must_be_given_together(missing):
    raw = {k: v for k, v in _evaluation().items() if k != missing}

    with pytest.raises(ValueError, match="together"):
        _normalize(raw)


@pytest.mark.parametrize("outcome", ["clean", "OK", "", None, 3])
def test_an_unrecognised_outcome_is_refused(outcome):
    with pytest.raises(ValueError, match="verdict_outcome"):
        _normalize(_evaluation(verdict_outcome=outcome))


@pytest.mark.parametrize(
    "reasons", ["one", [1], ["two\nlines"], ["has ``` fence"], [""]], ids=str
)
def test_malformed_reasons_are_refused(reasons):
    with pytest.raises(ValueError):
        _normalize(_evaluation(verdict_reasons=reasons))


def test_a_malformed_base_revision_is_refused():
    with pytest.raises(ValueError, match="policy_base_sha"):
        _normalize(_evaluation(policy_base_sha="abc"))


def test_the_fence_round_trips_through_the_gate_reader():
    block = build_verdict_block("reviewer", "blocking", HEAD, 5, None, _evaluation())

    parsed = parse_verdict_block(block)
    state = state_from_fence(parsed)

    assert state.evaluated is True
    assert state.verdict_outcome == "BLOCKED"
    assert state.verdict_reasons == tuple(_evaluation()["verdict_reasons"])
    assert state.evaluator_version == "1"
    assert state.policy_base_sha == BASE


def test_a_version_two_fence_without_the_fields_reads_as_not_evaluated():
    block = build_verdict_block("reviewer", "clean", HEAD, 5, None, {"supersedes": 3})

    state = state_from_fence(parse_verdict_block(block))

    assert state.fence_schema_version == 2
    assert state.evaluated is False
    assert state.verdict_outcome is None


def test_a_version_one_fence_reads_as_not_evaluated():
    state = state_from_fence(parse_verdict_block(build_verdict_block("reviewer", "clean", HEAD, 5)))

    assert state.evaluated is False


def test_an_outcome_stated_on_an_old_version_does_not_count_as_evaluated():
    forged = {
        "reviewer": "r", "review_status": "clean", "head_sha": HEAD, "pr_number": 5,
        "fence_schema_version": 2, "verdict_outcome": "CLEAN",
    }

    assert state_from_fence(forged).evaluated is False


def test_a_fence_carrying_an_unrecognised_outcome_is_not_evaluated():
    fence = {
        "reviewer": "r", "review_status": "clean", "head_sha": HEAD, "pr_number": 5,
        "fence_schema_version": 3, "verdict_outcome": "MAYBE",
    }

    assert state_from_fence(fence).evaluated is False


def test_the_schema_refuses_an_outcome_outside_the_vocabulary():
    raw = json.dumps(
        {"reviewer": "r", "review_status": "clean", "head_sha": HEAD, "pr_number": 5,
         "fence_schema_version": 3, "verdict_outcome": "MAYBE"}
    )

    with pytest.raises(VerdictMalformedError):
        parse_verdict_block(f"```review-result\n{raw}\n```")


def test_a_damaged_version_three_fence_is_treated_as_possibly_stateful():
    assert raw_mentions_findings_state('{"fence_schema_version": 3, oops')


def test_the_body_is_rendered_from_the_evaluation():
    findings = [
        {"file": "a.py", "line": 1, "rule_id": "R1", "message": "bad", "blocking": True,
         "notes": ["computed severity: high; blocking", "  dispositions:"]},
        {"file": "b.py", "line": 2, "rule_id": "R2", "message": "meh", "blocking": False,
         "notes": ["computed severity: low; advisory"]},
    ]

    body = build_findings_verdict_body(
        "reviewer", "blocking", HEAD, 5, findings, None, _evaluation()
    )

    lines = body.splitlines()
    assert lines[:2] == ["REVIEWER — blocking (2 finding(s))", "verdict: BLOCKED"]
    assert "Verdict reasons (1):" in lines
    assert "- src/a.py:10:R1 blocks (computed severity high)" in lines
    blocking = lines.index("Blocking findings (1):")
    advisory = lines.index("Advisory findings (1):")
    assert blocking < lines.index("- a.py:1 [R1] bad") < advisory < lines.index("- b.py:2 [R2] meh")
    assert "  computed severity: high; blocking" in lines
    assert "    dispositions:" in lines
    assert parse_verdict_block(body)["verdict_outcome"] == "BLOCKED"


def test_a_finding_that_does_not_say_it_is_advisory_is_listed_as_blocking():
    findings = [{"file": "a.py", "line": 1, "rule_id": "R1", "message": "bad"}]

    body = build_findings_verdict_body(
        "reviewer", "blocking", HEAD, 5, findings, None, _evaluation()
    )

    assert "Blocking findings (1):" in body
    assert "Advisory findings" not in body


def test_a_clean_body_has_no_reasons_section():
    body = build_findings_verdict_body(
        "reviewer", "clean", HEAD, 5, [], None,
        _evaluation(verdict_outcome="CLEAN", verdict_reasons=[]),
    )

    assert "verdict: CLEAN" in body
    assert "Verdict reasons" not in body


@pytest.mark.parametrize(
    "extra",
    [
        {"notes": "not a list"},
        {"notes": ["two\nlines"]},
        {"notes": [3]},
        {"notes": ["has ``` fence"]},
        {"blocking": "yes"},
    ],
    ids=str,
)
def test_notes_and_blocking_are_validated_like_every_other_field(extra):
    findings = [{"file": "a.py", "line": 1, "rule_id": "R1", "message": "bad", **extra}]

    with pytest.raises(ValueError):
        build_findings_verdict_body("reviewer", "blocking", HEAD, 5, findings, None, _evaluation())


def test_a_body_without_the_evaluation_is_unchanged():
    findings = [{"file": "a.py", "line": 1, "rule_id": "R1", "message": "bad"}]

    body = build_findings_verdict_body("reviewer", "blocking", HEAD, 5, findings)

    assert body.splitlines()[:2] == ["REVIEWER — blocking (1 finding(s))", "- a.py:1 [R1] bad"]
