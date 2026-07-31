"""test_merge_stale_sha.py — tests for clagentic_loadout.merge.stale_sha
(lr-885f, Wave B slice 4).

Coverage:
  - No-op when expected_head_sha is empty (never invents a SHA).
  - Match (identical, and abbreviated-prefix match via sha.compare_sha_values)
    passes silently.
  - Mismatch raises StaleHeadShaError, naming both SHAs.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.errors import StaleHeadShaError
from clagentic_loadout.merge.stale_sha import check_stale_head_sha

_FULL_SHA = "a" * 40
_OTHER_FULL_SHA = "b" * 40


class TestCheckStaleHeadSha:
    def test_noop_when_expected_empty(self):
        check_stale_head_sha("", _OTHER_FULL_SHA, 1, "some-owner", "some-repo")  # no raise

    def test_passes_on_exact_match(self):
        check_stale_head_sha(_FULL_SHA, _FULL_SHA, 1, "some-owner", "some-repo")  # no raise

    def test_passes_on_abbreviated_prefix_match(self):
        check_stale_head_sha(_FULL_SHA[:7], _FULL_SHA, 1, "some-owner", "some-repo")  # no raise

    def test_raises_on_mismatch(self):
        with pytest.raises(StaleHeadShaError) as exc_info:
            check_stale_head_sha(_FULL_SHA, _OTHER_FULL_SHA, 42, "some-owner", "some-repo")
        msg = str(exc_info.value)
        assert _FULL_SHA in msg
        assert _OTHER_FULL_SHA in msg
        assert "42" in msg
