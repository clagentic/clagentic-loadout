"""test_merge_close_backend.py — tests for close_pr on
clagentic_loadout.merge.forgejo_backend and clagentic_loadout.merge.github_backend
(lr-2ba5e1).

Coverage:
  - Forgejo close_pr: PATCH .../issues/{n} {"state": "closed"} succeeds on
    2xx; any non-2xx / network error raises MergeExecutionError.
  - GitHub close_pr: PATCH .../pulls/{n} {"state": "closed"} succeeds on
    200; any non-2xx / network error raises MergeExecutionError.
  - Neither call ever routes through
    transport.git_host_api.validate_body_stdin_content -- the payload these
    functions send has no 'body' key at all, and that validator is never
    imported/called anywhere in either backend module's close_pr.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from clagentic_loadout.merge import forgejo_backend, github_backend
from clagentic_loadout.merge.errors import MergeExecutionError

_API_BASE = "http://git-host.example.com"
_OWNER = "some-owner"
_REPO = "some-repo"


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


def _http_error(url: str, code: int, payload: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url, code, "err", {}, io.BytesIO(json.dumps(payload).encode("utf-8"))
    )


class TestForgejoClosePr:
    def test_happy_path_patches_issues_endpoint_with_state_closed(self):
        recorded = {}

        def opener(req, timeout=15):
            recorded["url"] = req.full_url
            recorded["method"] = req.get_method()
            recorded["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(200, b"{}")

        forgejo_backend.close_pr(_API_BASE, _OWNER, _REPO, 42, token="tok", opener=opener)

        assert recorded["method"] == "PATCH"
        assert recorded["url"] == f"{_API_BASE}/api/v1/repos/{_OWNER}/{_REPO}/issues/42"
        assert recorded["body"] == {"state": "closed"}

    def test_non_2xx_raises_merge_execution_error(self):
        def opener(req, timeout=15):
            raise _http_error(req.full_url, 404, {"message": "not found"})

        with pytest.raises(MergeExecutionError):
            forgejo_backend.close_pr(_API_BASE, _OWNER, _REPO, 42, token="tok", opener=opener)

    def test_network_error_raises_merge_execution_error(self):
        def opener(req, timeout=15):
            raise urllib.error.URLError("connection refused")

        with pytest.raises(MergeExecutionError):
            forgejo_backend.close_pr(_API_BASE, _OWNER, _REPO, 42, token="tok", opener=opener)


class TestGithubClosePr:
    def test_happy_path_patches_pulls_endpoint_with_state_closed(self):
        recorded = {}

        def opener(req, timeout=30):
            recorded["url"] = req.full_url
            recorded["method"] = req.get_method()
            recorded["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(200, b'{"state": "closed"}')

        github_backend.close_pr(_OWNER, _REPO, 42, token="tok", opener=opener)

        assert recorded["method"] == "PATCH"
        assert recorded["url"] == f"https://api.github.com/repos/{_OWNER}/{_REPO}/pulls/42"
        assert recorded["body"] == {"state": "closed"}

    def test_non_200_raises_merge_execution_error(self):
        def opener(req, timeout=30):
            raise _http_error(req.full_url, 404, {"message": "Not Found"})

        with pytest.raises(MergeExecutionError):
            github_backend.close_pr(_OWNER, _REPO, 42, token="tok", opener=opener)

    def test_network_error_raises_merge_execution_error(self):
        def opener(req, timeout=30):
            raise urllib.error.URLError("connection refused")

        with pytest.raises(MergeExecutionError):
            github_backend.close_pr(_OWNER, _REPO, 42, token="tok", opener=opener)
