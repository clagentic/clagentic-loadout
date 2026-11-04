"""review.clearance_policy: the base-branch data that says which findings a
reviewer's own clearance is enough for. Anything it does not classify is
security content."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge import fence_state
from clagentic_loadout.review.clearance_policy import (
    ClearancePolicy,
    ClearancePolicyError,
    parse_clearance_policy,
)
from clagentic_loadout.review.repo_profile import ProfileFormatError, parse_profile_document


def _block(**record):
    return {"process_record": record}


class TestParse:
    def test_an_absent_block_is_the_empty_policy(self):
        assert parse_clearance_policy(None) == ClearancePolicy()

    def test_rules_and_categories_are_read(self):
        policy = parse_clearance_policy(_block(rule_ids=["*.record.5"], categories=["test_gap"]))
        assert policy == ClearancePolicy(rule_ids=("*.record.5",), categories=("test_gap",))

    @pytest.mark.parametrize(
        "block",
        [
            "nope",
            {"unknown": {}},
            {"process_record": []},
            {"process_record": {"nope": []}},
            {"process_record": {"rule_ids": "R1"}},
            {"process_record": {"rule_ids": [""]}},
            {"process_record": {"rule_ids": ["a\nb"]}},
            {"process_record": {"categories": ["not_a_category"]}},
        ],
    )
    def test_a_half_understood_block_is_refused(self, block):
        with pytest.raises(ClearancePolicyError):
            parse_clearance_policy(block)

    def test_the_profile_carries_it_and_refuses_it_malformed(self):
        profile = parse_profile_document(
            {"class": "internal", "review": {"clearance": _block(rule_ids=["R1"])}}
        )
        assert profile.clearance.rule_ids == ("R1",)
        assert parse_profile_document({"class": "internal"}).clearance == ClearancePolicy()
        with pytest.raises(ProfileFormatError, match="review.clearance"):
            parse_profile_document({"class": "internal", "review": {"clearance": {"x": 1}}})


class TestClassification:
    def test_a_rule_pattern_decides_on_its_own(self):
        policy = ClearancePolicy(rule_ids=("*.record.5",))
        assert policy.is_process_record("builder.record.5", [])
        assert policy.is_process_record("builder.record.5", ["injection"])
        assert not policy.is_process_record("builder.record.6", [])

    def test_a_category_decides_only_when_every_finding_states_a_listed_one(self):
        policy = ClearancePolicy(categories=("test_gap",))
        assert policy.is_process_record("R1", ["test_gap", "test_gap"])
        assert not policy.is_process_record("R1", ["test_gap", "injection"])
        assert not policy.is_process_record("R1", ["test_gap", None])
        assert not policy.is_process_record("R1", [])

    def test_the_empty_policy_classifies_nothing(self):
        assert not ClearancePolicy().is_process_record("R1", ["test_gap"])

    def test_no_rule_name_or_category_is_built_into_the_default(self):
        for rule in ("builder.path-choice.5", "secrets_exposure", "injection", "supply_chain"):
            assert not ClearancePolicy().is_process_record(rule, [rule])


class TestFenceVersion:
    def test_every_version_up_to_seven_is_accepted(self):
        assert fence_state.FENCE_SCHEMA_VERSIONS == (1, 2, 3, 4, 5, 6, 7)

    def test_a_claim_fingerprint_is_checked_for_shape(self):
        claim = {"id": "f-1", "head": "a" * 40, "evidence": "e", "fingerprint": "fp2:nope"}
        with pytest.raises(ValueError, match="fingerprint"):
            fence_state.normalize_findings_state(
                {"cleared_claims": [claim]}, head_sha="a" * 40, review_status="clean"
            )

    def test_only_a_claim_that_states_a_fingerprint_raises_the_version(self):
        plain = {"id": "f-1", "head": "a" * 40, "evidence": "e"}
        out = fence_state.normalize_findings_state(
            {"cleared_claims": [plain]}, head_sha="a" * 40, review_status="clean"
        )
        assert out["fence_schema_version"] == fence_state.FENCE_SCHEMA_VERSION
        stamped = fence_state.normalize_findings_state(
            {"cleared_claims": [{**plain, "fingerprint": "fp2:0123456789abcdef"}]},
            head_sha="a" * 40, review_status="clean",
        )
        assert stamped["fence_schema_version"] == fence_state.CLEARANCE_FENCE_SCHEMA_VERSION == 7

    def test_a_version_seven_fence_reads_back_as_stateful(self):
        raw = '{"fence_schema_version": 7, "reviewer": "r"'
        assert fence_state.raw_mentions_findings_state(raw)
