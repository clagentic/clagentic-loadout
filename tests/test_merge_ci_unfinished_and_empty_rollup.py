"""test_merge_ci_unfinished_and_empty_rollup.py — unfinished check-runs never
pass, and a combined state resting on zero commit statuses decides nothing.

Payloads follow the public GitHub REST contract (a commit with no statuses
reports a combined `state` of `pending` with an empty `statuses` list; check-run
`status` is queued, in_progress or completed) and the Forgejo contract (the
combined-status endpoint is the only CI evidence). Every caller shape runs: the
merge itself and --report-only, on both platforms."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.ci_status import (
    CheckRun,
    CiStatusResult,
    StatusEntry,
    check_ci_status,
)
from clagentic_loadout.merge.errors import CiStatusFailedError
from tests._support.ci_gate import (
    HEAD_SHA,
    check_run,
    forgejo_ci_opener,
    github_ci_opener,
    status_entry,
)
from tests._support.ci_gate import run_merge as _run

_MODES = [pytest.param([], id="merge"), pytest.param(["--report-only"], id="report-only")]


def _decide(result: CiStatusResult) -> None:
    check_ci_status(result, 7, "owner", "repo", head_sha=HEAD_SHA)


class TestDecision:
    def test_green_check_runs_with_a_pending_rollup_of_zero_statuses_pass(self):
        runs = (CheckRun("build", "completed", "success", suite_id=1, run_id=1),)
        _decide(CiStatusResult(combined_state="pending", statuses=(), check_runs=runs))

    def test_an_in_progress_check_run_does_not_pass_and_is_named(self):
        runs = (
            CheckRun("lint", "completed", "success", suite_id=1, run_id=1),
            CheckRun("build", "in_progress", "", suite_id=1, run_id=2),
        )
        result = CiStatusResult(combined_state="pending", statuses=(), check_runs=runs)
        with pytest.raises(CiStatusFailedError, match="check 'build' is still pending"):
            _decide(result)

    def test_a_pending_status_with_no_check_runs_does_not_pass(self):
        result = CiStatusResult(
            combined_state="pending",
            statuses=(StatusEntry("ci/build", "pending"),),
            check_runs=(),
        )
        with pytest.raises(CiStatusFailedError, match="combined commit-status state='pending'"):
            _decide(result)

    def test_a_pending_rollup_of_zero_statuses_alone_is_the_no_ci_pass(self):
        _decide(CiStatusResult(combined_state="pending", statuses=(), check_runs=()))

    def test_unfinished_names_are_listed_once_in_first_seen_order(self):
        runs = (
            CheckRun("b", "queued", "", suite_id=1, run_id=1),
            CheckRun("a", "in_progress", "", suite_id=1, run_id=2),
            CheckRun("b", "queued", "", suite_id=2, run_id=3),
            CheckRun("c", "completed", "success", suite_id=1, run_id=4),
        )
        result = CiStatusResult(combined_state="", statuses=(), check_runs=runs)
        assert result.unfinished_check_names == ("b", "a")


@pytest.mark.parametrize("extra", _MODES)
class TestGithubCallerShapes:
    def test_green_check_runs_with_pending_rollup_pass(self, extra, capsys):
        merge_calls: list = []
        opener = github_ci_opener(
            state="pending",
            check_runs=[check_run("build", run_id=1, suite_id=10)],
            merge_calls=merge_calls,
        )

        assert _run(opener, extra=extra) == verb.EXIT_OK

        err = capsys.readouterr().err
        assert "ignored: 0 commit statuses" in err
        assert len(merge_calls) == (0 if extra else 1)

    @pytest.mark.parametrize("status", ["in_progress", "queued"])
    def test_unfinished_check_run_refuses_naming_it(self, extra, status, capsys):
        merge_calls: list = []
        opener = github_ci_opener(
            state="pending",
            check_runs=[
                check_run("lint", run_id=1, suite_id=10),
                check_run("build", run_id=2, suite_id=10, status=status),
            ],
            merge_calls=merge_calls,
        )

        assert _run(opener, extra=extra) == verb.EXIT_CI_STATUS_FAILED

        err = capsys.readouterr().err
        assert "check 'build' is still pending" in err
        assert "unfinished check-runs=['build']" in err
        assert merge_calls == []

    def test_pending_status_without_check_runs_refuses(self, extra):
        merge_calls: list = []
        opener = github_ci_opener(
            state="pending", statuses=[status_entry("ci", "pending")], merge_calls=merge_calls
        )
        assert _run(opener, extra=extra) == verb.EXIT_CI_STATUS_FAILED
        assert merge_calls == []

    def test_no_ci_at_all_still_passes(self, extra):
        assert _run(github_ci_opener(state="pending"), extra=extra) == verb.EXIT_OK


@pytest.mark.parametrize("extra", _MODES)
class TestForgejoCallerShapes:
    def test_pending_status_refuses(self, extra):
        merge_calls: list = []
        opener = forgejo_ci_opener(
            state="pending",
            statuses=[status_entry("build / test", "pending")],
            merge_calls=merge_calls,
        )
        assert _run(opener, platform="forgejo", extra=extra) == verb.EXIT_CI_STATUS_FAILED
        assert merge_calls == []

    def test_pending_rollup_of_zero_statuses_is_the_no_ci_pass(self, extra):
        opener = forgejo_ci_opener(state="pending")
        assert _run(opener, platform="forgejo", extra=extra) == verb.EXIT_OK
