"""ci.forgejo_backend -- cancel CI runs through the Forgejo REST API.

What the Forgejo API offers was read from the served swagger document
(``/swagger.v1.json``) of the deployment's Forgejo, not assumed. Under
``/repos/{owner}/{repo}/actions/runs/{run_id}`` it offers:

  GET    .../{run_id}          read a run (field ``commit_sha``)
  DELETE .../{run_id}          delete a completed run (not used here)
  POST   .../{run_id}/cancel   cancel a pending or running run; answers 204

It offers no endpoint that re-runs a run or the failed jobs of a run, so those
two operations are absent from ``supported`` and the verb reports them as not
supported. When a Forgejo release adds one, it is one row in ``_OPERATIONS``.
"""

from __future__ import annotations

from clagentic_loadout.ci.backend import OPERATION_CANCEL, HostAnswer, clean_message
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO
from clagentic_loadout.transport import git_host_api

#: operation -> (path suffix under the run, the one success status).
_OPERATIONS: dict[str, tuple[str, int]] = {
    OPERATION_CANCEL: ("cancel", 204),
}


def _run_path(owner: str, repo: str, run_id: int) -> str:
    return f"/api/v1/repos/{owner}/{repo}/actions/runs/{run_id}"


class ForgejoCiBackend:
    platform = PLATFORM_FORGEJO
    supported = frozenset(_OPERATIONS)

    def __init__(self, token: str, *, git_host_base: str, opener=None) -> None:
        self._token = token
        self._base = git_host_base
        self._opener = opener

    def read_head_sha(self, owner: str, repo: str, run_id: int) -> str | None:
        try:
            status, raw = git_host_api.request(
                self._base, "GET", _run_path(owner, repo, run_id), self._token, opener=self._opener
            )
        except git_host_api.GitHostApiError:
            return None
        if status != 200:
            return None
        head = git_host_api.parse_json_body(raw).get("commit_sha")
        return head if isinstance(head, str) and head else None

    def send(self, operation: str, owner: str, repo: str, run_id: int) -> HostAnswer:
        suffix, success = _OPERATIONS[operation]
        path = f"{_run_path(owner, repo, run_id)}/{suffix}"
        try:
            status, _raw = git_host_api.request(
                self._base, "POST", path, self._token, opener=self._opener
            )
        except git_host_api.GitHostApiError as exc:
            # A write that the host answered with a non-2xx carries its status;
            # a refused redirect or a network failure carries none.
            return HostAnswer(exc.http_status or 0, False, clean_message(str(exc), self._token))
        return HostAnswer(status, status == success, "")
