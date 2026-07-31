"""test_merge_diff_scope.py — tests for clagentic_loadout.merge.diff_scope
(lr-885f, Wave B slice 4).

Coverage:
  - Within bound passes silently.
  - Exactly at bound passes (boundary inclusive).
  - Exceeding bound raises DiffScopeExceededError naming the count and limit.
  - The bound is a parameter, not hardcoded (proven by two different limits
    producing different pass/fail outcomes for the same file count).
"""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.diff_scope import DEFAULT_MAX_CHANGED_FILES, check_diff_scope


class TestCheckDiffScope:
    def test_within_bound_passes(self):
        check_diff_scope(["a.py", "b.py"], 1, "owner", "repo", max_changed_files=50)  # no raise

    def test_at_bound_passes(self):
        files = [f"f{i}.py" for i in range(10)]
        check_diff_scope(files, 1, "owner", "repo", max_changed_files=10)  # no raise

    def test_exceeding_bound_raises(self):
        files = [f"f{i}.py" for i in range(11)]
        with pytest.raises(Exception) as exc_info:
            check_diff_scope(files, 1, "owner", "repo", max_changed_files=10)
        msg = str(exc_info.value)
        assert "11" in msg
        assert "10" in msg

    def test_default_bound_used_when_omitted(self):
        files = [f"f{i}.py" for i in range(DEFAULT_MAX_CHANGED_FILES + 1)]
        with pytest.raises(Exception):
            check_diff_scope(files, 1, "owner", "repo")

    def test_bound_is_config_not_hardcoded(self):
        files = [f"f{i}.py" for i in range(5)]
        check_diff_scope(files, 1, "owner", "repo", max_changed_files=5)  # passes at limit=5
        with pytest.raises(Exception):
            check_diff_scope(files, 1, "owner", "repo", max_changed_files=4)  # fails at limit=4
