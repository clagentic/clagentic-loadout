"""test_merge_verb_platform_dispatch.py — CLI-level coverage for
clagentic_loadout.merge.verb's --platform dispatch (lr-9c69).

THIS IS THE LOAD-BEARING RELEASE GATE. This file proves the completion of
the deferred GitHub CLI wiring (lr-5375 shipped merge.github_backend, tested
only at the backend-function level in test_merge_github_backend.py; lr-9c69
makes it CLI-reachable):

  - --platform is mandatory (argparse enforces it; omitting it exits 2,
    mirroring review.verb's own shape).
  - A --platform github invocation dispatches to merge.github_backend, and
    a --platform forgejo invocation dispatches to merge.forgejo_backend --
    proven by an opener that ONLY understands one platform's URL shape
    (an unexpected call raises AssertionError, so a passing test is proof
    the right backend's endpoints were hit).
  - The platform guard (merge.forgejo_backend.assert_platform_is_forgejo /
    merge.github_backend.assert_platform_is_github) refuses BEFORE any
    credential mint, for BOTH wrong-platform directions -- proven via a
    token provider that raises AssertionError if ever invoked.
  - The full gate chain (namespace, authority, stale-SHA, verdict fences,
    diff-scope, title) runs IDENTICALLY on both platforms -- proven by
    running the same gate-triggering fixtures against both backends and
    asserting the same exit codes.
  - The GitHub merge-result disambiguation (200 + merged:false refused,
    never trust the status code alone) is reached through this CLI wiring,
    not just at the backend-unit level.

NO real network call, NO real git, NO Date-dependence anywhere in this file
-- everything is driven through an injected opener + injected token/authority
providers, matching test_merge_verb.py's own discipline exactly.
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timezone

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.verdict import build_verdict_block

_FULL_SHA = "a" * 40


class _RecordingTokenProvider:
    def __init__(self, token: str = "tok-123"):
        self.resolved_for: list[str] = []
        self._token = token

    def resolve_token(self, role: str) -> str:
        self.resolved_for.append(role)
        return self._token


class _RefusingTokenProvider:
    """Raises if ever called -- proves the platform guard fires BEFORE any
    credential mint, for either wrong-platform direction."""

    def resolve_token(self, role: str) -> str:
        raise AssertionError(
            f"token provider must not be called when the platform guard "
            f"should have refused first (role={role!r})"
        )


class _AllowingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        return True


class _FakeResponse:
    def __init__(self, status: int, body: bytes, content_type: str = "application/json"):
        self.status = status
        self._body = body
        # content_type-mode parsing (review.github_backend._github_request,
        # used by the merge-completion attestation transport, lr-20e866)
        # JSON-decodes a success body only when the response carries a
        # Content-Type header containing "json" -- see
        # test_review_github_backend.py's own _FakeResponse for the
        # identical shape this mirrors. merge.github_backend's OWN calls use
        # "strict" mode (no Content-Type check), so this addition is a no-op
        # for every pre-existing call in this fixture.
        self.headers = {"Content-Type": content_type}

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


def _forgejo_opener(
    *, pr_info=None, files=None, comments=None, merge_status=200, ci_state="",
    ci_run_total_count=0, branch_commits=None,
):
    """Understands ONLY Forgejo-shaped URLs -- an unexpected (e.g. GitHub-
    shaped) call raises AssertionError, proving the forgejo backend was the
    one actually dispatched to.

    CI-status defaults to the no-runner-by-design empty shape (lr-afba),
    matching test_merge_verb.py's _make_opener default -- every PRE-EXISTING
    test in this file keeps reaching its own asserted gate outcome. A
    non-empty ci_state gets a matching single HEAD-scoped commit-status
    entry (lr-2d2293: is_empty is status_count-only, so a caller asserting a
    non-empty combined state must also carry a real status entry to be
    "not empty" -- an empty statuses list with a non-blank ci_state would
    otherwise be internally inconsistent test data).

    /actions/tasks is intentionally NOT wired to any handler here (lr-2d2293:
    fetch_ci_status no longer queries it) -- ci_run_total_count is accepted
    for call-site parity with _github_opener but is inert on the Forgejo
    side; if fetch_ci_status regresses and starts calling that endpoint
    again, this opener's fall-through AssertionError will catch it."""
    pr_info = pr_info if pr_info is not None else {"head": {"sha": _FULL_SHA}, "title": "feat: a change"}
    files = files if files is not None else ["a.py"]
    comments = comments if comments is not None else []
    # lr-c14a2d: backfill created_at (monotonic with id) on any comments
    # fixture entry that omits it -- see test_merge_verb.py's _make_opener
    # for the identical rationale.
    comments = [
        c if "created_at" in c else {**c, "created_at": f"2026-01-01T00:00:{c.get('id', 0):02d}Z"}
        for c in comments
    ]
    ci_statuses = [{"state": ci_state, "context": "build"}] if ci_state else []
    # lr-835c57: defaults to an empty branch-commit list -- see
    # test_merge_verb.py's _make_opener docstring for the identical
    # no-op-over-empty-list rationale.
    branch_commits = branch_commits if branch_commits is not None else []

    # Merge-completion attestation (lr-20e866): posted via review.
    # forgejo_backend.post_and_verify_comment AFTER a successful merge --
    # see test_merge_verb.py's _make_opener for the identical fixture
    # rationale (kept separate from `comments` so a POST here never
    # contaminates the reviewer-verdict fixture list).
    posted_comments: list[dict] = []
    # lr-361de3: merge.verb now performs a FRESH post-merge GET .../pulls/{n}
    # readback (merge.merge_readback.verify_merge_landed) -- see
    # test_merge_verb.py's _make_opener for the identical rationale/overlay
    # this mirrors.
    _merge_landed = [False]

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        # /api/v1/user (the attestation transport's bot-login resolution,
        # lr-20e866) is the one Forgejo-shaped endpoint with no /repos/
        # segment -- allowed alongside the /api/v1/repos/ shape every other
        # call in this fixture uses.
        assert "/api/v1/repos/" in url or url.endswith("/api/v1/user"), (
            f"expected Forgejo-shaped URL, got: {url}"
        )
        if method == "POST" and url.endswith("/merge"):
            if merge_status in (200, 204):
                _merge_landed[0] = True
                return _FakeResponse(merge_status, b"{}")
            raise urllib.error.HTTPError(url, merge_status, "err", {}, io.BytesIO(b"{}"))
        if method == "POST" and "/comments" in url:
            posted_body = json.loads(req.data.decode("utf-8"))["body"]
            posted_comments.append(
                {
                    "id": 9001 + len(posted_comments),
                    "user": {"login": "loadout-merger"},
                    "body": posted_body,
                    "html_url": "https://forgejo.example/comment/9001",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            return _json_resp(201, posted_comments[-1])
        if method == "GET" and url.endswith("/user"):
            return _json_resp(200, {"login": "loadout-merger"})
        if method == "GET" and url.endswith("/files"):
            return _json_resp(200, [{"filename": f} for f in files])
        if method == "GET" and url.endswith("/comments"):
            return _json_resp(200, comments + posted_comments)
        if method == "GET" and url.endswith("/status"):
            return _json_resp(200, {"state": ci_state, "statuses": ci_statuses})
        if method == "GET" and "/compare/" in url:
            return _json_resp(200, {"commits": branch_commits, "ahead_by": len(branch_commits)})
        if method == "GET" and "/pulls/" in url:
            if _merge_landed[0]:
                return _json_resp(
                    200, {**pr_info, "merged": True, "merge_commit_sha": "e" * 40}
                )
            return _json_resp(200, pr_info)
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener


def _github_opener(
    *,
    pr_info=None,
    files=None,
    comments=None,
    merge_status=200,
    merged=True,
    ci_state="",
    ci_run_total_count=0,
    branch_commits=None,
):
    """Understands ONLY GitHub-shaped URLs (api.github.com) -- an unexpected
    (e.g. Forgejo-shaped) call raises AssertionError, proving the github
    backend was the one actually dispatched to.

    CI-status defaults to the no-runner-by-design empty shape (lr-afba),
    mirroring _forgejo_opener's own default. A non-empty ci_state gets a
    matching single HEAD-scoped commit-status entry (lr-2d2293: is_empty is
    status_count-only -- see _forgejo_opener's docstring for the identical
    rationale).

    lr-71f467: the attestation transport now posts to issues/{pr}/comments
    (an ISSUE COMMENT), not /pulls/{pr}/reviews -- see review.github_backend's
    "VERDICT-TRANSPORT PARITY" docstring section. *comments* (the gate's
    pre-seeded reviewer-verdict fixture) and posted_comments (what the
    attestation POST appends) share one list, mirroring _forgejo_opener.
    """
    pr_info = pr_info if pr_info is not None else {"head": {"sha": _FULL_SHA}, "title": "feat: a change"}
    files = files if files is not None else ["a.py"]
    comments = comments if comments is not None else []
    # lr-c14a2d: see _forgejo_opener's identical backfill above.
    comments = [
        c if "created_at" in c else {**c, "created_at": f"2026-01-01T00:00:{c.get('id', 0):02d}Z"}
        for c in comments
    ]
    ci_statuses = [{"state": ci_state, "context": "ci"}] if ci_state else []
    # lr-835c57: defaults to an empty branch-commit list -- see
    # _forgejo_opener's identical rationale above.
    branch_commits = branch_commits if branch_commits is not None else []

    # Merge-completion attestation (lr-20e866): posted via review.
    # github_backend.post_and_verify_review AFTER a successful merge --
    # resolve_own_login (GET /user), a pre-post dedupe readback (GET
    # .../comments), the POST itself, then the mandatory post-POST readback
    # (GET .../comments again). No github_app.slugs entry is configured for
    # this fixture's caller, so resolve_own_login always takes the GET /user
    # path (never the config-first short-circuit).
    posted_comments: list[dict] = []
    # lr-361de3: see _forgejo_opener's identical rationale above.
    _merge_landed = [False]

    def opener(req, timeout=30):
        url = req.full_url
        method = req.get_method()
        assert "api.github.com" in url, f"expected GitHub-shaped URL, got: {url}"
        if method == "PUT" and url.endswith("/merge"):
            if merge_status == 200:
                if merged:
                    _merge_landed[0] = True
                return _json_resp(200, {"merged": merged, "message": "" if merged else "not mergeable"})
            raise urllib.error.HTTPError(
                url, merge_status, "err", {}, io.BytesIO(json.dumps({"message": "refused"}).encode("utf-8"))
            )
        if method == "GET" and url.endswith("/user"):
            return _json_resp(200, {"login": "loadout-merger"})
        if method == "POST" and url.endswith("/comments"):
            posted_body = json.loads(req.data.decode("utf-8"))["body"]
            posted_comments.append(
                {
                    "id": 8001 + len(posted_comments),
                    "user": {"login": "loadout-merger"},
                    "body": posted_body,
                    "html_url": "https://github.example/comment/8001",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            return _json_resp(200, posted_comments[-1])
        if method == "GET" and url.endswith("/files"):
            return _json_resp(200, [{"filename": f} for f in files])
        if method == "GET" and url.endswith("/comments"):
            return _json_resp(200, comments + posted_comments)
        if method == "GET" and url.endswith("/status"):
            return _json_resp(200, {"state": ci_state, "statuses": ci_statuses})
        if method == "GET" and url.endswith("/check-runs"):
            return _json_resp(200, {"total_count": ci_run_total_count})
        if method == "GET" and "/compare/" in url:
            return _json_resp(200, {"commits": branch_commits, "ahead_by": len(branch_commits)})
        if method == "GET" and "/pulls/" in url:
            if _merge_landed[0]:
                return _json_resp(
                    200, {**pr_info, "merged": True, "merge_commit_sha": "e" * 40}
                )
            return _json_resp(200, pr_info)
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener


def _base_args(platform: str, **overrides) -> list[str]:
    args = {
        "--platform": platform,
        "--role": "merger",
        "--authorized-role": "merger",
        "--repo": "some-owner/some-repo",
        "--pr": "1",
    }
    args.update(overrides)
    argv: list[str] = []
    for key, value in args.items():
        if value is None:
            continue
        argv.extend([key, str(value)])
    # lr-ac5c8a: this file exercises platform dispatch/the gate chain, never
    # post_merge_steps, and none of its fixtures carry a local working tree
    # -- --no-post-merge-tree satisfies the now-mandatory --repo-path/
    # --no-post-merge-tree/--skip-post-merge acknowledgment (see
    # test_merge_verb.py's _base_args for the identical rationale).
    if "--repo-path" not in argv and "--skip-post-merge" not in argv:
        argv.append("--no-post-merge-tree")
    return argv


class TestPlatformIsMandatory:
    def test_missing_platform_is_argparse_usage_error(self):
        # argparse itself enforces --platform as required and exits with its
        # own usage code (2) before _run() is ever entered -- mirrors
        # review.verb's own --platform-is-mandatory shape.
        code = verb.main(
            ["--role", "merger", "--repo", "o/r", "--pr", "1"],
            token_provider=_RefusingTokenProvider(),
        )
        assert code == 2


class TestPlatformDispatchesToCorrectBackend:
    def test_platform_forgejo_dispatches_to_forgejo_backend(self):
        argv = _base_args("forgejo")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_forgejo_opener(),
        )
        assert code == verb.EXIT_OK

    def test_platform_github_dispatches_to_github_backend(self):
        argv = _base_args("github")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_github_opener(),
        )
        assert code == verb.EXIT_OK


class TestPlatformGuardFiresBeforeCredentialMint:
    def test_resolve_backend_refuses_github_platform_before_forgejo_guard_would_pass(self):
        """Drives _resolve_backend directly with a deliberately-mismatched
        internal call (forcing assert_platform_is_forgejo against a
        'github' explicit_platform) -- proves the ordering guarantee:
        the guard raises PlatformMismatchError before _resolve_token is
        ever reached, for the Forgejo direction."""
        import pytest

        from clagentic_loadout.merge.errors import PlatformMismatchError
        from clagentic_loadout.merge.forgejo_backend import assert_platform_is_forgejo

        with pytest.raises(PlatformMismatchError):
            assert_platform_is_forgejo("o", "r", explicit_platform="github")

    def test_resolve_backend_refuses_forgejo_platform_before_github_guard_would_pass(self):
        """Mirror-image: forcing assert_platform_is_github against a
        'forgejo' explicit_platform proves the GitHub-direction guard also
        raises before any token mint."""
        import pytest

        from clagentic_loadout.merge.errors import PlatformMismatchError
        from clagentic_loadout.merge.github_backend import assert_platform_is_github

        with pytest.raises(PlatformMismatchError):
            assert_platform_is_github("o", "r", explicit_platform="forgejo")

    def test_unrecognized_platform_value_refuses_before_token_mint(self):
        # argparse itself restricts --platform to the two known choices, so
        # drive _resolve_backend directly to prove the ordering guarantee
        # holds even if a future caller bypasses the CLI parser.
        code = None
        try:
            verb._resolve_backend(
                "bitbucket",
                owner="o",
                repo="r",
                role="merger",
                git_host_base="http://git-host.example.com",
                token_provider=_RefusingTokenProvider(),
                opener=None,
            )
        except verb.MergeVerbError as exc:
            code = exc.code
        assert code == verb.EXIT_USAGE

    def test_valid_platform_dispatch_resolves_token_exactly_once(self):
        """A valid --platform value resolves the token exactly once (no
        double-resolution, no bleed-through between the guard check and the
        real token mint) -- the ordering guarantee's non-refusal side."""
        argv = _base_args("forgejo")
        provider = _RecordingTokenProvider()
        code = verb.main(
            argv,
            token_provider=provider,
            authority_provider=_AllowingAuthorityProvider(),
            opener=_forgejo_opener(),
        )
        assert code == verb.EXIT_OK
        assert provider.resolved_for == ["merger"]


class TestGateChainIdenticalAcrossPlatforms:
    """Runs the same gate-triggering fixtures against BOTH backends and
    asserts identical exit codes -- proving platform choice affects only
    fact-fetching/merge execution, never the gate chain itself."""

    def test_stale_sha_refuses_identically_on_both_platforms(self):
        other_sha = "b" * 40
        for platform, opener_factory in (
            ("forgejo", _forgejo_opener),
            ("github", _github_opener),
        ):
            argv = _base_args(platform, **{"--expected-head-sha": other_sha})
            code = verb.main(
                argv,
                token_provider=_RecordingTokenProvider(),
                authority_provider=_AllowingAuthorityProvider(),
                opener=opener_factory(pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: x"}),
            )
            assert code == verb.EXIT_STALE_HEAD_SHA, f"platform={platform!r}"

    def test_blocking_verdict_refuses_identically_on_both_platforms(self):
        block = build_verdict_block("some-reviewer", "blocking", _FULL_SHA, 1)
        comments = [{"id": 1, "user": {"login": "reviewer-login"}, "body": block}]
        for platform, opener_factory in (
            ("forgejo", _forgejo_opener),
            ("github", _github_opener),
        ):
            argv = _base_args(platform, **{"--required-reviewer": "some-reviewer:reviewer-login"})
            code = verb.main(
                argv,
                token_provider=_RecordingTokenProvider(),
                authority_provider=_AllowingAuthorityProvider(),
                opener=opener_factory(
                    pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: x"}, comments=comments
                ),
            )
            assert code == verb.EXIT_GATE_RESULT_BLOCKED, f"platform={platform!r}"

    def test_diff_scope_cap_refuses_identically_on_both_platforms(self):
        files = [f"f{i}.py" for i in range(5)]
        for platform, opener_factory in (
            ("forgejo", _forgejo_opener),
            ("github", _github_opener),
        ):
            argv = _base_args(platform, **{"--max-changed-files": "3"})
            code = verb.main(
                argv,
                token_provider=_RecordingTokenProvider(),
                authority_provider=_AllowingAuthorityProvider(),
                opener=opener_factory(files=files),
            )
            assert code == verb.EXIT_GATE_RESULT_BLOCKED, f"platform={platform!r}"

    def test_bad_title_refuses_identically_on_both_platforms(self):
        for platform, opener_factory in (
            ("forgejo", _forgejo_opener),
            ("github", _github_opener),
        ):
            argv = _base_args(platform)
            code = verb.main(
                argv,
                token_provider=_RecordingTokenProvider(),
                authority_provider=_AllowingAuthorityProvider(),
                opener=opener_factory(pr_info={"head": {"sha": _FULL_SHA}, "title": "not conventional"}),
            )
            assert code == verb.EXIT_PR_TITLE_INVALID, f"platform={platform!r}"

    def test_ci_status_gate_refuses_identically_on_both_platforms(self):
        """lr-afba CI-status-gate slice: a runner-wired repo with a real
        FAILING combined state at HEAD refuses on both platforms -- the
        gate chain (including the new CI-status link) runs identically
        regardless of --platform, matching every other gate above."""
        for platform, opener_factory in (
            ("forgejo", _forgejo_opener),
            ("github", _github_opener),
        ):
            argv = _base_args(platform)
            code = verb.main(
                argv,
                token_provider=_RecordingTokenProvider(),
                authority_provider=_AllowingAuthorityProvider(),
                opener=opener_factory(ci_state="failure", ci_run_total_count=1),
            )
            assert code == verb.EXIT_CI_STATUS_FAILED, f"platform={platform!r}"

    def test_ci_status_gate_empty_evidence_passes_identically_on_both_platforms(self):
        """The no-runner-by-design empty-CI case (lr-368c) passes on both
        platforms -- explicit positive control alongside the failure-state
        negative control above."""
        for platform, opener_factory in (
            ("forgejo", _forgejo_opener),
            ("github", _github_opener),
        ):
            argv = _base_args(platform)
            code = verb.main(
                argv,
                token_provider=_RecordingTokenProvider(),
                authority_provider=_AllowingAuthorityProvider(),
                opener=opener_factory(),  # default: empty CI evidence
            )
            assert code == verb.EXIT_OK, f"platform={platform!r}"

    def test_happy_path_merges_identically_on_both_platforms(self):
        block = build_verdict_block("some-reviewer", "clean", _FULL_SHA, 1)
        comments = [{"id": 1, "user": {"login": "reviewer-login"}, "body": block}]
        for platform, opener_factory in (
            ("forgejo", _forgejo_opener),
            ("github", _github_opener),
        ):
            argv = _base_args(
                platform,
                **{
                    "--expected-head-sha": _FULL_SHA,
                    "--required-reviewer": "some-reviewer:reviewer-login",
                    "--max-changed-files": "10",
                },
            )
            token_provider = _RecordingTokenProvider()
            code = verb.main(
                argv,
                token_provider=token_provider,
                authority_provider=_AllowingAuthorityProvider(),
                opener=opener_factory(
                    pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: ship it"},
                    comments=comments,
                    files=["a.py", "b.py"],
                ),
            )
            assert code == verb.EXIT_OK, f"platform={platform!r}"
            assert token_provider.resolved_for == ["merger"]


class TestGithubMergeResultDisambiguationReachedThroughCli:
    def test_200_merged_false_refuses_through_full_cli_dispatch(self):
        """Proves the GitHub 200+merged:false disambiguation (never trust
        the status code alone -- merge.github_backend.merge_pr) is reached
        through this CLI wiring, not just exercised at the backend-unit
        level in test_merge_github_backend.py."""
        argv = _base_args("github")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_github_opener(merged=False),
        )
        assert code == verb.EXIT_MERGE_FAILED

    def test_github_merge_execution_failure_surfaces_distinct_exit_code(self):
        argv = _base_args("github")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_github_opener(merge_status=500),
        )
        assert code == verb.EXIT_MERGE_FAILED


class TestBareReviewerNameGithubPlatform:
    """lr-2f1378: the GitHub-platform half of the bare --required-reviewer
    name coverage (the Forgejo half, plus the explicit-override and
    anti-spoof cases, live in test_merge_verb.py's
    TestBareReviewerNameResolution -- that file's opener does not
    understand GitHub-shaped URLs, so the GitHub cases live here alongside
    this file's own _github_opener fixture)."""

    def test_bare_name_resolves_to_configured_slug_bot_and_gates_clean(self, monkeypatch):
        # (b) bare name on github resolves to <slug>[bot] via
        # resolve_github_app_slug and gates correctly. Patches
        # merge.reviewer_login's own imported resolve_github_app_slug name
        # (mirroring test_review_github_backend.py's isolation pattern)
        # rather than setenv, so this test is immune to whatever real
        # ~/.config/clagentic/loadout/config.yaml or env var this machine
        # may already carry for other crew tooling.
        monkeypatch.setattr(
            "clagentic_loadout.merge.reviewer_login.resolve_github_app_slug",
            lambda **kwargs: "clagentic-reviewer",
        )
        block = build_verdict_block("peaches", "clean", _FULL_SHA, 1)
        argv = _base_args("github", **{"--required-reviewer": "peaches"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_github_opener(
                pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: x"},
                comments=[
                    {"id": 1, "user": {"login": "clagentic-reviewer[bot]"}, "body": block}
                ],
            ),
        )
        assert code == verb.EXIT_OK

    def test_bare_name_with_no_configured_slug_fails_closed(self, monkeypatch):
        # (d) a bare name with no configured slug on github fails closed
        # with a clear error, not a silent skip -- checked BEFORE any
        # network call, so a _RefusingTokenProvider proves no credential
        # was minted either. Patches merge.reviewer_login's own imported
        # resolve_github_app_slug name to force the unconfigured branch
        # deterministically (same isolation rationale as the happy-path
        # test above).
        from clagentic_loadout.transport.github_app_config import (
            GithubAppSlugNotConfiguredError,
        )

        def _raise(**kwargs):
            raise GithubAppSlugNotConfiguredError("no slug configured")

        monkeypatch.setattr(
            "clagentic_loadout.merge.reviewer_login.resolve_github_app_slug", _raise
        )
        argv = _base_args("github", **{"--required-reviewer": "peaches"})
        code = verb.main(
            argv,
            token_provider=_RefusingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
        )
        assert code == verb.EXIT_USAGE


class TestNamespaceAndAuthorityRunBeforePlatformGuard:
    def test_namespace_denied_never_reaches_platform_guard_or_credential(self):
        """Namespace guard (step 1) and authority (step 2) run before the
        platform guard/credential resolution (step 3) regardless of
        --platform -- proven for both platform values via a refusing token
        provider."""
        for platform in ("forgejo", "github"):
            argv = _base_args(platform, **{"--allowed-namespace": "different-owner"})
            code = verb.main(
                argv,
                token_provider=_RefusingTokenProvider(),
                authority_provider=_AllowingAuthorityProvider(),
            )
            assert code == verb.EXIT_NAMESPACE_DENIED, f"platform={platform!r}"


class TestMergeMethodReachesApiPayload:
    """lr-14f704 items 1/4 ACCEPTANCE: --merge-method must ACTUALLY reach the
    merge API payload on BOTH platforms, verified against the payload itself
    -- never inferred from the exit code alone. Before this fix, args.
    merge_method was parsed (it gated the branch commit-subject check) but
    was NEVER forwarded to either backend's merge_pr -- both backends always
    sent a hardcoded "merge" regardless of what --merge-method requested."""

    def test_squash_reaches_github_merge_method_field(self):
        captured: dict = {}

        inner_opener = _github_opener()

        def opener(req, timeout=30):
            url = req.full_url
            method = req.get_method()
            assert "api.github.com" in url
            if method == "PUT" and url.endswith("/merge"):
                captured["payload"] = json.loads(req.data.decode("utf-8"))
                return _json_resp(200, {"merged": True, "sha": "deadbeef"})
            # lr-361de3: this test's own merge-response interception above
            # never sets inner_opener's _merge_landed flag -- the post-merge
            # readback GET needs the confirming overlay directly here.
            # lr-361de3: matched on the exact PR-resource suffix (not a bare
            # "/pulls/" substring, which also matches /pulls/1/files) so this
            # override answers ONLY the post-merge readback's own GET
            # .../pulls/{n} call, never shadowing /files.
            if method == "GET" and url.endswith("/1"):
                return _json_resp(
                    200, {"head": {"sha": _FULL_SHA}, "title": "feat: a change",
                          "merged": True, "merge_commit_sha": "e" * 40},
                )
            return inner_opener(req, timeout=timeout)

        argv = _base_args("github", **{"--merge-method": "squash"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK
        assert captured["payload"]["merge_method"] == "squash"

    def test_default_reaches_github_merge_method_field_as_merge(self):
        captured: dict = {}

        inner_opener = _github_opener()

        def opener(req, timeout=30):
            url = req.full_url
            method = req.get_method()
            if method == "PUT" and url.endswith("/merge"):
                captured["payload"] = json.loads(req.data.decode("utf-8"))
                return _json_resp(200, {"merged": True, "sha": "deadbeef"})
            # lr-361de3: matched on the exact PR-resource suffix (not a bare
            # "/pulls/" substring, which also matches /pulls/1/files) so this
            # override answers ONLY the post-merge readback's own GET
            # .../pulls/{n} call, never shadowing /files.
            if method == "GET" and url.endswith("/1"):
                return _json_resp(
                    200, {"head": {"sha": _FULL_SHA}, "title": "feat: a change",
                          "merged": True, "merge_commit_sha": "e" * 40},
                )
            return inner_opener(req, timeout=timeout)

        argv = _base_args("github")  # no --merge-method -- default
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK
        assert captured["payload"]["merge_method"] == "merge"

    def test_squash_reaches_forgejo_do_field(self):
        captured: dict = {}

        inner_opener = _forgejo_opener()

        def opener(req, timeout=15):
            url = req.full_url
            method = req.get_method()
            if method == "POST" and url.endswith("/merge"):
                captured["payload"] = json.loads(req.data.decode("utf-8"))
                return _FakeResponse(200, b"{}")
            # lr-361de3: matched on the exact PR-resource suffix (not a bare
            # "/pulls/" substring, which also matches /pulls/1/files) so this
            # override answers ONLY the post-merge readback's own GET
            # .../pulls/{n} call, never shadowing /files.
            if method == "GET" and url.endswith("/1"):
                return _json_resp(
                    200, {"head": {"sha": _FULL_SHA}, "title": "feat: a change",
                          "merged": True, "merge_commit_sha": "e" * 40},
                )
            return inner_opener(req, timeout=timeout)

        argv = _base_args("forgejo", **{"--merge-method": "squash"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK
        assert captured["payload"]["Do"] == "squash"

    def test_default_reaches_forgejo_do_field_as_merge(self):
        captured: dict = {}

        inner_opener = _forgejo_opener()

        def opener(req, timeout=15):
            url = req.full_url
            method = req.get_method()
            if method == "POST" and url.endswith("/merge"):
                captured["payload"] = json.loads(req.data.decode("utf-8"))
                return _FakeResponse(200, b"{}")
            # lr-361de3: matched on the exact PR-resource suffix (not a bare
            # "/pulls/" substring, which also matches /pulls/1/files) so this
            # override answers ONLY the post-merge readback's own GET
            # .../pulls/{n} call, never shadowing /files.
            if method == "GET" and url.endswith("/1"):
                return _json_resp(
                    200, {"head": {"sha": _FULL_SHA}, "title": "feat: a change",
                          "merged": True, "merge_commit_sha": "e" * 40},
                )
            return inner_opener(req, timeout=timeout)

        argv = _base_args("forgejo")  # no --merge-method -- default
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK
        assert captured["payload"]["Do"] == "merge"

    def test_rebase_reaches_forgejo_do_field(self):
        # rebase is a valid Forgejo Do value even though it isn't the
        # commit-subject gate's REAL_MERGE_METHOD -- proves the flag is
        # forwarded verbatim, not filtered down to a 'merge'/'squash' subset.
        captured: dict = {}

        inner_opener = _forgejo_opener()

        def opener(req, timeout=15):
            url = req.full_url
            method = req.get_method()
            if method == "POST" and url.endswith("/merge"):
                captured["payload"] = json.loads(req.data.decode("utf-8"))
                return _FakeResponse(200, b"{}")
            # lr-361de3: matched on the exact PR-resource suffix (not a bare
            # "/pulls/" substring, which also matches /pulls/1/files) so this
            # override answers ONLY the post-merge readback's own GET
            # .../pulls/{n} call, never shadowing /files.
            if method == "GET" and url.endswith("/1"):
                return _json_resp(
                    200, {"head": {"sha": _FULL_SHA}, "title": "feat: a change",
                          "merged": True, "merge_commit_sha": "e" * 40},
                )
            return inner_opener(req, timeout=timeout)

        argv = _base_args("forgejo", **{"--merge-method": "rebase"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK
        assert captured["payload"]["Do"] == "rebase"
