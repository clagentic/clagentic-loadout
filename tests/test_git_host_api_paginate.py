"""test_git_host_api_paginate.py — the --paginate flag of loadout-git-host-api.

Drives main() through an injected opener (no network). The comment reader
itself is covered by test_comment_paging.py; this file proves the verb wiring:
one JSON array on stdout, fail-closed with no partial output, exactly one GET
on Forgejo, usage errors, and the non-paginate path left untouched.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from clagentic_loadout.transport import attestation, git_host_api, provider_config

GITHUB_URL = "https://api.github.com/repos/some-owner/some-repo/issues/50/comments"
FORGEJO_PATH = "/api/v1/repos/some-owner/some-repo/issues/644/comments"
FORGEJO_BASE = "http://forgejo.example.com:3000"


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    isolated_root = tmp_path / "isolated-user-config-root"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", isolated_root)
    monkeypatch.setattr(attestation, "DEFAULT_USER_CONFIG_ROOT", isolated_root)


class _Provider:
    def resolve_token(self, role: str, *, repo: str | None = None) -> str:
        return "tok-injected"


class _Response:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _comments(first_id: int, count: int) -> list[dict]:
    return [{"id": first_id + i, "body": f"c{first_id + i}"} for i in range(count)]


def _github_opener(total: int, *, per_page: int = 100, link: bool = True, calls=None):
    """Serve `total` comments in `per_page` pages; page past the end is []."""
    everything = _comments(1, total)

    def opener(req, timeout=15):
        if calls is not None:
            calls.append(req.full_url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        page = int(query["page"][0])
        chunk = everything[(page - 1) * per_page : page * per_page]
        last = page * per_page >= total
        headers = {}
        if link:
            headers["Link"] = (
                '<https://api.github.com/x?page=1>; rel="prev"'
                if last
                else '<https://api.github.com/x?page=%d>; rel="next"' % (page + 1)
            )
        return _Response(200, json.dumps(chunk).encode(), headers)

    return opener


def _run(argv, opener, capsys):
    rc = git_host_api.main(argv, token_provider=_Provider(), opener=opener)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


class TestPaginateGithub:
    def test_more_than_100_comments_across_pages_all_returned(self, capsys):
        calls: list[str] = []
        rc, out, _err = _run(
            ["--caller", "some-role", "--paginate", "GET", GITHUB_URL],
            _github_opener(250, calls=calls),
            capsys,
        )
        assert rc == git_host_api.EXIT_OK
        comments = json.loads(out)
        assert len(comments) == 250
        assert comments[-1]["id"] == 250
        assert [c["id"] for c in comments] == sorted(c["id"] for c in comments)
        assert len(calls) == 3

    def test_single_json_array_on_stdout(self, capsys):
        rc, out, _err = _run(
            ["--caller", "some-role", "--paginate", GITHUB_URL],
            _github_opener(120),
            capsys,
        )
        assert rc == git_host_api.EXIT_OK
        assert out.count("\n") == 1
        assert isinstance(json.loads(out), list)

    def test_page_without_proving_signal_fails_closed_with_no_output(self, capsys):
        # Page 1 advertises a next page; page 2 carries no Link header at all,
        # so completeness cannot be proven.
        everything = _comments(1, 150)

        def opener(req, timeout=15):
            page = int(urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)["page"][0])
            chunk = everything[(page - 1) * 100 : page * 100]
            headers = {"Link": '<https://api.github.com/x?page=2>; rel="next"'} if page == 1 else {}
            return _Response(200, json.dumps(chunk).encode(), headers)

        rc, out, err = _run(["--caller", "some-role", "--paginate", GITHUB_URL], opener, capsys)
        assert rc == git_host_api.EXIT_PAGINATION_FAILED
        assert out == ""
        assert "Link" in err

    def test_non_200_page_fails_closed_with_no_output(self, capsys):
        def opener(req, timeout=15):
            page = int(urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)["page"][0])
            if page == 1:
                return _Response(
                    200,
                    json.dumps(_comments(1, 100)).encode(),
                    {"Link": '<https://api.github.com/x?page=2>; rel="next"'},
                )
            return _Response(500, b"{}")

        rc, out, err = _run(["--caller", "some-role", "--paginate", GITHUB_URL], opener, capsys)
        assert rc == git_host_api.EXIT_PAGINATION_FAILED
        assert out == ""
        assert "500" in err

    def test_network_failure_mid_walk_prints_no_partial_array(self, capsys):
        import urllib.error

        state = {"n": 0}

        def opener(req, timeout=15):
            state["n"] += 1
            if state["n"] == 1:
                return _Response(
                    200,
                    json.dumps(_comments(1, 100)).encode(),
                    {"Link": '<https://api.github.com/x?page=2>; rel="next"'},
                )
            raise urllib.error.URLError("connection reset")

        rc, out, _err = _run(["--caller", "some-role", "--paginate", GITHUB_URL], opener, capsys)
        assert rc == git_host_api.EXIT_PAGINATION_FAILED
        assert out == ""

    def test_token_goes_only_in_authorization_header(self, capsys):
        seen: list = []

        def opener(req, timeout=15):
            seen.append((req.full_url, req.get_header("Authorization")))
            return _Response(200, b"[]")

        rc, _out, _err = _run(["--caller", "some-role", "--paginate", GITHUB_URL], opener, capsys)
        assert rc == git_host_api.EXIT_OK
        assert seen == [(GITHUB_URL + "?per_page=100&page=1", "token tok-injected")]


class TestPaginateForgejo:
    def test_exactly_one_get_and_full_array(self, capsys):
        calls: list[tuple[str, str]] = []

        def opener(req, timeout=15):
            calls.append((req.get_method(), req.full_url))
            return _Response(200, json.dumps(_comments(1, 130)).encode())

        rc, out, _err = _run(
            [
                "--caller", "some-role",
                "--git-host-base-url", FORGEJO_BASE,
                "--paginate", "GET", FORGEJO_PATH,
            ],
            opener,
            capsys,
        )
        assert rc == git_host_api.EXIT_OK
        assert calls == [("GET", FORGEJO_BASE + FORGEJO_PATH)]
        comments = json.loads(out)
        assert len(comments) == 130
        assert comments[-1]["id"] == 130

    def test_non_200_fails_closed_with_no_output(self, capsys):
        rc, out, err = _run(
            ["--caller", "some-role", "--git-host-base-url", FORGEJO_BASE, "--paginate", FORGEJO_PATH],
            lambda req, timeout=15: _Response(502, b"bad gateway"),
            capsys,
        )
        assert rc == git_host_api.EXIT_PAGINATION_FAILED
        assert out == ""
        assert "502" in err

    def test_absolute_url_on_the_git_host_is_accepted(self, capsys):
        calls: list[str] = []

        def opener(req, timeout=15):
            calls.append(req.full_url)
            return _Response(200, b"[]")

        rc, out, _err = _run(
            [
                "--caller", "some-role",
                "--git-host-base-url", FORGEJO_BASE,
                "--paginate", FORGEJO_BASE + FORGEJO_PATH,
            ],
            opener,
            capsys,
        )
        assert rc == git_host_api.EXIT_OK
        assert json.loads(out) == []
        assert calls == [FORGEJO_BASE + FORGEJO_PATH]


class TestPaginateUsageErrors:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/repos/o/r/pulls/1.diff",
            "/api/v1/repos/o/r/issues/1",
            "/api/v1/repos/o/r/issues/comments/9",
            "/api/v1/repos/o/r/issues/1/comments?since=2020-01-01T00:00:00Z",
            "https://api.github.com/repos/o/r/pulls/1/reviews",
            "https://api.github.com/repos/o/r/issues/1/comments?per_page=5",
        ],
    )
    def test_non_comments_url_is_usage_error_with_no_request(self, path, capsys):
        calls: list = []

        def opener(req, timeout=15):
            calls.append(req.full_url)
            return _Response(200, b"[]")

        rc, out, err = _run(["--caller", "some-role", "--paginate", path], opener, capsys)
        assert rc == git_host_api.EXIT_USAGE
        assert calls == []
        assert out == ""
        assert "--paginate" in err

    @pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
    def test_non_get_method_is_usage_error(self, method, capsys):
        calls: list = []
        rc, out, _err = _run(
            ["--caller", "some-role", "--paginate", method, FORGEJO_PATH],
            lambda req, timeout=15: calls.append(req) or _Response(200, b"[]"),
            capsys,
        )
        assert rc == git_host_api.EXIT_USAGE
        assert calls == []
        assert out == ""


class TestNonPaginateUnchanged:
    def test_github_get_is_one_bare_request_streamed_verbatim(self, capsys):
        calls: list[str] = []
        body = b'[{"id": 1}]'

        def opener(req, timeout=15):
            calls.append(req.full_url)
            return _Response(200, body, {"Link": '<https://api.github.com/x?page=2>; rel="next"'})

        rc, out, _err = _run(["--caller", "some-role", "GET", GITHUB_URL], opener, capsys)
        assert rc == git_host_api.EXIT_OK
        assert calls == [GITHUB_URL]
        assert out == body.decode()

    def test_forgejo_get_is_one_bare_request_streamed_verbatim(self, capsys):
        calls: list[str] = []
        body = b'[{"id": 1}]'

        def opener(req, timeout=15):
            calls.append(req.full_url)
            return _Response(200, body)

        rc, out, _err = _run(
            ["--caller", "some-role", "--git-host-base-url", FORGEJO_BASE, FORGEJO_PATH],
            opener,
            capsys,
        )
        assert rc == git_host_api.EXIT_OK
        assert calls == [FORGEJO_BASE + FORGEJO_PATH]
        assert out == body.decode()
