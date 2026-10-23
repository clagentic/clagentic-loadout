"""The evaluator applies per-PR rulings exactly where it applies base-revision
dispositions, with one difference: a ruling never clears a security-floor
finding, whatever its kind."""

from __future__ import annotations

import datetime as dt

import pytest

from clagentic_loadout.review import dispositions as disp
from clagentic_loadout.review import evaluate as ev
from clagentic_loadout.review.evaluate_render import render_text
from tests._review_evaluate_support import HEAD, disposition, internal_stakes
from tests._ruling_support import FLOOR, NON_FLOOR, OTHER_FP, TODAY, ruled_finding, ruling


def run(findings, entries=()):
    return ev.evaluate(findings, internal_stakes(), entries, HEAD, HEAD, today=TODAY)


class TestNonFloorFinding:
    @pytest.mark.parametrize("kind", disp.KINDS)
    def test_every_kind_of_ruling_clears_a_kept_non_floor_finding(self, kind):
        verdict = run([ruled_finding()], [ruling(kind=kind)])

        only = verdict.findings[0]
        assert not only.blocking
        assert verdict.outcome == ev.OUTCOME_CLEAN
        assert only.disposition["source"] == "ruling"
        assert only.disposition["kind"] == kind

    def test_the_cleared_finding_is_kept_and_named_with_who_requested_when_and_why(self):
        verdict = run([ruled_finding()], [ruling(reference="ABC-123", expires=dt.date(2026, 11, 1))])

        d = verdict.findings[0].disposition
        assert (d["by"], d["requested_by"], d["at"]) == ("some-role", "some-caller", "2026-10-01")
        assert d["source_ref"] == "design note 4"
        assert d["reference"] == "ABC-123"
        text = render_text(verdict)
        for expected in ("some-role", "some-caller", "design note 4", "ABC-123", "accepted for this change"):
            assert expected in text

    def test_a_ruling_does_not_clear_another_fingerprint(self):
        verdict = run([ruled_finding(fingerprint=OTHER_FP)], [ruling()])

        assert verdict.findings[0].blocking
        assert verdict.outcome == ev.OUTCOME_BLOCKED
        assert [r["unapplied_reason"] for r in verdict.unapplied_rulings] == ["matched no kept finding"]

    def test_a_ruling_does_not_clear_another_rule_or_path(self):
        other_rule = ruled_finding(rule_id="R2")
        other_path = ruled_finding(file="src/b.py")
        verdict = run([other_rule, other_path], [ruling()])

        assert all(f.blocking for f in verdict.findings)

    def test_a_finding_without_a_fingerprint_is_never_ruled_on(self):
        bare = ruled_finding()
        del bare["fingerprint"]

        assert run([bare], [ruling()]).findings[0].blocking

    def test_a_ruling_clears_every_copy_of_the_finding_that_carries_the_fingerprint(self):
        findings = [ruled_finding(line=10), ruled_finding(line=20)]

        verdict = run(findings, [ruling()])

        assert not any(f.blocking for f in verdict.findings)

    def test_an_expired_ruling_is_reported_and_not_applied(self):
        expired = ruling(kind="false_positive", at=dt.date(2026, 8, 1), expires=dt.date(2026, 9, 1))

        verdict = run([ruled_finding()], [expired])

        assert verdict.findings[0].blocking
        assert [e["source"] for e in verdict.expired_dispositions] == ["ruling"]
        assert "Expired dispositions" in render_text(verdict) and "ruling src/a.py" in render_text(verdict)

    def test_an_accepted_risk_ruling_expires_thirty_days_after_it_was_made(self):
        old = ruling(at=dt.date(2026, 8, 1))

        assert run([ruled_finding()], [old]).findings[0].blocking

    def test_a_deferral_names_its_reference_and_lapses_at_its_expiry(self):
        deferral = ruling(kind="mitigated", reference="ABC-123", expires=dt.date(2026, 10, 15))

        live = ev.evaluate([ruled_finding()], internal_stakes(), [deferral], HEAD, HEAD, today=dt.date(2026, 10, 15))
        lapsed = ev.evaluate([ruled_finding()], internal_stakes(), [deferral], HEAD, HEAD, today=dt.date(2026, 10, 16))

        assert not live.findings[0].blocking
        assert live.findings[0].disposition["reference"] == "ABC-123"
        assert lapsed.findings[0].blocking

    def test_a_ruling_that_clears_nothing_is_named_in_the_text_and_the_json(self):
        verdict = run([], [ruling()])

        assert verdict.as_dict()["unapplied_rulings"][0]["unapplied_reason"] == "matched no kept finding"
        text = render_text(verdict)
        assert "Rulings for this pull request that cleared nothing (1)" in text
        assert "some-caller" in text


class TestSecurityFloor:
    @pytest.mark.parametrize("kind", disp.KINDS)
    def test_no_ruling_of_any_kind_clears_a_floor_finding(self, kind):
        verdict = run([ruled_finding(FLOOR)], [ruling(kind=kind)])

        only = verdict.findings[0]
        assert only.blocking
        assert only.disposition is None
        assert verdict.outcome == ev.OUTCOME_BLOCKED

    def test_the_refusal_names_the_finding_and_the_one_route_that_clears_it(self):
        verdict = run([ruled_finding(FLOOR)], [ruling(kind="accepted_risk")])

        refused = verdict.findings[0].disposition_refused
        assert "src/a.py:10:R1" in refused
        assert "dispositions entry on the base branch" in refused and "fixing it" in refused
        text = render_text(verdict)
        assert "dispositions entry on the base branch" in text
        # The general by_design sentence is not repeated beside the specific one.
        assert "forbids clearing this finding as by_design" not in text

    def test_the_unapplied_ruling_is_reported_with_the_floor_reason(self):
        verdict = run([ruled_finding(FLOOR)], [ruling()])

        reason = verdict.unapplied_rulings[0]["unapplied_reason"]
        assert reason.startswith("refused:") and "src/a.py:10:R1" in reason

    def test_a_finding_with_no_stated_category_is_a_floor_finding(self):
        no_category = ruled_finding({k: v for k, v in NON_FLOOR.items() if k != "category"})

        assert run([no_category], [ruling()]).findings[0].blocking

    def test_a_base_disposition_keeps_todays_floor_behaviour(self):
        floor = ruled_finding(FLOOR)

        by_design = run([floor], [disposition(kind=disp.BY_DESIGN)])
        accepted = run([floor], [disposition(kind=disp.ACCEPTED_RISK)])

        assert by_design.findings[0].blocking
        assert "a by_design disposition (src/**, R1) cannot clear a security-floor finding" in (
            by_design.findings[0].disposition_refused
        )
        assert not accepted.findings[0].blocking

    def test_a_ruling_does_not_hide_behind_a_base_disposition_that_cleared_the_finding(self):
        floor = ruled_finding(FLOOR)

        verdict = run([floor], [disposition(kind=disp.FALSE_POSITIVE), ruling()])

        assert not verdict.findings[0].blocking
        assert verdict.findings[0].disposition["by"] == "a-maintainer"
        assert verdict.unapplied_rulings[0]["unapplied_reason"].endswith(
            "was already cleared by an earlier entry"
        )


class TestSeveralEntries:
    def test_a_base_disposition_applies_before_a_ruling_and_the_ruling_is_reported_superseded(self):
        verdict = run([ruled_finding()], [disposition(kind=disp.FALSE_POSITIVE), ruling()])

        assert verdict.findings[0].disposition["by"] == "a-maintainer"
        assert "already cleared by an earlier entry" in verdict.unapplied_rulings[0]["unapplied_reason"]


class TestNoRegistry:
    def test_a_verdict_without_rulings_has_no_ruling_keys_and_is_unchanged(self):
        verdict = run([ruled_finding(), ruled_finding(FLOOR, line=20, rule_id="R2")], [disposition()])

        out = verdict.as_dict()
        assert "unapplied_rulings" not in out
        assert verdict.unapplied_rulings == ()
        text = render_text(verdict)
        assert "Rulings for this pull request" not in text and "ruling (" not in text

    def test_the_evaluator_version_is_the_one_the_praise_rule_set_it_to(self):
        # The rulings feature itself adds no version: this pins the current rules.
        assert ev.EVALUATOR_VERSION == "2"
