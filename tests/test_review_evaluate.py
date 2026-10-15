"""The evaluator: severity from facts and stakes, dispositions, dedup, outcomes."""

from __future__ import annotations

import datetime as dt

import pytest

from clagentic_loadout.review import evaluate as ev
from clagentic_loadout.review import stakes as stk
from clagentic_loadout.review.evaluate_render import finding_notes, render_text
from clagentic_loadout.review.rubric import SEVERITIES

from _review_evaluate_support import (
    HEAD,
    OTHER_HEAD,
    TODAY,
    disposition,
    exposed_stakes,
    facts,
    finding,
    internal_stakes,
)


def run(findings, stakes=None, dispositions=(), **kwargs):
    return ev.evaluate(
        findings,
        stakes if stakes is not None else internal_stakes(),
        dispositions,
        HEAD,
        HEAD,
        today=TODAY,
        **kwargs,
    )


def serious_open() -> dict:
    return facts(integrity="full", precondition="none")


def test_identical_facts_give_identical_severity_and_blocking_every_run():
    items = [
        finding(line=1, message="one", fact=serious_open()),
        finding(line=2, message="two", fact=serious_open()),
    ]
    first, second = run(items), run(items)
    assert first.as_dict() == second.as_dict()
    a, b = first.findings
    assert (a.severity, a.blocking) == (b.severity, b.blocking) == ("high", True)


def test_finding_without_facts_is_worst_case_and_verdict_is_degraded_naming_it():
    verdict = run([finding(file="src/x.py", line=7, rule_id="RX")], stakes=internal_stakes())
    only = verdict.findings[0]
    assert only.severity == "critical"
    assert only.blocking
    assert only.facts_state == "absent"
    assert verdict.outcome == ev.OUTCOME_DEGRADED
    assert any("src/x.py:7:RX" in reason for reason in verdict.degraded)


def test_worst_case_unstated_stakes_make_a_finding_without_facts_critical():
    verdict = run([finding()], stakes=stk.Stakes())
    assert verdict.findings[0].severity == "critical"


def test_unknown_facts_resolve_to_worst_case_but_are_not_degraded():
    unknown = facts(
        reachable="unknown",
        precondition="unknown",
        confidentiality="unknown",
        integrity="unknown",
        availability="unknown",
        durability="unknown",
    )
    verdict = run([finding(fact=unknown)])
    assert verdict.findings[0].blocking
    assert verdict.findings[0].facts_state == "complete"
    assert verdict.outcome == ev.OUTCOME_BLOCKED


def test_invalid_dimension_resolves_to_worst_case_for_that_dimension_only():
    bad = facts(integrity="full")
    bad["attacker_precondition"] = "wizard"
    verdict = run([finding(fact=bad)])
    only = verdict.findings[0]
    assert only.facts_state == "invalid"
    assert only.score.precondition_rank == 0
    assert only.blocking
    assert verdict.outcome == ev.OUTCOME_DEGRADED


def test_a_milder_stated_fact_lowers_severity():
    mild = facts(integrity="partial", precondition="has_host_access")
    verdict = run([finding(fact=mild)])
    assert verdict.findings[0].severity == "low"
    assert not verdict.findings[0].blocking
    assert verdict.outcome == ev.OUTCOME_CLEAN
    assert verdict.advisory


def test_ephemeral_lowers_one_level_and_unreachable_caps_at_low():
    base = run([finding(fact=serious_open())]).findings[0].severity
    ephemeral = run([finding(fact=facts(integrity="full", durability="ephemeral"))]).findings[0]
    unreachable = run([finding(fact=facts(integrity="full", reachable="no"))]).findings[0]
    assert SEVERITIES.index(ephemeral.severity) == SEVERITIES.index(base) - 1
    assert unreachable.severity == "low"


def test_exposure_lowers_precondition_rank_and_data_class_shifts_impact():
    stated = facts(integrity="partial", precondition="can_merge")
    internal = run([finding(fact=stated)], stakes=internal_stakes("internal")).findings[0]
    regulated = run([finding(fact=stated)], stakes=internal_stakes("regulated")).findings[0]
    exposed = run([finding(fact=stated)], stakes=exposed_stakes("internal")).findings[0]
    assert SEVERITIES.index(regulated.severity) >= SEVERITIES.index(internal.severity)
    assert SEVERITIES.index(exposed.severity) >= SEVERITIES.index(internal.severity)
    assert regulated.score.impact_points > internal.score.impact_points
    assert exposed.score.effective_precondition_rank < exposed.score.precondition_rank


def test_uncontrolled_merges_collapse_can_merge_to_can_open_pr():
    stated = facts(integrity="full", precondition="can_merge")
    controlled = run([finding(fact=stated)], stakes=internal_stakes(merge_control="controlled"))
    uncontrolled = run([finding(fact=stated)], stakes=internal_stakes(merge_control="uncontrolled"))
    assert controlled.findings[0].score.effective_precondition_rank == 2
    assert uncontrolled.findings[0].score.effective_precondition_rank == 1


def test_block_at_threshold_is_honoured():
    item = finding(fact=facts(integrity="full", precondition="can_merge"))
    assert run([item]).findings[0].severity == "medium"
    assert not run([item]).findings[0].blocking
    assert run([item], block_at="medium").findings[0].blocking


def test_invalid_block_at_is_refused():
    with pytest.raises(ValueError):
        run([], block_at="none")


def test_disposition_annotates_demotes_and_keeps_the_finding():
    item = finding(fact=serious_open())
    verdict = run([item], dispositions=[disposition()])
    only = verdict.findings[0]
    assert only.disposition["kind"] == "false_positive"
    assert only.disposition["rationale"] == "checked by hand"
    assert not only.blocking and only.advisory
    assert only.severity == "high"
    assert verdict.outcome == ev.OUTCOME_CLEAN
    assert only in verdict.advisory
    assert "cleared by false_positive" in render_text(verdict)


def test_disposition_matching_uses_path_glob_rule_and_optional_fingerprint():
    item = finding(file="src/deep/a.py", fact=serious_open(), fingerprint="fp2:" + "0" * 16)
    narrow = disposition(path="src/*.py")
    other_rule = disposition(rule_id="R2")
    wrong_fp = disposition(fingerprint="fp2:" + "1" * 16)
    right_fp = disposition(fingerprint="fp2:" + "0" * 16)
    assert run([item], dispositions=[narrow]).findings[0].blocking
    assert run([item], dispositions=[other_rule]).findings[0].blocking
    assert run([item], dispositions=[wrong_fp]).findings[0].blocking
    assert not run([item], dispositions=[right_fp]).findings[0].blocking


def test_fingerprint_on_the_entry_alone_does_not_block_a_match():
    item = finding(fact=serious_open())
    verdict = run([item], dispositions=[disposition(fingerprint="fp2:" + "1" * 16)])
    assert not verdict.findings[0].blocking


def test_expired_disposition_does_not_apply_and_is_reported():
    expired = disposition(expires=dt.date(2026, 9, 30))
    verdict = run([finding(fact=serious_open())], dispositions=[expired])
    assert verdict.findings[0].blocking
    assert verdict.expired_dispositions[0]["rule_id"] == "R1"
    assert "Expired dispositions" in render_text(verdict)


def test_accepted_risk_defaults_to_thirty_days():
    entry = disposition(kind="accepted_risk", at=dt.date(2026, 9, 1))
    assert entry.effective_expiry == dt.date(2026, 10, 1)
    assert not entry.is_expired(dt.date(2026, 10, 1))
    assert entry.is_expired(dt.date(2026, 10, 2))
    assert disposition(kind="by_design").effective_expiry is None


def test_floor_finding_on_exposed_path_stays_blocking_under_by_design():
    floor = facts(category="injection", integrity="partial", precondition="has_host_access")
    verdict = run(
        [finding(fact=floor)],
        stakes=stk.Stakes(tree=stk.TreeStakes(exposed_dirs=("",))),
        dispositions=[disposition(kind="by_design")],
    )
    only = verdict.findings[0]
    assert only.blocking
    assert only.disposition is None
    assert "cannot clear a security-floor finding" in only.disposition_refused
    assert only.floor_forbids_clearing
    assert verdict.outcome in (ev.OUTCOME_BLOCKED, ev.OUTCOME_DEGRADED)
    assert "security floor forbids clearing" in render_text(verdict)


def test_floor_sentence_names_the_base_branch_entry_and_never_a_ruling():
    floor = facts(category="injection", integrity="partial", precondition="has_host_access")
    verdict = run(
        [finding(fact=floor)],
        stakes=stk.Stakes(tree=stk.TreeStakes(exposed_dirs=("",))),
        dispositions=[disposition(kind="by_design")],
    )
    expected = (
        "It can be cleared only by a dispositions entry on the base branch of kind "
        "accepted_risk (expires, default 30 days), false_positive or mitigated, or by a fix, "
        "and never by a ruling."
    )
    assert expected in " ".join(render_text(verdict).split())
    assert expected in " ".join(finding_notes(verdict.findings[0]))
    assert "can be ruled" not in render_text(verdict)


def test_floor_finding_can_be_cleared_by_accepted_risk_and_the_stanza_says_so():
    floor = facts(category="ssrf", integrity="partial", precondition="has_host_access")
    exposed = stk.Stakes(tree=stk.TreeStakes(exposed_dirs=("",)))
    blocked = run([finding(fact=floor)], stakes=exposed)
    assert "kind: accepted_risk" in blocked.findings[0].clear_with
    cleared = run([finding(fact=floor)], stakes=exposed, dispositions=[disposition(kind="accepted_risk")])
    assert not cleared.findings[0].blocking


def test_floor_category_on_an_internal_path_blocks_only_by_severity():
    floor = facts(category="injection", integrity="partial", precondition="has_host_access")
    verdict = run([finding(fact=floor)], stakes=internal_stakes())
    assert not verdict.findings[0].blocking


def test_stale_head_is_stale_even_with_nothing_wrong():
    verdict = ev.evaluate([], internal_stakes(), [], HEAD, OTHER_HEAD, today=TODAY)
    assert verdict.outcome == ev.OUTCOME_STALE
    assert "STALE" in render_text(verdict)


def test_stale_outranks_degraded_and_blocked():
    verdict = ev.evaluate(
        [finding()], internal_stakes(), [], HEAD, OTHER_HEAD, today=TODAY
    )
    assert verdict.outcome == ev.OUTCOME_STALE


def test_blocked_rendering_shows_the_exact_clearing_stanza():
    verdict = run([finding(file="src/a.py", rule_id="R1", fact=serious_open())])
    assert verdict.outcome == ev.OUTCOME_BLOCKED
    text = render_text(verdict)
    for line in (
        "dispositions:",
        '  - path: "src/a.py"',
        '    rule_id: "R1"',
        "    kind: by_design",
        f"    at: {TODAY.isoformat()}",
    ):
        assert line in text
    assert ".clagentic/loadout/dispositions.yaml" in text


def test_clean_lists_advisory_findings():
    verdict = run([finding(fact=facts())])
    assert verdict.outcome == ev.OUTCOME_CLEAN
    assert verdict.findings[0].severity == "none"
    assert "Advisory findings (1)" in render_text(verdict)


def test_praise_is_advisory_and_not_scored():
    verdict = run([finding(severity="praise")])
    assert verdict.outcome == ev.OUTCOME_CLEAN
    assert verdict.findings[0].praise
    assert verdict.findings[0].score is None


def test_same_file_and_rule_at_different_lines_are_kept_and_cross_annotated():
    items = [
        finding(line=3, message="a", fact=serious_open()),
        finding(line=30, message="b", fact=facts()),
        finding(file="src/other.py", line=3, message="c", fact=facts()),
    ]
    verdict = run(items)
    assert len(verdict.findings) == 3
    first, second, third = verdict.findings
    assert second.id in first.possible_duplicates
    assert first.id in second.possible_duplicates
    assert third.possible_duplicates == ()
    assert first.blocking


def test_identical_findings_merge_keeping_the_most_severe_copy():
    mild = finding(fact=facts(integrity="partial", precondition="has_host_access"))
    severe = finding(fact=serious_open())
    verdict = run([mild, severe])
    assert len(verdict.findings) == 1
    assert verdict.findings[0].blocking


def test_a_blocking_finding_is_never_dropped_by_dedup():
    items = [finding(line=n, message=f"m{n}", fact=serious_open()) for n in range(1, 6)]
    verdict = run(items)
    assert len(verdict.blocking) == 5


def test_required_scanner_failure_or_absence_is_degraded():
    clean = [finding(fact=facts())]
    assert run(clean, required_scanners=["s"], scanners_run=[{"scanner": "s", "status": "ran"}]).outcome == (
        ev.OUTCOME_CLEAN
    )
    failed = run(
        clean,
        required_scanners=["s"],
        scanners_run=[{"scanner": "s", "status": "failed", "reason": "timed out"}],
    )
    assert failed.outcome == ev.OUTCOME_DEGRADED
    assert "timed out" in failed.degraded[0]
    assert run(clean, required_scanners=["s"], scanners_run=[]).outcome == ev.OUTCOME_DEGRADED
    not_applicable = [{"scanner": "s", "status": "not_applicable", "reason": "no code"}]
    assert run(clean, required_scanners=["s"], scanners_run=not_applicable).outcome == ev.OUTCOME_CLEAN


@pytest.mark.parametrize("status", ["timeout", "failed", "not_invoked", "RAN", "", None, 7])
def test_a_required_scanner_with_any_status_outside_the_allowlist_is_degraded_naming_it(status):
    clean = [finding(fact=facts())]
    verdict = run(
        clean, required_scanners=["secret-scan"], scanners_run=[{"scanner": "secret-scan", "status": status}]
    )
    assert verdict.outcome == ev.OUTCOME_DEGRADED
    assert "secret-scan" in verdict.degraded[0] and repr(status) in verdict.degraded[0]


@pytest.mark.parametrize("reason", [None, "", "   ", 3])
def test_not_applicable_without_a_real_reason_does_not_satisfy_a_required_scanner(reason):
    entry = {"scanner": "s", "status": "not_applicable"}
    if reason is not None:
        entry["reason"] = reason
    verdict = run([finding(fact=facts())], required_scanners=["s"], scanners_run=[entry])
    assert verdict.outcome == ev.OUTCOME_DEGRADED


def test_a_scanner_reported_twice_is_satisfied_only_if_every_report_is():
    reports = [{"scanner": "s", "status": "failed"}, {"scanner": "s", "status": "ran"}]
    verdict = run([finding(fact=facts())], required_scanners=["s"], scanners_run=reports)
    assert verdict.outcome == ev.OUTCOME_DEGRADED


@pytest.mark.parametrize("praise_first", [True, False])
def test_a_praise_duplicate_never_hides_an_equal_scored_blocking_finding(praise_first):
    blocking = finding(fact=serious_open())
    praise = finding(fact=serious_open(), severity="praise")
    items = [praise, blocking] if praise_first else [blocking, praise]
    verdict = run(items)
    assert len(verdict.findings) == 1
    assert verdict.findings[0].blocking and not verdict.findings[0].praise
    assert verdict.outcome == ev.OUTCOME_BLOCKED


def test_a_blocking_floor_finding_beats_a_more_severe_non_blocking_duplicate():
    exposed = stk.Stakes(tree=stk.TreeStakes(exposed_dirs=("",)))
    floor_but_milder = finding(
        fact=facts(category="injection", integrity="partial", precondition="has_host_access")
    )
    # Same file, line, rule and message; only the stated facts differ.
    severe_not_floor = finding(
        fact=facts(category="correctness", integrity="full", precondition="has_host_access")
    )
    for items in ([severe_not_floor, floor_but_milder], [floor_but_milder, severe_not_floor]):
        verdict = run(items, stakes=exposed, block_at="critical")
        assert len(verdict.findings) == 1
        assert verdict.findings[0].blocking
        assert verdict.findings[0].floor_category


def test_codeowners_never_lowers_the_precondition_rank():
    stated = facts(integrity="full", precondition="can_merge")
    owned = stk.Stakes(
        profile=stk.StakesProfile(name="p", exposure=stk.INTERNAL, data_class="internal"),
        tree=stk.TreeStakes(codeowners=(stk.CodeownersRule(pattern="*", has_owner=True),)),
    )
    verdict = run([finding(fact=stated)], stakes=owned)
    only = verdict.findings[0]
    assert only.stakes.merge_control == stk.UNCONTROLLED
    assert only.stakes.codeowners == stk.CODEOWNERS_COVERED
    assert only.score.effective_precondition_rank == 1


def test_an_unrecognised_stake_value_resolves_to_the_worst_case():
    profile = stk.StakesProfile(
        name="p", exposure="sealed", data_class="nothing", merge_control="locked-down"
    )
    resolved = stk.Stakes(profile=profile).for_path("src/a.py")
    assert (resolved.exposure, resolved.data_class, resolved.merge_control) == (
        stk.EXPOSED,
        "regulated",
        stk.UNCONTROLLED,
    )


def test_a_disposition_of_an_unrecognised_kind_clears_nothing():
    verdict = run([finding(fact=serious_open())], dispositions=[disposition(kind="waived")])
    assert verdict.findings[0].blocking


def test_policy_degraded_reasons_surface_in_the_verdict():
    verdict = run([finding(fact=facts())], degraded_reasons=["dispositions ignored: bad"])
    assert verdict.outcome == ev.OUTCOME_DEGRADED
    assert "dispositions ignored: bad" in render_text(verdict)


def test_facts_missing_on_a_non_blocking_finding_does_not_degrade():
    # A disposition demotes the worst-case finding, so no required input is missing.
    verdict = run([finding()], dispositions=[disposition()])
    assert verdict.outcome == ev.OUTCOME_CLEAN
