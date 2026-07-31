"""test_acquire_forgejo_backend.py — tests for
clagentic_loadout.acquire.forgejo_backend (lr-c17040).

Coverage:
  - fetch_pr_content success path: base_sha/head_sha sourced from the PR
    metadata endpoint (never a caller-supplied or locally-resolved value),
    diff_text from the .diff endpoint, changed_files from the files
    endpoint (no per-file patch on this platform).
  - include_file_contents=True additionally fetches each non-deleted
    changed file's content from the contents endpoint, base64-decoded; a
    deleted file's content is never fetched.
  - A 404 from the contents endpoint yields "" content (deleted file case
    the files endpoint itself didn't already flag as deleted).
  - Failure modes: PR-info non-200, diff non-200, files non-200 each raise
    AcquireFetchError; nothing is a silent empty result.
  - ForgejoAcquireBackend (the AcquireBackend Protocol implementation)
    delegates to the module-level fetch_pr_content with its own stored
    token/git_host_base/opener.
  - Mocked HTTP throughout -- no real network call anywhere in this file.
"""

from __future__ import annotations

import base64
import json

import pytest

from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.acquire.forgejo_backend import ForgejoAcquireBackend, fetch_pr_content

_GIT_HOST_BASE = "http://git-host.example.com"
_OWNER = "some-owner"
_REPO = "some-repo"
_PR = 42


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


def _pr_info_body(base_sha="a" * 40, head_sha="b" * 40):
    return json.dumps(
        {"base": {"sha": base_sha}, "head": {"sha": head_sha}}
    ).encode("utf-8")


def _files_body(files):
    return json.dumps(files).encode("utf-8")


def _make_opener(
    *,
    pr_info_status=200,
    pr_info_body=None,
    diff_status=200,
    diff_body=b"diff --git a/a.py b/a.py\n",
    files_status=200,
    files=None,
    contents_by_path=None,
):
    files = files if files is not None else [{"filename": "a.py", "status": "modified"}]
    contents_by_path = contents_by_path or {}
    pr_info_body = pr_info_body if pr_info_body is not None else _pr_info_body()

    def opener(req, timeout=15):
        url = req.full_url
        if url.endswith(f"/pulls/{_PR}"):
            return _FakeResponse(pr_info_status, pr_info_body)
        if url.endswith(f"/pulls/{_PR}.diff"):
            return _FakeResponse(diff_status, diff_body)
        if url.endswith(f"/pulls/{_PR}/files"):
            return _FakeResponse(files_status, _files_body(files))
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
        raise AssertionError(f"unexpected request: {req.get_method()} {url}")

    return opener


class TestFetchPrContentSuccess:
    def test_base_and_head_sha_from_pr_metadata(self):
        opener = _make_opener(pr_info_body=_pr_info_body(base_sha="c" * 40, head_sha="d" * 40))
        acquired = fetch_pr_content(_GIT_HOST_BASE, "tok", _OWNER, _REPO, _PR, opener=opener)
        assert acquired.base_sha == "c" * 40
        assert acquired.head_sha == "d" * 40

    def test_diff_text_from_diff_endpoint(self):
        opener = _make_opener(diff_body=b"diff --git a/x b/x\n+hello\n")
        acquired = fetch_pr_content(_GIT_HOST_BASE, "tok", _OWNER, _REPO, _PR, opener=opener)
        assert acquired.diff_text == "diff --git a/x b/x\n+hello\n"

    def test_changed_files_from_files_endpoint_no_patch(self):
        opener = _make_opener(
            files=[
                {"filename": "a.py", "status": "modified"},
                {"filename": "b.py", "status": "added"},
            ]
        )
        acquired = fetch_pr_content(_GIT_HOST_BASE, "tok", _OWNER, _REPO, _PR, opener=opener)
        assert acquired.changed_filenames == ["a.py", "b.py"]
        assert all(cf.patch == "" for cf in acquired.changed_files)

    def test_owner_repo_pr_number_echoed(self):
        opener = _make_opener()
        acquired = fetch_pr_content(_GIT_HOST_BASE, "tok", _OWNER, _REPO, _PR, opener=opener)
        assert (acquired.owner, acquired.repo, acquired.pr_number) == (_OWNER, _REPO, _PR)

    def test_without_include_file_contents_content_is_empty(self):
        opener = _make_opener(files=[{"filename": "a.py", "status": "modified"}])
        acquired = fetch_pr_content(_GIT_HOST_BASE, "tok", _OWNER, _REPO, _PR, opener=opener)
        assert acquired.changed_files[0].content == ""


class TestIncludeFileContents:
    def test_fetches_content_for_non_deleted_files(self):
        content_b64 = base64.b64encode(b"print('hi')\n").decode("ascii")
        opener = _make_opener(
            files=[{"filename": "a.py", "status": "modified"}],
            contents_by_path={"a.py": (200, content_b64)},
        )
        acquired = fetch_pr_content(
            _GIT_HOST_BASE, "tok", _OWNER, _REPO, _PR, include_file_contents=True, opener=opener
        )
        assert acquired.changed_files[0].content == "print('hi')\n"

    def test_never_fetches_content_for_deleted_files(self):
        def opener(req, timeout=15):
            url = req.full_url
            if "/contents/" in url:
                raise AssertionError("must never fetch content for a deleted file")
            if url.endswith(f"/pulls/{_PR}"):
                return _FakeResponse(200, _pr_info_body())
            if url.endswith(f"/pulls/{_PR}.diff"):
                return _FakeResponse(200, b"")
            if url.endswith(f"/pulls/{_PR}/files"):
                return _FakeResponse(200, _files_body([{"filename": "gone.py", "status": "deleted"}]))
            raise AssertionError(f"unexpected: {url}")

        acquired = fetch_pr_content(
            _GIT_HOST_BASE, "tok", _OWNER, _REPO, _PR, include_file_contents=True, opener=opener
        )
        assert acquired.changed_files[0].content == ""

    def test_404_from_contents_yields_empty_content(self):
        opener = _make_opener(
            files=[{"filename": "a.py", "status": "modified"}],
            contents_by_path={},  # nothing configured -> falls to the 404 default
        )
        acquired = fetch_pr_content(
            _GIT_HOST_BASE, "tok", _OWNER, _REPO, _PR, include_file_contents=True, opener=opener
        )
        assert acquired.changed_files[0].content == ""


class TestFetchFailures:
    def test_pr_info_non_200_raises(self):
        opener = _make_opener(pr_info_status=404)
        with pytest.raises(AcquireFetchError):
            fetch_pr_content(_GIT_HOST_BASE, "tok", _OWNER, _REPO, _PR, opener=opener)

    def test_diff_non_200_raises(self):
        opener = _make_opener(diff_status=500)
        with pytest.raises(AcquireFetchError):
            fetch_pr_content(_GIT_HOST_BASE, "tok", _OWNER, _REPO, _PR, opener=opener)

    def test_files_non_200_raises(self):
        opener = _make_opener(files_status=500)
        with pytest.raises(AcquireFetchError):
            fetch_pr_content(_GIT_HOST_BASE, "tok", _OWNER, _REPO, _PR, opener=opener)

    def test_files_non_list_body_raises(self):
        def opener(req, timeout=15):
            url = req.full_url
            if url.endswith(f"/pulls/{_PR}"):
                return _FakeResponse(200, _pr_info_body())
            if url.endswith(f"/pulls/{_PR}.diff"):
                return _FakeResponse(200, b"")
            if url.endswith(f"/pulls/{_PR}/files"):
                return _FakeResponse(200, b'{"not": "a list"}')
            raise AssertionError(url)

        with pytest.raises(AcquireFetchError):
            fetch_pr_content(_GIT_HOST_BASE, "tok", _OWNER, _REPO, _PR, opener=opener)


class TestForgejoAcquireBackend:
    def test_delegates_to_module_level_function(self):
        opener = _make_opener()
        backend = ForgejoAcquireBackend("tok", git_host_base=_GIT_HOST_BASE, opener=opener)
        acquired = backend.fetch_pr_content(owner=_OWNER, repo=_REPO, pr_number=_PR)
        assert acquired.owner == _OWNER
        assert acquired.base_sha == "a" * 40
