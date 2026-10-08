"""Fakes shared by the verb tests: token providers that record or refuse, and
a minimal HTTP response with a JSON helper."""

from __future__ import annotations

import json
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
