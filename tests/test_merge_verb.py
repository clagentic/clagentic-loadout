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

import yaml

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.verdict import build_verdict_block
from tests._gate_repo import seed_base_commit
from tests._support.merge_verb import (
    FULL_SHA as _FULL_SHA,
    MERGED_COMMIT_SHA as _MERGED_COMMIT_SHA,
    OTHER_FULL_SHA as _OTHER_FULL_SHA,
    AllowingAuthorityProvider as _AllowingAuthorityProvider,
    DenyingAuthorityProvider as _DenyingAuthorityProvider,
    MissingCredsTokenProvider as _MissingCredsTokenProvider,
    RecordingTokenProvider as _RecordingTokenProvider,
    RefusingAuthorityProvider as _RefusingAuthorityProvider,
    RefusingTokenProvider as _RefusingTokenProvider,
    base_args as _base_args,
    make_opener as _make_opener,
)


def _pr_info_with_base(repo_path, *, title: str = "feat: a change") -> dict:
    """PR payload carrying the real base commit of *repo_path*: the merge gate
    reads its declaration from that commit and fails closed without one."""
    return {
        "head": {"sha": _FULL_SHA},
        "title": title,
        "base": {"ref": "main", "sha": seed_base_commit(repo_path)},
    }


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
    def _opener_with_comment(self, body: str, repo_path=None):
        return _make_opener(
            pr_info=(
                _pr_info_with_base(repo_path, title="feat: x")
                if repo_path is not None
                else {"head": {"sha": _FULL_SHA}, "title": "feat: x"}
            ),
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


def _write_merge_config(repo_path, content: dict) -> None:
    config_dir = repo_path / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(yaml.safe_dump({"merge": content}), encoding="utf-8")


class TestModelAttestation:
    """lr-95543d: `merge: require_model_attestation:` -- OPT-IN, default
    off. A clean verdict passes the reviewer-verdict gate (step 5) whether
    or not it carries model_attested UNLESS the repo's own config opts in
    -- at which point a clean verdict missing/malformed model_attested
    refuses with the SAME EXIT_GATE_RESULT_BLOCKED disposition a blocking
    verdict already gets. Uses --repo-path pointed at a bare tmp_path
    carrying only a config file -- the reviewer-verdict gate (step 5) runs
    well before tree_sync (step 10), so a refusal here never reaches any
    git-tree machinery; a PASS reaches EXIT_OK via _make_opener's ordinary
    merge/attestation-post responses exactly like every other test in this
    file.
    """

    def _opener_with_comment(self, body: str, repo_path=None):
        return _make_opener(
            pr_info=(
                _pr_info_with_base(repo_path, title="feat: x")
                if repo_path is not None
                else {"head": {"sha": _FULL_SHA}, "title": "feat: x"}
            ),
            comments=[{"id": 1, "user": {"login": "reviewer-login"}, "body": body}],
        )

    def test_default_off_clean_verdict_without_model_attested_passes(self, tmp_path):
        # No require_model_attestation key at all -- defaults False, so a
        # clean verdict with no model_attested field still passes, exactly
        # like TestReviewerVerdicts.test_clean_current_sha_verdict_passes.
        # sync_tree_after_merge: false -- this test asserts the gate outcome
        # only, never post_merge_steps/tree_sync; the bare tmp_path has no
        # local git tree for step 10's fetch/checkout to target.
        _write_merge_config(tmp_path, {"sync_tree_after_merge": False})
        block = build_verdict_block("some-reviewer", "clean", _FULL_SHA, 1)
        argv = _base_args(
            **{
                "--required-reviewer": "some-reviewer:reviewer-login",
                "--repo-path": str(tmp_path),
            }
        )
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=self._opener_with_comment(block, tmp_path),
        )
        assert code == verb.EXIT_OK

    def test_opted_in_clean_verdict_missing_model_attested_refuses(self, tmp_path):
        _write_merge_config(tmp_path, {"require_model_attestation": True})
        block = build_verdict_block("some-reviewer", "clean", _FULL_SHA, 1)
        argv = _base_args(
            **{
                "--required-reviewer": "some-reviewer:reviewer-login",
                "--repo-path": str(tmp_path),
            }
        )
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=self._opener_with_comment(block),
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_opted_in_clean_verdict_bare_tier_alias_refuses(self, tmp_path):
        _write_merge_config(tmp_path, {"require_model_attestation": True})
        block = build_verdict_block("some-reviewer", "clean", _FULL_SHA, 1, "gpt-flagship")
        argv = _base_args(
            **{
                "--required-reviewer": "some-reviewer:reviewer-login",
                "--repo-path": str(tmp_path),
            }
        )
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=self._opener_with_comment(block),
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_opted_in_clean_verdict_resolved_model_string_passes(self, tmp_path):
        # sync_tree_after_merge: false -- see the sibling PASS test above.
        _write_merge_config(
            tmp_path,
            {"require_model_attestation": True, "sync_tree_after_merge": False},
        )
        block = build_verdict_block(
            "some-reviewer", "clean", _FULL_SHA, 1, "claude-opus-4-1-20250805"
        )
        argv = _base_args(
            **{
                "--required-reviewer": "some-reviewer:reviewer-login",
                "--repo-path": str(tmp_path),
            }
        )
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=self._opener_with_comment(block, tmp_path),
        )
        assert code == verb.EXIT_OK

    def test_opted_in_clean_verdict_denylisted_model_refuses(self, tmp_path):
        _write_merge_config(
            tmp_path,
            {
                "require_model_attestation": True,
                "model_attestation_denylist": ["deprecated-fallback-4-0"],
            },
        )
        block = build_verdict_block(
            "some-reviewer", "clean", _FULL_SHA, 1, "deprecated-fallback-4-0"
        )
        argv = _base_args(
            **{
                "--required-reviewer": "some-reviewer:reviewer-login",
                "--repo-path": str(tmp_path),
            }
        )
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=self._opener_with_comment(block),
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_opted_in_blocking_verdict_unaffected_by_missing_model_attested(self, tmp_path):
        # A blocking verdict is already refused by assert_clean_verdict --
        # model-attestation enforcement must not additionally punish (or
        # change the disposition of) a reviewer that correctly found a
        # blocking issue, regardless of model_attested.
        _write_merge_config(tmp_path, {"require_model_attestation": True})
        block = build_verdict_block("some-reviewer", "blocking", _FULL_SHA, 1)
        argv = _base_args(
            **{
                "--required-reviewer": "some-reviewer:reviewer-login",
                "--repo-path": str(tmp_path),
            }
        )
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=self._opener_with_comment(block),
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED


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
                pr_info=_pr_info_with_base(tmp_path),
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
                pr_info=_pr_info_with_base(tmp_path),
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
                pr_info=_pr_info_with_base(tmp_path),
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
                pr_info=_pr_info_with_base(tmp_path),
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
                pr_info=_pr_info_with_base(tmp_path),
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
            opener=_make_opener(
                pr_info=_pr_info_with_base(tmp_path), post_merge_readback_confirms=False
            ),
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
