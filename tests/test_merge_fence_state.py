"""Unit tests for clagentic_loadout.merge.fence_state: validation of the
structured findings state a verdict fence may carry, and the reader-side walk
that decides which earlier findings are still unaccounted for."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.fence_state import (
    FENCE_SCHEMA_VERSION,
    FindingsState,
    normalize_findings_state,
    state_from_fence,
    unresolved_prior_findings,
)
from clagentic_loadout.merge.verdict import build_verdict_block, parse_verdict_block

HEAD_A = "a" * 40
HEAD_B = "b" * 40
HEAD_C = "c" * 40


def _finding(finding_id="F1", head=HEAD_A):
    return {"id": finding_id, "rule_id": "R1", "head": head}


def _claim(finding_id="F1", head=HEAD_B):
    return {"id": finding_id, "head": head, "evidence": "the guard was added"}


def _normalize(raw, *, head=HEAD_B, status="blocking"):
    return normalize_findings_state(raw, head_sha=head, review_status=status)


class TestNormalize:
    @pytest.mark.parametrize("raw", [None, {}])
    def test_no_state_renders_nothing_and_does_not_bump_the_version(self, raw):
        assert _normalize(raw) == {}

    def test_any_state_stamps_the_schema_version(self):
        out = _normalize({"findings_open": [_finding()]})
        assert out["fence_schema_version"] == FENCE_SCHEMA_VERSION == 2

    def test_all_four_fields_round_trip(self):
        out = _normalize(
            {
                "findings_open": [_finding("F2")],
                "supersedes": "77",
                "cleared_claims": [_claim("F1")],
                "scanners_run": [
                    {"scanner": "alpha", "status": "ran"},
                    {"scanner": "beta", "status": "not_applicable", "reason": "no files in scope"},
                ],
            }
        )
        assert out["supersedes"] == 77
        assert out["cleared_claims"] == [_claim("F1")]
        assert out["scanners_run"][1]["reason"] == "no files in scope"

    @pytest.mark.parametrize(
        "raw",
        [
            {"unknown": 1},
            {"findings_open": "F1"},
            {"findings_open": [{"id": "F1", "rule_id": "R1"}]},
            {"findings_open": [_finding("F1"), _finding("F1")]},
            {"findings_open": [{**_finding(), "head": "abc"}]},
            {"findings_open": [{**_finding(), "extra": 1}]},
            {"findings_open": [_finding("bad id")]},
            {"supersedes": 0},
            {"supersedes": True},
            {"cleared_claims": [{**_claim(), "evidence": ""}]},
            {"cleared_claims": [{**_claim(), "evidence": "line one\nline two"}]},
            {"cleared_claims": [_claim("F1"), _claim("F1")]},
            {"scanners_run": [{"scanner": "alpha", "status": "skipped"}]},
            {"scanners_run": [{"scanner": "alpha", "status": "failed"}]},
            {"scanners_run": [{"scanner": "alpha", "status": "ran"}] * 2},
        ],
    )
    def test_malformed_state_is_rejected(self, raw):
        with pytest.raises(ValueError):
            _normalize(raw)

    def test_a_claim_must_be_at_the_head_under_review(self):
        with pytest.raises(ValueError, match="not the head this verdict is for"):
            _normalize({"cleared_claims": [_claim("F1", head=HEAD_A)]}, head=HEAD_B)

    def test_a_finding_cannot_be_both_cleared_and_open(self):
        with pytest.raises(ValueError, match="both cleared and held open"):
            _normalize({"findings_open": [_finding("F1")], "cleared_claims": [_claim("F1")]})

    def test_a_clean_verdict_cannot_hold_findings_open(self):
        with pytest.raises(ValueError, match="clean verdict"):
            _normalize({"findings_open": [_finding()]}, status="clean")

    @pytest.mark.parametrize("text", ["```", "review-result"])
    def test_fence_shaped_text_is_rejected(self, text):
        with pytest.raises(ValueError, match="fence-delimiter"):
            _normalize({"cleared_claims": [{**_claim(), "evidence": f"see {text}"}]})


class TestFenceRoundTrip:
    def test_state_is_rendered_inside_the_fence_and_parses_back(self):
        block = build_verdict_block(
            "reviewer",
            "blocking",
            HEAD_B,
            7,
            findings_state={"findings_open": [_finding("F9", HEAD_B)], "supersedes": 12},
        )
        data = parse_verdict_block(block)
        state = state_from_fence(data)
        assert state.fence_schema_version == 2
        assert state.findings_open[0]["id"] == "F9"
        assert state.supersedes == 12

    def test_a_version_one_fence_still_parses_and_asserts_nothing(self):
        data = parse_verdict_block(build_verdict_block("reviewer", "clean", HEAD_B, 7))
        assert "fence_schema_version" not in data
        assert state_from_fence(data) == FindingsState()

    def test_unknown_fence_field_is_still_rejected_by_the_schema(self):
        from clagentic_loadout.merge.errors import VerdictMalformedError

        body = '```review-result\n{"reviewer": "r", "review_status": "clean", "head_sha": "%s", "pr_number": 1, "bogus": 1}\n```' % HEAD_B
        with pytest.raises(VerdictMalformedError):
            parse_verdict_block(body)


def _state(open_ids=(), cleared=()):
    return FindingsState(
        fence_schema_version=2,
        findings_open=tuple(_finding(i) for i in open_ids),
        cleared_claims=tuple(_claim(i, HEAD_B) for i in cleared),
    )


class TestUnresolvedPriorFindings:
    def test_no_history_means_nothing_unresolved(self):
        assert unresolved_prior_findings([], FindingsState(), HEAD_B) == []

    def test_a_version_one_history_asserts_nothing(self):
        assert unresolved_prior_findings([(HEAD_A, FindingsState())], FindingsState(), HEAD_B) == []

    def test_an_open_finding_with_no_resolution_stays_unresolved(self):
        out = unresolved_prior_findings([(HEAD_A, _state(open_ids=["F1"]))], FindingsState(), HEAD_B)
        assert [f["id"] for f in out] == ["F1"]

    def test_cleared_at_the_current_head_resolves_it(self):
        current = _state(cleared=["F1"])
        assert unresolved_prior_findings([(HEAD_A, _state(open_ids=["F1"]))], current, HEAD_B) == []

    def test_a_claim_at_another_head_does_not_count(self):
        stale_claim = FindingsState(cleared_claims=(_claim("F1", HEAD_A),))
        out = unresolved_prior_findings([(HEAD_A, _state(open_ids=["F1"]))], stale_claim, HEAD_B)
        assert [f["id"] for f in out] == ["F1"]

    def test_clearing_in_an_intermediate_fence_resolves_it_for_good(self):
        history = [(HEAD_A, _state(open_ids=["F1"])), (HEAD_B, _state(cleared=["F1"]))]
        assert unresolved_prior_findings(history, FindingsState(), HEAD_C) == []

    def test_re_raising_in_the_current_fence_is_left_to_the_verdict_status(self):
        current = _state(open_ids=["F1"])
        assert unresolved_prior_findings([(HEAD_A, _state(open_ids=["F1"]))], current, HEAD_B) == []

    def test_one_cleared_finding_does_not_clear_another(self):
        history = [(HEAD_A, _state(open_ids=["F1", "F2"]))]
        out = unresolved_prior_findings(history, _state(cleared=["F1"]), HEAD_B)
        assert [f["id"] for f in out] == ["F2"]

    def test_supersedes_alone_resolves_nothing(self):
        current = FindingsState(fence_schema_version=2, supersedes=5)
        out = unresolved_prior_findings([(HEAD_A, _state(open_ids=["F1"]))], current, HEAD_B)
        assert [f["id"] for f in out] == ["F1"]
