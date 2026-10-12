"""test_merge_ci_check_run_decision.py — the CI gate's decision over parsed
check-runs, the GitHub fetcher's check-run reading (paging, fail-closed on a
list it cannot read completely), and the `required_ci_checks` / `ci_strict`
gate keys."""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from clagentic_loadout.merge import github_backend
from clagentic_loadout.merge.ci_status import (
    CheckRun,
    CiStatusResult,
    StatusEntry,
    check_ci_status,
)
from clagentic_loadout.merge.errors import CiStatusFailedError, GateFactUnavailableError
from clagentic_loadout.merge.gate_config import InvalidMergeGateConfigError, ci_gate_from_section
from clagentic_loadout.merge.repo_gate_runtime import gate_from_tracked_text
from tests._support.ci_gate import HEAD_SHA, check_run, github_ci_opener
from tests._support.fakes import json_resp


def _run(name, conclusion="success", *, status="completed", suite=1, run=1) -> CheckRun:
    return CheckRun(name=name, status=status, conclusion=conclusion, suite_id=suite, run_id=run)


def _decide(result, **kwargs):
    check_ci_status(result, 7, "owner", "repo", head_sha=HEAD_SHA, **kwargs)


class TestSource:
    def test_source_labels(self):
        assert CiStatusResult().source == "none"
        assert CiStatusResult(status_count=1).source == "statuses"
        assert CiStatusResult(check_runs=(_run("t"),)).source == "check-runs"
        assert CiStatusResult(status_count=1, check_runs=(_run("t"),)).source == "check-runs+statuses"

    def test_check_runs_make_the_result_non_empty_but_run_count_alone_does_not(self):
        assert CiStatusResult(check_runs=(_run("t"),)).is_empty is False
        assert CiStatusResult(run_count=9).is_empty is True


class TestDecision:
    def test_refusal_names_check_conclusion_head_and_source(self):
        result = CiStatusResult(check_runs=(_run("test", "failure"),))
        with pytest.raises(CiStatusFailedError) as exc:
            _decide(result)
        message = str(exc.value)
        assert "check 'test' concluded 'failure'" in message
        assert HEAD_SHA in message
        assert "check-runs" in message
        assert "PR #7 in owner/repo" in message

    def test_neutral_and_skipped_runs_do_not_fail_the_default_mode(self):
        result = CiStatusResult(check_runs=(_run("a", "neutral"), _run("b", "skipped")))
        _decide(result)

    def test_unfinished_run_in_an_older_suite_still_counts_as_pending(self):
        result = CiStatusResult(
            check_runs=(
                _run("test", "success", suite=2, run=2),
                _run("test", "", status="in_progress", suite=1, run=1),
            )
        )
        with pytest.raises(CiStatusFailedError, match="still pending"):
            _decide(result)

    def test_failing_status_beside_green_check_runs_refuses(self):
        result = CiStatusResult(
            combined_state="failure", status_count=1, check_runs=(_run("test"),)
        )
        with pytest.raises(CiStatusFailedError, match="combined commit-status state='failure'"):
            _decide(result)

    def test_required_list_ignores_unlisted_failing_checks(self):
        result = CiStatusResult(check_runs=(_run("test"), _run("flaky", "failure")))
        _decide(result, required_checks=("test",))

    def test_required_list_reports_every_problem_at_once(self):
        result = CiStatusResult(check_runs=(_run("a", "failure"),))
        with pytest.raises(CiStatusFailedError) as exc:
            _decide(result, required_checks=("a", "b"))
        message = str(exc.value)
        assert "check 'a' concluded 'failure'" in message
        assert "required check 'b' was not reported at head" in message

    def test_required_name_satisfied_by_a_status_context(self):
        result = CiStatusResult(
            combined_state="success", status_count=1, statuses=(StatusEntry("ci/build", "success"),)
        )
        _decide(result, required_checks=("ci/build",))

    def test_strict_with_evidence_present_passes(self):
        _decide(CiStatusResult(check_runs=(_run("test"),)), strict=True)

    def test_strict_on_empty_refuses(self):
        with pytest.raises(CiStatusFailedError, match="no CI found at head"):
            _decide(CiStatusResult(), strict=True)

    def test_empty_without_strict_passes(self):
        _decide(CiStatusResult())


class TestGithubCheckRunReading:
    def _fetch(self, opener):
        return github_backend.fetch_ci_status("o", "r", HEAD_SHA, token="tok", opener=opener)

    def test_parses_name_status_conclusion_and_suite(self):
        opener = github_ci_opener(
            check_runs=[check_run("test", run_id=11, suite_id=22, conclusion="failure")]
        )
        result = self._fetch(opener)
        assert result.check_runs == (
            CheckRun(name="test", status="completed", conclusion="failure", suite_id=22, run_id=11),
        )
        assert result.run_count == 1

    def test_walks_every_page(self):
        runs = [check_run(f"job-{i}", run_id=i + 1, suite_id=1) for i in range(65)]
        result = self._fetch(github_ci_opener(check_runs=runs, check_runs_page_size=30))
        assert len(result.check_runs) == 65

    def test_a_stalled_page_walk_fails_closed(self):
        runs = [check_run(f"job-{i}", run_id=i + 1, suite_id=1) for i in range(40)]
        base = github_ci_opener(check_runs=runs)

        def opener(req, timeout=30):
            # A server that ignores the page parameter keeps answering page 1.
            return base(urllib.request.Request(req.full_url.split("?")[0]), timeout=timeout)

        with pytest.raises(GateFactUnavailableError, match="stalled"):
            self._fetch(opener)

    def test_a_later_page_error_fails_closed(self):
        runs = [check_run(f"job-{i}", run_id=i + 1, suite_id=1) for i in range(40)]
        base = github_ci_opener(check_runs=runs)

        def opener(req, timeout=30):
            if "page=2" in req.full_url:
                raise urllib.error.URLError("connection reset")
            return base(req, timeout=timeout)

        with pytest.raises(GateFactUnavailableError, match="page 2"):
            self._fetch(opener)

    def test_total_larger_than_the_list_fails_closed(self):
        runs = [check_run("test", run_id=1, suite_id=1)]
        base = github_ci_opener(check_runs=runs)

        def opener(req, timeout=30):
            if req.full_url.split("?")[0].endswith("/check-runs"):
                return json_resp(200, {"total_count": 2, "check_runs": runs})
            return base(req, timeout=timeout)

        with pytest.raises(GateFactUnavailableError):
            self._fetch(opener)


class TestGateKeys:
    def test_absent_keys_default(self):
        assert ci_gate_from_section("gate.yaml", {}) == ((), False)

    def test_values_are_read_and_trimmed(self):
        section = {"required_ci_checks": [" test ", "lint"], "ci_strict": True}
        assert ci_gate_from_section("gate.yaml", section) == (("test", "lint"), True)

    @pytest.mark.parametrize(
        "section, fragment",
        [
            ({"required_ci_checks": "test"}, "must be a list of check names"),
            ({"required_ci_checks": [""]}, "non-empty check-name strings"),
            ({"required_ci_checks": None}, "must be a list of check names"),
            ({"ci_strict": "yes"}, "must be a boolean"),
            ({"ci_strict": None}, "must be a boolean"),
        ],
    )
    def test_malformed_values_raise_naming_the_value(self, section, fragment):
        with pytest.raises(InvalidMergeGateConfigError, match=fragment):
            ci_gate_from_section("gate.yaml", section)

    def test_tracked_text_carries_the_ci_keys(self):
        text = "merge:\n  required_reviewer_roles: []\n  required_ci_checks: [test]\n  ci_strict: true\n"
        gate = gate_from_tracked_text(text, source="gate.yaml")
        assert gate.required_ci_checks == ("test",)
        assert gate.ci_strict is True
        assert gate.warnings == ()

    def test_a_malformed_ci_strict_reads_as_strict_and_keeps_a_valid_check_list(self):
        text = "merge:\n  required_reviewer_roles: []\n  required_ci_checks: [test]\n  ci_strict: 3\n"
        gate = gate_from_tracked_text(text, source="gate.yaml")
        assert gate.required_ci_checks == ("test",)
        assert gate.ci_strict is True
        assert len(gate.warnings) == 1
        assert "ci_strict must be a boolean" in gate.warnings[0]
        assert gate.pre_checks_error == ""

    @pytest.mark.parametrize("value", ['"yes"', "null", "3", "[true]"])
    def test_every_malformed_ci_strict_reads_as_strict(self, value):
        text = f"merge:\n  required_reviewer_roles: []\n  ci_strict: {value}\n"
        gate = gate_from_tracked_text(text, source="gate.yaml")
        assert gate.ci_strict is True
        assert "ci_strict" in gate.warnings[0]

    def test_a_malformed_check_list_falls_back_to_the_default_gate_and_keeps_strict(self):
        text = "merge:\n  required_reviewer_roles: []\n  required_ci_checks: test\n  ci_strict: true\n"
        gate = gate_from_tracked_text(text, source="gate.yaml")
        assert gate.required_ci_checks == ()
        assert gate.ci_strict is True
        assert len(gate.warnings) == 1
        assert "required_ci_checks NOT ENFORCED" in gate.warnings[0]

    def test_a_malformed_check_list_does_not_make_the_gate_strict(self):
        text = "merge:\n  required_reviewer_roles: []\n  required_ci_checks: [3]\n"
        gate = gate_from_tracked_text(text, source="gate.yaml")
        assert gate.required_ci_checks == ()
        assert gate.ci_strict is False
