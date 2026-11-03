"""Fakes shared by the verb tests: token providers that record or refuse, and
a minimal HTTP response with a JSON helper."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse


class RecordingTokenProvider:
    def __init__(self, token: str = "tok-123"):
        self.resolved_for: list[str] = []
        self._token = token

    def resolve_token(self, role: str) -> str:
        self.resolved_for.append(role)
        return self._token


class RefusingTokenProvider:
    def resolve_token(self, role: str) -> str:
        raise AssertionError(f"token provider must not be called (role={role!r})")


class FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": "application/json"}

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def paged(url: str, items: list) -> list:
    """The window of *items* that a real host would return for *url*.

    GitHub honors ``per_page``/``page``. Forgejo's issue-comments endpoint
    ignores ``page`` and ``limit`` entirely and always returns the whole list,
    so only ``per_page`` is read here and a Forgejo URL gets every item."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    size_param = query.get("per_page")
    if not size_param:
        return items
    size = int(size_param[0])
    page = int(query.get("page", ["1"])[0])
    return items[(page - 1) * size : page * size]


def json_resp(status: int, payload) -> FakeResponse:
    return FakeResponse(status, json.dumps(payload).encode("utf-8"))


def branch_tip_response(pr_info, branch_tip, branch_tip_http, url, platform_key=None):
    """The branch API's answer for a base-branch tip read: *branch_tip* when
    given, else the PR record's own base.sha (the tip is then not lagging),
    else a fixed placeholder. *branch_tip_http* != 200 makes the read fail.
    The body names the commit under both hosts' keys unless *platform_key*
    picks one."""
    if branch_tip_http != 200:
        raise urllib.error.HTTPError(url, branch_tip_http, "err", {}, io.BytesIO(b"{}"))
    recorded = (pr_info.get("base") or {}).get("sha") if isinstance(pr_info, dict) else None
    sha = branch_tip or recorded or "b" * 40
    commit = {"sha": sha, "id": sha} if platform_key is None else {platform_key: sha}
    return json_resp(200, {"name": "main", "commit": commit})



def with_default_base_ref(pr_info, *, no_base_ref=False):
    """*pr_info* with a base branch name when the payload carries none: the
    merge gate resolves the base tip through the branch API by that name, and
    a payload that never named one is a separate refusal (*no_base_ref*)."""
    if no_base_ref or not isinstance(pr_info, dict):
        return pr_info
    base = pr_info.get("base")
    if isinstance(base, dict) and "ref" in base:
        return pr_info
    return {**pr_info, "base": {"ref": "main", **(base if isinstance(base, dict) else {})}}
