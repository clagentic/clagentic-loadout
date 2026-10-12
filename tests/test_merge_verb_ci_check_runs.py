"""test_merge_verb_ci_check_runs.py — the merge verb's CI gate over every caller
shape: GitHub check-runs, legacy statuses, both, none, pending, the duplicate
check-name shape, and Forgejo Actions commit statuses. Required check names and
strict mode come from the tracked gate file at the PR BASE commit (or the
caller's flags); the PR head cannot relax them.

No real network call: the hosts are URL-routing doubles serving payloads shaped
like the real API responses (tests/_support/ci_gate.py)."""

from __future__ import annotations

import json

import pytest
import yaml

from clagentic_loadout.merge import verb
from clagentic_loadout.repo_config import TRACKED_GATE_RELATIVE_PATH
from tests._gate_repo import init_gate_repo
from tests._support.ci_gate import (
    HEAD_SHA,
    check_run,
    forgejo_ci_opener,
    github_ci_opener,
    status_entry,
)
from tests._support.ci_gate import run_merge as _run


def _result_line(out: str) -> dict:
    return json.loads([ln for ln in out.splitlines() if ln.startswith("{")][-1])


def _gate_repo(tmp_path, gate: dict, **kwargs):
    return init_gate_repo(tmp_path, tracked_gate={"required_reviewer_roles": [], **gate}, **kwargs)


# The console shape: the pull_request workflow's suite (101) skipped its test job
# and its gate job fails on purpose; the other suite (100) executed test and its
# gate job succeeded.
_DUPLICATE_RUNS = [
    check_run("test", run_id=1, suite_id=100, conclusion="success"),
    check_run("test-gate", run_id=2, suite_id=100, conclusion="success"),
    check_run("test", run_id=3, suite_id=101, conclusion="skipped"),
    check_run("test-gate", run_id=4, suite_id=101, conclusion="failure"),
]


class TestGithubCheckRuns:
    def test_failed_required_check_run_refuses_naming_check_and_head(self, capsys):
        merge_calls: list = []
        opener = github_ci_opener(
            check_runs=[
                check_run("test", run_id=1, suite_id=10, conclusion="failure"),
                check_run("test-gate", run_id=2, suite_id=10, conclusion="failure"),
            ],
            merge_calls=merge_calls,
        )

        assert _run(opener) == verb.EXIT_CI_STATUS_FAILED

        err = capsys.readouterr().err
        assert "check 'test' concluded 'failure'" in err
        assert "check 'test-gate' concluded 'failure'" in err
        assert HEAD_SHA in err
        assert merge_calls == []

    @pytest.mark.parametrize("conclusion", ["cancelled", "timed_out", "action_required"])
    def test_other_bad_conclusions_refuse(self, conclusion):
        merge_calls: list = []
        opener = github_ci_opener(
            check_runs=[check_run("test", run_id=1, suite_id=10, conclusion=conclusion)],
            merge_calls=merge_calls,
        )
        assert _run(opener) == verb.EXIT_CI_STATUS_FAILED
        assert merge_calls == []

    def test_every_check_run_green_merges_and_records_source(self, capsys):
        merge_calls: list = []
        opener = github_ci_opener(
            check_runs=[
                check_run("test", run_id=1, suite_id=10),
                check_run("test-gate", run_id=2, suite_id=10),
            ],
            merge_calls=merge_calls,
        )

        assert _run(opener) == verb.EXIT_OK

        assert len(merge_calls) == 1
        assert _result_line(capsys.readouterr().out)["ci_source"] == "check-runs"

    @pytest.mark.parametrize("status", ["in_progress", "queued"])
    def test_check_still_running_refuses_as_pending(self, status, capsys):
        merge_calls: list = []
        opener = github_ci_opener(
            check_runs=[
                check_run("lint", run_id=1, suite_id=10),
                check_run("test", run_id=2, suite_id=10, status=status),
            ],
            merge_calls=merge_calls,
        )

        assert _run(opener) == verb.EXIT_CI_STATUS_FAILED

        assert "check 'test' is still pending" in capsys.readouterr().err
        assert merge_calls == []

    def test_a_rerun_in_the_same_suite_replaces_the_failed_attempt(self):
        opener = github_ci_opener(
            check_runs=[
                check_run("test", run_id=1, suite_id=10, conclusion="failure"),
                check_run("test", run_id=2, suite_id=10, conclusion="success"),
            ]
        )
        assert _run(opener) == verb.EXIT_OK

    def test_a_failing_latest_suite_refuses_despite_an_older_success(self):
        opener = github_ci_opener(
            check_runs=[
                check_run("test", run_id=1, suite_id=10, conclusion="success"),
                check_run("test", run_id=2, suite_id=11, conclusion="failure"),
            ]
        )
        assert _run(opener) == verb.EXIT_CI_STATUS_FAILED

    def test_a_failure_past_the_first_page_is_still_found(self):
        runs = [check_run(f"job-{i}", run_id=i + 1, suite_id=10) for i in range(33)]
        runs.append(check_run("late-job", run_id=500, suite_id=10, conclusion="failure"))
        merge_calls: list = []
        opener = github_ci_opener(check_runs=runs, check_runs_page_size=30, merge_calls=merge_calls)

        assert _run(opener) == verb.EXIT_CI_STATUS_FAILED
        assert merge_calls == []


class TestGithubStatusesAndNone:
    def test_legacy_statuses_only_green_merges_with_statuses_source(self, capsys):
        opener = github_ci_opener(state="success", statuses=[status_entry("ci", "success")])
        assert _run(opener) == verb.EXIT_OK
        assert _result_line(capsys.readouterr().out)["ci_source"] == "statuses"

    def test_legacy_statuses_only_red_refuses(self):
        merge_calls: list = []
        opener = github_ci_opener(
            state="failure", statuses=[status_entry("ci", "failure")], merge_calls=merge_calls
        )
        assert _run(opener) == verb.EXIT_CI_STATUS_FAILED
        assert merge_calls == []

    def test_both_sources_green_reports_both(self, capsys):
        opener = github_ci_opener(
            state="success",
            statuses=[status_entry("ci", "success")],
            check_runs=[check_run("test", run_id=1, suite_id=10)],
        )
        assert _run(opener) == verb.EXIT_OK
        assert _result_line(capsys.readouterr().out)["ci_source"] == "check-runs+statuses"

    def test_red_check_run_refuses_even_when_the_legacy_status_is_green(self):
        opener = github_ci_opener(
            state="success",
            statuses=[status_entry("ci", "success")],
            check_runs=[check_run("test", run_id=1, suite_id=10, conclusion="failure")],
        )
        assert _run(opener) == verb.EXIT_CI_STATUS_FAILED

    def test_no_ci_at_all_still_passes_and_says_so(self, capsys):
        assert _run(github_ci_opener()) == verb.EXIT_OK
        captured = capsys.readouterr()
        assert "no CI found at head" in captured.err
        assert _result_line(captured.out)["ci_source"] == "none"

    def test_strict_flag_refuses_when_no_ci_is_found(self, capsys):
        merge_calls: list = []
        opener = github_ci_opener(merge_calls=merge_calls)

        assert _run(opener, extra=["--ci-strict"]) == verb.EXIT_CI_STATUS_FAILED

        assert "no CI found at head" in capsys.readouterr().err
        assert merge_calls == []


class TestDuplicateCheckNameShape:
    def test_default_mode_refuses_the_failing_duplicate_naming_it(self, capsys):
        assert _run(github_ci_opener(check_runs=_DUPLICATE_RUNS)) == verb.EXIT_CI_STATUS_FAILED
        assert "check 'test-gate' concluded 'failure'" in capsys.readouterr().err

    def test_required_list_naming_test_gates_on_that_check_only(self):
        merge_calls: list = []
        opener = github_ci_opener(check_runs=_DUPLICATE_RUNS, merge_calls=merge_calls)

        assert _run(opener, extra=["--required-ci-check", "test"]) == verb.EXIT_OK
        assert len(merge_calls) == 1

    def test_required_list_needs_an_executed_success_not_a_skip(self):
        runs = [
            check_run("test", run_id=3, suite_id=101, conclusion="skipped"),
            check_run("test-gate", run_id=4, suite_id=101, conclusion="failure"),
        ]
        assert (
            _run(github_ci_opener(check_runs=runs), extra=["--required-ci-check", "test"])
            == verb.EXIT_CI_STATUS_FAILED
        )

    def test_required_name_never_reported_refuses(self, capsys):
        opener = github_ci_opener(check_runs=[check_run("lint", run_id=1, suite_id=10)])

        assert _run(opener, extra=["--required-ci-check", "test"]) == verb.EXIT_CI_STATUS_FAILED

        assert "required check 'test' was not reported at head" in capsys.readouterr().err

    def test_required_name_with_no_ci_at_all_refuses(self):
        assert (
            _run(github_ci_opener(), extra=["--required-ci-check", "test"])
            == verb.EXIT_CI_STATUS_FAILED
        )


class TestRequiredChecksComeFromTheBaseCommit:
    def test_base_gate_list_applies(self, tmp_path):
        repo = _gate_repo(tmp_path, {"required_ci_checks": ["test"]})
        merge_calls: list = []
        opener = github_ci_opener(
            pr_info=repo.pr_info(), check_runs=_DUPLICATE_RUNS, merge_calls=merge_calls
        )

        assert _run(opener, repo=repo) == verb.EXIT_OK
        assert len(merge_calls) == 1

    def test_pr_head_dropping_the_required_check_does_not_relax_the_base_list(self, tmp_path):
        relaxed = yaml.safe_dump(
            {"merge": {"required_reviewer_roles": [], "required_ci_checks": ["lint"]}}
        )
        repo = _gate_repo(
            tmp_path,
            {"required_ci_checks": ["test"]},
            head_files={TRACKED_GATE_RELATIVE_PATH: relaxed},
        )
        merge_calls: list = []
        opener = github_ci_opener(
            pr_info=repo.pr_info(),
            check_runs=[
                check_run("lint", run_id=1, suite_id=10),
                check_run("test", run_id=2, suite_id=10, conclusion="failure"),
            ],
            merge_calls=merge_calls,
        )

        assert _run(opener, repo=repo) == verb.EXIT_CI_STATUS_FAILED
        assert merge_calls == []

    def test_base_strict_setting_refuses_a_head_with_no_ci(self, tmp_path, capsys):
        repo = _gate_repo(tmp_path, {"ci_strict": True})
        opener = github_ci_opener(pr_info=repo.pr_info())

        assert _run(opener, repo=repo) == verb.EXIT_CI_STATUS_FAILED
        assert "no CI found at head" in capsys.readouterr().err

    def test_a_malformed_ci_declaration_falls_back_to_the_default_gate_with_a_warning(
        self, tmp_path, capsys
    ):
        repo = _gate_repo(tmp_path, {"required_ci_checks": "test"})
        merge_calls: list = []
        opener = github_ci_opener(
            pr_info=repo.pr_info(),
            check_runs=[check_run("test", run_id=1, suite_id=10)],
            merge_calls=merge_calls,
        )

        assert _run(opener, repo=repo) == verb.EXIT_OK

        err = capsys.readouterr().err
        assert "merge.required_ci_checks NOT ENFORCED" in err
        assert "required_ci_checks must be a list of check names" in err
        assert len(merge_calls) == 1

    def test_a_malformed_ci_declaration_still_gates_every_check_run(self, tmp_path):
        repo = _gate_repo(tmp_path, {"ci_strict": "yes"})
        merge_calls: list = []
        opener = github_ci_opener(
            pr_info=repo.pr_info(),
            check_runs=[check_run("test", run_id=1, suite_id=10, conclusion="failure")],
            merge_calls=merge_calls,
        )

        assert _run(opener, repo=repo) == verb.EXIT_CI_STATUS_FAILED
        assert merge_calls == []

    def test_a_malformed_base_ci_strict_runs_strict_and_warns(self, tmp_path, capsys):
        repo = _gate_repo(tmp_path, {"ci_strict": "yes"})
        merge_calls: list = []
        opener = github_ci_opener(pr_info=repo.pr_info(), merge_calls=merge_calls)

        assert _run(opener, repo=repo) == verb.EXIT_CI_STATUS_FAILED

        err = capsys.readouterr().err
        assert "merge.ci_strict is malformed, so strict CI applies" in err
        assert "no CI found at head" in err
        assert merge_calls == []

    def test_a_malformed_base_ci_strict_still_merges_a_green_head(self, tmp_path):
        repo = _gate_repo(tmp_path, {"ci_strict": "yes"})
        opener = github_ci_opener(
            pr_info=repo.pr_info(), check_runs=[check_run("test", run_id=1, suite_id=10)]
        )
        assert _run(opener, repo=repo) == verb.EXIT_OK

    def test_no_ci_keys_at_base_keeps_the_default_behaviour(self, tmp_path):
        repo = _gate_repo(tmp_path, {})
        assert _run(github_ci_opener(pr_info=repo.pr_info()), repo=repo) == verb.EXIT_OK


class TestForgejoActionsStatuses:
    def test_green_statuses_merge_with_statuses_source(self, capsys):
        opener = forgejo_ci_opener(state="success", statuses=[status_entry("build / test", "success")])
        assert _run(opener, platform="forgejo") == verb.EXIT_OK
        assert _result_line(capsys.readouterr().out)["ci_source"] == "statuses"

    def test_red_status_refuses(self):
        merge_calls: list = []
        opener = forgejo_ci_opener(
            state="failure",
            statuses=[status_entry("build / test", "failure")],
            merge_calls=merge_calls,
        )
        assert _run(opener, platform="forgejo") == verb.EXIT_CI_STATUS_FAILED
        assert merge_calls == []

    def test_pending_status_refuses(self):
        opener = forgejo_ci_opener(state="pending", statuses=[status_entry("build / test", "pending")])
        assert _run(opener, platform="forgejo") == verb.EXIT_CI_STATUS_FAILED

    def test_no_runner_repo_passes_and_says_so(self, capsys):
        assert _run(forgejo_ci_opener(), platform="forgejo") == verb.EXIT_OK
        captured = capsys.readouterr()
        assert "no CI found at head" in captured.err
        assert _result_line(captured.out)["ci_source"] == "none"

    def test_no_runner_repo_refuses_under_strict(self):
        assert (
            _run(forgejo_ci_opener(), platform="forgejo", extra=["--ci-strict"])
            == verb.EXIT_CI_STATUS_FAILED
        )

    def test_required_context_is_matched_against_status_contexts(self):
        opener = forgejo_ci_opener(
            state="failure",
            statuses=[
                status_entry("build / test", "success"),
                status_entry("build / lint", "failure"),
            ],
        )
        extra = ["--required-ci-check", "build / test"]

        assert _run(opener, platform="forgejo", extra=extra) == verb.EXIT_OK

    def test_required_context_failing_refuses_naming_it(self, capsys):
        opener = forgejo_ci_opener(
            state="failure", statuses=[status_entry("build / test", "failure")]
        )
        extra = ["--required-ci-check", "build / test"]

        assert _run(opener, platform="forgejo", extra=extra) == verb.EXIT_CI_STATUS_FAILED
        assert "status 'build / test' reported 'failure'" in capsys.readouterr().err
