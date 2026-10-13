"""`merge.evaluator` in the tracked gate: the two settings, their defaults, and
that a declaration that cannot be read is the strict one."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.gate_config import (
    STRICT_EVALUATOR_GATE,
    EvaluatorGate,
    InvalidMergeGateConfigError,
    evaluator_gate_from_section,
)
from clagentic_loadout.merge.repo_gate_runtime import gate_from_tracked_text

SOURCE = "gate.yaml"


def test_absent_means_no_role_may_degrade_and_evaluation_is_not_yet_required():
    assert evaluator_gate_from_section(SOURCE, {}) == EvaluatorGate()
    assert EvaluatorGate().allow_degraded == frozenset()
    assert EvaluatorGate().require_evaluated is False


def test_the_roles_set_true_are_the_allowed_ones():
    section = {"evaluator": {"allow_degraded": {"reviewer": True, "security": False}}}

    assert evaluator_gate_from_section(SOURCE, section).allow_degraded == frozenset({"reviewer"})


def test_require_evaluated_is_read():
    assert evaluator_gate_from_section(SOURCE, {"evaluator": {"require_evaluated": True}}).require_evaluated


@pytest.mark.parametrize(
    "block",
    [
        None,
        "yes",
        {"allow_degraded": ["reviewer"]},
        {"allow_degraded": {"reviewer": "yes"}},
        {"allow_degraded": {"": True}},
        {"allow_degraded": None},
        {"require_evaluated": "true"},
        {"require_evaluated": None},
        {"allow_degrade": {"reviewer": True}},
    ],
    ids=str,
)
def test_a_malformed_declaration_is_refused_by_the_parser(block):
    with pytest.raises(InvalidMergeGateConfigError):
        evaluator_gate_from_section(SOURCE, {"evaluator": block})


def test_the_runtime_reads_it_from_the_tracked_text():
    text = "merge:\n  required_reviewer_roles: []\n  evaluator:\n    allow_degraded: {reviewer: true}\n"

    gate = gate_from_tracked_text(text, source=SOURCE)

    assert gate.evaluator == EvaluatorGate(allow_degraded=frozenset({"reviewer"}))
    assert gate.warnings == ()


def test_the_runtime_turns_an_unreadable_declaration_into_the_strict_setting_with_a_warning():
    text = "merge:\n  required_reviewer_roles: []\n  evaluator:\n    allow_degraded: {reviewer: maybe}\n"

    gate = gate_from_tracked_text(text, source=SOURCE)

    assert gate.evaluator == STRICT_EVALUATOR_GATE
    assert any("merge.evaluator is malformed" in warning for warning in gate.warnings)
