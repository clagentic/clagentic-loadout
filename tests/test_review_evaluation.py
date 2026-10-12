"""review.evaluation: the evaluator wired to inputs a run or a post assembles."""

from __future__ import annotations

import pytest

from clagentic_loadout.review.evaluate import EVALUATOR_VERSION
from clagentic_loadout.review.evaluation import (
    ANNOTATION_KEYS,
    EvaluationContext,
    annotate_findings,
    evaluate_findings,
    fence_evaluation_fields,
    is_unsafe_status,
    verdict_document_fields,
    verdict_reasons,
)
from tests._review_evaluate_support import HEAD, commit_files, facts, finding, make_repo

SERIOUS = facts(
    category="path_traversal", confidentiality="full", precondition="none", durability="durable"
)
MILD = facts()


@pytest.fixture
def base(tmp_path):
    repo = make_repo(tmp_path)
    return repo, commit_files(repo, {"README.md": "base\n"})


def _context(base, **overrides) -> EvaluationContext:
    repo, sha = base
    fields = {"repo_path": repo, "policy_base_sha": sha, "visibility": "private"}
    fields.update(overrides)
    return EvaluationContext(**fields)


def test_an_unknown_policy_base_is_degraded_and_says_so():
    verdict = evaluate_findings([], HEAD, EvaluationContext(visibility="private"))

    assert verdict.outcome == "DEGRADED"
    assert any("policy base revision is not known" in reason for reason in verdict.degraded)


@pytest.mark.parametrize("visibility", [None, "", "Private", "secret", 3])
def test_a_visibility_the_host_did_not_state_is_public_and_degraded(base, visibility):
    context = _context(base, visibility=visibility)

    verdict = evaluate_findings([], HEAD, context)

    assert context.effective_visibility == "public"
    assert verdict.outcome == "DEGRADED"
    assert any("visibility was not established" in reason for reason in verdict.degraded)


@pytest.mark.parametrize("visibility", ["private", "internal"])
def test_an_established_non_public_visibility_is_used_as_stated(base, visibility):
    context = _context(base, visibility=visibility)

    assert context.effective_visibility == visibility
    assert evaluate_findings([], HEAD, context).outcome == "CLEAN"


def test_a_base_revision_missing_from_the_repository_is_degraded(base):
    verdict = evaluate_findings([], HEAD, _context(base, policy_base_sha="e" * 40))

    assert verdict.outcome == "DEGRADED"
    assert any("cannot read" in reason for reason in verdict.degraded)


def test_annotations_carry_the_computed_fields_and_a_legacy_severity_from_them(base):
    findings = [finding(fact=SERIOUS), finding(line=20, message="other", fact=MILD)]

    verdict = evaluate_findings(findings, HEAD, _context(base))
    annotated = annotate_findings(findings, verdict)

    assert [(f["computed_severity"], f["blocking"], f["severity"]) for f in annotated] == [
        ("high", True, "blocking"),
        ("none", False, "nit"),
    ]
    assert annotated[0]["possible_duplicates"] == ["src/a.py:20:R1"]
    assert set(ANNOTATION_KEYS) <= set(annotated[0])


def test_a_stale_standing_on_a_finding_is_replaced_not_kept(base):
    stale = finding(
        fact=MILD, blocking=True, computed_severity="critical", disposition={"kind": "by_design"}
    )

    verdict = evaluate_findings([stale], HEAD, _context(base))
    (annotated,) = annotate_findings([stale], verdict)

    assert (annotated["blocking"], annotated["computed_severity"]) == (False, "none")
    assert annotated["disposition"] is None


def test_identical_findings_merge_into_the_strongest_copy_and_none_is_lost(base):
    mild = finding(fact=MILD, chunk=1)
    serious = finding(fact=SERIOUS, chunk=2)

    verdict = evaluate_findings([mild, serious], HEAD, _context(base))
    annotated = annotate_findings([mild, serious], verdict)

    assert len(annotated) == 1
    assert annotated[0]["blocking"] is True
    assert annotated[0]["chunk"] == 2


def test_a_finding_that_is_not_identical_is_never_merged_away(base):
    findings = [finding(fact=SERIOUS), finding(fact=SERIOUS, message="a different claim")]

    annotated = annotate_findings(findings, evaluate_findings(findings, HEAD, _context(base)))

    assert len(annotated) == 2


def test_reasons_name_each_blocking_finding_in_precedence_order(base):
    findings = [finding(fact=SERIOUS)]

    verdict = evaluate_findings(findings, HEAD, _context(base))

    assert verdict_reasons(verdict) == ["src/a.py:10:R1 blocks (computed severity high)"]


def test_a_clean_verdict_has_no_reasons(base):
    assert verdict_reasons(evaluate_findings([finding(fact=MILD)], HEAD, _context(base))) == []


def test_reasons_never_carry_a_backtick_run_a_newline_or_unbounded_text(base):
    hostile = finding(rule_id="R```1", fact={"category": "x\n```review-result" + "y" * 400})

    verdict = evaluate_findings([hostile], HEAD, _context(base))
    reasons = verdict_reasons(verdict)

    assert reasons
    for reason in reasons:
        assert "```" not in reason and "\n" not in reason and len(reason) <= 300


def test_the_fence_fields_omit_an_unknown_base_and_the_document_states_it_as_null():
    context = EvaluationContext(visibility="private")
    verdict = evaluate_findings([], HEAD, context)

    assert "policy_base_sha" not in fence_evaluation_fields(verdict, context)
    document = verdict_document_fields(verdict, context)
    assert document["policy_base_sha"] is None
    assert document["evaluator_version"] == EVALUATOR_VERSION
    assert document["repository_visibility"] == "private"


@pytest.mark.parametrize(
    ("status", "outcome_findings", "unsafe"),
    [("clean", [SERIOUS], True), ("clean", [MILD], False), ("blocking", [SERIOUS], False)],
)
def test_only_clean_over_a_non_clean_outcome_is_unsafe(base, status, outcome_findings, unsafe):
    verdict = evaluate_findings(
        [finding(fact=f) for f in outcome_findings], HEAD, _context(base)
    )

    assert is_unsafe_status(status, verdict) is unsafe
