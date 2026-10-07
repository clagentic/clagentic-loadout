"""The shared finite-positive-number check, and a sweep guard: every duration
validator in the package must reject NaN and infinity."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.post_merge import PostMergeConfigError, validate_post_merge_steps
from clagentic_loadout.numeric_validation import is_finite_number, is_finite_positive_number
from clagentic_loadout.wait.poll import poll_wait

NON_FINITE = [float("nan"), float("inf"), float("-inf")]


@pytest.mark.parametrize("value", [1, 0.5, 600.0])
def test_accepts_finite_positive(value):
    assert is_finite_positive_number(value)


@pytest.mark.parametrize("value", [0, -1, True, False, "5", None, *NON_FINITE])
def test_rejects_everything_else(value):
    assert not is_finite_positive_number(value)


HUGE_INT = 10**400


def test_huge_int_is_rejected_not_raised():
    assert not is_finite_positive_number(HUGE_INT)
    assert not is_finite_positive_number(-HUGE_INT)
    assert not is_finite_number(HUGE_INT)


def test_repo_bound_passes_huge_int_through_to_validation():
    from clagentic_loadout.review.profile_config import _bound_repo_value

    assert _bound_repo_value("timeout_seconds", HUGE_INT, "p") == HUGE_INT


@pytest.mark.parametrize("bad", NON_FINITE)
def test_poll_wait_rejects_non_finite_durations(tmp_path, bad):
    target = str(tmp_path / "f")
    with pytest.raises(ValueError):
        poll_wait(target, timeout=bad)
    with pytest.raises(ValueError):
        poll_wait(target, interval=bad)


@pytest.mark.parametrize("bad", NON_FINITE)
def test_post_merge_step_timeout_rejects_non_finite(bad):
    with pytest.raises(PostMergeConfigError):
        validate_post_merge_steps([{"cmd": "true", "timeout_seconds": bad}])


@pytest.mark.parametrize("bad", NON_FINITE)
def test_post_merge_liveness_poll_interval_rejects_non_finite(bad):
    step = {
        "cmd": "true",
        "liveness_probe": {"cmd": "true", "poll_interval_seconds": bad},
    }
    with pytest.raises(PostMergeConfigError):
        validate_post_merge_steps([step])
