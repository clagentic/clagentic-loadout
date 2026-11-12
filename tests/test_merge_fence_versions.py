"""The fence versions a reader accepts are defined once (FENCE_SCHEMA_VERSIONS)
and every other statement of them is held to it here, so a bump cannot leave a
copy behind: the review-result schema's enum, the stamping chain in
normalize_findings_state, and the regex a damaged fence is scanned with. Also
the content rule for the newest version: stamped only by a multi-pass review."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.merge import fence_state
from clagentic_loadout.merge.fence_state import FENCE_SCHEMA_VERSIONS, normalize_findings_state
from clagentic_loadout.merge.verdict import build_verdict_block, parse_verdict_block, verdict_schema_path

HEAD = "a" * 40
FINGERPRINT = "fp2:" + "0" * 16
EVALUATION = {"verdict_outcome": "CLEAN", "verdict_reasons": [], "evaluator_version": "x"}
FINDING = {"file": "a.py", "line": 3, "rule_id": "R1", "message": "m"}
STABILITY = {
    "passes_total": 2,
    "stable": False,
    "seen_in_some_passes": [{**FINDING, "passes_seen": 1}],
}

#: One raw input per version a chain step stamps, version 1 being no stamp at all.
STEPS = {
    2: {"supersedes": 5},
    3: EVALUATION,
    4: {
        "spec_hash": "f" * 64,
        "spec_tags": [],
        "spec_coverage": [{"criterion": "AC1", "status": "met"}],
    },
    5: {
        "docs_only": {
            "classification": "docs-only",
            "allowlist_source": {"base_sha": HEAD, "path": "p"},
            "justification": "j",
            "spec_hash": "f" * 64,
            "accepted": True,
            "rejection_reason": None,
        }
    },
    6: {"findings_open": [{"id": "f-1", "rule_id": "R1", "head": HEAD, "fingerprint": FINGERPRINT}]},
    7: {"cleared_claims": [{"id": "f-1", "head": HEAD, "evidence": "e", "fingerprint": FINGERPRINT}]},
    8: {**EVALUATION, "evaluated_findings": [{**FINDING, "defect_class": "c"}]},
    9: {**EVALUATION, "evaluated_findings": [{**FINDING, "passes_seen": 1, "passes_total": 2}]},
}


def _stamped(raw: dict) -> int:
    return normalize_findings_state(raw, head_sha=HEAD, review_status="blocking")["fence_schema_version"]


def test_every_definition_of_the_accepted_fence_versions_agrees():
    top = max(FENCE_SCHEMA_VERSIONS)
    schema = json.loads(verdict_schema_path().read_text(encoding="utf-8"))
    enum = schema["properties"]["fence_schema_version"]["enum"]

    # The one definition is the whole range, and the schema's enum is a copy of it.
    assert FENCE_SCHEMA_VERSIONS == tuple(range(1, top + 1))
    assert enum == list(FENCE_SCHEMA_VERSIONS)
    # The regex a damaged fence is scanned with names every stamped version and no other.
    for version in range(1, top + 2):
        scanned = fence_state.raw_mentions_findings_state(f'{{"fence_schema_version": {version}}}')
        assert scanned is (1 < version <= top), version
    # The stamping chain reaches each stamped version, and the top one is the newest.
    assert {_stamped(raw) for raw in STEPS.values()} == set(FENCE_SCHEMA_VERSIONS) - {1}
    assert _stamped(STEPS[9]) == top
    for version, raw in STEPS.items():
        body = build_verdict_block("reviewer", "blocking", HEAD, 1, findings_state=raw)
        assert parse_verdict_block(body)["fence_schema_version"] == _stamped(raw), version


@pytest.mark.parametrize("version", FENCE_SCHEMA_VERSIONS)
def test_a_reader_accepts_a_fence_of_every_version(version):
    fence = {"reviewer": "reviewer", "review_status": "clean", "head_sha": HEAD, "pr_number": 1}
    if version > 1:
        fence["fence_schema_version"] = version

    parsed = parse_verdict_block("```review-result\n" + json.dumps(fence) + "\n```\n")

    assert parsed.get("fence_schema_version", 1) == version


def test_version_nine_is_stamped_only_by_the_pass_fields():
    single = {**EVALUATION, "evaluated_findings": [FINDING]}

    assert _stamped(single) == 3
    assert _stamped({**single, "evaluated_findings": [{**FINDING, "defect_class": "c"}]}) == 8
    assert _stamped({**single, "stability": STABILITY}) == fence_state.PASSES_FENCE_SCHEMA_VERSION == 9
    assert _stamped(STEPS[9]) == 9


@pytest.mark.parametrize(
    "bad",
    [
        {**STABILITY, "passes_total": 1},
        {**STABILITY, "stable": "yes"},
        {**STABILITY, "seen_in_some_passes": [{**FINDING, "passes_seen": 2}]},
        {"passes_total": 2, "stable": True},
    ],
    ids=["one-pass", "not-a-bool", "seen-in-every-pass", "missing-list"],
)
def test_a_malformed_stability_block_is_refused(bad):
    with pytest.raises(ValueError):
        normalize_findings_state({**EVALUATION, "stability": bad}, head_sha=HEAD, review_status="clean")


@pytest.mark.parametrize(
    "pair",
    [{"passes_seen": 1}, {"passes_seen": 3, "passes_total": 2}, {"passes_seen": 1, "passes_total": 1}],
    ids=["half-a-pair", "seen-above-total", "one-pass"],
)
def test_a_malformed_pass_pair_on_a_finding_is_refused(pair):
    raw = {**EVALUATION, "evaluated_findings": [{**FINDING, **pair}]}

    with pytest.raises(ValueError):
        _stamped(raw)
