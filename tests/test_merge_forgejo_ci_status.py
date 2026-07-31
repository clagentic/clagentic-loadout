"""test_merge_forgejo_ci_status.py — tests for
clagentic_loadout.merge.forgejo_backend.fetch_ci_status (lr-afba
CI-status-gate slice, comment #6; HEAD-scoping fix lr-2d2293).

Coverage:
  - Empty combined state + zero HEAD-scoped commit-status entries => a
    CiStatusResult with is_empty True — the exact shape observed on PR #49
    @ head 6a8fcbd (empty combined state, zero commit-status entries).
  - Non-empty combined state (a real statuses list) => a populated,
    non-empty CiStatusResult carrying the real state.
  - Unreachable / non-200 status endpoint => GateFactUnavailableError
    (fail-closed, mirrors every other gate-fact fetcher in this module) --
    negative control so "empty is pass" can never mask a genuine read
    failure.
  - GET .../actions/tasks is NEVER called (lr-2d2293: that endpoint is
    repo-global, not HEAD-scoped, and produced a false refusal on
    mirror-runner repos when its total_count fed is_empty — see session
    d5aee241). run_count on the returned CiStatusResult is always 0 from
    this backend.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from clagentic_loadout.merge import forgejo_backend
from clagentic_loadout.merge.errors import GateFactUnavailableError

_API_BASE = "http://git-host.example.com"
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


def _opener_for(status_response):
    def opener(req, timeout=15):
        url = req.full_url
        if url.endswith("/status"):
            return status_response
        raise AssertionError(
            f"unexpected call: {url} -- lr-2d2293: /actions/tasks must NEVER "
            f"be called (repo-global, not HEAD-scoped)"
        )

    return opener


class TestFetchCiStatusEmpty:
    def test_empty_combined_state_is_empty(self):
        opener = _opener_for(_json_resp(200, {"state": "", "statuses": []}))
        result = forgejo_backend.fetch_ci_status(
            _API_BASE, "clagentic", "clagentic-loadout", _HEAD_SHA, token="tok", opener=opener
        )
        assert result.is_empty is True
        assert result.combined_state == ""
        assert result.status_count == 0
        assert result.run_count == 0

    def test_missing_state_field_defaults_empty(self):
        opener = _opener_for(_json_resp(200, {}))
        result = forgejo_backend.fetch_ci_status(
            _API_BASE, "owner", "repo", _HEAD_SHA, token="tok", opener=opener
        )
        assert result.is_empty is True

    def test_actions_tasks_endpoint_is_never_called(self):
        # lr-2d2293 regression: the mirror-runner false-refusal originated
        # from feeding /actions/tasks total_count (repo-global) into
        # is_empty. The fetcher must not even query that endpoint anymore --
        # the opener raises AssertionError if it is hit.
        opener = _opener_for(_json_resp(200, {"state": "", "statuses": []}))
        result = forgejo_backend.fetch_ci_status(
            _API_BASE, "clagentic", "clagentic-loadout", _HEAD_SHA, token="tok", opener=opener
        )
        assert result.is_empty is True
        assert result.run_count == 0


class TestFetchCiStatusNonEmpty:
    def test_success_state_with_statuses(self):
        opener = _opener_for(
            _json_resp(
                200,
                {
                    "state": "success",
                    "statuses": [{"state": "success", "context": "build"}],
                },
            ),
        )
        result = forgejo_backend.fetch_ci_status(
            _API_BASE, "owner", "repo", _HEAD_SHA, token="tok", opener=opener
        )
        assert result.is_empty is False
        assert result.combined_state == "success"
        assert result.status_count == 1
        assert result.run_count == 0

    def test_failure_state_reported_verbatim_lowercased(self):
        opener = _opener_for(
            _json_resp(200, {"state": "FAILURE", "statuses": [{"state": "failure"}]}),
        )
        result = forgejo_backend.fetch_ci_status(
            _API_BASE, "owner", "repo", _HEAD_SHA, token="tok", opener=opener
        )
        assert result.combined_state == "failure"
        assert result.is_empty is False


class TestFetchCiStatusFailClosed:
    def test_non_200_status_endpoint_raises(self):
        opener = _opener_for(_FakeResponse(500, b"{}"))
        with pytest.raises(GateFactUnavailableError):
            forgejo_backend.fetch_ci_status(
                _API_BASE, "owner", "repo", _HEAD_SHA, token="tok", opener=opener
            )

    def test_network_error_on_status_endpoint_raises(self):
        def opener(req, timeout=15):
            raise urllib.error.URLError("connection refused")

        with pytest.raises(GateFactUnavailableError):
            forgejo_backend.fetch_ci_status(
                _API_BASE, "owner", "repo", _HEAD_SHA, token="tok", opener=opener
            )

    def test_malformed_head_sha_rejected_before_any_request(self):
        """bobbie.sast.7 (lr-afba): head_sha must pass sha.validate_sha
        BEFORE URL construction — never silently proceed with an
        unvalidated value. No request is issued for a malformed SHA."""

        def opener(req, timeout=15):
            raise AssertionError(
                f"no request should be issued for a malformed head_sha: {req.full_url}"
            )

        with pytest.raises(GateFactUnavailableError):
            forgejo_backend.fetch_ci_status(
                _API_BASE, "owner", "repo", "not-a-sha", token="tok", opener=opener
            )
