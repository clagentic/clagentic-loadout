"""test_merge_github_ci_status.py — tests for
clagentic_loadout.merge.github_backend.fetch_ci_status (lr-afba
CI-status-gate slice, comment #6). Mirrors
test_merge_forgejo_ci_status.py's coverage shape exactly (GitHub-backend
parity for the CI-status gate-fact fetch).

Coverage:
  - Empty combined state + zero check-runs total_count => a CiStatusResult
    with is_empty True — the same no-runner-by-design shape observed on the
    Forgejo side (PR #49 @ head 6a8fcbd).
  - Non-empty combined state (a real statuses list + a non-zero check-runs
    total_count) => a populated, non-empty CiStatusResult carrying the real
    state.
  - Unreachable / non-200 status endpoint => GateFactUnavailableError
    (fail-closed, mirrors every other gate-fact fetcher in this module).
  - Unreachable / non-200 check-runs endpoint => GateFactUnavailableError.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from clagentic_loadout.merge import github_backend
from clagentic_loadout.merge.errors import GateFactUnavailableError

_OWNER = "some-owner"
_REPO = "some-repo"
_HEAD_SHA = "a" * 40


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json_resp(status: int, payload) -> _FakeResponse:
    return _FakeResponse(status, json.dumps(payload).encode("utf-8"))


def _opener_for(status_response, check_runs_response):
    def opener(req, timeout=30):
        url = req.full_url
        if url.endswith("/status"):
            return status_response
        if url.endswith("/check-runs"):
            return check_runs_response
        raise AssertionError(f"unexpected call: {url}")

    return opener


class TestFetchCiStatusEmpty:
    def test_empty_combined_state_and_zero_check_runs_is_empty(self):
        opener = _opener_for(
            _json_resp(200, {"state": "", "statuses": []}),
            _json_resp(200, {"total_count": 0, "check_runs": []}),
        )
        result = github_backend.fetch_ci_status(
            _OWNER, _REPO, _HEAD_SHA, token="tok", opener=opener
        )
        assert result.is_empty is True
        assert result.combined_state == ""
        assert result.status_count == 0
        assert result.run_count == 0

    def test_missing_state_field_defaults_empty(self):
        opener = _opener_for(
            _json_resp(200, {}),
            _json_resp(200, {"total_count": 0}),
        )
        result = github_backend.fetch_ci_status(
            _OWNER, _REPO, _HEAD_SHA, token="tok", opener=opener
        )
        assert result.is_empty is True


class TestFetchCiStatusNonEmpty:
    def test_success_state_with_statuses_and_runs(self):
        opener = _opener_for(
            _json_resp(
                200,
                {"state": "success", "statuses": [{"state": "success", "context": "ci"}]},
            ),
            _json_resp(200, {"total_count": 1}),
        )
        result = github_backend.fetch_ci_status(
            _OWNER, _REPO, _HEAD_SHA, token="tok", opener=opener
        )
        assert result.is_empty is False
        assert result.combined_state == "success"
        assert result.status_count == 1
        assert result.run_count == 1

    def test_failure_state_reported_verbatim_lowercased(self):
        opener = _opener_for(
            _json_resp(200, {"state": "FAILURE", "statuses": [{"state": "failure"}]}),
            _json_resp(200, {"total_count": 1}),
        )
        result = github_backend.fetch_ci_status(
            _OWNER, _REPO, _HEAD_SHA, token="tok", opener=opener
        )
        assert result.combined_state == "failure"
        assert result.is_empty is False


class TestFetchCiStatusFailClosed:
    def test_non_200_status_endpoint_raises(self):
        opener = _opener_for(
            _FakeResponse(500, b"{}"),
            _json_resp(200, {"total_count": 0}),
        )
        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_ci_status(_OWNER, _REPO, _HEAD_SHA, token="tok", opener=opener)

    def test_network_error_on_status_endpoint_raises(self):
        def opener(req, timeout=30):
            raise urllib.error.URLError("connection refused")

        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_ci_status(_OWNER, _REPO, _HEAD_SHA, token="tok", opener=opener)

    def test_non_200_check_runs_endpoint_raises(self):
        opener = _opener_for(
            _json_resp(200, {"state": "", "statuses": []}),
            _FakeResponse(503, b"{}"),
        )
        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_ci_status(_OWNER, _REPO, _HEAD_SHA, token="tok", opener=opener)

    def test_network_error_on_check_runs_endpoint_raises(self):
        def opener(req, timeout=30):
            if req.full_url.endswith("/status"):
                return _json_resp(200, {"state": "", "statuses": []})
            raise urllib.error.URLError("connection refused")

        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_ci_status(_OWNER, _REPO, _HEAD_SHA, token="tok", opener=opener)

    def test_malformed_head_sha_rejected_before_any_request(self):
        """bobbie.sast.7 (lr-afba): head_sha must pass sha.validate_sha
        BEFORE URL construction — never silently proceed with an
        unvalidated value. No request is issued for a malformed SHA."""

        def opener(req, timeout=30):
            raise AssertionError(
                f"no request should be issued for a malformed head_sha: {req.full_url}"
            )

        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_ci_status(
                _OWNER, _REPO, "not-a-sha", token="tok", opener=opener
            )
