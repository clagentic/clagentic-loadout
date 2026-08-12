"""test_merge_verb.py — end-to-end tests for clagentic_loadout.merge.verb
(lr-885f, Wave B slice 4).

THIS IS THE LOAD-BEARING RELEASE GATE. Every fail-closed link in the gate
chain is exercised here in isolation, proving each one refuses BEFORE the
merge executes: namespace-deny, authority-deny, stale-SHA, missing-verdict,
wrong-reviewer-login (authorship), wrong-SHA-fenced verdict, blocking
verdict, diff-scope-cap, bad-title (+ skip bypass), CI-status (empty-passes,
runner-wired failing/pending refuses, unreachable fails closed -- lr-afba
CI-status-gate slice), credential-missing, and the happy path that actually
merges. NO real network call, NO real git, NO Date-dependence anywhere in
this file -- everything is driven through an injected opener + injected
token/authority providers.
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timezone

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.verdict import build_verdict_block
from clagentic_loadout.transport.credential_provider import CredentialProviderError

_FULL_SHA = "a" * 40
_OTHER_FULL_SHA = "b" * 40


class _RecordingTokenProvider:
    def __init__(self, token: str = "tok-123"):
        self.resolved_for: list[str] = []
        self._token = token

    def resolve_token(self, role: str) -> str:
        self.resolved_for.append(role)
        return self._token


class _RefusingTokenProvider:
    def resolve_token(self, role: str) -> str:
        raise AssertionError(f"token provider must not be called (role={role!r})")


class _MissingCredsTokenProvider:
    def resolve_token(self, role: str) -> str:
        raise CredentialProviderError("no credentials configured for this role")


class _AllowingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        return True


class _DenyingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        return False


class _RefusingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        raise AssertionError("authority provider must not be called")


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


def _json_resp(status: int, payload) -> _FakeResponse:
    return _FakeResponse(status, json.dumps(payload).encode("utf-8"))


_MERGED_COMMIT_SHA = "e" * 40


def _make_opener(
    *,
    pr_info=None,
    files=None,
    comments=None,
    merge_status=200,
    ci_state="",
    ci_statuses=None,
    ci_run_total_count=0,
    branch_commits=None,
    post_merge_readback_confirms=True,
):
    """Route GET/POST calls to canned responses keyed by URL shape. No real
    network call is ever made -- every test in this file supplies its own
    opener via this factory.

    CI-status defaults to the no-runner-by-design empty shape (empty
    combined state, zero statuses, zero actions/tasks total_count) -- see
    merge.ci_status's module docstring -- so every PRE-EXISTING test in this
    file (none of which reasons about CI at all) keeps reaching the exact
    gate outcome it asserted before the CI-status gate (lr-afba) existed.
    Tests that DO want to exercise the CI-status gate pass ci_state /
    ci_statuses / ci_run_total_count explicitly (see TestCiStatusGate).

    branch_commits (lr-835c57) defaults to an EMPTY commit list -- the
    branch commit-subject gate is a no-op over an empty list regardless of
    subject content, so every PRE-EXISTING test in this file (none of which
    reasons about branch commit subjects) keeps reaching the exact gate
    outcome it asserted before that gate existed. Tests that DO want to
    exercise it (see TestBranchCommitSubjectGate) pass a list of
    {"sha": ..., "commit": {"message": ...}} dicts explicitly -- the same
    shape the real compare API's 'commits' field carries.

    post_merge_readback_confirms (lr-361de3, default True): merge.verb now
    performs a FRESH post-merge GET .../pulls/{n} (merge.merge_readback.
    verify_merge_landed) to confirm merged==true with a non-empty
    merge_commit_sha, AFTER the merge_status POST above has already
    succeeded. This fixture tracks whether the merge POST has fired yet
    (`_merge_landed`, a mutable single-element list so the nested `opener`
    closure can flip it) and, once it has, overlays `merged`/
    `merge_commit_sha` onto every subsequent `pr_info` GET response -- the
    SAME pr_info dict a PRE-merge gate read saw stays unmerged-shaped (no
    gate in this file's existing coverage reasons about `merged`/
    `merge_commit_sha` at all, so this is additive, not a behavior change
    for any pre-existing gate assertion). Set False to exercise the
    readback-failure path (TestPostMergeReadback below) without touching
    every other test in this file's own `pr_info` fixture.
    """
    pr_info = pr_info if pr_info is not None else {"head": {"sha": _FULL_SHA}, "title": "feat: a change"}
    files = files if files is not None else ["a.py"]
    comments = comments if comments is not None else []
    branch_commits = branch_commits if branch_commits is not None else []
    # lr-c14a2d: read_reviewer_verdict now requires a valid created_at on
    # every candidate comment for its deterministic latest-verdict
    # selection. Test fixtures across this file predate that requirement
    # and construct comment dicts by id alone (id order == intended
    # chronological order in every existing fixture) -- backfill a
    # monotonic-with-id created_at here rather than touching every call
    # site, so pre-existing fixtures keep exercising the SAME gate outcome
    # they asserted before. A fixture that explicitly needs to test
    # out-of-order created_at vs. id supplies its own 'created_at' key,
    # which this backfill never overwrites.
    comments = [
        c if "created_at" in c else {**c, "created_at": f"2026-01-01T00:00:{c.get('id', 0):02d}Z"}
        for c in comments
    ]
    ci_statuses = ci_statuses if ci_statuses is not None else []

    # Merge-completion attestation (lr-20e866): posted via review.
    # forgejo_backend.post_and_verify_comment AFTER a successful merge --
    # POST issues/<pr>/comments, then GET /api/v1/user (bot-login
    # resolution), then GET issues/<pr>/comments again (readback). Recorded
    # separately from `comments` (the reviewer-verdict fixture list) so a
    # POST here never contaminates the verdict-fence gate's own comment
    # list, and the readback sees exactly the posted attestation body.
    posted_comments: list[dict] = []
    _merge_landed = [False]

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "POST" and url.endswith("/merge"):
            if merge_status in (200, 204):
                _merge_landed[0] = True
                return _FakeResponse(merge_status, b"{}")
            import io
            import urllib.error

            raise urllib.error.HTTPError(url, merge_status, "err", {}, io.BytesIO(b"{}"))
        if method == "POST" and "/comments" in url:
            # created_at is captured AT POST TIME (not a fixed literal) so
            # this always clears post_and_verify_comment's freshness anchor
            # (not_before, captured immediately before the POST) regardless
            # of when the test suite itself runs -- this is a same-instant
            # readback fixture, not an assumption about any real calendar
            # date.
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
        if method == "GET" and url.endswith("/actions/tasks"):
            return _json_resp(200, {"total_count": ci_run_total_count})
        if method == "GET" and "/compare/" in url:
            return _json_resp(200, {"commits": branch_commits, "ahead_by": len(branch_commits)})
        if method == "GET" and "/pulls/" in url:
            if _merge_landed[0] and post_merge_readback_confirms:
                return _json_resp(
                    200,
                    {**pr_info, "merged": True, "merge_commit_sha": _MERGED_COMMIT_SHA},
                )
            return _json_resp(200, pr_info)
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener


def _base_args(**overrides) -> list[str]:
    args = {
        "--platform": "forgejo",
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
    # lr-ac5c8a: this file exercises the gate chain, never post_merge_steps
    # (see test_merge_verb_post_merge.py for that), and none of its fixtures
    # carry a local working tree -- --no-post-merge-tree explicitly
    # acknowledges that, satisfying the now-mandatory --repo-path/
    # --no-post-merge-tree/--skip-post-merge requirement (checked before any
    # credential mint) without changing any gate outcome this file asserts.
    if "--repo-path" not in argv and "--skip-post-merge" not in argv:
        argv.append("--no-post-merge-tree")
    return argv


class TestNamespaceGuard:
    def test_denied_before_any_credential_or_authority_call(self):
        argv = _base_args(**{"--allowed-namespace": "different-owner"})
        code = verb.main(
            argv,
            token_provider=_RefusingTokenProvider(),
            authority_provider=_RefusingAuthorityProvider(),
        )
        assert code == verb.EXIT_NAMESPACE_DENIED

    def test_permissive_when_no_allowlist_configured(self):
        argv = _base_args()
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK


class TestAuthorityGuard:
    def test_denied_before_token_resolution(self):
        argv = _base_args()
        code = verb.main(
            argv,
            token_provider=_RefusingTokenProvider(),
            authority_provider=_DenyingAuthorityProvider(),
        )
        assert code == verb.EXIT_AUTHORITY_DENIED

    def test_empty_authorized_roles_denies_by_default(self):
        """Standalone StaticRoleAuthorityProvider: no --authorized-role
        supplied at all -- must deny, never default-allow."""
        argv = [
            "--platform", "forgejo", "--role", "merger", "--repo", "some-owner/some-repo",
            "--pr", "1", "--no-post-merge-tree",
        ]
        code = verb.main(argv, token_provider=_RefusingTokenProvider())
        assert code == verb.EXIT_AUTHORITY_DENIED


class TestCredentialMissing:
    def test_fails_closed_with_token_fetch_failed(self):
        argv = _base_args()
        code = verb.main(
            argv,
            token_provider=_MissingCredsTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
        )
        assert code == verb.EXIT_TOKEN_FETCH_FAILED


class TestStaleSha:
    def test_mismatched_expected_sha_refuses(self):
        argv = _base_args(**{"--expected-head-sha": _OTHER_FULL_SHA})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: x"}),
        )
        assert code == verb.EXIT_STALE_HEAD_SHA

    def test_matching_expected_sha_passes(self):
        argv = _base_args(**{"--expected-head-sha": _FULL_SHA})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: x"}),
        )
        assert code == verb.EXIT_OK

    def test_absent_expected_sha_is_noop(self):
        argv = _base_args()  # no --expected-head-sha
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK


class TestReviewerVerdicts:
    def _opener_with_comment(self, body: str):
        return _make_opener(
            pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: x"},
            comments=[{"id": 1, "user": {"login": "reviewer-login"}, "body": body}],
        )

    def test_missing_verdict_comment_refuses(self):
        argv = _base_args(**{"--required-reviewer": "some-reviewer:reviewer-login"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: x"}, comments=[]),
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_wrong_reviewer_login_refuses(self):
        # A comment exists with a well-formed block, but from an account
        # that is NOT the configured reviewer login -- authorship is by
        # user.login, so this must refuse exactly like a missing comment.
        block = build_verdict_block("some-reviewer", "clean", _FULL_SHA, 1)
        argv = _base_args(**{"--required-reviewer": "some-reviewer:reviewer-login"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: x"},
                comments=[{"id": 1, "user": {"login": "attacker-account"}, "body": block}],
            ),
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_wrong_sha_fenced_verdict_refuses(self):
        block = build_verdict_block("some-reviewer", "clean", _OTHER_FULL_SHA, 1)
        argv = _base_args(**{"--required-reviewer": "some-reviewer:reviewer-login"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=self._opener_with_comment(block),
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_blocking_verdict_refuses(self):
        block = build_verdict_block("some-reviewer", "blocking", _FULL_SHA, 1)
        argv = _base_args(**{"--required-reviewer": "some-reviewer:reviewer-login"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=self._opener_with_comment(block),
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_clean_current_sha_verdict_passes(self):
        block = build_verdict_block("some-reviewer", "clean", _FULL_SHA, 1)
        argv = _base_args(**{"--required-reviewer": "some-reviewer:reviewer-login"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=self._opener_with_comment(block),
        )
        assert code == verb.EXIT_OK

    def test_no_required_reviewers_is_noop(self):
        argv = _base_args()  # no --required-reviewer at all
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK


class TestBareReviewerNameResolution:
    """lr-2f1378: a bare --required-reviewer name (no ':login') derives its
    expected login platform-aware instead of requiring the caller to know
    the target platform's login convention up front."""

    def test_bare_name_on_forgejo_resolves_to_bare_login_and_gates_clean(self):
        # (a) bare name on forgejo resolves to the bare login and gates
        # correctly: the comment must be authored by literally "peaches".
        block = build_verdict_block("peaches", "clean", _FULL_SHA, 1)
        argv = _base_args(**{"--required-reviewer": "peaches"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: x"},
                comments=[{"id": 1, "user": {"login": "peaches"}, "body": block}],
            ),
        )
        assert code == verb.EXIT_OK

    # NOTE: the GitHub-platform bare-name coverage (test (b) and (d) from the
    # task's required-tests list) lives in
    # test_merge_verb_platform_dispatch.py's TestBareReviewerNameGithubPlatform
    # -- that file already owns the GitHub-shaped opener (api.github.com
    # URLs, including /check-runs) this module's own _make_opener does not
    # understand; reusing it here rather than duplicating a second GitHub
    # opener fixture in this file.

    def test_explicit_name_login_pair_still_works_as_override(self):
        # (c) explicit name:login still works (override/back-compat path),
        # even where the bare-name derivation would have produced a
        # DIFFERENT login (forgejo's bare-name-is-login rule would have
        # expected "peaches"; this pins a different literal login instead).
        block = build_verdict_block("peaches", "clean", _FULL_SHA, 1)
        argv = _base_args(**{"--required-reviewer": "peaches:pinned-login"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: x"},
                comments=[{"id": 1, "user": {"login": "pinned-login"}, "body": block}],
            ),
        )
        assert code == verb.EXIT_OK

    def test_bare_name_anti_spoof_binding_still_holds(self):
        # (e) the anti-spoof binding still holds -- a comment authored by a
        # non-matching login (an attacker claiming to be "peaches" inside
        # the verdict block's own reviewer field) does NOT satisfy the gate,
        # even though the block content itself is well-formed and clean.
        block = build_verdict_block("peaches", "clean", _FULL_SHA, 1)
        argv = _base_args(**{"--required-reviewer": "peaches"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: x"},
                comments=[{"id": 1, "user": {"login": "attacker-account"}, "body": block}],
            ),
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED


class TestDiffScopeCap:
    def test_exceeding_cap_refuses(self):
        files = [f"f{i}.py" for i in range(5)]
        argv = _base_args(**{"--max-changed-files": "3"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(files=files),
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_within_cap_passes(self):
        files = [f"f{i}.py" for i in range(3)]
        argv = _base_args(**{"--max-changed-files": "3"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(files=files),
        )
        assert code == verb.EXIT_OK


class TestTitleGate:
    def test_bad_title_refuses(self):
        argv = _base_args()
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(pr_info={"head": {"sha": _FULL_SHA}, "title": "not conventional"}),
        )
        assert code == verb.EXIT_PR_TITLE_INVALID

    def test_bad_title_with_skip_bypass_passes(self):
        argv = _base_args(**{"--skip-title-check": None})
        argv.append("--skip-title-check")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(pr_info={"head": {"sha": _FULL_SHA}, "title": "not conventional"}),
        )
        assert code == verb.EXIT_OK


class TestCiStatusGate:
    """lr-afba CI-status-gate slice (comment #6); HEAD-scoping fix lr-2d2293.
    Cases, mirroring the task's required coverage:
      - empty CI / zero runs => PASS (the no-runner-by-design case this
        repo itself hits).
      - runner-wired with a FAILING/pending state => REFUSE (negative
        control, so pass-on-empty can never mask a real red).
      - unreachable status endpoint => GateFactUnavailableError / fail
        closed (negative control).
      - mirror-runner shape: zero HEAD-scoped commit statuses but a
        non-zero repo-global run count => PASS, not refuse (lr-2d2293
        regression -- the live false-refusal from session d5aee241)."""

    def test_empty_ci_evidence_passes(self):
        # _make_opener's defaults ARE the empty-CI shape -- explicit here
        # for readability even though every other test in this file already
        # exercises this default implicitly.
        argv = _base_args()
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(ci_state="", ci_statuses=[], ci_run_total_count=0),
        )
        assert code == verb.EXIT_OK

    def test_success_state_passes(self):
        argv = _base_args()
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                ci_state="success",
                ci_statuses=[{"state": "success", "context": "build"}],
                ci_run_total_count=1,
            ),
        )
        assert code == verb.EXIT_OK

    def test_runner_wired_failing_ci_refuses(self):
        argv = _base_args()
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                ci_state="failure",
                ci_statuses=[{"state": "failure", "context": "build"}],
                ci_run_total_count=1,
            ),
        )
        assert code == verb.EXIT_CI_STATUS_FAILED

    def test_runner_wired_pending_ci_refuses(self):
        argv = _base_args()
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                ci_state="pending",
                ci_statuses=[{"state": "pending", "context": "build"}],
                ci_run_total_count=1,
            ),
        )
        assert code == verb.EXIT_CI_STATUS_FAILED

    def test_unreachable_ci_status_endpoint_fails_closed(self):
        def opener(req, timeout=15):
            url = req.full_url
            if url.endswith("/status"):
                raise urllib.error.HTTPError(url, 503, "unavailable", {}, io.BytesIO(b"{}"))
            return _make_opener()(req, timeout=timeout)

        argv = _base_args()
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_GATE_FACT_UNAVAILABLE

    def test_actions_tasks_endpoint_never_called(self):
        # lr-2d2293: fetch_ci_status no longer queries /actions/tasks at
        # all (it is repo-global, not HEAD-scoped) -- the merge verb must
        # complete without ever hitting that endpoint.
        inner_opener = _make_opener()

        def opener(req, timeout=15):
            url = req.full_url
            if url.endswith("/actions/tasks"):
                raise AssertionError(
                    f"unexpected call: {url} -- /actions/tasks must never "
                    f"be queried (lr-2d2293)"
                )
            return inner_opener(req, timeout=timeout)

        argv = _base_args()
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK

    def test_mirror_runner_zero_head_statuses_nonzero_repo_global_runs_passes(self):
        # lr-2d2293 regression: a mirror-runner repo with NO CI runner has
        # zero commit statuses at HEAD (status endpoint) but Forgejo's
        # /actions/tasks (repo-global mirror-sync + historical tasks) can
        # still be non-zero. This must PASS -- the exact false-refusal from
        # session d5aee241. (fetch_ci_status no longer even calls
        # /actions/tasks, so ci_run_total_count here is inert -- kept to
        # document intent and guard against a future regression that
        # reintroduces the call.)
        argv = _base_args()
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(ci_state="", ci_statuses=[], ci_run_total_count=5),
        )
        assert code == verb.EXIT_OK


class TestHappyPath:
    def test_all_gates_pass_and_merge_executes(self):
        block = build_verdict_block("some-reviewer", "clean", _FULL_SHA, 1)
        argv = _base_args(
            **{
                "--expected-head-sha": _FULL_SHA,
                "--required-reviewer": "some-reviewer:reviewer-login",
                "--max-changed-files": "10",
            }
        )
        token_provider = _RecordingTokenProvider()
        code = verb.main(
            argv,
            token_provider=token_provider,
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: ship it"},
                comments=[{"id": 1, "user": {"login": "reviewer-login"}, "body": block}],
                files=["a.py", "b.py"],
                merge_status=200,
            ),
        )
        assert code == verb.EXIT_OK
        assert token_provider.resolved_for == ["merger"]

    def test_merge_execution_failure_surfaces_distinct_exit_code(self):
        argv = _base_args()
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(merge_status=500),
        )
        assert code == verb.EXIT_MERGE_FAILED


class TestUsageErrors:
    def test_malformed_required_reviewer_entry_rejected(self):
        # lr-2f1378: a colon-less entry is now a valid BARE reviewer name
        # (resolved platform-aware), not itself malformed -- an entry is
        # only malformed when it carries a ':' with an empty name or login
        # on either side.
        argv = _base_args(**{"--required-reviewer": "name-with-no-login:"})
        code = verb.main(
            argv,
            token_provider=_RefusingTokenProvider(),
            authority_provider=_RefusingAuthorityProvider(),
        )
        assert code == verb.EXIT_USAGE

    def test_bare_reviewer_name_is_no_longer_malformed(self):
        # lr-2f1378: the colon-less shape this test file previously treated
        # as malformed is now valid input on its own -- see
        # TestBareReviewerNameResolution for the full platform-aware
        # resolution coverage. This asserts the usage-error class alone no
        # longer fires for this shape (namespace guard fires first here,
        # before token resolution, so no live gate-fact fetch happens).
        argv = _base_args(
            **{"--required-reviewer": "no-colon-here", "--allowed-namespace": "different-owner"}
        )
        code = verb.main(
            argv,
            token_provider=_RefusingTokenProvider(),
            authority_provider=_RefusingAuthorityProvider(),
        )
        assert code == verb.EXIT_NAMESPACE_DENIED

    def test_malformed_repo_rejected(self):
        argv = _base_args(**{"--repo": "not-owner-slash-repo-shape"})
        code = verb.main(
            argv,
            token_provider=_RefusingTokenProvider(),
            authority_provider=_RefusingAuthorityProvider(),
        )
        assert code == verb.EXIT_USAGE


class TestCliHygiene:
    def test_help_exits_ok_before_any_argument_is_treated_as_input(self, capsys):
        code = verb.main(
            ["--help"],
            token_provider=_RefusingTokenProvider(),
            authority_provider=_RefusingAuthorityProvider(),
        )
        assert code == verb.EXIT_OK
        assert "merge" in capsys.readouterr().out

    def test_missing_required_repo_is_usage_error(self):
        # argparse itself enforces --repo as required and exits with its own
        # usage code (2) before _run() is ever entered -- the same shape
        # push.verb's own argument parser produces for a missing required
        # flag.
        code = verb.main(
            ["--pr", "1"],
            token_provider=_RefusingTokenProvider(),
            authority_provider=_RefusingAuthorityProvider(),
        )
        assert code == 2


class _RepoRecordingTokenProvider:
    """TokenProvider recording (role, repo) so a test can assert merge's
    --repo (already parsed to owner/repo before the platform guard) reaches
    resolve_token too (lr-ea28)."""

    def __init__(self, token: str = "tok-123"):
        self.calls: list[tuple] = []
        self._token = token

    def resolve_token(self, role: str, *, repo: str | None = None) -> str:
        self.calls.append((role, repo))
        return self._token


class TestRepoContextReachesProvider:
    def test_merge_passes_resolved_repo_to_provider(self):
        block = build_verdict_block("some-reviewer", "clean", _FULL_SHA, 1)
        argv = _base_args(
            **{
                "--expected-head-sha": _FULL_SHA,
                "--required-reviewer": "some-reviewer:reviewer-login",
                "--max-changed-files": "10",
            }
        )
        token_provider = _RepoRecordingTokenProvider()
        code = verb.main(
            argv,
            token_provider=token_provider,
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: ship it"},
                comments=[{"id": 1, "user": {"login": "reviewer-login"}, "body": block}],
                files=["a.py", "b.py"],
                merge_status=200,
            ),
        )
        assert code == verb.EXIT_OK
        assert token_provider.calls == [("merger", "some-owner/some-repo")]


class TestMergeTitleFromPrTitle:
    """lr-1953a8: merge.verb._run passes the PR's own (step-7-gated) title
    through to backend.merge_pr as merge_title, so the merge commit's
    SUBJECT reads from the PR title rather than each backend's own
    branch-ref-bearing default. Uses the SAME Forgejo opener fixture as
    every other test in this file -- see test_merge_verb_platform_dispatch.py
    for the equivalent GitHub-platform coverage (that file's own
    _github_opener fixture is the right home for a GitHub-shaped assertion,
    not a second opener reimplemented here)."""

    def test_pr_title_reaches_merge_post_body_as_commit_title(self):
        captured = {}
        base_opener = _make_opener(
            pr_info={"head": {"sha": _FULL_SHA}, "title": "fix(merge): improve the subject"},
        )

        def _capturing_opener(req, timeout=15):
            if req.get_method() == "POST" and req.full_url.endswith("/merge"):
                captured["data"] = req.data
            return base_opener(req, timeout)

        argv = _base_args()
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_capturing_opener,
        )
        assert code == verb.EXIT_OK
        payload = json.loads(captured["data"].decode("utf-8"))
        # Forgejo is the default --platform in _base_args -- its merge_title
        # field is MergeTitleField (see merge.forgejo_backend.merge_pr).
        assert payload["MergeTitleField"] == "fix(merge): improve the subject"


class TestBranchCommitSubjectGate:
    """lr-835c57: end-to-end CLI wiring for the branch commit-subject
    backstop -- --merge-method resolves whether the gate fires at all, and
    --skip-commit-check bypasses it. Grammar identical to the PR-title gate
    (merge.title_gate), reused unchanged."""

    def test_real_merge_method_non_conformant_subject_refuses(self):
        """Acceptance: merge_method='merge' repo + a branch commit subject
        'lr-XXXX: <desc>' (ID-leading, no type) -> merge REFUSED."""
        argv = _base_args(**{"--merge-method": "merge"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                branch_commits=[
                    {"sha": "c" * 40, "commit": {"message": "lr-835c57: not conventional"}},
                ],
            ),
        )
        assert code == verb.EXIT_COMMIT_SUBJECT_INVALID

    def test_real_merge_method_every_subject_conformant_merges(self):
        """Acceptance: merge_method='merge' repo + every branch subject
        'type(scope): desc (lr-XXXX)' -> merges."""
        argv = _base_args(**{"--merge-method": "merge"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                branch_commits=[
                    {"sha": "c" * 40, "commit": {"message": "feat(lr-835c57): add the gate"}},
                    {"sha": "d" * 40, "commit": {"message": "test(lr-835c57): cover the gate"}},
                ],
            ),
        )
        assert code == verb.EXIT_OK

    def test_squash_repo_is_a_no_op(self):
        """Acceptance: squash repo -> check is a no-op (unaffected) -- a
        non-conformant branch subject never refuses when --merge-method is
        anything other than 'merge'."""
        argv = _base_args(**{"--merge-method": "squash"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                branch_commits=[
                    {"sha": "c" * 40, "commit": {"message": "lr-835c57: would refuse on a real merge"}},
                ],
            ),
        )
        assert code == verb.EXIT_OK

    def test_default_merge_method_is_real_merge(self):
        """--merge-method defaults to 'merge' (a real, non-squash merge is
        the default merge shape both backends actually execute today) -- the
        gate fires without the caller having to opt in explicitly."""
        argv = _base_args()  # no --merge-method supplied
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                branch_commits=[
                    {"sha": "c" * 40, "commit": {"message": "lr-835c57: not conventional"}},
                ],
            ),
        )
        assert code == verb.EXIT_COMMIT_SUBJECT_INVALID

    def test_skip_commit_check_bypasses_on_real_merge(self):
        """Acceptance: --skip-commit-check bypasses, even with a
        non-conformant subject on a merge_method='merge' repo."""
        argv = _base_args(**{"--merge-method": "merge"})
        argv.append("--skip-commit-check")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                branch_commits=[
                    {"sha": "c" * 40, "commit": {"message": "lr-835c57: not conventional"}},
                ],
            ),
        )
        assert code == verb.EXIT_OK


#: Synthetic pattern, per CLAUDE.md rule 6 conformance -- no test in this
#: class depends on the real internal lr-XXXXXX task-id shape.
_SYNTHETIC_GUARD_PATTERN = r"\bWIDGET-\d+\b"


def _write_task_id_guard_config(repo_path, *, pattern: str, mode: str | None = None) -> None:
    import yaml

    config_dir = repo_path / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    push_section: dict = {"task_id_guard_pattern": pattern}
    if mode is not None:
        push_section["task_id_guard_mode"] = mode
    # sync_tree_after_merge: false -- these tests point --repo-path at a
    # plain tmp_path directory (no .git at all, deliberately -- see this
    # class's own docstring), so step 10's working-tree sync would ALWAYS
    # fail regardless of this guard's own outcome; --skip-post-merge alone
    # does not suppress the sync (only the configured STEPS -- see
    # merge.verb's own "AFTER post_merge_steps run" docstring section), so
    # this key is required for a passing-path test in this class to reach
    # EXIT_OK at all.
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({
            "push": push_section,
            "merge": {"sync_tree_after_merge": False},
        }),
        encoding="utf-8",
    )


class TestTaskIdGuardCommitSubjectGate:
    """lr-4005f5: end-to-end CLI wiring for the merge-time task-id guard on
    branch commit subjects -- shares the SAME merge_method='merge' scoping
    as TestBranchCommitSubjectGate above, layered as an INDEPENDENT check
    over the same already-fetched branch_commits. Config is read from
    --repo-path (the same repo-tier config root every other gate key in
    this module resolves through) -- these tests write a REAL
    `.clagentic/loadout/config.yaml` under tmp_path and point --repo-path at
    it (a plain directory, not a git working tree: step 0's repo-path/slug
    consistency check tolerates an unconfirmable tree, see
    merge.repo_path_consistency's own docstring)."""

    def test_no_pattern_configured_matching_shape_subject_is_unaffected(self, tmp_path):
        """Hard acceptance criterion: no configured pattern -> the guard is
        a strict no-op, even with a subject that WOULD match the synthetic
        pattern."""
        import yaml

        config_dir = tmp_path / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump({"merge": {"sync_tree_after_merge": False}}), encoding="utf-8"
        )
        argv = _base_args(**{"--merge-method": "merge", "--repo-path": str(tmp_path)})
        argv.append("--skip-post-merge")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                branch_commits=[
                    {"sha": "c" * 40, "commit": {"message": "feat: fix WIDGET-42 leak"}},
                ],
            ),
        )
        assert code == verb.EXIT_OK

    def test_configured_pattern_matching_subject_blocks_by_default(self, tmp_path):
        """Operator-pinned default: once a pattern IS configured, mode
        defaults to block."""
        _write_task_id_guard_config(tmp_path, pattern=_SYNTHETIC_GUARD_PATTERN)
        argv = _base_args(**{"--merge-method": "merge", "--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                branch_commits=[
                    {"sha": "c" * 40, "commit": {"message": "feat: fix WIDGET-42 leak"}},
                ],
            ),
        )
        assert code == verb.EXIT_TASK_ID_GUARD_VIOLATION

    def test_configured_pattern_non_matching_subject_merges(self, tmp_path):
        _write_task_id_guard_config(tmp_path, pattern=_SYNTHETIC_GUARD_PATTERN)
        argv = _base_args(**{"--merge-method": "merge", "--repo-path": str(tmp_path)})
        argv.append("--skip-post-merge")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                branch_commits=[
                    {"sha": "c" * 40, "commit": {"message": "feat(auth): add the gate"}},
                ],
            ),
        )
        assert code == verb.EXIT_OK

    def test_squash_repo_is_a_no_op_even_with_configured_pattern(self, tmp_path):
        """Shares the SAME merge_method scoping as the grammar gate -- a
        squash repo never fires the task-id guard either, even in block
        mode with a matching subject."""
        _write_task_id_guard_config(tmp_path, pattern=_SYNTHETIC_GUARD_PATTERN)
        argv = _base_args(**{"--merge-method": "squash", "--repo-path": str(tmp_path)})
        argv.append("--skip-post-merge")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                branch_commits=[
                    {"sha": "c" * 40, "commit": {"message": "feat: fix WIDGET-42 leak"}},
                ],
            ),
        )
        assert code == verb.EXIT_OK

    def test_skip_commit_check_bypasses_task_id_guard_too(self, tmp_path):
        _write_task_id_guard_config(tmp_path, pattern=_SYNTHETIC_GUARD_PATTERN)
        argv = _base_args(**{"--merge-method": "merge", "--repo-path": str(tmp_path)})
        argv.append("--skip-commit-check")
        argv.append("--skip-post-merge")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                branch_commits=[
                    {"sha": "c" * 40, "commit": {"message": "feat: fix WIDGET-42 leak"}},
                ],
            ),
        )
        assert code == verb.EXIT_OK

    def test_warn_mode_merges_and_prints_warning(self, tmp_path, capsys):
        _write_task_id_guard_config(tmp_path, pattern=_SYNTHETIC_GUARD_PATTERN, mode="warn")
        argv = _base_args(**{"--merge-method": "merge", "--repo-path": str(tmp_path)})
        argv.append("--skip-post-merge")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                branch_commits=[
                    {"sha": "c" * 40, "commit": {"message": "feat: fix WIDGET-42 leak"}},
                ],
            ),
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "WIDGET-42" in stderr

    def test_violation_message_names_field_value_and_config_key(self, tmp_path, capsys):
        _write_task_id_guard_config(tmp_path, pattern=_SYNTHETIC_GUARD_PATTERN)
        argv = _base_args(**{"--merge-method": "merge", "--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                branch_commits=[
                    {"sha": "c" * 40, "commit": {"message": "feat: fix WIDGET-42 leak"}},
                ],
            ),
        )
        assert code == verb.EXIT_TASK_ID_GUARD_VIOLATION
        stderr = capsys.readouterr().err
        assert "WIDGET-42" in stderr
        assert "task_id_guard_pattern" in stderr
        assert "task_id_guard_mode" in stderr

    def test_grammar_gate_runs_before_task_id_guard(self, tmp_path):
        """A subject that is BOTH non-conventional AND task-id-matching
        refuses on the grammar gate first (EXIT_COMMIT_SUBJECT_INVALID)."""
        _write_task_id_guard_config(tmp_path, pattern=_SYNTHETIC_GUARD_PATTERN)
        argv = _base_args(**{"--merge-method": "merge", "--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                branch_commits=[
                    {"sha": "c" * 40, "commit": {"message": "WIDGET-42: id-leading, no type"}},
                ],
            ),
        )
        assert code == verb.EXIT_COMMIT_SUBJECT_INVALID


class TestPostMergeReadback:
    """lr-361de3: merge.verb performs a FRESH post-merge GET .../pulls/{n}
    readback (merge.merge_readback.verify_merge_landed) AFTER merge_pr's own
    response reports success -- fail-closed, distinct EXIT_MERGE_READBACK_FAILED,
    and a stable {verified, source, detail} envelope key ('readback') every
    other remote-mutating verb's envelope shares.

    NON-VACUITY (task acceptance criterion 4): test_confirmed_merge_reports_verified
    proves the PASSING case actually exercises the readback path (not merely
    that EXIT_OK happens to fall out some other way) -- flip
    post_merge_readback_confirms to False (below, and in the two failure
    tests) and the SAME assertions FAIL, proving this suite would catch a
    reversion of the check.
    """

    def test_confirmed_merge_reports_verified_in_envelope(self, capsys):
        argv = _base_args()
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),  # post_merge_readback_confirms=True (default)
        )
        assert code == verb.EXIT_OK
        out = capsys.readouterr().out
        payload = json.loads(out.strip().splitlines()[-1])
        assert payload["readback"]["verified"] is True
        assert payload["readback"]["source"] == "api_get"
        assert payload["readback"]["detail"]["merged_commit_sha"] == _MERGED_COMMIT_SHA

    def test_unconfirmed_merge_fails_closed_with_distinct_exit_code(self):
        """THE MUTATION-DID-NOT-LAND CASE (task acceptance criterion 4): the
        merge_pr POST itself reports success (200), but the readback GET
        that follows does NOT show merged=True -- this must FAIL the verb,
        never report EXIT_OK. Reverting merge.verb's own fail-closed check
        (i.e. ignoring merge_readback.verify_merge_landed's result) would
        make this test go green on EXIT_OK instead of
        EXIT_MERGE_READBACK_FAILED -- demonstrating the assertion is not
        vacuous."""
        argv = _base_args()
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(post_merge_readback_confirms=False),
        )
        assert code == verb.EXIT_MERGE_READBACK_FAILED

    def test_unconfirmed_merge_never_reaches_post_merge_steps(self, tmp_path):
        """A readback failure refuses BEFORE step 10 (working-tree sync /
        post_merge_steps) -- proven by supplying --repo-path pointed at an
        empty directory (no .git at all): if the verb incorrectly proceeded
        past the readback refusal, tree_sync would raise its OWN
        TreeSyncError/EXIT_POST_MERGE_FAILED, which would be a DIFFERENT exit
        code than the one this test asserts -- proving the refusal really
        happens at the readback point, not coincidentally later."""
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(post_merge_readback_confirms=False),
        )
        assert code == verb.EXIT_MERGE_READBACK_FAILED

    def test_skip_commit_check_never_fetches_branch_commits(self):
        """The bypass short-circuits BEFORE the compare-API fetch -- an
        opener that would raise on any /compare/ call proves the fetch is
        never attempted when skipped."""

        inner_opener = _make_opener()

        def opener(req, timeout=15):
            url = req.full_url
            if "/compare/" in url:
                raise AssertionError("compare API must not be called when --skip-commit-check is set")
            return inner_opener(req, timeout=timeout)

        argv = _base_args(**{"--merge-method": "merge"})
        argv.append("--skip-commit-check")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK
