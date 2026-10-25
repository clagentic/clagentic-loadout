"""The churn net delta names each file from structured host fields, or from a diff
header read without guessing, on both host shapes."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.acquire.forgejo_backend import ForgejoAcquireBackend
from clagentic_loadout.acquire.github_backend import GithubAcquireBackend
from clagentic_loadout.review import churn_delta

OWNER, REPO = "some-owner", "some-repo"
BASE, HEAD = "a" * 40, "b" * 40
HOST_BASE = "http://git-host.example.com"
HUNK = "@@ -1 +1,2 @@\n-old\n+new\n+newer\n"


class _Response:
    def __init__(self, status, body, content_type="application/json"):
        self.status, self._body = status, body
        self.headers = {"Content-Type": content_type}

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _github(diff, files):
    compare = {"status": "ahead"}
    if files is not None:
        compare["files"] = files

    def opener(req, timeout=30):
        if req.get_header("Accept", "") == "application/vnd.github.v3.diff":
            return _Response(200, diff.encode("utf-8"), "text/plain")
        return _Response(200, json.dumps(compare).encode("utf-8"))

    return GithubAcquireBackend("tok", opener=opener)


def _forgejo(diff):
    def opener(req, timeout=15):
        url = req.full_url
        if url.endswith(".diff"):
            return _Response(200, diff.encode("utf-8"))
        count = 2 if f"{BASE}...{HEAD}" in url else 0
        return _Response(200, json.dumps({"total_commits": count}).encode("utf-8"))

    return ForgejoAcquireBackend("tok", git_host_base=HOST_BASE, opener=opener)


def _read(backend):
    return churn_delta.read_net_delta(
        backend, owner=OWNER, repo=REPO, from_head=BASE, to_head=HEAD
    )


def _rows(delta):
    return [(f.path, f.added, f.removed) for f in delta.files]


def _entry(filename, additions=2, deletions=1):
    return {"filename": filename, "additions": additions, "deletions": deletions}


QUOTED = 'diff --git "a/caf\\303\\251 x.py" "b/caf\\303\\251 x.py"\n--- a\n+++ b\n' + HUNK
B_IN_NAME = "diff --git a/dir b/file.py b/dir b/file.py\n--- a\n+++ b\n" + HUNK
RENAME = "diff --git a/old.py b/new.py\nsimilarity index 90%\nrename from old.py\nrename to new.py\n" + HUNK
RENAME_B_IN_NAME = (
    "diff --git a/p b/old.py b/p b/new.py\nrename from p b/old.py\nrename to p b/new.py\n" + HUNK
)
RENAME_QUOTED = (
    'diff --git "a/o\\303\\251.py" "b/n\\303\\251.py"\nrename from "o\\303\\251.py"\n'
    'rename to "n\\303\\251.py"\n' + HUNK
)
AMBIGUOUS = "diff --git a/p b/old.py b/q b/new.py\n--- a\n+++ b\n" + HUNK


class TestDiffText:
    @pytest.mark.parametrize(
        ("diff", "path"),
        [
            (QUOTED, "café x.py"),
            (B_IN_NAME, "dir b/file.py"),
            (RENAME, "new.py"),
            (RENAME_B_IN_NAME, "p b/new.py"),
            (RENAME_QUOTED, "né.py"),
        ],
        ids=["quoted", "b-in-name", "rename", "rename-b-in-name", "rename-quoted"],
    )
    def test_each_file_is_named_by_its_real_path(self, diff, path):
        assert [(f.path, f.added, f.removed) for f in churn_delta.count_changes(diff)] == [
            (path, 2, 1)
        ]

    def test_a_header_that_stays_ambiguous_raises(self):
        with pytest.raises(churn_delta.AmbiguousDiffHeader):
            churn_delta.count_changes(AMBIGUOUS)

    @pytest.mark.parametrize("make", [lambda d: _forgejo(d), lambda d: _github(d, None)])
    def test_an_ambiguous_header_is_a_null_delta_with_a_reason_on_both_hosts(self, make):
        delta, reason = _read(make(AMBIGUOUS))

        assert delta is None and "ambiguous" in reason

    @pytest.mark.parametrize("diff", [QUOTED, B_IN_NAME, RENAME], ids=["quoted", "b", "rename"])
    def test_forgejo_reads_the_paths_from_diff_text(self, diff):
        delta, reason = _read(_forgejo(diff))

        assert reason is None and _rows(delta) == [(churn_delta.count_changes(diff)[0].path, 2, 1)]


class TestStructuredFields:
    def test_github_uses_the_listed_paths_and_counts_not_the_diff_headers(self):
        files = [_entry('café "x".py'), _entry("dir b/file.py", 5, 0), _entry("new.py", 0, 3)]

        delta, reason = _read(_github(AMBIGUOUS, files))

        assert reason is None
        assert _rows(delta) == [
            ('café "x".py', 2, 1), ("dir b/file.py", 5, 0), ("new.py", 0, 3),
        ]

    @pytest.mark.parametrize(
        "files",
        [
            [{"filename": "a.py", "additions": "2", "deletions": 1}],
            [{"filename": "", "additions": 1, "deletions": 1}],
            [{"additions": 1, "deletions": 1}],
            ["a.py"],
            [_entry("f%d.py" % n) for n in range(300)],
        ],
        ids=["string-count", "empty-name", "no-name", "not-an-object", "possibly-cut-short"],
    )
    def test_an_untrustworthy_list_falls_back_to_the_diff_text(self, files):
        delta, reason = _read(_github(RENAME, files))

        assert reason is None and _rows(delta) == [("new.py", 2, 1)]

    def test_a_rename_entry_is_counted_under_its_new_name(self):
        entry = dict(_entry("new.py"), previous_filename="old.py", status="renamed")

        delta, _ = _read(_github(AMBIGUOUS, [entry]))

        assert _rows(delta) == [("new.py", 2, 1)]
