"""test_acquire_github_backend.py — tests for
clagentic_loadout.acquire.github_backend (lr-c17040).

Coverage:
  - fetch_pr_content success path: base_sha/head_sha sourced from the PR
    metadata endpoint (JSON media type); diff_text from the SAME endpoint
    requested with the .v3.diff media type; changed_files from the files
    endpoint, INCLUDING each file's per-file patch (unlike Forgejo/Gitea).
  - include_file_contents=True fetches each non-removed changed file's
    content from the contents endpoint, base64-decoded; a removed file's
    content is never fetched.
  - A 404 from the contents endpoint yields "" content.
  - Failure modes: PR-info non-200, diff non-200, files non-200 each raise
    AcquireFetchError.
  - GithubAcquireBackend delegates to the module-level fetch_pr_content.
  - Mocked HTTP throughout -- no real network call anywhere in this file.
"""

from __future__ import annotations

import base64
import json

import pytest

from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.acquire.github_backend import GithubAcquireBackend, fetch_pr_content

_OWNER = "some-owner"
_REPO = "some-repo"
_PR = 42


class _FakeResponse:
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


def _pr_info_body(base_sha="a" * 40, head_sha="b" * 40):
    return json.dumps({"base": {"sha": base_sha}, "head": {"sha": head_sha}}).encode("utf-8")


def _make_opener(
    *,
    pr_info_status=200,
    pr_info_body=None,
    diff_status=200,
    diff_text="diff --git a/a.py b/a.py\n",
    files_status=200,
    files=None,
    contents_by_path=None,
):
    files = files if files is not None else [{"filename": "a.py", "status": "modified", "patch": "@@ ..."}]
    contents_by_path = contents_by_path or {}
    pr_info_body = pr_info_body if pr_info_body is not None else _pr_info_body()

    def opener(req, timeout=30):
        url = req.full_url
        accept = req.get_header("Accept", "")
        if url.endswith(f"/pulls/{_PR}") and accept == "application/vnd.github.v3.diff":
            return _FakeResponse(diff_status, diff_text.encode("utf-8"), content_type="text/plain")
        if url.endswith(f"/pulls/{_PR}"):
            return _FakeResponse(pr_info_status, pr_info_body)
        if url.endswith(f"/pulls/{_PR}/files"):
            return _FakeResponse(files_status, json.dumps(files).encode("utf-8"))
        if "/contents/" in url:
            for path, (status, content_b64) in contents_by_path.items():
                if f"/contents/{path}" in url:
                    return _FakeResponse(
                        status,
                        json.dumps({"content": content_b64, "encoding": "base64"}).encode("utf-8")
                        if status == 200
                        else b"{}",
                    )
            return _FakeResponse(404, b"{}")
        raise AssertionError(f"unexpected request: {req.get_method()} {url} accept={accept!r}")

    return opener


class TestFetchPrContentSuccess:
    def test_base_and_head_sha_from_pr_metadata(self):
        opener = _make_opener(pr_info_body=_pr_info_body(base_sha="c" * 40, head_sha="d" * 40))
        acquired = fetch_pr_content(_OWNER, _REPO, _PR, "tok", opener=opener)
        assert acquired.base_sha == "c" * 40
        assert acquired.head_sha == "d" * 40

    def test_diff_text_via_diff_media_type(self):
        opener = _make_opener(diff_text="diff --git a/x b/x\n+hello\n")
        acquired = fetch_pr_content(_OWNER, _REPO, _PR, "tok", opener=opener)
        assert acquired.diff_text == "diff --git a/x b/x\n+hello\n"

    def test_changed_files_include_patch(self):
        opener = _make_opener(
            files=[{"filename": "a.py", "status": "modified", "patch": "@@ -1 +1 @@\n-x\n+y\n"}]
        )
        acquired = fetch_pr_content(_OWNER, _REPO, _PR, "tok", opener=opener)
        assert acquired.changed_files[0].patch == "@@ -1 +1 @@\n-x\n+y\n"

    def test_without_include_file_contents_content_is_empty(self):
        opener = _make_opener()
        acquired = fetch_pr_content(_OWNER, _REPO, _PR, "tok", opener=opener)
        assert acquired.changed_files[0].content == ""


class TestIncludeFileContents:
    def test_fetches_content_for_non_removed_files(self):
        content_b64 = base64.b64encode(b"print('hi')\n").decode("ascii")
        opener = _make_opener(
            files=[{"filename": "a.py", "status": "modified", "patch": ""}],
            contents_by_path={"a.py": (200, content_b64)},
        )
        acquired = fetch_pr_content(
            _OWNER, _REPO, _PR, "tok", include_file_contents=True, opener=opener
        )
        assert acquired.changed_files[0].content == "print('hi')\n"

    def test_never_fetches_content_for_removed_files(self):
        def opener(req, timeout=30):
            url = req.full_url
            accept = req.get_header("Accept", "")
            if "/contents/" in url:
                raise AssertionError("must never fetch content for a removed file")
            if url.endswith(f"/pulls/{_PR}") and accept == "application/vnd.github.v3.diff":
                return _FakeResponse(200, b"", content_type="text/plain")
            if url.endswith(f"/pulls/{_PR}"):
                return _FakeResponse(200, _pr_info_body())
            if url.endswith(f"/pulls/{_PR}/files"):
                return _FakeResponse(
                    200, json.dumps([{"filename": "gone.py", "status": "removed"}]).encode("utf-8")
                )
            raise AssertionError(url)

        acquired = fetch_pr_content(
            _OWNER, _REPO, _PR, "tok", include_file_contents=True, opener=opener
        )
        assert acquired.changed_files[0].content == ""

    def test_404_from_contents_yields_empty_content(self):
        opener = _make_opener(
            files=[{"filename": "a.py", "status": "modified"}], contents_by_path={}
        )
        acquired = fetch_pr_content(
            _OWNER, _REPO, _PR, "tok", include_file_contents=True, opener=opener
        )
        assert acquired.changed_files[0].content == ""


class TestFetchFailures:
    def test_pr_info_non_200_raises(self):
        opener = _make_opener(pr_info_status=404)
        with pytest.raises(AcquireFetchError):
            fetch_pr_content(_OWNER, _REPO, _PR, "tok", opener=opener)

    def test_diff_non_200_raises(self):
        opener = _make_opener(diff_status=500)
        with pytest.raises(AcquireFetchError):
            fetch_pr_content(_OWNER, _REPO, _PR, "tok", opener=opener)

    def test_files_non_200_raises(self):
        opener = _make_opener(files_status=500)
        with pytest.raises(AcquireFetchError):
            fetch_pr_content(_OWNER, _REPO, _PR, "tok", opener=opener)


class TestGithubAcquireBackend:
    def test_delegates_to_module_level_function(self):
        opener = _make_opener()
        backend = GithubAcquireBackend("tok", opener=opener)
        acquired = backend.fetch_pr_content(owner=_OWNER, repo=_REPO, pr_number=_PR)
        assert acquired.owner == _OWNER
        assert acquired.base_sha == "a" * 40
