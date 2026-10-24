"""The two published contracts the docs-only review extends: the verdict record
(schema_version 1 -> 2) and the review-result fence (fence_schema_version up to 5).
Both changes are additive: every earlier record and fence still reads, and a
record or fence that carries nothing new is written exactly as before."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clagentic_loadout.envelope import validate_against_schema
from clagentic_loadout.merge import fence_state
from clagentic_loadout.merge.fence_state import (
    normalize_docs_only,
    normalize_findings_state,
    state_from_fence,
)
from clagentic_loadout.merge.verdict import (
    build_findings_verdict_body,
    build_verdict_block,
    parse_verdict_block,
    verdict_schema_path,
)
from clagentic_loadout.verdict_record import record as record_module
from clagentic_loadout.verdict_record.emit import emit_record
from clagentic_loadout.verdict_record.reader import (
    RequiredGate,
    read_gates,
    record_from_fence,
)
from clagentic_loadout.verdict_record.record import (
    RecordFormatError,
    build_record,
    validate_record_dict,
)
from clagentic_loadout.verdict_record.store import load_record, write_record
from clagentic_loadout.merge.record_gate import check_gate_records, required_gates

HEAD = "b" * 40
T0, T1 = "2026-01-01T00:00:00Z", "2026-01-01T00:00:05Z"
PAYLOADS = Path(__file__).parent / "real_payloads"
RECORD_SCHEMA = json.loads(Path(record_module.SCHEMA_PATH).read_text(encoding="utf-8"))
FENCE_SCHEMA = json.loads(verdict_schema_path().read_text(encoding="utf-8"))


def _docs_only(**overrides):
    value = {
        "classification": "docs-only",
        "allowlist_source": {"base_sha": "a" * 40, "path": ".clagentic/loadout/profile.yaml"},
        "justification": "Wording only.\nNo behaviour is touched.",
        "spec_hash": "e" * 64,
        "accepted": True,
        "rejection_reason": None,
    }
    value.update(overrides)
    return value


def _record(**kw):
    return build_record(
        gate="reviewer:agent-alpha", head_sha=HEAD, tool="t", exit=0, started=T0, finished=T1, **kw
    )


class TestVerdictRecordVersions:
    def test_a_record_with_no_docs_only_is_still_version_one_with_the_same_fields(self):
        data = _record().as_dict()

        assert data["schema_version"] == 1 and "docs_only" not in data

    def test_a_record_with_docs_only_is_version_two(self):
        data = _record(docs_only=_docs_only()).as_dict()

        assert data["schema_version"] == record_module.SCHEMA_VERSION == 2
        assert data["docs_only"] == _docs_only()

    def test_readers_accept_version_one_and_version_two(self):
        v1 = _record().as_dict()
        v2 = _record(docs_only=_docs_only()).as_dict()

        assert validate_record_dict(v1).docs_only is None
        assert validate_record_dict(v2).docs_only == _docs_only()
        assert validate_record_dict({**v1, "schema_version": 2}).docs_only is None

    def test_an_old_record_written_before_the_field_existed_still_reads(self):
        written_by_an_earlier_release = {
            "schema_version": 1, "id": "20260101T000000Z-0a1b2c3d", "gate": "ci", "head_sha": HEAD,
            "tree_sha": None, "repo": "o/r", "spec_hash": None, "tool": "loadout-merge", "exit": 0,
            "started": T0, "finished": T1, "evidence_ref": None, "waiver": None,
        }

        assert validate_record_dict(written_by_an_earlier_release).gate == "ci"

    def test_the_object_cannot_ride_on_a_version_one_record(self):
        with pytest.raises(RecordFormatError):
            validate_record_dict({**_record().as_dict(), "docs_only": _docs_only()})

    @pytest.mark.parametrize("version", [0, 3, "2", None])
    def test_a_version_outside_one_and_two_is_refused(self, version):
        with pytest.raises(RecordFormatError):
            validate_record_dict({**_record().as_dict(), "schema_version": version})

    @pytest.mark.parametrize(
        "broken",
        [
            {"classification": "other"},
            {"accepted": "yes"},
            {"spec_hash": "short"},
            {"allowlist_source": {"base_sha": "a" * 40}},
            {"allowlist_source": {"base_sha": "a" * 40, "path": "p", "extra": 1}},
            {"justification": ""},
            {"extra": 1},
        ],
    )
    def test_a_malformed_object_is_refused(self, broken):
        data = {**_record(docs_only=_docs_only()).as_dict(), "docs_only": _docs_only(**broken)}

        with pytest.raises(RecordFormatError):
            validate_record_dict(data)

    def test_a_missing_key_of_the_object_is_refused(self):
        partial = {k: v for k, v in _docs_only().items() if k != "rejection_reason"}
        data = {**_record(docs_only=_docs_only()).as_dict(), "docs_only": partial}

        with pytest.raises(RecordFormatError):
            validate_record_dict(data)

    def test_the_record_round_trips_through_the_store(self, tmp_path):
        record = emit_record(
            gate="reviewer:agent-alpha", head_sha=HEAD, tool="t", exit=0, started=T0, finished=T1,
            docs_only=_docs_only(), root=tmp_path / "records",
        )

        assert load_record(tmp_path / "records", record.id) == record

    def test_the_fence_adapter_carries_the_object_and_the_reader_reports_the_gate_measured(
        self, tmp_path
    ):
        adapted = record_from_fence(
            gate="reviewer:agent-alpha", head_sha=HEAD, exit=0, comment_id=7,
            created_at="2026-01-01T00:00:03Z", docs_only=_docs_only(),
        )

        readings, _ = read_gates(
            tmp_path / "missing-records-root",
            [RequiredGate("reviewer:agent-alpha")],
            HEAD,
            adapted={"reviewer:agent-alpha": [adapted]},
        )

        assert readings[0].ok and readings[0].record.docs_only == _docs_only()

    def test_both_schemas_define_the_object_the_same_way(self):
        def shape(schema):
            return {k: v for k, v in schema["properties"]["docs_only"].items() if k != "description"}

        assert shape(RECORD_SCHEMA) == shape(FENCE_SCHEMA)


class TestRequiredReviewerRecordsStillRequired:
    def test_a_docs_only_record_for_one_role_does_not_measure_another(self, tmp_path):
        root = tmp_path / "records"
        write_record(
            root,
            build_record(
                gate="reviewer:one", head_sha=HEAD, tool="t", exit=0, started=T0, finished=T1,
                repo="o/r", docs_only=_docs_only(),
            ),
        )
        required = required_gates(
            reviewer_names=["one", "two"], pre_check_steps=[], push_verify_names=[]
        )

        refusal = check_gate_records(
            [g for g in required if g.gate.startswith("reviewer:")],
            head_sha=HEAD, repo="o/r", adapted={}, root=root,
        )

        assert refusal is not None
        assert "reviewer:two" in refusal and "reviewer:one" not in refusal

    def test_a_record_that_took_the_lighter_path_but_did_not_pass_still_refuses(self, tmp_path):
        root = tmp_path / "records"
        write_record(
            root,
            build_record(
                gate="reviewer:one", head_sha=HEAD, tool="t", exit=1, started=T0, finished=T1,
                repo="o/r", docs_only=_docs_only(),
            ),
        )

        refusal = check_gate_records(
            [RequiredGate("reviewer:one")], head_sha=HEAD, repo="o/r", adapted={}, root=root
        )

        assert refusal is not None and "did not pass" in refusal


class TestFenceVersions:
    def test_the_docs_only_object_raises_the_version_to_five_over_every_other_field(self):
        out = normalize_findings_state(
            {"docs_only": _docs_only(), "spec_hash": "e" * 64, "spec_tags": [],
             "spec_coverage": [{"criterion": "ac-1", "status": "met"}]},
            head_sha=HEAD, review_status="clean",
        )

        assert out["fence_schema_version"] == fence_state.DOCS_ONLY_FENCE_SCHEMA_VERSION == 5
        assert out["docs_only"] == _docs_only()

    def test_a_fence_without_the_object_keeps_the_version_it_had(self):
        assert normalize_findings_state({}, head_sha=HEAD, review_status="clean") == {}
        out = normalize_findings_state(
            {"verdict_outcome": "CLEAN", "verdict_reasons": [], "evaluator_version": "1"},
            head_sha=HEAD, review_status="clean",
        )
        assert out["fence_schema_version"] == 3

    @pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
    def test_a_reader_accepts_every_version_one_to_five(self, version):
        fence = {
            "reviewer": "r", "review_status": "clean", "head_sha": HEAD, "pr_number": 1,
            "fence_schema_version": version,
        }

        assert validate_against_schema(fence, verdict_schema_path()) == []

    def test_a_version_six_fence_is_refused(self):
        fence = {
            "reviewer": "r", "review_status": "clean", "head_sha": HEAD, "pr_number": 1,
            "fence_schema_version": 6,
        }

        assert validate_against_schema(fence, verdict_schema_path())

    def test_the_schema_lists_exactly_the_versions_the_code_names(self):
        assert FENCE_SCHEMA["properties"]["fence_schema_version"]["enum"] == list(
            fence_state.FENCE_SCHEMA_VERSIONS
        )

    @pytest.mark.parametrize(
        "builder",
        [
            lambda: build_verdict_block("r", "clean", HEAD, 5),
            lambda: build_verdict_block("r", "clean", HEAD, 5, None, {"supersedes": 3}),
            lambda: build_verdict_block(
                "r", "clean", HEAD, 5, None,
                {"verdict_outcome": "CLEAN", "verdict_reasons": [], "evaluator_version": "1"},
            ),
            lambda: build_verdict_block(
                "r", "clean", HEAD, 5, None,
                {"spec_hash": "e" * 64, "spec_tags": [],
                 "spec_coverage": [{"criterion": "ac-1", "status": "met"}]},
            ),
        ],
        ids=["v1", "v2", "v3", "v4"],
    )
    def test_fences_of_every_earlier_version_still_read_and_carry_no_docs_only(self, builder):
        state = state_from_fence(parse_verdict_block(builder()))

        assert state.docs_only is None

    @pytest.mark.parametrize("name", ["github_comments.json", "forgejo_comments.json"])
    def test_fences_captured_from_real_hosts_still_read(self, name):
        comments = json.loads((PAYLOADS / name).read_text(encoding="utf-8"))
        fences = [parse_verdict_block(c["body"]) for c in comments if "review-result" in c["body"]]

        assert fences
        for fence in fences:
            assert fence["fence_schema_version"] == 3
            state = state_from_fence(fence)
            assert state.evaluated and state.docs_only is None

    def test_a_version_five_fence_round_trips_through_the_gate_reader(self):
        block = build_verdict_block("r", "clean", HEAD, 5, None, {"docs_only": _docs_only()})

        state = state_from_fence(parse_verdict_block(block))

        assert state.fence_schema_version == 5 and state.docs_only == _docs_only()
        assert state.evaluated is False  # it says nothing about evaluation by itself

    def test_the_body_states_the_path_taken_in_one_line(self):
        accepted = build_findings_verdict_body(
            "r", "clean", HEAD, 5, [], None, {"docs_only": _docs_only()}
        )
        rejected = build_findings_verdict_body(
            "r", "blocking", HEAD, 5, [], None,
            {"docs_only": _docs_only(accepted=False, rejection_reason="needs tests")},
        )

        assert "docs-only: lighter review, justification accepted; findings are advisory" in accepted
        assert "docs-only: justification rejected, full review: needs tests" in rejected

    def test_a_hand_typed_fence_cannot_carry_a_malformed_object(self):
        raw = json.dumps(
            {"reviewer": "r", "review_status": "clean", "head_sha": HEAD, "pr_number": 1,
             "fence_schema_version": 5, "docs_only": {"accepted": True}}
        )

        from clagentic_loadout.merge.errors import VerdictMalformedError

        with pytest.raises(VerdictMalformedError):
            parse_verdict_block(f"```review-result\n{raw}\n```")


class TestNormalizeDocsOnly:
    def test_an_accepted_object_normalizes_unchanged_and_keeps_a_multi_line_justification(self):
        assert normalize_docs_only(_docs_only()) == _docs_only()

    @pytest.mark.parametrize(
        "overrides",
        [
            {"classification": "docs"},
            {"accepted": 1},
            {"accepted": True, "rejection_reason": "but rejected"},
            {"accepted": False, "rejection_reason": None},
            {"spec_hash": "A" * 64},
            {"justification": ""},
            {"justification": "has ``` fence"},
            {"rejection_reason": "two\nlines", "accepted": False},
            {"allowlist_source": {"base_sha": "short", "path": "p"}},
            {"allowlist_source": {"base_sha": "a" * 40, "path": ""}},
            {"allowlist_source": []},
            {"unknown": 1},
            {"classification": "code"},
        ],
    )
    def test_a_malformed_object_is_refused(self, overrides):
        with pytest.raises(ValueError):
            normalize_docs_only(_docs_only(**overrides))

    def test_a_waiver_on_a_change_classified_as_code_is_refused_by_name(self):
        with pytest.raises(ValueError, match="accepted may be true only"):
            normalize_docs_only(_docs_only(classification="code"))

    def test_a_rejection_may_state_that_the_change_is_code(self):
        rejected = _docs_only(classification="code", accepted=False, rejection_reason="it is code")

        assert normalize_docs_only(rejected) == rejected

    @pytest.mark.parametrize("value", [None, [], "docs-only", 3])
    def test_a_value_that_is_not_an_object_is_refused(self, value):
        with pytest.raises(ValueError):
            normalize_docs_only(value)

    def test_a_missing_key_is_refused(self):
        partial = {k: v for k, v in _docs_only().items() if k != "spec_hash"}

        with pytest.raises(ValueError, match="missing"):
            normalize_docs_only(partial)
