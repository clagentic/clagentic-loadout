"""Tests for the commit-range read both acquire transports expose
(acquire.contract.RangeDiffBackend): the relation of two commits and, for a
strict fast-forward, the net diff between them. Mocked HTTP throughout."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.acquire.contract import RangeDiffBackend
from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.acquire.forgejo_backend import ForgejoAcquireBackend
from clagentic_loadout.acquire.github_backend import GithubAcquireBackend

_OWNER = "some-owner"
_REPO = "some-repo"
_BASE = "a" * 40
_HEAD = "b" * 40
_DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
_GIT_HOST_BASE = "http://git-host.example.com"


class _Response:
    def __init__(self, status: int, body: bytes, content_type: str = "application/json"):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _github_opener(*, status="ahead", compare_http=200, diff_http=200, seen=None):
    def opener(req, timeout=30):
        url = req.full_url
        accept = req.get_header("Accept", "")
        if seen is not None:
            seen.append((url, accept))
        assert url.endswith(f"/repos/{_OWNER}/{_REPO}/compare/{_BASE}...{_HEAD}"), url
        if accept == "application/vnd.github.v3.diff":
            return _Response(diff_http, _DIFF.encode("utf-8"), "text/plain")
        return _Response(compare_http, json.dumps({"status": status}).encode("utf-8"))

    return opener


def _forgejo_opener(*, forward, backward, diff_http=200, seen=None):
    def opener(req, timeout=15):
        url = req.full_url
        if seen is not None:
            seen.append(url)
        if url.endswith(f"/api/v1/repos/{_OWNER}/{_REPO}/compare/{_BASE}...{_HEAD}"):
            return _Response(200, json.dumps({"total_commits": forward}).encode("utf-8"))
        if url.endswith(f"/api/v1/repos/{_OWNER}/{_REPO}/compare/{_HEAD}...{_BASE}"):
            return _Response(200, json.dumps({"total_commits": backward}).encode("utf-8"))
        if url.endswith(f"/{_OWNER}/{_REPO}/compare/{_BASE}...{_HEAD}.diff"):
            return _Response(diff_http, _DIFF.encode("utf-8"))
        raise AssertionError(f"unexpected request: {url}")

    return opener


def _github(opener) -> GithubAcquireBackend:
    return GithubAcquireBackend("tok", opener=opener)


def _forgejo(opener) -> ForgejoAcquireBackend:
    return ForgejoAcquireBackend("tok", git_host_base=_GIT_HOST_BASE, opener=opener)


def _range(backend):
    return backend.fetch_range_diff(owner=_OWNER, repo=_REPO, base_sha=_BASE, head_sha=_HEAD)


def test_both_transports_satisfy_the_range_diff_protocol():
    assert isinstance(_github(None), RangeDiffBackend)
    assert isinstance(_forgejo(None), RangeDiffBackend)


class TestGithub:
    def test_ahead_is_a_fast_forward_with_its_diff(self):
        span = _range(_github(_github_opener(status="ahead")))

        assert span.fast_forward is True
        assert span.diff_text == _DIFF
        assert (span.base_sha, span.head_sha) == (_BASE, _HEAD)

    @pytest.mark.parametrize("relation", ["behind", "diverged", "identical"])
    def test_every_other_relation_is_not_a_fast_forward_and_fetches_no_diff(self, relation):
        seen: list = []

        span = _range(_github(_github_opener(status=relation, seen=seen)))

        assert span.fast_forward is False
        assert span.diff_text == ""
        assert all(accept != "application/vnd.github.v3.diff" for _, accept in seen)

    def test_an_unreadable_range_raises(self):
        with pytest.raises(AcquireFetchError, match="HTTP 404"):
            _range(_github(_github_opener(compare_http=404)))

    def test_an_unreadable_diff_raises(self):
        with pytest.raises(AcquireFetchError, match="HTTP 500"):
            _range(_github(_github_opener(diff_http=500)))


class TestForgejo:
    def test_commits_one_way_and_none_back_is_a_fast_forward_with_its_diff(self):
        span = _range(_forgejo(_forgejo_opener(forward=3, backward=0)))

        assert span.fast_forward is True
        assert span.diff_text == _DIFF

    @pytest.mark.parametrize(
        ("forward", "backward"), [(0, 0), (0, 2), (2, 2)], ids=["identical", "behind", "diverged"]
    )
    def test_any_other_relation_is_not_a_fast_forward_and_fetches_no_diff(
        self, forward, backward
    ):
        seen: list = []

        span = _range(_forgejo(_forgejo_opener(forward=forward, backward=backward, seen=seen)))

        assert span.fast_forward is False
        assert span.diff_text == ""
        assert not any(url.endswith(".diff") for url in seen)

    def test_an_unreadable_diff_raises(self):
        with pytest.raises(AcquireFetchError, match="HTTP 500"):
            _range(_forgejo(_forgejo_opener(forward=1, backward=0, diff_http=500)))

    def test_a_compare_without_a_commit_count_raises(self):
        def opener(req, timeout=15):
            return _Response(200, b"{}")

        with pytest.raises(AcquireFetchError, match="no commit count"):
            _range(_forgejo(opener))
