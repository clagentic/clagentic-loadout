"""The one fence-syntax predicate, and proof that every validated caller field
goes through it. A new field that skips the predicate shows up here as a case
that is not rejected."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.fence_state import normalize_findings_state
from clagentic_loadout.merge.fence_syntax import find_fence_syntax
from clagentic_loadout.merge.verdict import build_findings_verdict_body

HEAD = "b" * 40
OPENER = "```review-result"


@pytest.mark.parametrize(
    "value",
    [
        "src/clagentic_loadout/schemas/review-result.schema.json",
        "review-result",
        "a ~~~ b",
        "two `` backticks",
        "inline `code` span",
    ],
)
def test_ordinary_text_is_not_fence_syntax(value):
    assert find_fence_syntax(value) is None


@pytest.mark.parametrize(
    "value",
    ["```", OPENER, "x ```", "line\n```review-result\n", "~~~", "  ~~~review-result", "a\n~~~~"],
)
def test_fence_syntax_is_found(value):
    assert find_fence_syntax(value) is not None


def _finding(**overrides):
    return {"file": "a.py", "line": 1, "rule_id": "X", "message": "ok", **overrides}


_STATE_SITES = {
    "open.rule_id": lambda v: {"findings_open": [{"id": "F1", "rule_id": v, "head": HEAD}]},
    "cleared.evidence": lambda v: {"cleared_claims": [{"id": "F1", "head": HEAD, "evidence": v}]},
    "scanner.name": lambda v: {"scanners_run": [{"scanner": v, "status": "ran"}]},
    "scanner.reason": lambda v: {
        "scanners_run": [{"scanner": "s", "status": "failed", "reason": v}]
    },
}


@pytest.mark.parametrize("site", sorted(_STATE_SITES))
def test_every_state_text_field_rejects_a_fence_opener(site):
    with pytest.raises(ValueError, match="fence-delimiter"):
        normalize_findings_state(_STATE_SITES[site](OPENER), head_sha=HEAD, review_status="blocking")


@pytest.mark.parametrize("site", sorted(_STATE_SITES))
def test_every_state_text_field_accepts_the_plain_word(site):
    normalize_findings_state(
        _STATE_SITES[site]("review-result.schema.json"), head_sha=HEAD, review_status="blocking"
    )


@pytest.mark.parametrize("field", ["file", "rule_id", "message"])
def test_every_body_field_rejects_a_fence_opener_and_accepts_the_plain_word(field):
    with pytest.raises(ValueError, match="fence-delimiter"):
        build_findings_verdict_body("r", "blocking", HEAD, 1, [_finding(**{field: OPENER})])
    build_findings_verdict_body(
        "r", "blocking", HEAD, 1, [_finding(**{field: "review-result.schema.json"})]
    )
