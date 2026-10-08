"""Tests for transport.comment_paging and every comment reader built on it.

The regression shape: a PR with more comments than one API page, where the
comment that matters (the one just posted, or the latest reviewer verdict) is
LAST. A single-page reader sees only the oldest page and misses it.

All HTTP goes through injected fake openers that model the real servers --
GitHub serves page windows honoring ``per_page``; Forgejo ignores ``page`` and
``limit`` and returns the whole list -- no network.
"""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from clagentic_loadout.merge import forgejo_backend as merge_forgejo
from clagentic_loadout.merge import github_backend as merge_github
from clagentic_loadout.merge.errors import GateFactUnavailableError
from clagentic_loadout.merge.verdict import build_verdict_block, read_reviewer_verdict
from clagentic_loadout.review import github_backend as review_github
from clagentic_loadout.review.errors import ReviewVerifyError
from clagentic_loadout.transport import comment_paging, git_host_api

_OWNER = "some-owner"
_REPO = "some-repo"
_SHA_OLD = "a" * 40
_SHA_NEW = "b" * 40
_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _Resp:
    def __init__(self, body: bytes, headers: dict | None = None, status: int = 200):
        self.status = status
        self._body = body
        self.headers = headers or {"Content-Type": "application/json"}

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _comment(i: int, login: str = "someone", body: str | None = None, created=None) -> dict:
    return {
        "id": i,
        "user": {"login": login},
        "body": body if body is not None else f"comment {i}",
        "html_url": f"http://x/{i}",
        "created_at": _iso(created or _BASE_TIME + timedelta(seconds=i)),
    }


def _fillers(count: int) -> list[dict]:
    return [_comment(i) for i in range(1, count + 1)]


class _GithubServer:
    """Serves GET .../issues/N/comments windows (ascending, honoring per_page
    and page), GET /user, and POST appending a comment."""

    def __init__(
        self, comments, *, login="some-bot", link_headers=True, descending=False,
        max_page_size=100,
    ):
        self.max_page_size = max_page_size
        self.comments = list(comments)
        self.login = login
        self.link_headers = link_headers
        self.descending = descending
        self.comment_gets = 0
        self.post_body = None

    def __call__(self, req, timeout=30):
        parsed = urllib.parse.urlparse(req.full_url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/user":
            return _Resp(json.dumps({"login": self.login}).encode())
        if req.get_method() == "POST":
            payload = json.loads(req.data.decode())
            self.post_body = payload["body"]
            new = _comment(
                max(c["id"] for c in self.comments) + 1,
                self.login,
                payload["body"],
                created=datetime.now(timezone.utc),
            )
            self.comments.append(new)
            return _Resp(json.dumps(new).encode(), status=201)
        self.comment_gets += 1
        per_page = min(int(query.get("per_page", ["30"])[0]), self.max_page_size)
        page = int(query.get("page", ["1"])[0])
        ordered = list(reversed(self.comments)) if self.descending else self.comments
        window = ordered[(page - 1) * per_page : page * per_page]
        headers = {"Content-Type": "application/json"}
        if self.link_headers and page * per_page < len(ordered):
            headers["Link"] = f'<{req.full_url}>; rel="next"'
        elif self.link_headers and len(ordered) > per_page:
            headers["Link"] = f'<{req.full_url}>; rel="prev"'
        return _Resp(json.dumps(window).encode(), headers)


class _ForgejoServer:
    """Models the real Forgejo issue-comments endpoint: ``page`` and ``limit``
    are ignored and the full list comes back on every GET."""

    def __init__(self, comments, *, descending=False):
        self.comments = list(comments)
        self.descending = descending
        self.gets = 0
        self.get_urls: list[str] = []

    def __call__(self, req, timeout=15):
        parsed = urllib.parse.urlparse(req.full_url)
        if req.get_method() == "POST":
            payload = json.loads(req.data.decode())
            new = _comment(
                max(c["id"] for c in self.comments) + 1,
                "some-bot",
                payload["body"],
                created=datetime.now(timezone.utc),
            )
            self.comments.append(new)
            return _Resp(json.dumps(new).encode(), status=201)
        if parsed.path.endswith("/user"):
            return _Resp(json.dumps({"login": "some-bot"}).encode())
        self.gets += 1
        self.get_urls.append(req.full_url)
        ordered = list(reversed(self.comments)) if self.descending else self.comments
        return _Resp(json.dumps(ordered).encode())


class TestGithubReader:
    def test_reads_all_pages_with_link_headers(self):
        server = _GithubServer(_fillers(250))
        got = merge_github.fetch_comments(_OWNER, _REPO, 7, token="t", opener=server)
        assert [c["id"] for c in got] == list(range(1, 251))
        assert server.comment_gets == 3

    def test_reads_all_pages_without_link_headers(self):
        server = _GithubServer(_fillers(250), link_headers=False)
        got = merge_github.fetch_comments(_OWNER, _REPO, 7, token="t", opener=server)
        assert len(got) == 250

    def test_single_page_costs_two_requests(self):
        # No Link header proves nothing, so the empty page after it ends the list.
        server = _GithubServer(_fillers(5))
        got = merge_github.fetch_comments(_OWNER, _REPO, 7, token="t", opener=server)
        assert len(got) == 5
        assert server.comment_gets == 2

    def test_no_link_header_and_server_page_cap_below_per_page_returns_all(self):
        server = _GithubServer(_fillers(75), link_headers=False, max_page_size=30)
        got = merge_github.fetch_comments(_OWNER, _REPO, 7, token="t", opener=server)
        assert [c["id"] for c in got] == list(range(1, 76))

    def test_link_without_next_ends_the_list_without_an_extra_request(self):
        server = _GithubServer(_fillers(250))
        merge_github.fetch_comments(_OWNER, _REPO, 7, token="t", opener=server)
        assert server.comment_gets == 3

    def test_link_header_disappearing_after_next_fails_closed(self):
        calls = {"n": 0}

        def opener(req, timeout=30):
            calls["n"] += 1
            if calls["n"] == 1:
                return _Resp(
                    json.dumps(_fillers(100)).encode(),
                    {"Content-Type": "application/json", "Link": '<http://x>; rel="next"'},
                )
            return _Resp(json.dumps([_comment(i) for i in range(101, 120)]).encode())

        with pytest.raises(GateFactUnavailableError, match="Link header"):
            merge_github.fetch_comments(_OWNER, _REPO, 7, token="t", opener=opener)

    def test_empty_body_200_fails_closed(self):
        def opener(req, timeout=30):
            return _Resp(b"")

        with pytest.raises(GateFactUnavailableError):
            merge_github.fetch_comments(_OWNER, _REPO, 7, token="t", opener=opener)

    def test_order_does_not_depend_on_api_ordering(self):
        server = _GithubServer(_fillers(150), descending=True)
        got = merge_github.fetch_comments(_OWNER, _REPO, 7, token="t", opener=server)
        assert [c["id"] for c in got] == list(range(1, 151))

    def test_request_uses_per_page_100(self):
        seen = []

        def opener(req, timeout=30):
            seen.append(req.full_url)
            return _Resp(b"[]")

        merge_github.fetch_comments(_OWNER, _REPO, 7, token="t", opener=opener)
        assert seen == [
            f"https://api.github.com/repos/{_OWNER}/{_REPO}/issues/7/comments?per_page=100&page=1"
        ]

    def test_page_cap_fails_closed(self):
        # Every page is full, brand new, and advertises a next page, forever.
        calls = {"n": 0}

        def opener(req, timeout=30):
            calls["n"] += 1
            start = calls["n"] * 1000
            return _Resp(
                json.dumps([_comment(start + i) for i in range(100)]).encode(),
                {"Content-Type": "application/json", "Link": '<http://x>; rel="next"'},
            )

        with pytest.raises(GateFactUnavailableError, match="page cap"):
            merge_github.fetch_comments(_OWNER, _REPO, 7, token="t", opener=opener)

    def test_short_non_final_page_continues_while_link_has_next(self):
        server = _GithubServer(_fillers(250), max_page_size=40)
        got = merge_github.fetch_comments(_OWNER, _REPO, 7, token="t", opener=server)
        assert [c["id"] for c in got] == list(range(1, 251))

    def test_server_ignoring_page_with_full_list_fails_closed(self):
        comments = _fillers(150)

        def opener(req, timeout=30):
            return _Resp(json.dumps(comments).encode())

        with pytest.raises(GateFactUnavailableError, match="stalled"):
            merge_github.fetch_comments(_OWNER, _REPO, 7, token="t", opener=opener)

    def test_server_ignoring_page_at_exactly_per_page_fails_closed(self):
        comments = _fillers(comment_paging.GITHUB_PER_PAGE)

        def opener(req, timeout=30):
            return _Resp(
                json.dumps(comments).encode(),
                {"Content-Type": "application/json", "Link": '<http://x>; rel="next"'},
            )

        with pytest.raises(GateFactUnavailableError, match="stalled"):
            merge_github.fetch_comments(_OWNER, _REPO, 7, token="t", opener=opener)

    def test_later_page_http_error_fails_closed(self):
        calls = {"n": 0}

        def opener(req, timeout=30):
            calls["n"] += 1
            if calls["n"] == 1:
                return _Resp(
                    json.dumps([_comment(i) for i in range(100)]).encode(),
                    {"Content-Type": "application/json", "Link": '<http://x>; rel="next"'},
                )
            return _Resp(b"{}", status=500)

        with pytest.raises(GateFactUnavailableError):
            merge_github.fetch_comments(_OWNER, _REPO, 7, token="t", opener=opener)


class TestForgejoReader:
    def test_reads_the_full_list_with_one_get(self):
        server = _ForgejoServer(_fillers(130))
        got = merge_forgejo.fetch_comments(
            "http://git-host.example.com", _OWNER, _REPO, 7, token="t", opener=server
        )
        assert [c["id"] for c in got] == list(range(1, 131))
        assert server.gets == 1

    def test_request_carries_no_page_or_limit_parameter(self):
        server = _ForgejoServer(_fillers(4))
        merge_forgejo.fetch_comments(
            "http://git-host.example.com", _OWNER, _REPO, 7, token="t", opener=server
        )
        assert server.get_urls == [
            f"http://git-host.example.com/api/v1/repos/{_OWNER}/{_REPO}/issues/7/comments"
        ]

    def test_empty_list_is_a_pr_with_no_comments(self):
        def opener(req, timeout=15):
            return _Resp(b"[]")

        got = merge_forgejo.fetch_comments(
            "http://git-host.example.com", _OWNER, _REPO, 7, token="t", opener=opener
        )
        assert got == []

    def test_duplicate_ids_are_collapsed(self):
        def opener(req, timeout=15):
            return _Resp(json.dumps([_comment(2), _comment(1), _comment(2)]).encode())

        got = merge_forgejo.fetch_comments(
            "http://git-host.example.com", _OWNER, _REPO, 7, token="t", opener=opener
        )
        assert [c["id"] for c in got] == [1, 2]

    def test_non_200_fails_closed(self):
        def opener(req, timeout=15):
            return _Resp(b"{}", status=500)

        with pytest.raises(GateFactUnavailableError, match="HTTP 500"):
            merge_forgejo.fetch_comments(
                "http://git-host.example.com", _OWNER, _REPO, 7, token="t", opener=opener
            )

    def test_non_list_body_fails_closed(self):
        def opener(req, timeout=15):
            return _Resp(b'{"message": "nope"}')

        with pytest.raises(GateFactUnavailableError, match="non-list"):
            merge_forgejo.fetch_comments(
                "http://git-host.example.com", _OWNER, _REPO, 7, token="t", opener=opener
            )

    def test_unparseable_body_fails_closed(self):
        def opener(req, timeout=15):
            return _Resp(b"<html>")

        with pytest.raises(GateFactUnavailableError, match="unparseable"):
            merge_forgejo.fetch_comments(
                "http://git-host.example.com", _OWNER, _REPO, 7, token="t", opener=opener
            )

    def test_empty_body_200_fails_closed(self):
        def opener(req, timeout=15):
            return _Resp(b"")

        with pytest.raises(GateFactUnavailableError, match="empty body"):
            merge_forgejo.fetch_comments(
                "http://git-host.example.com", _OWNER, _REPO, 7, token="t", opener=opener
            )

    def test_order_does_not_depend_on_api_ordering(self):
        server = _ForgejoServer(_fillers(120), descending=True)
        got = merge_forgejo.fetch_comments(
            "http://git-host.example.com", _OWNER, _REPO, 7, token="t", opener=server
        )
        assert [c["id"] for c in got] == list(range(1, 121))

    def test_same_list_is_returned_whatever_page_is_requested(self):
        # The live server returned the identical list for page=1, 2 and 3.
        comments = [21259, 21262, 21264, 21265]
        bodies = []

        def request_fn(base, method, path, token, opener=None):
            bodies.append(path)
            return 200, json.dumps([_comment(i) for i in comments]).encode()

        got = comment_paging.list_forgejo_issue_comments(
            request_fn, "http://h", "o", "r", 645, "t"
        )
        assert [c["id"] for c in got] == comments
        assert bodies == ["/api/v1/repos/o/r/issues/645/comments"]


class TestMergeGateUsesLatestVerdict:
    def _verdict_comment(self, i, sha):
        block = build_verdict_block("reviewer-a", "clean", sha, 7)
        return _comment(i, "reviewer-login", f"verdict\n{block}")

    @pytest.mark.parametrize("platform", ["github", "forgejo"])
    def test_latest_verdict_past_first_page_is_current(self, platform):
        comments = [self._verdict_comment(1, _SHA_OLD)] + [
            _comment(i) for i in range(2, 140)
        ] + [self._verdict_comment(140, _SHA_NEW)]
        if platform == "github":
            fetched = merge_github.fetch_comments(
                _OWNER, _REPO, 7, token="t", opener=_GithubServer(comments)
            )
        else:
            fetched = merge_forgejo.fetch_comments(
                "http://git-host.example.com", _OWNER, _REPO, 7, token="t",
                opener=_ForgejoServer(comments),
            )
        verdict = read_reviewer_verdict(fetched, "reviewer-login", _SHA_NEW, 7, _OWNER, _REPO)
        assert verdict.head_sha == _SHA_NEW


class TestPostAndVerifyBeyondFirstPage:
    def test_github_review_post_finds_last_comment(self):
        server = _GithubServer(_fillers(150))
        verified = review_github.post_and_verify_review(
            _OWNER, _REPO, 7, "my review body", "t", opener=server
        )
        assert verified.id == 151
        assert verified.body == "my review body"

    def test_github_review_readback_over_cap_fails_closed(self, monkeypatch):
        server = _GithubServer(_fillers(5))
        real_list = comment_paging.list_github_issue_comments

        def list_then_cap_after_post(*args, **kwargs):
            # The pre-POST dedupe read must succeed; only the readback fails.
            if server.post_body is not None:
                raise comment_paging.CommentPageCapError(1, 100)
            return real_list(*args, **kwargs)

        monkeypatch.setattr(
            comment_paging, "list_github_issue_comments", list_then_cap_after_post
        )
        with pytest.raises(ReviewVerifyError, match="page cap"):
            review_github.post_and_verify_review(
                _OWNER, _REPO, 7, "my review body", "t", opener=server
            )
        assert server.post_body == "my review body"

    def test_forgejo_verify_comment_finds_last_comment(self):
        server = _ForgejoServer(_fillers(130))
        not_before = datetime.now(timezone.utc) - timedelta(seconds=1)
        new = _comment(131, "some-bot", "my review body", created=datetime.now(timezone.utc))
        server.comments.append(new)
        verified = git_host_api.verify_comment_on_pr(
            "http://git-host.example.com", "t", _OWNER, _REPO, "7",
            "my review body", "some-bot", not_before=not_before, opener=server,
        )
        assert verified["id"] == 131

    def test_forgejo_verify_comment_missing_comment_fails_closed(self):
        def opener(req, timeout=15):
            return _Resp(json.dumps([_comment(i) for i in range(50)]).encode())

        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.verify_comment_on_pr(
                "http://git-host.example.com", "t", _OWNER, _REPO, "7",
                "x", "some-bot", not_before=datetime.now(timezone.utc), opener=opener,
            )
        assert exc_info.value.code == git_host_api.EXIT_VERIFY_FAILED
