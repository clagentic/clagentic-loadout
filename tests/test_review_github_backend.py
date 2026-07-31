"""test_review_github_backend.py — tests for
clagentic_loadout.review.github_backend (lr-412f, Wave B slice 2;
identity-resolution fix lr-d31e; verdict-transport parity fix lr-71f467).

Coverage:
  - assert_platform_is_github: passes for 'github', raises
    PlatformMismatchError for 'forgejo' and for an unrecognized value.
  - resolve_own_login: BOTH token-type paths --
      (a) GET /user 200 -> login returned directly (PAT/OAuth path,
          UNCHANGED by lr-d31e).
      (b) GET /user 403 -> bot login resolved from a CONFIGURED app slug
          as '<slug>[bot]' -- lr-d31e replaces the old (broken) GET /app
          live-lookup fallback entirely; these tests assert NO request to
          /app is ever made.
    Plus failure paths: /user non-200/non-403 raises ReviewVerifyError
    without consulting the app-slug config at all; /user 403 with NO app
    slug configured fails closed with a ReviewVerifyError naming the
    credential-type-determined 403 and both config seams that resolve it.
  - post_and_verify_review (lr-71f467: posts an ISSUE COMMENT, not a native
    PR review -- see review.github_backend's module docstring "VERDICT-
    TRANSPORT PARITY" section): success path (id/login/body all match,
    freshness anchor satisfied); FAILURE modes -- POST non-2xx
    (ReviewPostError), readback non-200 (ReviewVerifyError), readback finds
    no matching comment / wrong login / wrong PR / stale (freshness-anchor
    failure) (ReviewVerifyError). Mocked HTTP throughout -- no real network.
  - Redirect hardening (lr-412f pre-merge security review finding):
    _github_request's DEFAULT opener (no `opener` injected) must be built
    via transport.redirect_guard.no_redirect_opener(), never bare
    urllib.request.urlopen -- every call here carries a live GitHub bearer/
    App-installation token in Authorization, and urllib's default redirect
    handler would replay that header to whatever host a 3xx Location names.
    Covers the call shapes this backend makes (GET /user, the comment POST,
    and the readback GET -- GET /app is never called post-lr-d31e): a 3xx
    surfaces as a failure (ReviewPostError or ReviewVerifyError, never a
    false 'verified' result), and -- using the SAME injected-fake-opener
    pattern as the rest of this file -- proves only ONE request is ever made
    per call, never a second request to a redirect target, with no real
    network and no wall-clock dependence.
"""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.review import github_backend
from clagentic_loadout.review.errors import (
    DeleteOwnCommentRefusedError,
    PlatformMismatchError,
    ReviewPostError,
    ReviewVerifyError,
)
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.transport.github_app_config import GithubAppSlugNotConfiguredError


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


def _json_response(status: int, payload) -> _FakeResponse:
    return _FakeResponse(status, json.dumps(payload).encode("utf-8"))


def _http_error(url: str, code: int, payload: dict) -> "Exception":
    import io
    import urllib.error

    return urllib.error.HTTPError(
        url, code, "err", {}, io.BytesIO(json.dumps(payload).encode("utf-8"))
    )


def _configure_app_slug(monkeypatch, slug: str) -> None:
    """Test helper: makes resolve_own_login's app-slug resolution return
    *slug* deterministically, without touching real env/config state --
    patches github_backend's own imported name (the same pattern this file
    already uses for `no_redirect_opener`), so no test leaks into or reads
    from the real process environment or the real
    ~/.config/clagentic/loadout/config.yaml."""
    monkeypatch.setattr(github_backend, "resolve_github_app_slug", lambda: slug)


def _configure_app_slug_unconfigured(monkeypatch) -> None:
    def _raise():
        raise GithubAppSlugNotConfiguredError(
            "GitHub App slug is not configured: neither "
            "CLAGENTIC_LOADOUT_GITHUB_APP_SLUG nor the user-level config "
            "file's github_app.slug key names one."
        )

    monkeypatch.setattr(github_backend, "resolve_github_app_slug", _raise)


# ---------------------------------------------------------------------------
# assert_platform_is_github
# ---------------------------------------------------------------------------


class TestAssertPlatformIsGithub:
    def test_github_passes(self):
        github_backend.assert_platform_is_github("o", "r", explicit_platform=PLATFORM_GITHUB)

    def test_forgejo_raises(self):
        with pytest.raises(PlatformMismatchError, match="WRONG PLATFORM"):
            github_backend.assert_platform_is_github(
                "o", "r", explicit_platform=PLATFORM_FORGEJO
            )

    def test_unrecognized_platform_raises(self):
        with pytest.raises(PlatformMismatchError, match="unrecognized platform"):
            github_backend.assert_platform_is_github("o", "r", explicit_platform="bitbucket")


# ---------------------------------------------------------------------------
# resolve_own_login -- both token-type paths
# ---------------------------------------------------------------------------


class TestResolveOwnLogin:
    def test_pat_oauth_path_get_user_200(self):
        def opener(req, timeout=15):
            assert req.full_url == f"{github_backend._GITHUB_API}/user"
            return _json_response(200, {"login": "some-human"})

        login = github_backend.resolve_own_login("pat-token", opener=opener)
        assert login == "some-human"

    def test_app_installation_token_path_403_resolves_from_configured_slug_no_app_call(
        self, monkeypatch
    ):
        """lr-d31e: an App installation token's 403 on /user resolves the
        bot login from CONFIGURED slug, never a live GET /app call -- that
        endpoint is JWT-only and this backend never holds a JWT."""
        _configure_app_slug(monkeypatch, "some-reviewer-app")
        calls = []

        def opener(req, timeout=15):
            calls.append(req.full_url)
            assert req.full_url.endswith("/user")
            raise _http_error(req.full_url, 403, {"message": "not accessible"})

        login = github_backend.resolve_own_login("installation-token", opener=opener)
        assert login == "some-reviewer-app[bot]"
        # Exactly one request -- /user -- was ever made. No /app call.
        assert calls == [f"{github_backend._GITHUB_API}/user"]

    def test_user_403_with_unconfigured_slug_fails_closed(self, monkeypatch):
        """lr-d31e: no live /app fallback exists any more -- an unconfigured
        slug on a 403'd /user is a genuine config gap, reported as such."""
        _configure_app_slug_unconfigured(monkeypatch)
        calls = []

        def opener(req, timeout=15):
            calls.append(req.full_url)
            raise _http_error(req.full_url, 403, {})

        with pytest.raises(ReviewVerifyError, match="INSTALLATION token") as excinfo:
            github_backend.resolve_own_login("bad-token", opener=opener)
        # The error names the credential-type-determined 403 and points at
        # the config seam, never implying a further live call would help.
        assert "GET /app" in str(excinfo.value)
        assert "not configured" in str(excinfo.value)
        # No /app request was ever attempted.
        assert calls == [f"{github_backend._GITHUB_API}/user"]

    def test_user_non_200_non_403_raises_without_consulting_app_slug(self, monkeypatch):
        def _fail_if_called():
            raise AssertionError(
                "app-slug resolution must not be consulted for a non-403 "
                "/user failure"
            )

        monkeypatch.setattr(github_backend, "resolve_github_app_slug", _fail_if_called)
        calls = []

        def opener(req, timeout=15):
            calls.append(req.full_url)
            raise _http_error(req.full_url, 500, {})

        with pytest.raises(ReviewVerifyError, match="neither a 200 success nor"):
            github_backend.resolve_own_login("bad-token", opener=opener)
        assert calls == [f"{github_backend._GITHUB_API}/user"]


class TestResolveOwnLoginConfigFirstShortCircuit:
    """lr-b2d1c3: when a caller's own github_app.slugs.<caller> entry is
    configured, resolve_own_login resolves the bot login from that slug
    FIRST, WITHOUT ever issuing the GET /user probe -- the probe is a
    guaranteed-403 wasted call for an installation token (lr-e41f)."""

    def test_configured_slug_caller_resolves_with_no_get_user_call(self, monkeypatch):
        captured = {}

        def fake_resolve(*, caller=None):
            captured["caller"] = caller
            return "reviewer-app"

        monkeypatch.setattr(github_backend, "resolve_github_app_slug", fake_resolve)

        def opener(req, timeout=15):
            raise AssertionError(
                "GET /user must never be called when a configured slug "
                "short-circuits resolution"
            )

        login = github_backend.resolve_own_login("tok", caller="reviewer", opener=opener)
        assert login == "reviewer-app[bot]"
        assert captured["caller"] == "reviewer"

    def test_no_slug_configured_for_caller_falls_back_to_get_user(self, monkeypatch):
        """A caller IS supplied, but resolve_github_app_slug has no entry
        for it -- falls through to the GET /user probe exactly like the
        no-caller-supplied path always has."""

        def fake_resolve(*, caller=None):
            raise GithubAppSlugNotConfiguredError(f"no slug for {caller!r}")

        monkeypatch.setattr(github_backend, "resolve_github_app_slug", fake_resolve)

        calls = []

        def opener(req, timeout=15):
            calls.append(req.full_url)
            return _json_response(200, {"login": "some-human"})

        login = github_backend.resolve_own_login("pat-token", caller="reviewer", opener=opener)
        assert login == "some-human"
        assert calls == [f"{github_backend._GITHUB_API}/user"]

    def test_pat_token_path_unaffected_by_config_first_change(self):
        """No caller supplied at all -- byte-identical to before this
        change: GET /user is consulted first and the short-circuit helper
        is never even reached (caller is falsy)."""
        calls = []

        def opener(req, timeout=15):
            calls.append(req.full_url)
            return _json_response(200, {"login": "some-human"})

        login = github_backend.resolve_own_login("pat-token", opener=opener)
        assert login == "some-human"
        assert calls == [f"{github_backend._GITHUB_API}/user"]

    def test_no_caller_never_consults_configured_slug_short_circuit(self, monkeypatch):
        """caller=None must never reach the config-first short-circuit at
        all -- resolve_github_app_slug is only called (if ever) from the
        post-403 fallback path, matching pre-lr-b2d1c3 zero-arg call shape."""
        calls = []
        monkeypatch.setattr(
            github_backend, "resolve_github_app_slug",
            lambda: calls.append("called") or "legacy-app",
        )

        def opener(req, timeout=15):
            return _json_response(200, {"login": "some-human"})

        login = github_backend.resolve_own_login("pat-token", opener=opener)
        assert login == "some-human"
        assert calls == []


class TestResolveOwnLoginCallerForwarding:
    """lr-d72d: `caller` is forwarded to resolve_github_app_slug ONLY when
    supplied and non-empty -- resolve_own_login's own default (caller=None)
    must keep calling resolve_github_app_slug with ZERO arguments, so every
    pre-lr-d72d call site (including this file's own zero-arg-lambda
    monkeypatch helpers) keeps working unmodified."""

    def test_no_caller_calls_resolve_github_app_slug_with_zero_args(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            github_backend, "resolve_github_app_slug", lambda: calls.append("called") or "app-slug"
        )

        def opener(req, timeout=15):
            raise _http_error(req.full_url, 403, {})

        login = github_backend.resolve_own_login("tok", opener=opener)
        assert login == "app-slug[bot]"
        assert calls == ["called"]

    def test_caller_supplied_forwards_caller_kwarg(self, monkeypatch):
        captured = {}

        def fake_resolve(*, caller=None):
            captured["caller"] = caller
            return "reviewer-app"

        monkeypatch.setattr(github_backend, "resolve_github_app_slug", fake_resolve)

        def opener(req, timeout=15):
            raise _http_error(req.full_url, 403, {})

        login = github_backend.resolve_own_login("tok", caller="reviewer", opener=opener)
        assert login == "reviewer-app[bot]"
        assert captured["caller"] == "reviewer"


# ---------------------------------------------------------------------------
# post_and_verify_review
# ---------------------------------------------------------------------------


def _future_timestamp() -> str:
    """A created_at timestamp guaranteed to be at/after any pre_post_utc
    captured during the test -- avoids wall-clock flakiness in the freshness
    anchor check (matches this file's no-wall-clock-dependence contract)."""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()


def _make_success_opener(*, own_login="reviewer-bot[bot]", posted_id=99, pr_number=42):
    """Builds an opener sequencing (lr-39f8 dedupe-first order, lr-71f467
    issue-comment transport): GET /user (403) -> GET issue comments (dedupe
    check, MISS -- empty) -> POST comment -> GET issue comments (verify
    readback, match). No /app call -- lr-d31e resolves the bot login from
    configured slug instead."""
    comments_get_calls = {"count": 0}

    def opener(req, timeout=15):
        url = req.full_url
        if url.endswith("/user"):
            raise _http_error(url, 403, {})
        if req.get_method() == "GET" and url.endswith(f"/issues/{pr_number}/comments"):
            comments_get_calls["count"] += 1
            if comments_get_calls["count"] == 1:
                # Dedupe pre-check: no existing own comment yet.
                return _json_response(200, [])
            return _json_response(
                200,
                [
                    {
                        "id": posted_id,
                        "user": {"login": own_login},
                        "body": "LGTM, findings: none",
                        "html_url": "http://readback-confirmed",
                        "created_at": _future_timestamp(),
                    }
                ],
            )
        if req.get_method() == "POST" and url.endswith(f"/issues/{pr_number}/comments"):
            return _json_response(200, {"id": posted_id, "html_url": "http://post-response"})
        raise AssertionError(f"unexpected request: {req.get_method()} {url}")

    return opener


class TestPostAndVerifyReviewSuccess:
    def test_success_returns_verified_review_from_readback(self, monkeypatch):
        _configure_app_slug(monkeypatch, "reviewer-bot")
        opener = _make_success_opener()
        verified = github_backend.post_and_verify_review(
            "some-owner", "some-repo", 42, "LGTM, findings: none", "tok", opener=opener
        )
        assert verified.id == 99
        assert verified.url == "http://readback-confirmed"  # sourced from READBACK, not POST
        assert verified.login == "reviewer-bot[bot]"

    def test_caller_kwarg_forwarded_to_own_login_resolution(self, monkeypatch):
        """lr-d72d: post_and_verify_review's optional `caller` reaches
        resolve_github_app_slug via resolve_own_login."""
        captured = {}

        def fake_resolve(*, caller=None):
            captured["caller"] = caller
            return "security-app"

        monkeypatch.setattr(github_backend, "resolve_github_app_slug", fake_resolve)
        opener = _make_success_opener(own_login="security-app[bot]")
        verified = github_backend.post_and_verify_review(
            "some-owner", "some-repo", 42, "LGTM, findings: none", "tok",
            caller="security", opener=opener,
        )
        assert verified.login == "security-app[bot]"
        assert captured["caller"] == "security"


class _FakeReviewsServer:
    """In-memory stand-in for GitHub's issues/{pr}/comments endpoint
    (lr-71f467: the verdict transport this backend now posts to), driving a
    stateful opener across MULTIPLE post_and_verify_review calls -- proves
    the lr-39f8 read-back-before-post dedupe end to end (not just against a
    single call's mocked sequence): a POST actually appends to this list, and
    the NEXT call's dedupe pre-check GET sees what the previous call posted,
    exactly like the real PEACHES-on-PR#318 retry scenario (three
    invocations against the same live PR)."""

    def __init__(self, *, own_login="reviewer-bot[bot]", pr_number=42):
        self.own_login = own_login
        self.pr_number = pr_number
        self.reviews: list[dict] = []
        self._next_id = 100
        self.post_count = 0

    def opener(self, req, timeout=15):
        url = req.full_url
        if url.endswith("/user"):
            raise _http_error(url, 403, {})
        if req.get_method() == "GET" and url.endswith(f"/issues/{self.pr_number}/comments"):
            return _json_response(200, list(self.reviews))
        if req.get_method() == "POST" and url.endswith(f"/issues/{self.pr_number}/comments"):
            self.post_count += 1
            payload = json.loads(req.data.decode("utf-8"))
            review = {
                "id": self._next_id,
                "user": {"login": self.own_login},
                "body": payload["body"],
                "html_url": f"http://readback-confirmed/{self._next_id}",
                "created_at": _future_timestamp(),
            }
            self._next_id += 1
            self.reviews.append(review)
            return _json_response(200, {"id": review["id"], "html_url": review["html_url"]})
        raise AssertionError(f"unexpected request: {req.get_method()} {url}")


class TestPostAndVerifyReviewDedupe:
    """lr-39f8: read-back-before-post idempotency. Two consecutive
    post_and_verify_review calls with the SAME body against the same live PR
    state must produce exactly ONE POST (one review); a call with a CHANGED
    body must always post fresh."""

    def test_two_consecutive_calls_same_body_post_exactly_once(self, monkeypatch):
        _configure_app_slug(monkeypatch, "reviewer-bot")
        server = _FakeReviewsServer()

        first = github_backend.post_and_verify_review(
            "some-owner", "some-repo", 42, "blocking: findings here", "tok",
            opener=server.opener,
        )
        second = github_backend.post_and_verify_review(
            "some-owner", "some-repo", 42, "blocking: findings here", "tok",
            opener=server.opener,
        )

        assert server.post_count == 1
        assert len(server.reviews) == 1
        # Both calls return the SAME review (the one real POST that landed).
        assert first.id == second.id
        assert second.id == server.reviews[0]["id"]

    def test_three_consecutive_calls_same_body_post_exactly_once(self, monkeypatch):
        """Mirrors the PR#318 evidence exactly: three retried invocations of
        the same review, identical body -- must converge on ONE landed
        review, not three."""
        _configure_app_slug(monkeypatch, "reviewer-bot")
        server = _FakeReviewsServer()

        for _ in range(3):
            github_backend.post_and_verify_review(
                "some-owner", "some-repo", 42, "blocking: findings here", "tok",
                opener=server.opener,
            )

        assert server.post_count == 1
        assert len(server.reviews) == 1

    def test_changed_body_always_posts_fresh(self, monkeypatch):
        """A legitimate later re-review with DIFFERENT findings must never
        be suppressed by the dedupe check -- exact-body-equality gating, not
        substring, so a changed body is never mistaken for a repost."""
        _configure_app_slug(monkeypatch, "reviewer-bot")
        server = _FakeReviewsServer()

        first = github_backend.post_and_verify_review(
            "some-owner", "some-repo", 42, "blocking: findings here", "tok",
            opener=server.opener,
        )
        second = github_backend.post_and_verify_review(
            "some-owner", "some-repo", 42, "blocking: DIFFERENT findings now", "tok",
            opener=server.opener,
        )

        assert server.post_count == 2
        assert len(server.reviews) == 2
        assert first.id != second.id

    def test_dedupe_scoped_to_own_login_never_matches_a_different_authors_comment(
        self, monkeypatch
    ):
        """A comment with the identical body but authored by a DIFFERENT
        login (e.g. BOBBIE's own comment, or a stale comment from a prior
        deployment's differently-slugged app) must never be treated as this
        caller's own prior post -- the dedupe check is scoped to own_login,
        exactly like the mandatory post-POST readback already is."""
        _configure_app_slug(monkeypatch, "reviewer-bot")
        server = _FakeReviewsServer()
        server.reviews.append(
            {
                "id": 1,
                "user": {"login": "someone-else-bot[bot]"},
                "body": "blocking: findings here",
                "html_url": "http://other",
                "created_at": _future_timestamp(),
            }
        )

        github_backend.post_and_verify_review(
            "some-owner", "some-repo", 42, "blocking: findings here", "tok",
            opener=server.opener,
        )

        assert server.post_count == 1
        assert len(server.reviews) == 2


class TestFindExistingOwnComment:
    """Unit coverage for _find_existing_own_comment in isolation, independent
    of the HTTP call sequence."""

    def test_no_comments_returns_none(self):
        assert github_backend._find_existing_own_comment(
            [], own_login="me", body="x"
        ) is None

    def test_exact_body_match_own_login_returns_verified_review(self):
        comments = [
            {
                "id": 7,
                "user": {"login": "me"},
                "body": "exact text",
                "html_url": "http://x",
            }
        ]
        result = github_backend._find_existing_own_comment(
            comments, own_login="me", body="exact text"
        )
        assert result is not None
        assert result.id == 7
        assert result.login == "me"
        assert result.url == "http://x"

    def test_substring_only_match_does_not_count(self):
        """Exact-equality gate, not substring -- a shorter posted body must
        never spuriously match a longer pre-existing comment, and vice versa."""
        comments = [
            {
                "id": 7,
                "user": {"login": "me"},
                "body": "exact text plus extra content",
                "html_url": "http://x",
            }
        ]
        assert github_backend._find_existing_own_comment(
            comments, own_login="me", body="exact text"
        ) is None

    def test_returns_newest_match_when_multiple_exist(self):
        comments = [
            {
                "id": 1,
                "user": {"login": "me"},
                "body": "same body",
                "html_url": "http://old",
            },
            {
                "id": 2,
                "user": {"login": "me"},
                "body": "same body",
                "html_url": "http://new",
            },
        ]
        result = github_backend._find_existing_own_comment(
            comments, own_login="me", body="same body"
        )
        assert result.id == 2


class TestGithubReviewBackendCallerThreading:
    """lr-d72d: GithubReviewBackend(..., caller=...) threads through to
    post_and_verify_review -> resolve_own_login -> resolve_github_app_slug."""

    def test_backend_forwards_caller_to_post_and_verify(self, monkeypatch):
        captured = {}

        def fake_resolve(*, caller=None):
            captured["caller"] = caller
            return "merger-app"

        monkeypatch.setattr(github_backend, "resolve_github_app_slug", fake_resolve)
        opener = _make_success_opener(own_login="merger-app[bot]")
        backend = github_backend.GithubReviewBackend("tok", caller="merger", opener=opener)
        verified = backend.post_and_verify(
            owner="some-owner", repo="some-repo", pr_number=42, body="LGTM, findings: none"
        )
        assert verified.login == "merger-app[bot]"
        assert captured["caller"] == "merger"

    def test_backend_omitting_caller_stays_byte_identical(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            github_backend, "resolve_github_app_slug", lambda: calls.append("called") or "legacy-app"
        )
        opener = _make_success_opener(own_login="legacy-app[bot]")
        backend = github_backend.GithubReviewBackend("tok", opener=opener)
        verified = backend.post_and_verify(
            owner="some-owner", repo="some-repo", pr_number=42, body="LGTM, findings: none"
        )
        assert verified.login == "legacy-app[bot]"
        assert calls == ["called"]


class TestPostAndVerifyReviewPostFailures:
    def test_post_non_2xx_raises_review_post_error(self):
        def opener(req, timeout=15):
            if req.full_url.endswith("/user"):
                return _json_response(200, {"login": "me"})
            if req.get_method() == "GET" and req.full_url.endswith("/comments"):
                return _json_response(200, [])  # dedupe pre-check: no match
            raise _http_error(req.full_url, 422, {"message": "Unprocessable"})

        with pytest.raises(ReviewPostError, match="HTTP 422"):
            github_backend.post_and_verify_review(
                "o", "r", 1, "body", "tok", opener=opener
            )


class TestPostAndVerifyReviewVerifyFailures:
    def test_readback_non_200_raises_review_verify_error(self):
        comments_get_calls = {"count": 0}

        def opener(req, timeout=15):
            if req.full_url.endswith("/user"):
                return _json_response(200, {"login": "me"})
            if req.get_method() == "GET" and req.full_url.endswith("/comments"):
                comments_get_calls["count"] += 1
                if comments_get_calls["count"] == 1:
                    return _json_response(200, [])  # dedupe pre-check: no match
                return _json_response(500, {})  # verify readback fails
            if req.get_method() == "POST":
                return _json_response(200, {"id": 1, "html_url": "http://x"})
            raise AssertionError(f"unexpected request: {req.get_method()} {req.full_url}")

        with pytest.raises(ReviewVerifyError, match="during readback"):
            github_backend.post_and_verify_review(
                "o", "r", 1, "body", "tok", opener=opener
            )

    def test_readback_wrong_login_raises_mismatch(self):
        comments_get_calls = {"count": 0}

        def opener(req, timeout=15):
            if req.full_url.endswith("/user"):
                return _json_response(200, {"login": "me"})
            if req.get_method() == "GET" and req.full_url.endswith("/comments"):
                comments_get_calls["count"] += 1
                if comments_get_calls["count"] == 1:
                    return _json_response(200, [])  # dedupe pre-check: no match
                return _json_response(
                    200,
                    [
                        {
                            "id": 1,
                            "user": {"login": "someone-else"},
                            "body": "body",
                            "created_at": _future_timestamp(),
                        }
                    ],
                )
            if req.get_method() == "POST":
                return _json_response(200, {"id": 1, "html_url": "http://x"})
            raise AssertionError(f"unexpected request: {req.get_method()} {req.full_url}")

        with pytest.raises(ReviewVerifyError, match="MISMATCH"):
            github_backend.post_and_verify_review(
                "o", "r", 1, "body", "tok", opener=opener
            )

    def test_readback_wrong_pr_no_matching_comment_raises_mismatch(self):
        """Simulates the comment landing on a DIFFERENT PR's comments list --
        this PR's own readback finds no login/body match at all."""
        comments_get_calls = {"count": 0}

        def opener(req, timeout=15):
            if req.full_url.endswith("/user"):
                return _json_response(200, {"login": "me"})
            if req.get_method() == "GET" and req.full_url.endswith("/comments"):
                comments_get_calls["count"] += 1
                return _json_response(200, [])  # empty on both dedupe + verify reads
            if req.get_method() == "POST":
                return _json_response(200, {"id": 1, "html_url": "http://x"})
            raise AssertionError(f"unexpected request: {req.get_method()} {req.full_url}")

        with pytest.raises(ReviewVerifyError, match="MISMATCH"):
            github_backend.post_and_verify_review(
                "o", "r", 1, "body", "tok", opener=opener
            )

    def test_readback_stale_comment_fails_freshness_anchor(self):
        """A matching login+body comment that predates the POST (a
        stale/pre-existing comment with overlapping body text) must never
        satisfy this post's verify -- the freshness anchor (lr-71f467,
        mirroring transport.git_host_api.verify_comment_on_pr's not_before
        contract) requires created_at at or after the moment immediately
        before the POST was issued."""
        from datetime import datetime, timedelta, timezone

        comments_get_calls = {"count": 0}
        stale_timestamp = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).isoformat()

        def opener(req, timeout=15):
            if req.full_url.endswith("/user"):
                return _json_response(200, {"login": "me"})
            if req.get_method() == "GET" and req.full_url.endswith("/comments"):
                comments_get_calls["count"] += 1
                if comments_get_calls["count"] == 1:
                    return _json_response(200, [])  # dedupe pre-check: no match
                return _json_response(
                    200,
                    [
                        {
                            "id": 1,
                            "user": {"login": "me"},
                            "body": "body",
                            "created_at": stale_timestamp,
                        }
                    ],
                )
            if req.get_method() == "POST":
                return _json_response(200, {"id": 1, "html_url": "http://x"})
            raise AssertionError(f"unexpected request: {req.get_method()} {req.full_url}")

        with pytest.raises(ReviewVerifyError, match="freshness anchor"):
            github_backend.post_and_verify_review(
                "o", "r", 1, "body", "tok", opener=opener
            )


# ---------------------------------------------------------------------------
# Redirect hardening (lr-412f pre-merge security review finding)
#
# Every call _github_request makes carries a live GitHub bearer/App-
# installation token in its Authorization header. Plain
# urllib.request.urlopen follows a 3xx response and RE-ISSUES the request --
# original headers intact, including Authorization -- against the
# redirect's Location target. These tests prove _github_request's DEFAULT
# opener (the one used when no `opener` is injected) is built via
# transport.redirect_guard.no_redirect_opener() rather than bare urlopen,
# and that every call shape this backend makes (GET /user, the issue-comment
# POST, the readback GET) fails closed -- never a false 'verified' -- on a 3xx.
# ---------------------------------------------------------------------------


def _redirect_http_error(url: str, code: int = 302) -> "Exception":
    import io
    import urllib.error

    return urllib.error.HTTPError(
        url, code, "Found", {"Location": "http://attacker.example.net/collect"},
        io.BytesIO(b""),
    )


class TestRedirectHardeningDefaultOpener:
    """No `opener` injected -- proves _github_request itself builds a
    no-redirect opener rather than falling back to bare urlopen. Verified by
    monkeypatching redirect_guard.no_redirect_opener (the shared, already-
    hardened builder transport.git_host_api's Forgejo path also uses) and
    asserting it was actually called, with zero real network I/O."""

    def test_github_request_default_opener_is_redirect_guarded(self, monkeypatch):
        captured = {}

        class _FakeOpenerDirector:
            def open(self, req, timeout=30):
                captured["req"] = req
                return _json_response(200, {"login": "some-role"})

        def fake_no_redirect_opener():
            captured["called"] = True
            return _FakeOpenerDirector()

        monkeypatch.setattr(
            "clagentic_loadout.review.github_backend.no_redirect_opener",
            fake_no_redirect_opener,
        )

        status, body = github_backend._github_request(
            "GET", f"{github_backend._GITHUB_API}/user", "tok"
        )
        assert status == 200
        assert captured.get("called") is True


class TestRedirectHardeningEachCallShape:
    """Simulates what a no-redirect opener actually returns when a 3xx is
    refused (an HTTPError with the 3xx code, per NoRedirectHandler's
    contract) for each of the call shapes this backend makes, and proves:
    (1) the redirect surfaces as a failure, never a false success; (2) the
    failure is the RIGHT exception class for that phase (post vs. verify);
    (3) only ONE request is ever observed -- no second request to the
    attacker-controlled Location target; (4) the live token is never sent
    to that target (because no second request happens at all)."""

    def test_get_user_redirect_falls_through_to_verify_error(self):
        """A 3xx on GET /user is neither 200 nor 403 -- resolve_own_login
        must not special-case it as the App-token 403 path; it fails closed
        via the generic identity-resolution-failed branch."""
        request_log = []

        def opener(req, timeout=30):
            request_log.append(req.full_url)
            raise _redirect_http_error(req.full_url, 302)

        with pytest.raises(ReviewVerifyError):
            github_backend.resolve_own_login("super-secret-app-token", opener=opener)
        assert request_log == [f"{github_backend._GITHUB_API}/user"]

    def test_get_user_403_with_unconfigured_slug_fails_closed_no_further_request(
        self, monkeypatch
    ):
        """The /user call legitimately 403s (real App-token behavior); with
        no app slug configured, resolution must fail closed WITHOUT ever
        attempting a second (redirect-vulnerable or otherwise) request --
        there is no live fallback call left to make post-lr-d31e."""
        _configure_app_slug_unconfigured(monkeypatch)
        request_log = []

        def opener(req, timeout=30):
            request_log.append(req.full_url)
            raise _http_error(req.full_url, 403, {})

        with pytest.raises(ReviewVerifyError, match="not configured"):
            github_backend.resolve_own_login("super-secret-app-token", opener=opener)
        assert request_log == [f"{github_backend._GITHUB_API}/user"]

    def test_post_comment_redirect_raises_review_post_error_not_verified(self):
        """A 3xx on the issue-comment POST must surface as ReviewPostError --
        the post never landed, so there is nothing to verify -- and must
        never be reported as a clean post-and-verify. Identity resolution
        and the pre-post dedupe readback (lr-39f8) both succeed normally;
        only the POST itself is redirected."""
        request_log = []

        def opener(req, timeout=30):
            request_log.append((req.get_method(), req.full_url))
            if req.full_url.endswith("/user"):
                return _json_response(200, {"login": "some-role"})
            if req.get_method() == "GET" and req.full_url.endswith("/comments"):
                return _json_response(200, [])  # dedupe pre-check: no match
            raise _redirect_http_error(req.full_url, 302)

        with pytest.raises(ReviewPostError):
            github_backend.post_and_verify_review(
                "o", "r", 1, "body", "super-secret-app-token", opener=opener
            )
        # The redirected request (the POST) never reached the attacker host
        # -- the no-redirect contract refuses the 3xx before a second request
        # is dispatched, so the attacker host never appears in the log.
        assert all("attacker.example.net" not in url for _method, url in request_log)
        post_requests = [
            (method, url) for method, url in request_log if method == "POST"
        ]
        assert post_requests == [
            ("POST", f"{github_backend._GITHUB_API}/repos/o/r/issues/1/comments")
        ]

    def test_readback_redirect_raises_review_verify_error_not_verified(self):
        """The POST succeeds and login resolution succeeds, but the
        readback GET is redirected -- must surface as ReviewVerifyError,
        never a false 'verified' result. The pre-post dedupe readback
        (lr-39f8) succeeds normally (empty -- no existing comment); only the
        POST-phase verify readback is redirected."""
        request_log = []
        comments_get_calls = {"count": 0}

        def opener(req, timeout=30):
            request_log.append((req.get_method(), req.full_url))
            if req.full_url.endswith("/user"):
                return _json_response(200, {"login": "some-role"})
            if req.get_method() == "GET" and req.full_url.endswith("/comments"):
                comments_get_calls["count"] += 1
                if comments_get_calls["count"] == 1:
                    return _json_response(200, [])  # dedupe pre-check: no match
                # The verify-phase readback GET is redirected.
                raise _redirect_http_error(req.full_url, 307)
            if req.get_method() == "POST":
                return _json_response(200, {"id": 1, "html_url": "http://x"})
            raise AssertionError(f"unexpected request: {req.get_method()} {req.full_url}")

        with pytest.raises(ReviewVerifyError):
            github_backend.post_and_verify_review(
                "o", "r", 1, "body", "super-secret-app-token", opener=opener
            )
        # No request in the log ever targeted the attacker host -- the
        # no-redirect contract means a 3xx is refused before a second
        # request is dispatched, so the attacker host never appears here.
        assert all("attacker.example.net" not in url for _method, url in request_log)

    def test_authorization_header_never_sent_to_redirect_target(self):
        """Models what a REAL redirect-following opener would do (re-issue
        the request against Location, headers intact) and asserts that path
        is never exercised: the fake opener stands in for
        NoRedirectHandler's refusal, so only one request -- to the
        ORIGINAL host -- is ever observed, and its own Authorization header
        is checked to confirm the real token only ever reaches the intended
        host, never a second request carrying it elsewhere."""
        seen_hosts = []

        def opener(req, timeout=30):
            import urllib.parse

            seen_hosts.append(urllib.parse.urlsplit(req.full_url).hostname)
            assert req.get_header("Authorization") == "token super-secret-app-token"
            raise _redirect_http_error(req.full_url, 302)

        with pytest.raises(ReviewVerifyError):
            github_backend.resolve_own_login("super-secret-app-token", opener=opener)
        assert seen_hosts == ["api.github.com"]
        assert "attacker.example.net" not in seen_hosts


# ---------------------------------------------------------------------------
# get_issue_comment / delete_own_comment (lr-e2ce66) -- GitHub-side parity
# with transport.git_host_api.get_comment / delete_own_comment: belt-and-
# suspenders self-delete of an ISSUE comment (a distinct endpoint from the
# PR-review machinery above).
#
# comment_id digit-only constraint (lr-26f774, BOBBIE bobbie.sast.5 on
# PR #53): both functions must reject a non-digit-only comment_id BEFORE
# any request is issued, mirroring transport.git_host_api.
# _ISSUE_COMMENT_ID_RE's `\d+` shape.
# ---------------------------------------------------------------------------

_COMMENTS_URL = f"{github_backend._GITHUB_API}/repos/o/r/issues/comments/123"


class TestGetIssueComment:
    def test_non_numeric_comment_id_rejected_before_any_request(self):
        """lr-26f774 (BOBBIE bobbie.sast.5, PR #53): a non-digit comment_id
        must be refused BEFORE any GET is issued -- mirrors transport.
        git_host_api._ISSUE_COMMENT_ID_RE's `\\d+` constraint on the Forgejo
        side. The opener asserts it is never called."""

        def opener(req, timeout=15):
            raise AssertionError(
                "no request may be issued for a non-digit-only comment_id"
            )

        with pytest.raises(DeleteOwnCommentRefusedError, match="not digit-only"):
            github_backend.get_issue_comment("o", "r", "123; DROP TABLE", "tok", opener=opener)

    def test_success_returns_parsed_comment(self):
        def opener(req, timeout=15):
            assert req.full_url == _COMMENTS_URL
            return _json_response(200, {"id": 123, "user": {"login": "some-bot"}, "body": "hi"})

        comment = github_backend.get_issue_comment("o", "r", 123, "tok", opener=opener)
        assert comment == {"id": 123, "user": {"login": "some-bot"}, "body": "hi"}

    def test_non_200_raises_delete_own_comment_refused(self):
        def opener(req, timeout=15):
            raise _http_error(req.full_url, 404, {"message": "Not Found"})

        with pytest.raises(DeleteOwnCommentRefusedError, match="HTTP 404"):
            github_backend.get_issue_comment("o", "r", 123, "tok", opener=opener)

    def test_non_object_body_raises_delete_own_comment_refused(self):
        def opener(req, timeout=15):
            return _json_response(200, [1, 2, 3])

        with pytest.raises(DeleteOwnCommentRefusedError):
            github_backend.get_issue_comment("o", "r", 123, "tok", opener=opener)


class TestDeleteOwnComment:
    def _opener(self, *, comment_login="some-bot", comment_body="plain comment", own_login="some-bot"):
        state = {"deleted": False}

        def opener(req, timeout=15):
            url = req.full_url
            if url.endswith("/user"):
                return _json_response(200, {"login": own_login})
            if req.get_method() == "GET" and url == _COMMENTS_URL:
                return _json_response(
                    200, {"id": 123, "user": {"login": comment_login}, "body": comment_body}
                )
            if req.get_method() == "DELETE":
                state["deleted"] = True
                return _FakeResponse(204, b"")
            raise AssertionError(f"unexpected request to {url}")

        opener.state = state
        return opener

    def test_non_numeric_comment_id_rejected_before_any_request(self):
        """lr-26f774 (BOBBIE bobbie.sast.5, PR #53): a non-digit comment_id
        must be refused BEFORE any I/O (GET or DELETE) -- mirrors
        transport.git_host_api._ISSUE_COMMENT_ID_RE's `\\d+` constraint on
        the Forgejo side. The opener asserts it is never called."""

        def opener(req, timeout=15):
            raise AssertionError(
                "no request may be issued for a non-digit-only comment_id"
            )

        with pytest.raises(DeleteOwnCommentRefusedError, match="not digit-only"):
            github_backend.delete_own_comment("o", "r", "../123", "tok", opener=opener)

    def test_happy_path_deletes(self):
        opener = self._opener()
        github_backend.delete_own_comment("o", "r", 123, "tok", opener=opener)
        assert opener.state["deleted"] is True

    def test_cross_author_refused_before_delete(self):
        opener = self._opener(comment_login="a-different-bot", own_login="some-bot")
        with pytest.raises(DeleteOwnCommentRefusedError, match="cross-author"):
            github_backend.delete_own_comment("o", "r", 123, "tok", opener=opener)
        assert opener.state["deleted"] is False

    def test_verdict_fence_present_refused_before_delete_even_when_own_comment(self):
        from clagentic_loadout.merge.verdict import build_verdict_block

        fence = build_verdict_block("some-reviewer", "clean", "a" * 40, 5)
        opener = self._opener(
            comment_login="some-bot", own_login="some-bot", comment_body=f"LGTM.\n{fence}"
        )
        with pytest.raises(DeleteOwnCommentRefusedError, match="review-result"):
            github_backend.delete_own_comment("o", "r", 123, "tok", opener=opener)
        assert opener.state["deleted"] is False

    def test_unreadable_comment_refused_before_delete(self):
        def opener(req, timeout=15):
            if req.get_method() == "GET" and req.full_url == _COMMENTS_URL:
                raise _http_error(req.full_url, 404, {"message": "Not Found"})
            raise AssertionError("DELETE/user must never be reached when the comment GET fails")

        with pytest.raises(DeleteOwnCommentRefusedError):
            github_backend.delete_own_comment("o", "r", 123, "tok", opener=opener)

    def test_delete_non_2xx_raises_review_post_error(self):
        def opener(req, timeout=15):
            url = req.full_url
            if url.endswith("/user"):
                return _json_response(200, {"login": "some-bot"})
            if req.get_method() == "GET" and url == _COMMENTS_URL:
                return _json_response(200, {"id": 123, "user": {"login": "some-bot"}, "body": "hi"})
            if req.get_method() == "DELETE":
                raise _http_error(url, 403, {"message": "Forbidden"})
            raise AssertionError(f"unexpected request to {url}")

        with pytest.raises(ReviewPostError, match="HTTP 403"):
            github_backend.delete_own_comment("o", "r", 123, "tok", opener=opener)

    def test_app_installation_token_resolves_own_login_via_configured_slug(self, monkeypatch):
        """delete_own_comment reuses resolve_own_login exactly like
        post_and_verify_review does -- an installation token's 403 on /user
        resolves the bot login from configured slug, and the comment
        authored by '<slug>[bot]' is treated as the caller's own."""
        _configure_app_slug(monkeypatch, "some-app")
        state = {"deleted": False}

        def opener(req, timeout=15):
            url = req.full_url
            if url.endswith("/user"):
                raise _http_error(url, 403, {"message": "not accessible"})
            if req.get_method() == "GET" and url == _COMMENTS_URL:
                return _json_response(
                    200, {"id": 123, "user": {"login": "some-app[bot]"}, "body": "hi"}
                )
            if req.get_method() == "DELETE":
                state["deleted"] = True
                return _FakeResponse(204, b"")
            raise AssertionError(f"unexpected request to {url}")

        github_backend.delete_own_comment("o", "r", 123, "installation-token", opener=opener)
        assert state["deleted"] is True


class TestGithubReviewBackendDeleteOwnComment:
    def test_delegates_to_module_function(self):
        state = {"deleted": False}

        def opener(req, timeout=15):
            url = req.full_url
            if url.endswith("/user"):
                return _json_response(200, {"login": "some-bot"})
            if req.get_method() == "GET" and url == _COMMENTS_URL:
                return _json_response(200, {"id": 123, "user": {"login": "some-bot"}, "body": "hi"})
            if req.get_method() == "DELETE":
                state["deleted"] = True
                return _FakeResponse(204, b"")
            raise AssertionError(f"unexpected request to {url}")

        backend = github_backend.GithubReviewBackend("tok", opener=opener)
        backend.delete_own_comment(owner="o", repo="r", comment_id=123)
        assert state["deleted"] is True
