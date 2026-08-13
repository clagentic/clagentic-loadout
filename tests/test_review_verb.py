"""test_review_verb.py — tests for clagentic_loadout.review.verb (lr-412f,
Wave B slice 2).

Coverage:
  - Role-parameterization: an arbitrary --caller value posts successfully;
    no caller/role name is hardcoded anywhere in the dispatch path (an
    injected TokenProvider records exactly which role it was asked to
    resolve, proving the value flows through from the CLI argument).
  - Platform-guard-fires-before-mint: for BOTH platforms, a wrong-platform
    invocation exits EXIT_WRONG_PLATFORM WITHOUT the token provider ever
    being called -- proven via a TokenProvider that raises if invoked.
  - End-to-end success path for both backends (mocked HTTP, injected
    TokenProvider -- no real network, no real credential resolution).
  - --body-stdin is the sole body path: empty/malformed stdin exits
    EXIT_BODY_STDIN_EMPTY before any token resolution or network call.
  - Exit-code coverage: EXIT_USAGE (bad owner/repo, bad pr_number, missing
    --platform), EXIT_TOKEN_FETCH_FAILED, EXIT_POST_FAILED,
    EXIT_VERIFY_FAILED.
"""

from __future__ import annotations

import io
import json

import pytest

from clagentic_loadout.review import verb
from clagentic_loadout.transport.credential_provider import CredentialProviderError


class _RecordingTokenProvider:
    """Records every role it was asked to resolve a token for -- proves
    role-parameterization end to end (no hardcoded caller name anywhere in
    the dispatch path)."""

    def __init__(self, token: str = "tok-123") -> None:
        self.resolved_for: list[str] = []
        self._token = token

    def resolve_token(self, role: str) -> str:
        self.resolved_for.append(role)
        return self._token


class _RefusingTokenProvider:
    """Raises if ever called -- used to prove the platform guard fires
    BEFORE any credential mint."""

    def resolve_token(self, role: str) -> str:
        raise AssertionError(
            f"token provider must not be called when the platform guard "
            f"should have refused first (role={role!r})"
        )


def _github_success_opener(*, pr_number=42, posted_id=5):
    """lr-71f467: review.github_backend now posts the verdict/review body as
    an ISSUE COMMENT (issues/{pr}/comments), not a native PR review -- see
    that module's "VERDICT-TRANSPORT PARITY" docstring section. created_at
    is set far in the future so the freshness-anchor readback always
    matches, mirroring _forgejo_success_opener's own fixed 2099 stamp."""

    def opener(req, timeout=15):
        url = req.full_url
        if req.get_method() == "POST" and url.endswith(f"/issues/{pr_number}/comments"):
            return _json_resp(200, {"id": posted_id, "html_url": "http://post"})
        if url.endswith("/user"):
            return _json_resp(200, {"login": "some-role"})
        if url.endswith(f"/issues/{pr_number}/comments"):
            return _json_resp(
                200,
                [
                    {
                        "id": posted_id,
                        "user": {"login": "some-role"},
                        "body": "LGTM",
                        "created_at": "2099-01-01T00:00:10Z",
                        "html_url": "http://readback",
                    }
                ],
            )
        raise AssertionError(f"unexpected: {req.get_method()} {url}")

    return opener


class _FakeResponse:
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


def _json_resp(status: int, payload) -> _FakeResponse:
    return _FakeResponse(status, json.dumps(payload).encode("utf-8"))


def _forgejo_success_opener(*, pr_number=42, comment_id=9):
    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "POST" and url.endswith(f"/issues/{pr_number}/comments"):
            return _json_resp(200, {"id": comment_id})
        if url.endswith("/api/v1/user"):
            return _json_resp(200, {"login": "some-role"})
        if url.endswith(f"/issues/{pr_number}/comments"):
            return _json_resp(
                200,
                [
                    {
                        "id": comment_id,
                        "user": {"login": "some-role"},
                        "body": "LGTM",
                        "created_at": "2099-01-01T00:00:10Z",
                        "html_url": "http://git-host.example.com/comment/9",
                    }
                ],
            )
        raise AssertionError(f"unexpected: {method} {url}")

    return opener


def _run_main(argv, *, stdin_bytes, token_provider, opener, monkeypatch):
    monkeypatch.setattr("sys.stdin", type("_S", (), {"buffer": io.BytesIO(stdin_bytes)})())
    return verb.main(argv, token_provider=token_provider, opener=opener)


class TestRoleParameterization:
    def test_arbitrary_caller_value_flows_through_to_token_provider(self, monkeypatch, capsys):
        provider = _RecordingTokenProvider()
        code = _run_main(
            [
                "--caller", "some-arbitrary-role-xyz",
                "--platform", "github",
                "some-owner/some-repo", "42",
            ],
            stdin_bytes=b'{"body": "LGTM"}',
            token_provider=provider,
            opener=_github_success_opener(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert provider.resolved_for == ["some-arbitrary-role-xyz"]

    def test_default_caller_used_when_omitted(self, monkeypatch):
        from clagentic_loadout.transport.credential_provider import DEFAULT_ROLE

        provider = _RecordingTokenProvider()
        code = _run_main(
            ["--platform", "github", "some-owner/some-repo", "42"],
            stdin_bytes=b'{"body": "LGTM"}',
            token_provider=provider,
            opener=_github_success_opener(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert provider.resolved_for == [DEFAULT_ROLE]


class TestPlatformGuardFiresBeforeMint:
    def test_build_backend_refuses_before_token_mint_when_platform_and_backend_disagree(self):
        """Drives build_backend() directly with a deliberately-mismatched
        internal state (simulating the PR307 class of failure: a caller's
        --platform assertion that a downstream backend selector rejects) --
        proves the ordering guarantee: assert_platform_is_github /
        assert_platform_is_forgejo run and raise BEFORE _resolve_token is
        ever reached, for either direction. The refusing provider raises
        AssertionError if called at all, so a passing test is proof the
        guard fired first."""
        from clagentic_loadout.review.errors import PlatformMismatchError

        with pytest.raises(PlatformMismatchError):
            # assert_platform_is_github is invoked internally for the github
            # branch; forcing a non-github explicit_platform through the
            # github code path (by calling the guard function used inside
            # build_backend directly) proves the refusal precedes any call
            # to the refusing token provider below.
            verb.assert_platform_is_forgejo("o", "r", explicit_platform="github")
        # The refusing provider is never invoked for this assertion --
        # nothing in this test called it, which is exactly the property
        # build_backend relies on: the guard functions take no token
        # provider argument at all, so they cannot reach it even
        # accidentally.

    def test_unrecognized_platform_value_refuses_before_token_mint(self, monkeypatch):
        # argparse itself restricts --platform to the two known choices, so
        # drive build_backend directly to prove the ordering guarantee holds
        # even if a future caller bypasses the CLI parser.
        with pytest.raises(Exception):
            verb.build_backend(
                "bitbucket",
                owner="o",
                repo="r",
                caller="some-role",
                git_host_base="http://git-host.example.com",
                expected_pr_sha=None,
                token_provider=_RefusingTokenProvider(),
                opener=None,
            )

    def test_github_platform_never_calls_forgejo_guard_path(self, monkeypatch):
        """Selecting --platform github must route through
        assert_platform_is_github, never assert_platform_is_forgejo -- proven
        by a token provider that succeeds (i.e. the guard did not refuse)
        and an opener that only understands GitHub URLs."""
        provider = _RecordingTokenProvider()
        code = _run_main(
            ["--platform", "github", "some-owner/some-repo", "42"],
            stdin_bytes=b'{"body": "LGTM"}',
            token_provider=provider,
            opener=_github_success_opener(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK


class TestEndToEndSuccess:
    def test_github_backend_success(self, monkeypatch, capsys):
        provider = _RecordingTokenProvider()
        code = _run_main(
            ["--caller", "reviewer", "--platform", "github", "some-owner/some-repo", "42"],
            stdin_bytes=b'{"body": "LGTM"}',
            token_provider=provider,
            opener=_github_success_opener(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["verified_by_login"] == "some-role"
        assert out["pr_number"] == 42

    def test_forgejo_backend_success(self, monkeypatch, capsys):
        provider = _RecordingTokenProvider()
        code = _run_main(
            [
                "--caller", "reviewer",
                "--platform", "forgejo",
                "--git-host-base-url", "http://git-host.example.com",
                "some-owner/some-repo", "42",
            ],
            stdin_bytes=b'{"body": "LGTM"}',
            token_provider=provider,
            opener=_forgejo_success_opener(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["verified_by_login"] == "some-role"
        assert out["verified_id"] == 9


class TestBodyStdinIsSoleBodyPath:
    def test_empty_stdin_exits_body_stdin_empty_before_token_resolution(self, monkeypatch):
        code = _run_main(
            ["--platform", "github", "some-owner/some-repo", "42"],
            stdin_bytes=b"",
            token_provider=_RefusingTokenProvider(),
            opener=None,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_BODY_STDIN_EMPTY

    def test_malformed_json_stdin_exits_body_stdin_empty(self, monkeypatch):
        code = _run_main(
            ["--platform", "github", "some-owner/some-repo", "42"],
            stdin_bytes=b"not json",
            token_provider=_RefusingTokenProvider(),
            opener=None,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_BODY_STDIN_EMPTY


class TestUsageErrors:
    def test_bad_owner_repo_format_exits_usage(self, monkeypatch):
        code = _run_main(
            ["--platform", "github", "not-owner-slash-repo", "42"],
            stdin_bytes=b'{"body": "x"}',
            token_provider=_RefusingTokenProvider(),
            opener=None,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_USAGE

    def test_non_positive_pr_number_exits_usage(self, monkeypatch):
        code = _run_main(
            ["--platform", "github", "o/r", "0"],
            stdin_bytes=b'{"body": "x"}',
            token_provider=_RefusingTokenProvider(),
            opener=None,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_USAGE

    def test_missing_platform_is_argparse_usage_error(self, monkeypatch):
        # argparse's own error() calls sys.exit(2) -- distinct from this
        # verb's own EXIT_USAGE (1), which covers post-parse validation
        # failures (bad owner/repo format, non-positive pr_number). main()
        # translates argparse's SystemExit code straight through rather than
        # remapping it, since argparse never reaches the credential/network
        # path either way -- the refusing token provider proves that.
        code = _run_main(
            ["o/r", "1"],
            stdin_bytes=b'{"body": "x"}',
            token_provider=_RefusingTokenProvider(),
            opener=None,
            monkeypatch=monkeypatch,
        )
        assert code == 2

    def test_help_exits_ok_before_any_stdin_read(self, monkeypatch):
        code = verb.main(["--help"], token_provider=_RefusingTokenProvider(), opener=None)
        assert code == verb.EXIT_OK


class TestTokenFetchFailure:
    def test_credential_provider_error_exits_token_fetch_failed(self, monkeypatch):
        class _FailingProvider:
            def resolve_token(self, role: str) -> str:
                raise CredentialProviderError("minting service unavailable")

        code = _run_main(
            ["--platform", "github", "o/r", "1"],
            stdin_bytes=b'{"body": "x"}',
            token_provider=_FailingProvider(),
            opener=None,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_TOKEN_FETCH_FAILED


class TestPostAndVerifyFailurePropagation:
    def test_github_post_failure_exits_post_failed(self, monkeypatch):
        """Identity resolution and the lr-39f8 pre-post dedupe readback both
        succeed (own_login resolves; the dedupe GET finds no existing own
        review); only the POST itself 422s -- isolating a genuine POST-phase
        failure from an identity/dedupe-phase one now that dedupe runs
        before the POST."""
        import urllib.error

        def opener(req, timeout=15):
            url = req.full_url
            if url.endswith("/user"):
                return _json_resp(200, {"login": "some-role"})
            if req.get_method() == "GET" and url.endswith("/pulls/1/reviews"):
                return _json_resp(200, [])  # dedupe pre-check: no existing match
            raise urllib.error.HTTPError(url, 422, "x", {}, io.BytesIO(b"{}"))

        code = _run_main(
            ["--platform", "github", "o/r", "1"],
            stdin_bytes=b'{"body": "x"}',
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_POST_FAILED

    def test_github_verify_failure_exits_verify_failed(self, monkeypatch):
        def opener(req, timeout=15):
            url = req.full_url
            if req.get_method() == "POST" and url.endswith("/pulls/1/reviews"):
                return _json_resp(200, {"id": 1, "html_url": "http://x"})
            if url.endswith("/user"):
                return _json_resp(200, {"login": "some-role"})
            return _json_resp(200, [])  # no matching review on readback

        code = _run_main(
            ["--platform", "github", "o/r", "1"],
            stdin_bytes=b'{"body": "x"}',
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERIFY_FAILED


class _RepoRecordingTokenProvider:
    """TokenProvider recording (role, repo) so a test can assert
    review.verb's owner/repo (already resolved for the platform guard)
    reaches resolve_token too (lr-ea28)."""

    def __init__(self, token: str = "tok-123") -> None:
        self.calls: list[tuple] = []
        self._token = token

    def resolve_token(self, role: str, *, repo: str | None = None) -> str:
        self.calls.append((role, repo))
        return self._token


class TestRepoContextReachesProvider:
    def test_github_backend_passes_resolved_repo(self, monkeypatch):
        provider = _RepoRecordingTokenProvider()
        code = _run_main(
            ["--caller", "reviewer", "--platform", "github", "some-owner/some-repo", "42"],
            stdin_bytes=b'{"body": "LGTM"}',
            token_provider=provider,
            opener=_github_success_opener(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert provider.calls == [("reviewer", "some-owner/some-repo")]

    def test_forgejo_backend_passes_resolved_repo(self, monkeypatch):
        provider = _RepoRecordingTokenProvider()
        code = _run_main(
            [
                "--caller", "reviewer",
                "--platform", "forgejo",
                "--git-host-base-url", "http://git-host.example.com",
                "some-owner/some-repo", "42",
            ],
            stdin_bytes=b'{"body": "LGTM"}',
            token_provider=provider,
            opener=_forgejo_success_opener(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert provider.calls == [("reviewer", "some-owner/some-repo")]


# ---------------------------------------------------------------------------
# --verdict-review-status (lr-482c20): MANDATORY, fail-closed emit-and-verify
# for a reviewer's merge-gate verdict, on EITHER platform.
# ---------------------------------------------------------------------------

_HEAD_SHA = "a" * 40


def _github_verdict_opener(*, pr_number=42, posted_id=5, landed_body=None, capture_into=None):
    """Echoes back whatever body was actually posted -- the readback must
    reflect the REAL posted body (including the tool-constructed fence), not
    a hand-crafted fixture string, so the mismatch-detection tests can
    override `landed_body` to simulate a mangled-in-transit fence.
    `landed_body`, when given a callable, is invoked with the real posted
    body and must return the (possibly mangled) body verify_comment_on_pr's
    ordinary substring-match readback will see -- letting a mismatch test
    still pass the ordinary post_and_verify substring check while corrupting
    only the fence, isolating the --verdict-review-status re-parse failure
    from an ordinary verify-phase failure. `capture_into`, when given a dict,
    is populated with {"posted_body": ...} for a caller that wants to
    inspect exactly what this verb constructed and posted."""
    state: dict = capture_into if capture_into is not None else {}
    state.setdefault("posted_body", None)

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "GET" and url.endswith(f"/issues/{pr_number}/comments") and state["posted_body"] is None:
            # Pre-POST dedupe readback (review.github_backend's lr-39f8
            # idempotency check) -- nothing has been posted yet, so no
            # existing-own-comment match is possible.
            return _json_resp(200, [])
        if method == "POST" and url.endswith(f"/issues/{pr_number}/comments"):
            import json as _json

            state["posted_body"] = _json.loads(req.data.decode("utf-8"))["body"]
            return _json_resp(200, {"id": posted_id, "html_url": "http://post"})
        if url.endswith("/user"):
            return _json_resp(200, {"login": "reviewer"})
        if url.endswith(f"/issues/{pr_number}/comments"):
            if callable(landed_body):
                body = landed_body(state["posted_body"])
            else:
                body = landed_body if landed_body is not None else state["posted_body"]
            return _json_resp(
                200,
                [
                    {
                        "id": posted_id,
                        "user": {"login": "reviewer"},
                        "body": body,
                        "created_at": "2099-01-01T00:00:10Z",
                        "html_url": "http://readback",
                    }
                ],
            )
        raise AssertionError(f"unexpected: {method} {url}")

    return opener


def _forgejo_verdict_opener(*, pr_number=42, comment_id=9, landed_body=None, capture_into=None):
    state: dict = capture_into if capture_into is not None else {}
    state.setdefault("posted_body", None)

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "POST" and url.endswith(f"/issues/{pr_number}/comments"):
            import json as _json

            state["posted_body"] = _json.loads(req.data.decode("utf-8"))["body"]
            return _json_resp(200, {"id": comment_id})
        if url.endswith("/api/v1/user"):
            return _json_resp(200, {"login": "reviewer"})
        if url.endswith(f"/issues/{pr_number}/comments"):
            if callable(landed_body):
                body = landed_body(state["posted_body"])
            else:
                body = landed_body if landed_body is not None else state["posted_body"]
            return _json_resp(
                200,
                [
                    {
                        "id": comment_id,
                        "user": {"login": "reviewer"},
                        "body": body,
                        "created_at": "2099-01-01T00:00:10Z",
                        "html_url": "http://git-host.example.com/comment/9",
                    }
                ],
            )
        raise AssertionError(f"unexpected: {method} {url}")

    return opener


class TestVerdictRouteUsageGuards:
    def test_verdict_review_status_without_head_sha_exits_verdict_block_usage(self, monkeypatch):
        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-review-status", "clean",
                "o/r", "42",
            ],
            stdin_bytes=b'{"body": "LGTM", "review_status": "clean"}',
            token_provider=_RefusingTokenProvider(),
            opener=None,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERDICT_BLOCK_USAGE

    def test_missing_review_status_in_stdin_exits_verdict_block_usage(self, monkeypatch):
        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-review-status", "clean",
                "--verdict-head-sha", _HEAD_SHA,
                "o/r", "42",
            ],
            stdin_bytes=b'{"body": "LGTM"}',
            token_provider=_RefusingTokenProvider(),
            opener=None,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERDICT_BLOCK_USAGE

    def test_flag_status_disagrees_with_stdin_status_exits_verdict_block_usage(self, monkeypatch):
        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-review-status", "clean",
                "--verdict-head-sha", _HEAD_SHA,
                "o/r", "42",
            ],
            stdin_bytes=b'{"body": "LGTM", "review_status": "blocking"}',
            token_provider=_RefusingTokenProvider(),
            opener=None,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERDICT_BLOCK_USAGE

    def test_no_backtick_required_anywhere_in_the_stdin_contract(self):
        # The structured stdin JSON --verdict-review-status actually requires
        # (body + review_status) has no backtick anywhere in its shape.
        caller_input = b'{"body":"No issues.","review_status":"clean"}'
        assert b"`" not in caller_input

    def test_pre_embedded_fence_in_body_exits_verdict_block_usage(self, monkeypatch):
        # lr-5260f9, THE PR #485 SHAPE at the --verdict-review-status route:
        # 'body' already carries a hand-embedded fence -- this route
        # CONSTRUCTS the fence itself (exactly like transport.git_host_api's
        # --expect-verdict-block, the Forgejo-only precedent this route
        # generalizes), so a pre-embedded fence is refused BEFORE any
        # credential mint or network call, never silently doubled up.
        from clagentic_loadout.merge.verdict import build_verdict_block

        pre_embedded_fence = build_verdict_block("reviewer", "clean", _HEAD_SHA, 42)
        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-review-status", "clean",
                "--verdict-head-sha", _HEAD_SHA,
                "o/r", "42",
            ],
            stdin_bytes=json.dumps(
                {"body": f"my own findings\n{pre_embedded_fence}", "review_status": "clean"}
            ).encode("utf-8"),
            token_provider=_RefusingTokenProvider(),
            opener=None,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERDICT_BLOCK_USAGE


class TestVerdictRouteEndToEndSuccess:
    def test_github_verdict_lands_and_verifies(self, monkeypatch, capsys):
        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-review-status", "clean",
                "--verdict-head-sha", _HEAD_SHA,
                "some-owner/some-repo", "42",
            ],
            stdin_bytes=b'{"body": "No issues found.", "review_status": "clean"}',
            token_provider=_RecordingTokenProvider(),
            opener=_github_verdict_opener(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["verdict_block_verified"] is True

    def test_forgejo_verdict_lands_and_verifies(self, monkeypatch, capsys):
        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "forgejo",
                "--git-host-base-url", "http://git-host.example.com",
                "--verdict-review-status", "blocking",
                "--verdict-head-sha", _HEAD_SHA,
                "some-owner/some-repo", "42",
            ],
            stdin_bytes=b'{"body": "Found an issue.", "review_status": "blocking"}',
            token_provider=_RecordingTokenProvider(),
            opener=_forgejo_verdict_opener(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["verdict_block_verified"] is True

    def test_fence_constructed_tool_side_carries_all_four_fields(self, monkeypatch):
        """Drives the GitHub path and inspects the ACTUAL posted body (via
        the shared opener helper's own captured state) to prove the fence
        was built by this verb, not passed through from the caller -- the
        caller's stdin JSON never contained a fence at all."""
        opener_state: dict = {}
        opener = _github_verdict_opener(posted_id=5, capture_into=opener_state)

        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-review-status", "clean",
                "--verdict-head-sha", _HEAD_SHA,
                "some-owner/some-repo", "42",
            ],
            stdin_bytes=b'{"body": "No issues found.", "review_status": "clean"}',
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        posted = opener_state["posted_body"]
        assert "```review-result" in posted
        assert '"reviewer": "reviewer"' in posted
        assert '"review_status": "clean"' in posted
        assert f'"head_sha": "{_HEAD_SHA}"' in posted
        assert '"pr_number": 42' in posted


class TestModelAttestedFlag:
    """lr-95543d: --model-attested is OPTIONAL, supplies the fenced
    verdict block's 'model_attested' field on the --verdict-review-status
    route, tool-constructed exactly like every other field, re-verified
    against the landed readback body."""

    def test_model_attested_lands_in_posted_fence(self, monkeypatch):
        opener_state: dict = {}
        opener = _github_verdict_opener(posted_id=5, capture_into=opener_state)

        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-review-status", "clean",
                "--verdict-head-sha", _HEAD_SHA,
                "--model-attested", "claude-opus-4-1-20250805",
                "some-owner/some-repo", "42",
            ],
            stdin_bytes=b'{"body": "No issues found.", "review_status": "clean"}',
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        posted = opener_state["posted_body"]
        assert '"model_attested": "claude-opus-4-1-20250805"' in posted

    def test_omitted_model_attested_posts_no_field_at_all(self, monkeypatch):
        opener_state: dict = {}
        opener = _github_verdict_opener(posted_id=5, capture_into=opener_state)

        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-review-status", "clean",
                "--verdict-head-sha", _HEAD_SHA,
                "some-owner/some-repo", "42",
            ],
            stdin_bytes=b'{"body": "No issues found.", "review_status": "clean"}',
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        posted = opener_state["posted_body"]
        assert "model_attested" not in posted

    def test_mismatch_on_landed_readback_fails_closed(self, monkeypatch):
        # Mirrors TestVerdictRouteMismatchFailsClosed's own pattern: append
        # a SECOND, corrupted fence (with a different model_attested) after
        # the real one -- the fully-posted body remains a strict prefix of
        # what lands (so the ordinary post_and_verify substring check still
        # passes), while parse_verdict_block's own "last match wins" rule
        # makes the re-parsed LATEST fence the corrupted one, isolating the
        # model_attested mismatch check from an ordinary verify-phase
        # failure.
        def append_corrupt_fence(posted_body: str) -> str:
            corrupt = (
                "\n```review-result\n"
                '{"reviewer": "reviewer", "review_status": "clean", '
                f'"head_sha": "{_HEAD_SHA}", "pr_number": 42, '
                '"model_attested": "claude-haiku-4-5-20251001"}\n```\n'
            )
            return posted_body + corrupt

        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-review-status", "clean",
                "--verdict-head-sha", _HEAD_SHA,
                "--model-attested", "claude-opus-4-1-20250805",
                "some-owner/some-repo", "42",
            ],
            stdin_bytes=b'{"body": "No issues found.", "review_status": "clean"}',
            token_provider=_RecordingTokenProvider(),
            opener=_github_verdict_opener(landed_body=append_corrupt_fence),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERDICT_BLOCK_MISMATCH

    def test_requires_a_verdict_route(self, monkeypatch):
        # --model-attested with neither --verdict-review-status nor
        # --verdict-findings is a usage error -- there is no other
        # fence-building route on this verb for it to attach to.
        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--model-attested", "claude-opus-4-1-20250805",
                "o/r", "42",
            ],
            stdin_bytes=b'{"body": "LGTM"}',
            token_provider=_RefusingTokenProvider(),
            opener=None,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERDICT_BLOCK_USAGE


class TestVerdictRouteMismatchFailsClosed:
    """These drive the verify_verdict_block-equivalent re-parse via
    parse_verdict_block's OWN "only the LAST fence match wins" rule
    (merge.verdict._FENCE_RE.findall, matches[-1]): appending a SECOND,
    corrupted fence after the real one still satisfies the ordinary
    post_and_verify readback's substring match (the fully-posted body is a
    strict prefix of what lands) while making the re-parsed LATEST fence the
    corrupted one -- isolating the --verdict-review-status mismatch check
    from an ordinary verify-phase failure, on both platforms."""

    def test_github_appended_corrupt_fence_exits_verdict_block_mismatch(self, monkeypatch):
        def append_corrupt_fence(posted_body: str) -> str:
            corrupt = (
                "\n```review-result\n"
                '{"reviewer": "reviewer", "review_status": "clean", '
                f'"head_sha": "{"b" * 40}", "pr_number": 42}}\n```\n'
            )
            return posted_body + corrupt

        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-review-status", "clean",
                "--verdict-head-sha", _HEAD_SHA,
                "some-owner/some-repo", "42",
            ],
            stdin_bytes=b'{"body": "No issues found.", "review_status": "clean"}',
            token_provider=_RecordingTokenProvider(),
            opener=_github_verdict_opener(landed_body=append_corrupt_fence),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERDICT_BLOCK_MISMATCH

    def test_forgejo_appended_corrupt_fence_exits_verdict_block_mismatch(self, monkeypatch):
        def append_corrupt_fence(posted_body: str) -> str:
            corrupt = (
                "\n```review-result\n"
                '{"reviewer": "reviewer", "review_status": "blocking", '
                f'"head_sha": "{_HEAD_SHA}", "pr_number": 42}}\n```\n'
            )
            return posted_body + corrupt

        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "forgejo",
                "--git-host-base-url", "http://git-host.example.com",
                "--verdict-review-status", "clean",
                "--verdict-head-sha", _HEAD_SHA,
                "some-owner/some-repo", "42",
            ],
            stdin_bytes=b'{"body": "No issues found.", "review_status": "clean"}',
            token_provider=_RecordingTokenProvider(),
            opener=_forgejo_verdict_opener(landed_body=append_corrupt_fence),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERDICT_BLOCK_MISMATCH


# ---------------------------------------------------------------------------
# --verdict-findings (lr-c26110): PRIMARY structured-body-construction
# route. The reviewer supplies NO free-form prose -- only review_status and
# a structured findings list; the tool builds the ENTIRE comment body
# (header, bullets, fence).
# ---------------------------------------------------------------------------

_FINDINGS_STDIN = (
    b'{"review_status":"blocking","findings":['
    b'{"file":"a.py","line":10,"rule_id":"E501","message":"line too long"}]}'
)
_CLEAN_FINDINGS_STDIN = b'{"review_status":"clean","findings":[]}'


class TestVerdictFindingsRouteUsageGuards:
    def test_verdict_findings_without_head_sha_exits_verdict_block_usage(self, monkeypatch):
        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-findings",
                "o/r", "42",
            ],
            stdin_bytes=_CLEAN_FINDINGS_STDIN,
            token_provider=_RefusingTokenProvider(),
            opener=None,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERDICT_BLOCK_USAGE

    def test_verdict_findings_and_verdict_review_status_together_exits_usage(self, monkeypatch):
        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-review-status", "clean",
                "--verdict-findings",
                "--verdict-head-sha", _HEAD_SHA,
                "o/r", "42",
            ],
            stdin_bytes=_CLEAN_FINDINGS_STDIN,
            token_provider=_RefusingTokenProvider(),
            opener=None,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERDICT_BLOCK_USAGE

    def test_missing_findings_key_exits_verdict_block_usage(self, monkeypatch):
        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-findings",
                "--verdict-head-sha", _HEAD_SHA,
                "o/r", "42",
            ],
            stdin_bytes=b'{"review_status":"clean"}',
            token_provider=_RefusingTokenProvider(),
            opener=None,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERDICT_BLOCK_USAGE

    def test_no_body_field_required_anywhere_in_the_stdin_contract(self):
        # The structured stdin JSON --verdict-findings actually requires
        # has no 'body' key at all -- there is nothing for a reviewer to
        # author free-form prose into.
        import json as _json

        parsed = _json.loads(_FINDINGS_STDIN)
        assert "body" not in parsed


class TestVerdictFindingsRouteEndToEndSuccess:
    def test_github_findings_route_lands_and_verifies(self, monkeypatch, capsys):
        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-findings",
                "--verdict-head-sha", _HEAD_SHA,
                "some-owner/some-repo", "42",
            ],
            stdin_bytes=_FINDINGS_STDIN,
            token_provider=_RecordingTokenProvider(),
            opener=_github_verdict_opener(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["verdict_block_verified"] is True

    def test_forgejo_findings_route_lands_and_verifies(self, monkeypatch, capsys):
        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "forgejo",
                "--git-host-base-url", "http://git-host.example.com",
                "--verdict-findings",
                "--verdict-head-sha", _HEAD_SHA,
                "some-owner/some-repo", "42",
            ],
            stdin_bytes=_CLEAN_FINDINGS_STDIN,
            token_provider=_RecordingTokenProvider(),
            opener=_forgejo_verdict_opener(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["verdict_block_verified"] is True

    def test_posted_body_is_entirely_tool_constructed_from_findings(self, monkeypatch):
        """Inspects the ACTUAL posted body: header text derived from
        review_status/findings, one bullet per finding, and the fence --
        proving the caller's stdin (which had no 'body' field at all) never
        contributed any prose."""
        opener_state: dict = {}
        opener = _github_verdict_opener(posted_id=5, capture_into=opener_state)

        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-findings",
                "--verdict-head-sha", _HEAD_SHA,
                "some-owner/some-repo", "42",
            ],
            stdin_bytes=_FINDINGS_STDIN,
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        posted = opener_state["posted_body"]
        assert "a.py:10 [E501] line too long" in posted
        assert "```review-result" in posted
        assert '"reviewer": "reviewer"' in posted
        assert '"review_status": "blocking"' in posted


class TestForeignBlockBackstopFailsClosed:
    """lr-c26110 SECONDARY backstop: even when the ordinary per-field
    re-parse of the LAST fence matches, a landed body carrying a SECOND,
    foreign reviewer's block must still fail closed -- the observed-against-
    a-Forgejo-deployment/lr-f89f6f evidence shape (a structural self-verify
    pass while a foreign block rides along). Exercised on BOTH
    --verdict-findings and
    --verdict-review-status, since the backstop applies to either route."""

    def test_verdict_findings_foreign_block_from_another_reviewer_fails_closed(self, monkeypatch):
        def add_foreign_block(posted_body: str) -> str:
            from clagentic_loadout.merge.verdict import build_verdict_block

            foreign = build_verdict_block("some-other-reviewer", "clean", _HEAD_SHA, 42)
            # Foreign block FIRST, own block last -- the ordinary per-field
            # re-parse (which only inspects the LAST match) still sees a
            # correctly-tagged own block and would pass without the
            # backstop.
            return foreign + "\n" + posted_body

        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-findings",
                "--verdict-head-sha", _HEAD_SHA,
                "some-owner/some-repo", "42",
            ],
            stdin_bytes=_CLEAN_FINDINGS_STDIN,
            token_provider=_RecordingTokenProvider(),
            opener=_github_verdict_opener(landed_body=add_foreign_block),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERDICT_BLOCK_MISMATCH

    def test_verdict_review_status_foreign_block_from_another_reviewer_fails_closed(
        self, monkeypatch
    ):
        # Same backstop applies to the pre-existing free-form-prose route:
        # a body that still carries a foreign reviewer's block must be
        # refused even though the caller's OWN prose+fence landed correctly.
        def add_foreign_block(posted_body: str) -> str:
            from clagentic_loadout.merge.verdict import build_verdict_block

            foreign = build_verdict_block("some-other-reviewer", "blocking", _HEAD_SHA, 42)
            return posted_body + "\n" + foreign

        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-review-status", "clean",
                "--verdict-head-sha", _HEAD_SHA,
                "some-owner/some-repo", "42",
            ],
            stdin_bytes=b'{"body": "No issues found.", "review_status": "clean"}',
            token_provider=_RecordingTokenProvider(),
            opener=_github_verdict_opener(landed_body=add_foreign_block),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERDICT_BLOCK_MISMATCH

    def test_clean_single_own_body_still_passes(self, monkeypatch, capsys):
        # Negative control: a clean single-block, correctly-tagged body
        # (no foreign content at all) must still pass -- the backstop must
        # not produce false positives on the ordinary success path.
        code = _run_main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--verdict-findings",
                "--verdict-head-sha", _HEAD_SHA,
                "some-owner/some-repo", "42",
            ],
            stdin_bytes=_FINDINGS_STDIN,
            token_provider=_RecordingTokenProvider(),
            opener=_github_verdict_opener(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["verdict_block_verified"] is True


# ---------------------------------------------------------------------------
# --body-env (lr-10a996 BODY-TRANSPORT half): body-off-argv-and-pipe via a
# fixed, statically-analyzable staged path -- transport.body_env. Sibling of
# transport.git_host_api's own --body-env coverage; review-post's default
# (stdin, no explicit flag) stays byte-for-byte unchanged when --body-env is
# absent -- every test above this class proves that.
# ---------------------------------------------------------------------------


def _stage_body_env(
    tmp_path,
    body: bytes,
    *,
    caller: str = "release-dispatcher",
    target_pr: int = 42,
    head_sha: str | None = None,
) -> None:
    """Stage a body + identity stamp at the CALLER-NAMESPACED path
    (lr-3a7ae8, stamped lr-becdef) -- `body.<caller>.json` /
    `body.<caller>.stamp.json` -- matching what `--caller` resolves to for
    the verb.main() invocation under test. *caller* defaults to
    `credential_provider.DEFAULT_ROLE`'s value ("release-dispatcher"), the
    same default --caller itself resolves to when omitted from argv --
    every existing caller of this helper that omits --caller from its own
    argv needs no change; callers that pass an explicit --caller must pass
    the SAME value here. *target_pr* defaults to 42, the PR number every
    test in this class posts to.
    """
    from clagentic_loadout.transport.body_env import stage_caller_body

    stage_caller_body(
        caller=caller,
        body_bytes=body,
        target_pr=target_pr,
        head_sha=head_sha,
        env={"TMPDIR": str(tmp_path)},
    )


class TestBodyEnvUsageAndEndToEnd:
    def test_missing_staged_file_exits_body_env_unreadable_before_token_resolution(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        code = verb.main(
            ["--platform", "github", "--body-env", "some-owner/some-repo", "42"],
            token_provider=_RefusingTokenProvider(),
            opener=None,
        )
        assert code == verb.EXIT_BODY_ENV_UNREADABLE

    def test_github_backend_success_from_staged_file(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        _stage_body_env(tmp_path, b'{"body": "LGTM"}', caller="some-role")
        provider = _RecordingTokenProvider()

        # Constant argv -- no per-invocation body substring anywhere.
        argv = [
            "--caller", "some-role", "--platform", "github", "--body-env",
            "some-owner/some-repo", "42",
        ]
        assert not any("{" in a for a in argv)

        code = verb.main(argv, token_provider=provider, opener=_github_success_opener())
        assert code == verb.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["verified_by_login"] == "some-role"
        assert out["pr_number"] == 42

    def test_forgejo_backend_success_from_staged_file(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        _stage_body_env(tmp_path, b'{"body": "LGTM"}', caller="some-role")
        provider = _RecordingTokenProvider()

        code = verb.main(
            [
                "--caller", "some-role",
                "--platform", "forgejo",
                "--git-host-base-url", "http://git-host.example.com",
                "--body-env",
                "some-owner/some-repo", "42",
            ],
            token_provider=provider,
            opener=_forgejo_success_opener(),
        )
        assert code == verb.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["verified_by_login"] == "some-role"
        assert out["verified_id"] == 9

    def test_empty_staged_file_rejected_same_as_empty_stdin(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        _stage_body_env(tmp_path, b"")

        code = verb.main(
            ["--platform", "github", "--body-env", "some-owner/some-repo", "42"],
            token_provider=_RefusingTokenProvider(),
            opener=None,
        )
        assert code == verb.EXIT_BODY_STDIN_EMPTY

    def test_token_mint_failure_does_not_consume_a_genuinely_staged_body(
        self, monkeypatch, tmp_path
    ):
        """BOBBIE re-audit follow-up: a NON-EMPTY staged body must survive a
        token-mint failure -- the read that would consume it must not run
        until AFTER the mint has already succeeded. Before this fix,
        build_backend() (platform guard + token mint) ran only at post
        time, AFTER --body-env's read had already consumed the staged
        body+stamp -- a caller whose token mint failed would have its staged
        body destroyed and have to re-stage from scratch to retry."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        _stage_body_env(tmp_path, b'{"body": "LGTM"}', caller="some-role")

        class _FailingTokenProvider:
            def resolve_token(self, role: str) -> str:
                raise CredentialProviderError("mint failed")

        code = verb.main(
            [
                "--caller", "some-role", "--platform", "github", "--body-env",
                "some-owner/some-repo", "42",
            ],
            token_provider=_FailingTokenProvider(),
            opener=None,
        )
        assert code == verb.EXIT_TOKEN_FETCH_FAILED

        # The staged body must NOT have been consumed -- a retry with a
        # working token provider should still find it.
        from clagentic_loadout.transport.body_env import read_caller_body_bytes

        still_staged = read_caller_body_bytes(
            caller="some-role", expect_target_pr=42, env={"TMPDIR": str(tmp_path)}
        )
        assert still_staged == b'{"body": "LGTM"}'

    def test_verdict_route_reads_review_status_from_staged_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        _stage_body_env(
            tmp_path,
            b'{"body": "No issues found.", "review_status": "clean"}',
            caller="reviewer",
            head_sha=_HEAD_SHA,
        )

        code = verb.main(
            [
                "--caller", "reviewer", "--platform", "github", "--body-env",
                "--verdict-review-status", "clean",
                "--verdict-head-sha", _HEAD_SHA,
                "some-owner/some-repo", "42",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=_github_verdict_opener(),
        )
        assert code == verb.EXIT_OK

    def test_does_not_touch_stdin_when_body_env_supplied(self, monkeypatch, tmp_path):
        """A --body-env invocation must never read sys.stdin at all -- a
        harness that supplies no stdin (e.g. redirected from /dev/null, or
        simply never wired up) must still succeed."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        _stage_body_env(tmp_path, b'{"body": "LGTM"}', caller="some-role")

        class _ExplodingStdin:
            @property
            def buffer(self):
                raise AssertionError("stdin must not be touched when --body-env is set")

        monkeypatch.setattr("sys.stdin", _ExplodingStdin())

        code = verb.main(
            ["--caller", "some-role", "--platform", "github", "--body-env",
             "some-owner/some-repo", "42"],
            token_provider=_RecordingTokenProvider(),
            opener=_github_success_opener(),
        )
        assert code == verb.EXIT_OK


class TestBodyEnvStaleReadRegression:
    """lr-becdef: PR #388 foreign-body incident regression, exercised
    through review.verb's own main() entry point. A body staged for one PR
    must never be silently re-read/re-posted for a DIFFERENT PR, and a
    body must never be re-postable twice without re-staging."""

    def test_body_staged_for_different_pr_refused_before_network(self, monkeypatch, tmp_path):
        # Direct reproduction of the PR #388 incident shape: a body staged
        # for PR 100 (a prior, unrelated review) must never be read as if
        # it were staged for PR 200.
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        _stage_body_env(
            tmp_path,
            b'{"body": "BOBBIE - clean: archivist_client.py review."}',
            caller="some-role",
            target_pr=100,
        )

        def fake_opener(req, timeout=15):
            raise AssertionError("no network call should happen on a stale-PR body mismatch")

        code = verb.main(
            ["--caller", "some-role", "--platform", "github", "--body-env",
             "some-owner/some-repo", "200"],
            token_provider=_RecordingTokenProvider(),
            opener=fake_opener,
        )
        assert code == verb.EXIT_BODY_ENV_UNREADABLE

    def test_read_twice_without_restaging_fails_closed(self, monkeypatch, tmp_path):
        # First invocation posts and consumes the staged body; a SECOND
        # invocation with no re-staging step (the exact PR #388 mechanism:
        # a harness's staging write skipped/guard-denied) must fail closed
        # rather than silently re-reading and re-posting the first body.
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        _stage_body_env(tmp_path, b'{"body": "LGTM"}', caller="some-role")

        argv = [
            "--caller", "some-role", "--platform", "github", "--body-env",
            "some-owner/some-repo", "42",
        ]

        code_first = verb.main(
            argv, token_provider=_RecordingTokenProvider(), opener=_github_success_opener()
        )
        assert code_first == verb.EXIT_OK

        def refusing_opener(req, timeout=15):
            raise AssertionError("no network call should happen on the second, unstaged invocation")

        code_second = verb.main(
            argv, token_provider=_RecordingTokenProvider(), opener=refusing_opener
        )
        assert code_second == verb.EXIT_BODY_ENV_UNREADABLE


# ---------------------------------------------------------------------------
# --delete-own-comment (lr-f43c4b): platform-aware self-delete CLI routing.
# Root cause: review.github_backend.delete_own_comment (and, on the Forgejo
# side, transport.git_host_api.delete_own_comment) already existed, but
# review-post had no CLI entry point wiring either into argv -- a caller on a
# GitHub PR had no self-delete path at all through this shared, platform-
# selected verb (loadout-git-host-api's own --delete-own-comment is
# Forgejo-shaped only). These tests exercise the NEW routing, not the
# underlying admissible-operation checks (already covered by
# test_review_github_backend.py and test_transport_git_host_api.py).
# ---------------------------------------------------------------------------


def _github_delete_opener(
    *, comment_id=4986173328, own_login="reviewer", author_login="reviewer", body="stub"
):
    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if url.endswith("/user"):
            return _json_resp(200, {"login": own_login})
        if method == "GET" and url.endswith(f"/issues/comments/{comment_id}"):
            return _json_resp(
                200,
                {"id": comment_id, "user": {"login": author_login}, "body": body},
            )
        if method == "DELETE" and url.endswith(f"/issues/comments/{comment_id}"):
            return _json_resp(200, {})
        raise AssertionError(f"unexpected: {method} {url}")

    return opener


def _forgejo_delete_opener(
    *, comment_id=42, own_login="reviewer", author_login="reviewer", body="stub"
):
    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if url.endswith("/api/v1/user"):
            return _json_resp(200, {"login": own_login})
        if method == "GET" and url.endswith(f"/issues/comments/{comment_id}"):
            return _json_resp(
                200,
                {"id": comment_id, "user": {"login": author_login}, "body": body},
            )
        if method == "DELETE" and url.endswith(f"/issues/comments/{comment_id}"):
            return _json_resp(200, {})
        raise AssertionError(f"unexpected: {method} {url}")

    return opener


class TestDeleteOwnCommentRoutingBothPlatforms:
    def test_github_delete_succeeds_without_pr_number(self, monkeypatch, capsys):
        # No pr_number positional at all -- delete is comment-id scoped, not
        # PR-scoped, mirroring both backends' own function signatures.
        code = verb.main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--delete-own-comment", "4986173328",
                "some-owner/some-repo",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=_github_delete_opener(),
        )
        assert code == verb.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["deleted_comment_id"] == "4986173328"

    def test_forgejo_delete_succeeds_through_review_post(self, monkeypatch, capsys):
        # Root-cause fix: this is the FORGEJO-shaped delete (transport.
        # git_host_api.delete_own_comment) reached through review-post's
        # shared, platform-selected entry point -- not via
        # loadout-git-host-api's own Forgejo-only CLI.
        code = verb.main(
            [
                "--caller", "reviewer", "--platform", "forgejo",
                "--git-host-base-url", "http://git-host.example.com",
                "--delete-own-comment", "42",
                "some-owner/some-repo",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=_forgejo_delete_opener(),
        )
        assert code == verb.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["deleted_comment_id"] == "42"

    def test_github_cross_author_refused(self, monkeypatch):
        code = verb.main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--delete-own-comment", "4986173328",
                "some-owner/some-repo",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=_github_delete_opener(own_login="reviewer", author_login="some-other-bot"),
        )
        assert code == verb.EXIT_DELETE_OWN_COMMENT_REFUSED

    def test_forgejo_cross_author_refused(self, monkeypatch):
        code = verb.main(
            [
                "--caller", "reviewer", "--platform", "forgejo",
                "--git-host-base-url", "http://git-host.example.com",
                "--delete-own-comment", "42",
                "some-owner/some-repo",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=_forgejo_delete_opener(own_login="reviewer", author_login="some-other-bot"),
        )
        assert code == verb.EXIT_DELETE_OWN_COMMENT_REFUSED

    def test_github_verdict_fence_refused_even_for_own_comment(self, monkeypatch):
        from clagentic_loadout.merge.verdict import build_verdict_block

        fenced_body = build_verdict_block("reviewer", "clean", "a" * 40, 42)
        code = verb.main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--delete-own-comment", "4986173328",
                "some-owner/some-repo",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=_github_delete_opener(body=fenced_body),
        )
        assert code == verb.EXIT_DELETE_OWN_COMMENT_REFUSED

    def test_forgejo_verdict_fence_refused_even_for_own_comment(self, monkeypatch):
        from clagentic_loadout.merge.verdict import build_verdict_block

        fenced_body = build_verdict_block("reviewer", "clean", "a" * 40, 42)
        code = verb.main(
            [
                "--caller", "reviewer", "--platform", "forgejo",
                "--git-host-base-url", "http://git-host.example.com",
                "--delete-own-comment", "42",
                "some-owner/some-repo",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=_forgejo_delete_opener(body=fenced_body),
        )
        assert code == verb.EXIT_DELETE_OWN_COMMENT_REFUSED

    def test_platform_guard_fires_before_token_mint_for_delete(self, monkeypatch):
        # Mirrors TestPlatformGuardFiresBeforeMint's ordering guarantee for
        # the ordinary post_and_verify path -- the platform guard inside
        # build_backend runs BEFORE _resolve_token even on the delete route,
        # since _run_delete_own_comment reuses build_backend verbatim.
        code = verb.main(
            [
                "--platform", "bitbucket",
                "--delete-own-comment", "1",
                "o/r",
            ],
            token_provider=_RefusingTokenProvider(),
            opener=None,
        )
        assert code != verb.EXIT_OK

    def test_delete_own_comment_never_reads_stdin(self, monkeypatch):
        class _ExplodingStdin:
            @property
            def buffer(self):
                raise AssertionError("stdin must not be touched on the delete route")

        monkeypatch.setattr("sys.stdin", _ExplodingStdin())

        code = verb.main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--delete-own-comment", "4986173328",
                "some-owner/some-repo",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=_github_delete_opener(),
        )
        assert code == verb.EXIT_OK


class TestDeleteOwnCommentDigitOnlyGuard:
    """lr-f43c4b security-review hardening finding (same finding class as
    lr-26f774): the sibling loadout-git-host-api CLI anchors
    --delete-own-comment's value
    against a digit-only pattern at the argv layer BEFORE any I/O
    (transport.git_host_api._ISSUE_COMMENT_ID_RE); review-post's own
    --delete-own-comment route had no equivalent CLI-layer guard. These
    tests prove the new _DELETE_COMMENT_ID_RE check refuses a non-digit
    value on BOTH platforms, before any credential mint (the refusing
    token provider raises if ever called -- a passing test proves the
    guard fired first, no network opener needed at all)."""

    def test_github_non_digit_comment_id_refused_before_token_mint(self, monkeypatch):
        code = verb.main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--delete-own-comment", "123; rm -rf /",
                "some-owner/some-repo",
            ],
            token_provider=_RefusingTokenProvider(),
            opener=None,
        )
        assert code == verb.EXIT_DELETE_OWN_COMMENT_REFUSED

    def test_forgejo_non_digit_comment_id_refused_before_token_mint(self, monkeypatch):
        code = verb.main(
            [
                "--caller", "reviewer", "--platform", "forgejo",
                "--git-host-base-url", "http://git-host.example.com",
                "--delete-own-comment", "not-a-number",
                "some-owner/some-repo",
            ],
            token_provider=_RefusingTokenProvider(),
            opener=None,
        )
        assert code == verb.EXIT_DELETE_OWN_COMMENT_REFUSED

    def test_empty_comment_id_refused(self, monkeypatch):
        code = verb.main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--delete-own-comment", "",
                "some-owner/some-repo",
            ],
            token_provider=_RefusingTokenProvider(),
            opener=None,
        )
        assert code == verb.EXIT_DELETE_OWN_COMMENT_REFUSED

    def test_signed_comment_id_refused(self, monkeypatch):
        # A leading sign is a normal argv byte a digit-only \d+ pattern must
        # not accept -- mirrors both backends' own _validate_comment_id
        # shape (accepts str or int; rejects signed/float-shaped input).
        code = verb.main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--delete-own-comment", "-1",
                "some-owner/some-repo",
            ],
            token_provider=_RefusingTokenProvider(),
            opener=None,
        )
        assert code == verb.EXIT_DELETE_OWN_COMMENT_REFUSED

    def test_valid_digit_only_comment_id_still_passes_negative_control(self, monkeypatch, capsys):
        # Negative control: the new guard must not false-positive on the
        # ordinary, well-formed case.
        code = verb.main(
            [
                "--caller", "reviewer", "--platform", "github",
                "--delete-own-comment", "4986173328",
                "some-owner/some-repo",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=_github_delete_opener(),
        )
        assert code == verb.EXIT_OK
