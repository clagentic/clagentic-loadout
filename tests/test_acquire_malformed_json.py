"""Every JSON read in both acquire transports fails with AcquireFetchError on a
malformed or wrong-shaped 200 body, never a parser error, an AttributeError, or
a silent empty result. Sweeps each fetch path of each backend."""

from __future__ import annotations

import json
import urllib.error

import pytest

from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.acquire.forgejo_backend import ForgejoAcquireBackend
from clagentic_loadout.acquire.github_backend import GithubAcquireBackend

_OWNER = "some-owner"
_REPO = "some-repo"
_BASE = "a" * 40
_HEAD = "b" * 40
_GIT_HOST_BASE = "http://git-host.example.com"
_DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"

#: Bodies a hostile or broken host could return with HTTP 200 where an object
#: (or list) is expected.
_BAD_BODIES = [b"{not json", b"\xff\xfe", b"[1, 2]", b'"text"', b"null", b"7"]


class _Response:
    def __init__(self, body: bytes, content_type: str = "application/json"):
        self.status = 200
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self):
        return self._body

    def getcode(self):
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _pr_json() -> bytes:
    return json.dumps({"base": {"sha": _BASE}, "head": {"sha": _HEAD}}).encode()


def _files_json() -> bytes:
    return json.dumps([{"filename": "x.py", "status": "modified", "patch": "p"}]).encode()


def _router(overrides: dict[str, bytes], *, forgejo: bool):
    """Opener answering every acquire endpoint validly except the one whose
    URL suffix appears in *overrides*."""

    def opener(req, timeout=30):
        url = req.full_url
        for suffix, body in overrides.items():
            if suffix in url:
                return _Response(body)
        accept = req.get_header("Accept", "")
        if url.endswith(".diff") or accept == "application/vnd.github.v3.diff":
            return _Response(_DIFF.encode(), "text/plain")
        if "/contents/" in url:
            return _Response(json.dumps({"content": "", "encoding": "base64"}).encode())
        if "/compare/" in url:
            payload = {"total_commits": 1} if forgejo else {"status": "ahead"}
            return _Response(json.dumps(payload).encode())
        if url.endswith("/files"):
            return _Response(_files_json())
        return _Response(_pr_json())

    return opener


def _forgejo(overrides):
    return ForgejoAcquireBackend(
        "tok", git_host_base=_GIT_HOST_BASE, opener=_router(overrides, forgejo=True)
    )


def _github(overrides):
    return GithubAcquireBackend("tok", opener=_router(overrides, forgejo=False))


def _pr(backend, *, contents=False):
    return backend.fetch_pr_content(
        owner=_OWNER, repo=_REPO, pr_number=5, include_file_contents=contents
    )


def _range(backend):
    return backend.fetch_range_diff(owner=_OWNER, repo=_REPO, base_sha=_BASE, head_sha=_HEAD)


@pytest.mark.parametrize("bad", _BAD_BODIES)
class TestForgejoMalformedBodies:
    def test_pr_metadata(self, bad):
        with pytest.raises(AcquireFetchError):
            _pr(_forgejo({"pulls/5": bad}))

    def test_changed_file_list(self, bad):
        if bad == b"[1, 2]":
            pytest.skip("a list is the expected shape here")
        with pytest.raises(AcquireFetchError):
            _pr(_forgejo({"/files": bad}))

    def test_file_content(self, bad):
        with pytest.raises(AcquireFetchError):
            _pr(_forgejo({"/contents/": bad}), contents=True)

    def test_compare(self, bad):
        with pytest.raises(AcquireFetchError):
            _range(_forgejo({"/compare/": bad}))


@pytest.mark.parametrize("bad", _BAD_BODIES)
class TestGithubMalformedBodies:
    def test_pr_metadata(self, bad):
        with pytest.raises(AcquireFetchError):
            _pr(_github({"pulls/5": bad}))

    def test_changed_file_list(self, bad):
        if bad == b"[1, 2]":
            pytest.skip("a list is the expected shape here")
        with pytest.raises(AcquireFetchError):
            _pr(_github({"/files": bad}))

    def test_file_content(self, bad):
        with pytest.raises(AcquireFetchError):
            _pr(_github({"/contents/": bad}), contents=True)

    def test_compare(self, bad):
        with pytest.raises(AcquireFetchError):
            _range(_github({"/compare/": bad}))


def test_a_github_network_failure_is_a_fetch_error():
    def opener(req, timeout=30):
        raise urllib.error.URLError("unreachable")

    with pytest.raises(AcquireFetchError, match="unreachable"):
        _pr(GithubAcquireBackend("tok", opener=opener))
