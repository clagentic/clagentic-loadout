"""ci.github_backend -- re-run and cancel CI runs through the GitHub REST API.

Reuses the shared request primitive (transport.github_client), which sends the
credential only in the Authorization header and refuses every redirect.

Endpoints, each a POST with no body (the documented success status in brackets):

  rerun              /repos/{owner}/{repo}/actions/runs/{run_id}/rerun               [201]
  rerun-failed-jobs  /repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs   [201]
  cancel             /repos/{owner}/{repo}/actions/runs/{run_id}/cancel              [202]

The head commit is read with GET /repos/{owner}/{repo}/actions/runs/{run_id}
(field ``head_sha``).
"""

from __future__ import annotations

import urllib.error
from typing import Any

from clagentic_loadout.ci.backend import (
    OPERATION_CANCEL,
    OPERATION_RERUN,
    OPERATION_RERUN_FAILED_JOBS,
    HostAnswer,
    clean_message,
)
from clagentic_loadout.platform_detect import PLATFORM_GITHUB
from clagentic_loadout.transport.github_client import GITHUB_API_BASE, request_json

#: operation -> (path suffix under the run, the one success status).
_OPERATIONS: dict[str, tuple[str, int]] = {
    OPERATION_RERUN: ("rerun", 201),
    OPERATION_RERUN_FAILED_JOBS: ("rerun-failed-jobs", 201),
    OPERATION_CANCEL: ("cancel", 202),
}


def _run_url(owner: str, repo: str, run_id: int) -> str:
    return f"{GITHUB_API_BASE}/repos/{owner}/{repo}/actions/runs/{run_id}"


def _message_of(body: Any) -> str:
    if isinstance(body, dict):
        message = body.get("message")
        return message if isinstance(message, str) else ""
    return body if isinstance(body, str) else ""


class GithubCiBackend:
    platform = PLATFORM_GITHUB
    supported = frozenset(_OPERATIONS)

    def __init__(self, token: str, *, opener=None) -> None:
        self._token = token
        self._opener = opener

    def read_head_sha(self, owner: str, repo: str, run_id: int) -> str | None:
        try:
            status, body = request_json(
                "GET", _run_url(owner, repo, run_id), self._token, opener=self._opener
            )
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None
        if status != 200 or not isinstance(body, dict):
            return None
        head = body.get("head_sha")
        return head if isinstance(head, str) and head else None

    def send(self, operation: str, owner: str, repo: str, run_id: int) -> HostAnswer:
        suffix, success = _OPERATIONS[operation]
        url = f"{_run_url(owner, repo, run_id)}/{suffix}"
        try:
            status, body = request_json("POST", url, self._token, opener=self._opener)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return HostAnswer(0, False, clean_message(f"no answer from the host: {exc}", self._token))
        return HostAnswer(
            status, status == success, clean_message(_message_of(body), self._token)
        )
