"""test_merge_merge_readback.py — unit coverage for
clagentic_loadout.merge.merge_readback (lr-361de3).

Direct, backend-agnostic coverage of the two predicates merge.verb /
merge.close_verb call through a bound get_pr_info callable:
  - verify_merge_landed: merged==True AND a non-empty merge_commit_sha.
  - verify_pr_closed: state=="closed".

Both return a transport.readback_envelope.Readback -- never raise for an
ordinary "did not confirm" outcome (only a genuine GateFactUnavailableError
from the underlying read folds into verified=False,
source=READBACK_SOURCE_READ_UNAVAILABLE).
"""

from __future__ import annotations

from clagentic_loadout.merge.errors import GateFactUnavailableError
from clagentic_loadout.merge.merge_readback import verify_merge_landed, verify_pr_closed
from clagentic_loadout.transport.readback_envelope import (
    READBACK_SOURCE_API_GET,
    READBACK_SOURCE_READ_UNAVAILABLE,
    READBACK_SOURCE_VERIFY_FAILED,
)

_MERGED_SHA = "e" * 40


class TestVerifyMergeLanded:
    def test_merged_true_with_sha_is_verified(self):
        result = verify_merge_landed(
            lambda: {"merged": True, "merge_commit_sha": _MERGED_SHA}
        )
        assert result.verified is True
        assert result.source == READBACK_SOURCE_API_GET
        assert result.detail["merged_commit_sha"] == _MERGED_SHA

    def test_merged_false_is_not_verified(self):
        """NON-VACUITY: the mutation did NOT land (merged: False) -- the
        predicate must FAIL. This is a direct assertion of the acceptance
        criterion 4 shape: a case where the mutation did not land fails the
        readback."""
        result = verify_merge_landed(
            lambda: {"merged": False, "merge_commit_sha": ""}
        )
        assert result.verified is False
        assert result.source == READBACK_SOURCE_VERIFY_FAILED

    def test_merged_true_but_no_sha_is_not_verified(self):
        """merged=True alone is insufficient -- a non-empty merge_commit_sha
        is also required (the predicate's own stated shape, seq 2 item (b))."""
        result = verify_merge_landed(lambda: {"merged": True, "merge_commit_sha": ""})
        assert result.verified is False
        assert result.source == READBACK_SOURCE_VERIFY_FAILED

    def test_missing_merged_field_is_not_verified(self):
        result = verify_merge_landed(lambda: {})
        assert result.verified is False
        assert result.source == READBACK_SOURCE_VERIFY_FAILED

    def test_read_failure_is_read_unavailable_not_verify_failed(self):
        """A GENUINE inability to even perform the read (e.g. the underlying
        GET failed/timed out) is a DIFFERENT source than an ordinary
        did-not-confirm outcome -- a caller must be able to tell "we don't
        know" apart from "we checked and it didn't land"."""

        def _raise():
            raise GateFactUnavailableError("cannot read PR: HTTP 503")

        result = verify_merge_landed(_raise)
        assert result.verified is False
        assert result.source == READBACK_SOURCE_READ_UNAVAILABLE


class TestVerifyPrClosed:
    def test_state_closed_is_verified(self):
        result = verify_pr_closed(lambda: {"state": "closed"})
        assert result.verified is True
        assert result.source == READBACK_SOURCE_API_GET
        assert result.detail["state"] == "closed"

    def test_state_open_is_not_verified(self):
        """NON-VACUITY: the close did NOT land (state still 'open') -- the
        predicate must FAIL."""
        result = verify_pr_closed(lambda: {"state": "open"})
        assert result.verified is False
        assert result.source == READBACK_SOURCE_VERIFY_FAILED

    def test_missing_state_field_is_not_verified(self):
        result = verify_pr_closed(lambda: {})
        assert result.verified is False
        assert result.source == READBACK_SOURCE_VERIFY_FAILED

    def test_read_failure_is_read_unavailable(self):
        def _raise():
            raise GateFactUnavailableError("cannot read PR: network error")

        result = verify_pr_closed(_raise)
        assert result.verified is False
        assert result.source == READBACK_SOURCE_READ_UNAVAILABLE
