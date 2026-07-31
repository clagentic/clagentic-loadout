"""test_merge_ci_status.py — tests for clagentic_loadout.merge.ci_status
(lr-afba CI-status-gate slice, comment #6; HEAD-scoping fix lr-2d2293).

Coverage:
  - Empty CI evidence (zero HEAD-scoped commit statuses) PASSES — the
    no-runner-by-design case this repo itself hits (PR #49 @ head 6a8fcbd:
    empty combined state, zero commit-status entries).
  - Non-empty + combined_state="success" PASSES.
  - Non-empty + a failure/error/pending state REFUSES
    (CiStatusFailedError), naming the actual state observed.
  - A non-empty result with NO combined_state string (evidence present but
    no rollup state reported) still refuses — never silently treated as a
    pass just because the state string itself is empty.
  - is_empty is driven ONLY off status_count (lr-2d2293) — a non-zero
    run_count (diagnostic-only, possibly repo-global e.g. Forgejo mirror-
    sync/historical Actions tasks) never makes a HEAD with zero commit
    statuses look non-empty. This is the mirror-runner negative control:
    the exact false-refusal shape from session d5aee241 must PASS.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.ci_status import CiStatusResult, check_ci_status
from clagentic_loadout.merge.errors import CiStatusFailedError


class TestIsEmpty:
    def test_zero_statuses_and_zero_runs_is_empty(self):
        assert CiStatusResult(status_count=0, run_count=0).is_empty is True

    def test_nonzero_status_count_is_not_empty(self):
        assert CiStatusResult(status_count=1, run_count=0).is_empty is False

    def test_nonzero_run_count_alone_is_still_empty(self):
        # lr-2d2293: run_count is diagnostic-only (possibly repo-global,
        # e.g. Forgejo mirror-sync/historical Actions tasks) and must NEVER
        # override a zero HEAD-scoped status_count. This is the exact
        # mirror-runner false-refusal shape from session d5aee241.
        assert CiStatusResult(status_count=0, run_count=1).is_empty is True


class TestCheckCiStatusEmptyPasses:
    """The no-runner-by-design case: zero HEAD-scoped commit statuses must
    PASS, not refuse. This is the exact shape this repo's own PRs hit (no
    runners wired up by design, lr-368c)."""

    def test_empty_result_passes(self):
        result = CiStatusResult(combined_state="", status_count=0, run_count=0)
        check_ci_status(result, 49, "clagentic", "clagentic-loadout")  # no raise

    def test_empty_combined_state_string_with_zero_counts_passes(self):
        # Mirrors the real PR #49 @ 6a8fcbd observation: Forgejo's combined
        # status endpoint returned an empty state, zero commit statuses.
        result = CiStatusResult(combined_state="", status_count=0, run_count=0, raw_states=())
        check_ci_status(result, 49, "clagentic", "clagentic-loadout")  # no raise

    def test_mirror_runner_negative_control_zero_status_nonzero_repo_global_runs_passes(self):
        # lr-2d2293: a repo with a mirror runner but NO CI runner has zero
        # commit statuses at HEAD (no CI ran -- correct) but a non-zero
        # repo-global run_count (e.g. Forgejo /actions/tasks total_count
        # counting mirror-sync + historical tasks for the whole repo, not
        # this PR's HEAD). This must PASS, not refuse -- the live
        # false-refusal this task fixes (session d5aee241).
        result = CiStatusResult(combined_state="", status_count=0, run_count=7)
        check_ci_status(result, 49, "clagentic", "clagentic-loadout")  # no raise


class TestCheckCiStatusSuccessPasses:
    def test_success_state_passes(self):
        result = CiStatusResult(combined_state="success", status_count=1, run_count=1)
        check_ci_status(result, 1, "owner", "repo")  # no raise


class TestCheckCiStatusNonEmptyFailingRefuses:
    """Negative controls: a runner-wired repo with a REAL red or pending
    state must still refuse — pass-on-empty must never mask a real red."""

    @pytest.mark.parametrize("state", ["failure", "error", "cancelled", "timed_out"])
    def test_failure_states_refuse(self, state):
        result = CiStatusResult(combined_state=state, status_count=2, run_count=2)
        with pytest.raises(CiStatusFailedError) as exc_info:
            check_ci_status(result, 7, "owner", "repo")
        assert state in str(exc_info.value)
        assert "PR #7" in str(exc_info.value)

    @pytest.mark.parametrize("state", ["pending", "in_progress", "queued", "waiting"])
    def test_non_terminal_states_refuse(self, state):
        result = CiStatusResult(combined_state=state, status_count=1, run_count=1)
        with pytest.raises(CiStatusFailedError) as exc_info:
            check_ci_status(result, 7, "owner", "repo")
        assert state in str(exc_info.value)

    def test_unrecognized_nonempty_state_refuses(self):
        result = CiStatusResult(combined_state="something-weird", status_count=1, run_count=0)
        with pytest.raises(CiStatusFailedError) as exc_info:
            check_ci_status(result, 7, "owner", "repo")
        assert "something-weird" in str(exc_info.value)

    def test_nonempty_status_count_with_no_combined_state_string_refuses(self):
        # HEAD-scoped evidence exists (a real commit-status entry was seen)
        # but the combined state string itself is empty -- never silently
        # treated as a pass just because the label is blank. Distinct from
        # the mirror-runner case (status_count=0) which must pass.
        result = CiStatusResult(combined_state="", status_count=1, run_count=0)
        with pytest.raises(CiStatusFailedError):
            check_ci_status(result, 7, "owner", "repo")

    def test_real_ci_positive_case_failure_at_head_refuses(self):
        # tome #688 real-CI positive control: a repo where CI IS wired up
        # (HEAD-scoped commit statuses present) and the check genuinely
        # failed must still refuse -- the HEAD-scoping fix must not weaken
        # the gate for repos that DO have CI.
        result = CiStatusResult(combined_state="failure", status_count=1, run_count=1)
        with pytest.raises(CiStatusFailedError) as exc_info:
            check_ci_status(result, 7, "owner", "repo")
        assert "failure" in str(exc_info.value)


class TestErrorMessageReportsResolvedValues:
    def test_message_names_counts_and_state(self):
        result = CiStatusResult(combined_state="failure", status_count=3, run_count=2)
        with pytest.raises(CiStatusFailedError) as exc_info:
            check_ci_status(result, 42, "some-owner", "some-repo")
        msg = str(exc_info.value)
        assert "3" in msg
        assert "2" in msg
        assert "failure" in msg
        assert "some-owner/some-repo" in msg
