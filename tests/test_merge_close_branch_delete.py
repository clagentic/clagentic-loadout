"""test_merge_close_branch_delete.py — loadout-close-pr --delete-branch.

Drives the verb end to end through a stateful stub host for BOTH platforms
(no network, no git). Covers the deleted path, each keep reason, a delete API
error, and the flag-absent path (no branch call of any kind).
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse

import pytest

from clagentic_loadout.merge import close_verb

_OWNER = "some-owner"
_REPO = "some-repo"
_BRANCH = "chore/some-work"
_PR_SHA = "a" * 40
_NEWER_SHA = "b" * 40
_FORGEJO_BASE = "https://git.example.test"


class _Token:
    def resolve_token(self, role: str) -> str:
        return "tok-123"


class _Allow:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        return True


class _Resp:
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


class _StubHost:
    """One repository on one platform. `calls` records (method, route) for
    every request, where route is the platform-neutral operation name."""

    def __init__(self, platform: str, **overrides):
        self.platform = platform
        self.calls: list[tuple[str, str]] = []
        self.head_repo = f"{_OWNER}/{_REPO}"
        self.head_ref = _BRANCH
        self.base_ref = "main"
        self.default_branch = "main"
        self.branch_tip: str | None = _PR_SHA
        self.protected = False
        self.delete_status = 204
        self.pr_sha = _PR_SHA
        # A push that lands between the close and the cleanup: the branch tip
        # and the PR record's head both move once the close has happened.
        self.advance_on_close = False
        self.fail_pr_reads = 0
        for key, value in overrides.items():
            setattr(self, key, value)

    def _pr(self) -> dict:
        head_repo = {"full_name": self.head_repo} if self.head_repo else None
        return {
            "state": "closed",
            "head": {"ref": self.head_ref, "sha": self.pr_sha, "repo": head_repo},
            "base": {"ref": self.base_ref, "repo": {"full_name": f"{_OWNER}/{_REPO}"}},
        }

    def _route(self, method: str, url: str) -> str:
        path = urllib.parse.urlparse(url).path
        if self.platform == "forgejo":
            path = path.removeprefix("/api/v1")
        if path.endswith(("/pulls/42", "/issues/42")):
            return "close" if method == "PATCH" else "pr"
        if path == f"/repos/{_OWNER}/{_REPO}":
            return "repo"
        branches = f"/repos/{_OWNER}/{_REPO}/branches/{self.head_ref}"
        refs = f"/repos/{_OWNER}/{_REPO}/git/refs/heads/{self.head_ref}"
        if method == "GET" and path == branches:
            return "branch"
        if method == "DELETE" and path == (branches if self.platform == "forgejo" else refs):
            return "delete"
        raise AssertionError(f"unexpected {method} {url}")

    def opener(self, req, timeout=15):
        method = req.get_method()
        route = self._route(method, req.full_url)
        self.calls.append((method, route))
        if route in ("close",):
            if self.advance_on_close:
                self.branch_tip = _NEWER_SHA
                self.pr_sha = _NEWER_SHA
            return _Resp(200, b"{}")
        if route == "pr":
            if self.fail_pr_reads > 0:
                self.fail_pr_reads -= 1
                raise urllib.error.HTTPError(
                    req.full_url, 500, "err", {}, io.BytesIO(b"{}")
                )
            return _Resp(200, json.dumps(self._pr()).encode())
        if route == "repo":
            return _Resp(200, json.dumps({"default_branch": self.default_branch}).encode())
        if route == "branch":
            if self.branch_tip is None:
                return _Resp(404, b"{}")
            key = "id" if self.platform == "forgejo" else "sha"
            body = {"commit": {key: self.branch_tip}, "protected": self.protected}
            return _Resp(200, json.dumps(body).encode())
        if self.delete_status >= 300:
            raise urllib.error.HTTPError(
                req.full_url, self.delete_status, "err", {}, io.BytesIO(b'{"message": "denied"}')
            )
        return _Resp(self.delete_status, b"")

    @property
    def routes(self) -> list[str]:
        return [r for _, r in self.calls]


def _run(host: _StubHost, capsys, *extra: str):
    argv = [
        "--platform", host.platform, "--repo", f"{_OWNER}/{_REPO}", "--pr", "42",
        "--git-host-base-url", _FORGEJO_BASE, *extra,
    ]
    code = close_verb.main(
        argv, token_provider=_Token(), authority_provider=_Allow(), opener=host.opener
    )
    captured = capsys.readouterr()
    return code, json.loads(captured.out.strip().splitlines()[-1]), captured.err


@pytest.fixture(params=["forgejo", "github"])
def platform(request):
    return request.param


class TestDeleteBranch:
    def test_deleted(self, platform, capsys):
        host = _StubHost(platform)
        code, payload, err = _run(host, capsys, "--delete-branch")
        assert code == close_verb.EXIT_OK
        assert payload["readback"]["verified"] is True
        assert payload["branch_deletion"]["outcome"] == "deleted"
        assert payload["branch_deletion"]["branch"] == _BRANCH
        assert host.routes[-1] == "delete"
        assert f"branch deletion deleted -- branch '{_BRANCH}' in {_OWNER}/{_REPO}" in err

    @pytest.mark.parametrize(
        "overrides, reason",
        [
            ({"head_repo": "someone-else/fork"}, "fork-head"),
            ({"head_repo": None}, "head-repo-unavailable"),
            ({"branch_tip": _NEWER_SHA}, "head-advanced"),
            ({"protected": True}, "protected"),
            ({"head_ref": "main"}, "base-branch"),
            ({"head_ref": "trunk", "default_branch": "trunk"}, "default-branch"),
            ({"branch_tip": None}, "branch-absent"),
        ],
    )
    def test_kept_with_reason_and_close_unaffected(self, platform, capsys, overrides, reason):
        host = _StubHost(platform, **overrides)
        code, payload, err = _run(host, capsys, "--delete-branch")
        assert code == close_verb.EXIT_OK
        assert payload["readback"]["verified"] is True
        assert payload["branch_deletion"]["outcome"] == "kept"
        assert payload["branch_deletion"]["reason"] == reason
        assert "delete" not in host.routes
        assert "branch deletion kept" in err and reason in err

    def test_delete_api_error_is_reported_not_raised(self, platform, capsys):
        host = _StubHost(platform, delete_status=403)
        code, payload, err = _run(host, capsys, "--delete-branch")
        assert code == close_verb.EXIT_OK
        assert payload["readback"]["verified"] is True
        assert payload["branch_deletion"]["outcome"] == "error"
        assert "403" in payload["branch_deletion"]["reason"]
        assert "branch deletion error" in err

    def test_flag_absent_makes_no_branch_call_and_no_new_key(self, platform, capsys):
        host = _StubHost(platform)
        code, payload, err = _run(host, capsys)
        assert code == close_verb.EXIT_OK
        assert "branch_deletion" not in payload
        assert host.routes == ["close", "pr"]
        assert "branch deletion" not in err

    def test_push_after_pre_close_read_is_kept(self, platform, capsys):
        host = _StubHost(platform, advance_on_close=True)
        code, payload, _ = _run(host, capsys, "--delete-branch")
        assert code == close_verb.EXIT_OK
        assert payload["branch_deletion"]["outcome"] == "kept"
        assert payload["branch_deletion"]["reason"] == "head-advanced"
        assert host.routes[:3] == ["pr", "close", "pr"]
        assert "delete" not in host.routes

    def test_pre_close_read_failure_keeps_branch_and_still_closes(self, platform, capsys):
        host = _StubHost(platform, fail_pr_reads=1)
        code, payload, _ = _run(host, capsys, "--delete-branch")
        assert code == close_verb.EXIT_OK
        assert payload["readback"]["verified"] is True
        assert payload["branch_deletion"]["outcome"] == "kept"
        assert payload["branch_deletion"]["reason"] == "head-unknown"
        assert "delete" not in host.routes

    def test_flag_absent_adds_no_pre_close_read(self, platform, capsys):
        host = _StubHost(platform)
        _run(host, capsys)
        assert host.routes[0] == "close"

    def test_unconfirmed_close_never_reaches_branch_deletion(self, platform, capsys):
        host = _StubHost(platform)
        original = host._pr
        host._pr = lambda: {**original(), "state": "open"}
        code = close_verb.main(
            ["--platform", platform, "--repo", f"{_OWNER}/{_REPO}", "--pr", "42",
             "--git-host-base-url", _FORGEJO_BASE, "--delete-branch"],
            token_provider=_Token(), authority_provider=_Allow(), opener=host.opener,
        )
        assert code == close_verb.EXIT_CLOSE_READBACK_FAILED
        assert host.routes == ["pr", "close", "pr"]
