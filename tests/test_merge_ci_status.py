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
  - is_empty is driven ONLY off the HEAD-scoped statuses and check-runs
    (lr-2d2293); no repo-global count is carried, so the mirror-runner shape
    (zero statuses at head) stays empty and passes.
  - CiStatusResult has no defaults: a no-CI fact can only be built by naming
    every field, so an omitted field is a TypeError, never an empty pass.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.ci_status import CiStatusResult, StatusEntry, check_ci_status
from clagentic_loadout.merge.errors import CiStatusFailedError


def _result(state: str = "", statuses: int = 0) -> CiStatusResult:
    entries = tuple(StatusEntry(context=f"ci/{i}", state=state) for i in range(statuses))
    return CiStatusResult(combined_state=state, statuses=entries, check_runs=())


class TestIsEmpty:
    def test_zero_statuses_and_zero_runs_is_empty(self):
        assert _result().is_empty is True

    def test_a_status_entry_is_not_empty(self):
        result = _result("success", statuses=1)
        assert result.is_empty is False
        assert result.status_count == 1

    def test_constructing_without_arguments_raises(self):
        with pytest.raises(TypeError):
            CiStatusResult()  # type: ignore[call-arg]

    @pytest.mark.parametrize("missing", ["combined_state", "statuses", "check_runs"])
    def test_every_gate_read_field_is_required(self, missing):
        fields = {"combined_state": "", "statuses": (), "check_runs": ()}
        del fields[missing]
        with pytest.raises(TypeError, match=missing):
            CiStatusResult(**fields)  # type: ignore[arg-type]


class TestCheckCiStatusEmptyPasses:
    """The no-runner-by-design case: zero HEAD-scoped commit statuses must
    PASS, not refuse. This is the exact shape this repo's own PRs hit (no
    runners wired up by design, lr-368c)."""

    def test_empty_result_passes(self):
        check_ci_status(_result(), 49, "clagentic", "clagentic-loadout")  # no raise

    def test_mirror_runner_negative_control_zero_status_passes(self):
        # lr-2d2293: a repo with a mirror runner but NO CI runner has zero
        # commit statuses at HEAD. A repo-global task count (Forgejo
        # /actions/tasks) is not carried at all, so it cannot make this look
        # non-empty -- the live false-refusal shape from session d5aee241.
        check_ci_status(_result(), 49, "clagentic", "clagentic-loadout")  # no raise


class TestCheckCiStatusSuccessPasses:
    def test_success_state_passes(self):
        check_ci_status(_result("success", statuses=1), 1, "owner", "repo")  # no raise


class TestCheckCiStatusNonEmptyFailingRefuses:
    """Negative controls: a runner-wired repo with a REAL red or pending
    state must still refuse — pass-on-empty must never mask a real red."""

    @pytest.mark.parametrize("state", ["failure", "error", "cancelled", "timed_out"])
    def test_failure_states_refuse(self, state):
        with pytest.raises(CiStatusFailedError) as exc_info:
            check_ci_status(_result(state, statuses=2), 7, "owner", "repo")
        assert state in str(exc_info.value)
        assert "PR #7" in str(exc_info.value)

    @pytest.mark.parametrize("state", ["pending", "in_progress", "queued", "waiting"])
    def test_non_terminal_states_refuse(self, state):
        with pytest.raises(CiStatusFailedError) as exc_info:
            check_ci_status(_result(state, statuses=1), 7, "owner", "repo")
        assert state in str(exc_info.value)

    def test_unrecognized_nonempty_state_refuses(self):
        with pytest.raises(CiStatusFailedError) as exc_info:
            check_ci_status(_result("something-weird", statuses=1), 7, "owner", "repo")
        assert "something-weird" in str(exc_info.value)

    def test_nonempty_status_count_with_no_combined_state_string_refuses(self):
        # HEAD-scoped evidence exists (a real commit-status entry was seen)
        # but the combined state string itself is empty -- never silently
        # treated as a pass just because the label is blank. Distinct from
        # the mirror-runner case (zero statuses) which must pass.
        result = CiStatusResult(
            combined_state="",
            statuses=(StatusEntry(context="ci/0", state="success"),),
            check_runs=(),
        )
        with pytest.raises(CiStatusFailedError):
            check_ci_status(result, 7, "owner", "repo")

    def test_real_ci_positive_case_failure_at_head_refuses(self):
        # tome #688 real-CI positive control: a repo where CI IS wired up
        # (HEAD-scoped commit statuses present) and the check genuinely
        # failed must still refuse -- the HEAD-scoping fix must not weaken
        # the gate for repos that DO have CI.
        with pytest.raises(CiStatusFailedError) as exc_info:
            check_ci_status(_result("failure", statuses=1), 7, "owner", "repo")
        assert "failure" in str(exc_info.value)


class TestErrorMessageReportsResolvedValues:
    def test_message_names_counts_and_state(self):
        with pytest.raises(CiStatusFailedError) as exc_info:
            check_ci_status(_result("failure", statuses=3), 42, "some-owner", "some-repo")
        msg = str(exc_info.value)
        assert "3" in msg
        assert "failure" in msg
        assert "some-owner/some-repo" in msg
