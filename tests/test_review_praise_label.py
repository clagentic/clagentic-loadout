"""A `praise` label clears nothing.

The evaluator scores every finding from its facts. `praise` is a presentation
label, shown only on a finding whose own facts already score none; on any other
finding it is ignored. What a praise note with absent facts does is pinned here
too: it is worst case, like any finding with no facts.
"""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.review import evaluate as ev
from clagentic_loadout.review.delta import open_findings_of
from clagentic_loadout.review.evaluate_render import render_text
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import HEAD_SHA, Env
from tests._review_evaluate_support import (
    HEAD,
    TODAY,
    exposed_stakes,
    facts,
    finding,
    internal_stakes,
)

LOW_RISK = facts(category="other", reachable="no")
FLOOR_IN_RANGE = facts(category="injection", precondition="none", integrity="full")


def run(findings, stakes=None, **kwargs):
    return ev.evaluate(
        findings,
        stakes if stakes is not None else exposed_stakes(),
        (),
        HEAD,
        HEAD,
        today=TODAY,
        **kwargs,
    )


def test_the_evaluator_version_moved_with_this_rule():
    assert ev.EVALUATOR_VERSION == "2"


def test_an_in_range_floor_finding_relabelled_praise_still_blocks():
    verdict = run([finding(fact=FLOOR_IN_RANGE, severity="praise")])

    only = verdict.findings[0]
    assert only.blocking and not only.praise
    assert only.floor_category
    assert only.score is not None and only.score.severity == "high"
    assert verdict.outcome == ev.OUTCOME_BLOCKED


def test_a_floor_category_with_no_stated_impact_on_an_exposed_path_still_blocks_as_praise():
    # Scores none, but the floor blocks on an exposed path whatever the score.
    quiet_floor = facts(category="injection")
    verdict = run([finding(fact=quiet_floor, severity="praise")])

    assert verdict.findings[0].blocking and not verdict.findings[0].praise
    assert verdict.outcome == ev.OUTCOME_BLOCKED


@pytest.mark.parametrize("severity", ["praise", "nit", None])
def test_the_label_makes_no_difference_to_a_scored_finding(severity):
    serious = facts(integrity="full", precondition="none")
    verdict = run([finding(fact=serious, severity=severity)], stakes=internal_stakes())

    assert verdict.findings[0].severity == "high"
    assert verdict.findings[0].blocking
    assert verdict.outcome == ev.OUTCOME_BLOCKED


def test_a_praise_note_with_complete_low_risk_facts_is_clean_and_listed_as_praise():
    verdict = run([finding(fact=LOW_RISK, severity="praise")])

    only = verdict.findings[0]
    assert only.praise and not only.blocking
    assert only.severity == "none"
    assert verdict.outcome == ev.OUTCOME_CLEAN
    assert "(praise)" in render_text(verdict)


def test_a_praise_note_with_absent_facts_is_worst_case_and_degrades_the_verdict():
    verdict = run([finding(severity="praise")], stakes=internal_stakes())

    only = verdict.findings[0]
    assert not only.praise
    assert only.blocking and only.severity == "critical"
    assert only.facts_state == "absent"
    assert verdict.outcome == ev.OUTCOME_DEGRADED
    assert any("src/a.py:10:R1" in reason for reason in verdict.degraded)


def test_a_praise_note_with_invalid_facts_is_scored_and_degrades_the_verdict():
    # Reachable, so the unreadable impact resolves to the worst case and blocks.
    broken = {**LOW_RISK, "reachable": "yes", "impact": {**LOW_RISK["impact"], "integrity": "total"}}
    verdict = run([finding(fact=broken, severity="praise")], stakes=internal_stakes())

    assert not verdict.findings[0].praise
    assert verdict.findings[0].blocking
    assert verdict.outcome == ev.OUTCOME_DEGRADED


def test_a_praise_note_that_states_any_impact_is_scored_not_praised():
    mild = facts(category="other", integrity="partial", precondition="has_host_access")
    verdict = run([finding(fact=mild, severity="praise")], stakes=internal_stakes())

    only = verdict.findings[0]
    assert not only.praise
    assert only.severity == "low" and not only.blocking
    assert verdict.outcome == ev.OUTCOME_CLEAN


@pytest.mark.parametrize("praise_first", [True, False])
def test_dedupe_keeps_the_scored_copy_over_a_praise_copy_of_the_same_finding(praise_first):
    # Same file, line, rule and message; only the facts and the label differ.
    praise_copy = finding(fact=LOW_RISK, severity="praise")
    scored_copy = finding(fact=FLOOR_IN_RANGE, severity=None)
    items = [praise_copy, scored_copy] if praise_first else [scored_copy, praise_copy]

    verdict = run(items)

    assert len(verdict.findings) == 1
    assert verdict.findings[0].blocking and not verdict.findings[0].praise
    assert verdict.outcome == ev.OUTCOME_BLOCKED


@pytest.mark.parametrize("praise_first", [True, False])
def test_dedupe_keeps_a_praise_labelled_copy_whose_facts_block_over_a_milder_copy(praise_first):
    relabelled = finding(fact=FLOOR_IN_RANGE, severity="praise")
    milder = finding(fact=LOW_RISK, severity=None)
    items = [relabelled, milder] if praise_first else [milder, relabelled]

    verdict = run(items)

    assert len(verdict.findings) == 1
    assert verdict.findings[0].blocking
    assert verdict.outcome == ev.OUTCOME_BLOCKED


def test_the_delta_carry_forward_keeps_a_relabelled_finding_and_drops_a_real_praise_note():
    relabelled = finding(fact=FLOOR_IN_RANGE, severity="praise", rule_id="R-block")
    absent = finding(severity="praise", rule_id="R-absent")
    real = finding(fact=LOW_RISK, severity="praise", rule_id="R-praise")

    carried = open_findings_of([relabelled, absent, real])

    assert [f["rule_id"] for f in carried] == ["R-block", "R-absent"]


class TestPostCommand:
    """`loadout-review post` end to end, asking for --status clean."""

    @pytest.fixture
    def env(self, tmp_path, monkeypatch) -> Env:
        environment = Env(tmp_path)
        monkeypatch.setenv("STUB_DIR", str(environment.stubs))
        monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
        monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
        return environment

    def _post(self, env, tmp_path, capsys, findings):
        path = tmp_path / "findings.json"
        path.write_text(json.dumps(findings), encoding="utf-8")
        return env.invoke(
            "post", "--findings", str(path), "--head-sha", HEAD_SHA,
            "--base-sha", env.base_sha, "--status", "clean",
            capsys=capsys,
        )

    def _praise(self, **extra):
        return {
            "file": "src/a.py", "line": 4, "rule_id": "R-praise", "severity": "praise",
            "message": "a clear and well-tested change", **extra,
        }

    def test_a_praise_note_with_complete_low_risk_facts_posts_clean(self, env, tmp_path, capsys):
        code, out, err = self._post(env, tmp_path, capsys, [self._praise(facts=LOW_RISK)])

        assert code == 0, err
        assert json.loads(out.strip().splitlines()[-1])["verdict_outcome"] == "CLEAN"
        assert "src/a.py:4 [R-praise]" in env.opener_state["posted_body"]

    def test_a_floor_blocker_relabelled_praise_is_refused(self, env, tmp_path, capsys):
        blocker = self._praise(facts=FLOOR_IN_RANGE, rule_id="R-floor")

        code, _, err = self._post(env, tmp_path, capsys, [blocker])

        assert code == review_cli.EXIT_FINDINGS_INVALID
        assert "contradicts the computed verdict" in err
        assert "src/a.py:4:R-floor" in err
        assert env.opener_state["posted_body"] is None

    def test_a_praise_note_with_no_facts_is_refused_as_degraded(self, env, tmp_path, capsys):
        code, _, err = self._post(env, tmp_path, capsys, [self._praise()])

        assert code == review_cli.EXIT_FINDINGS_INVALID
        assert "contradicts the computed verdict DEGRADED" in err
        assert "src/a.py:4:R-praise" in err
        assert env.opener_state["posted_body"] is None
