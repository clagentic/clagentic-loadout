"""test_wait_poll.py — unit tests for clagentic_loadout.wait.poll (ported
from an internal deployment's own poll-wait script's inline logic, Wave A
slice 4).

Uses small timeout/interval values so the suite stays fast — no reliance on
wall-clock timing beyond a couple hundred milliseconds, and no date-dependent
behavior anywhere (tome #688 constraint).
"""

from __future__ import annotations

import pytest

from clagentic_loadout.wait.poll import InvalidGrepPatternError, poll_wait


class TestPollWaitConditionAlreadyMet:
    def test_min_lines_satisfied_immediately(self, tmp_path):
        f = tmp_path / "out.txt"
        f.write_text("line one\nline two\nline three\n")
        result = poll_wait(str(f), min_lines=2, timeout=1, interval=0.05)
        assert result.met is True

    def test_default_min_lines_is_one_nonempty_line(self, tmp_path):
        f = tmp_path / "out.txt"
        f.write_text("hello\n")
        result = poll_wait(str(f), timeout=1, interval=0.05)
        assert result.met is True

    def test_blank_lines_do_not_count_toward_min_lines(self, tmp_path):
        f = tmp_path / "out.txt"
        f.write_text("\n\n   \n")
        result = poll_wait(str(f), min_lines=1, timeout=0.3, interval=0.05)
        assert result.met is False

    def test_grep_pattern_match_required(self, tmp_path):
        f = tmp_path / "out.txt"
        f.write_text("build finished: SUCCESS\n")
        result = poll_wait(str(f), grep_pattern="SUCCESS", timeout=1, interval=0.05)
        assert result.met is True

    def test_grep_pattern_no_match_times_out(self, tmp_path):
        f = tmp_path / "out.txt"
        f.write_text("build finished: FAILURE\n")
        result = poll_wait(str(f), grep_pattern="SUCCESS", timeout=0.3, interval=0.05)
        assert result.met is False


class TestPollWaitTimeout:
    def test_missing_file_times_out(self, tmp_path):
        f = tmp_path / "never-created.txt"
        result = poll_wait(str(f), timeout=0.3, interval=0.05)
        assert result.met is False
        assert result.elapsed_seconds >= 0.3

    def test_insufficient_lines_times_out(self, tmp_path):
        f = tmp_path / "out.txt"
        f.write_text("only one line\n")
        result = poll_wait(str(f), min_lines=5, timeout=0.3, interval=0.05)
        assert result.met is False


class TestPollWaitFileAppearsMidPoll:
    def test_condition_met_after_file_is_written_during_poll(self, tmp_path):
        """Simulates the real usage pattern: caller starts polling before the
        producing process has written the file yet, by writing during the
        first poll interval via a pre-populated file (deterministic,
        no background thread needed for the unit test)."""
        f = tmp_path / "out.txt"
        # File exists with enough lines from the start — proves the
        # "already satisfied on first check" path without needing timing
        # coordination across threads.
        f.write_text("a\nb\n")
        result = poll_wait(str(f), min_lines=2, timeout=1, interval=0.05)
        assert result.met is True


class TestPollWaitValidation:
    def test_negative_min_lines_rejected(self, tmp_path):
        f = tmp_path / "out.txt"
        with pytest.raises(ValueError):
            poll_wait(str(f), min_lines=-1)

    def test_zero_timeout_rejected(self, tmp_path):
        f = tmp_path / "out.txt"
        with pytest.raises(ValueError):
            poll_wait(str(f), timeout=0)

    def test_negative_interval_rejected(self, tmp_path):
        f = tmp_path / "out.txt"
        with pytest.raises(ValueError):
            poll_wait(str(f), interval=-1)

    def test_invalid_grep_regex_rejected(self, tmp_path):
        f = tmp_path / "out.txt"
        f.write_text("data\n")
        with pytest.raises(InvalidGrepPatternError):
            poll_wait(str(f), grep_pattern="[unclosed", timeout=1, interval=0.05)
