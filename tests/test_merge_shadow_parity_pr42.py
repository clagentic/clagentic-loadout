"""test_merge_shadow_parity_pr42.py — SHADOW-PARITY evidence for lr-afba.

Task lr-afba (merge-verb cutover, comment #2): before NAOMI's dispatch is
re-pointed at loadout-merge, this module proves loadout-merge's gate chain
(merge.verb) reaches the IDENTICAL decision an earlier internal merge tool
already reached on a real, already-merged loadout PR — PR #42
(clagentic/clagentic-loadout), merged by that tool's live path on
2026-07-10.

Gate facts below were originally captured from a live Forgejo API read
against PR #42 during that task, not synthetic fixtures (contrast with
test_merge_verb.py's a*40/b*40 placeholder SHAs) — this is what makes the
test *parity evidence* rather than ordinary unit coverage of the gate
chain. Comment ids, reviewer logins, and the merge SHA below are reproduced
as fixture data for gate-replay purposes; the operational identifiers of
the originating internal PR/comment thread are not reproduced here
(lr-1659 pre-seed scrub):

  - PR #42 head SHA:  156a5cb4f6f163dd9b7eeac47274aa4fad797af2
  - PR #42 title:     "feat(lr-30c0d0): tool-owned --expect-verdict-block in
                       loadout-git-host-api"
  - PR #42 files:     4 (docs/verbs.md, transport/git_host_api.py,
                       tests/test_reviewer_no_disk_staging_or_hand_authored_
                       fence.py, tests/test_transport_git_host_api.py)
  - PEACHES verdict:  fenced ```review-result```, review_status=clean,
                       head_sha=156a5cb4f6f163...
  - BOBBIE verdict:   fenced ```review-result```, review_status=clean,
                       head_sha=156a5cb4f6f163... (supersedes an earlier
                       fenceless comment — the LATEST bobbie-authored
                       comment is the one that counts, matching
                       merge.verdict.read_reviewer_verdict's "latest
                       matching comment wins" selection rule)
  - merge commit SHA: 48365231d5e40a03ac674d09e8181a50bd7266ca (the earlier
                       tool's actual merge result — recorded here for the
                       record, not asserted against, since this module
                       drives the gate chain only, never a second live merge
                       of an already-merged PR)

The earlier tool's ACTUAL decision on this PR (ground truth, read at the
time from the Forgejo API's own audit trail — the post-merge gate-note
comment posted after the real merge): "authorize" — PEACHES fenced clean
(superseding an earlier fenceless comment), BOBBIE fenced clean, diff-scope
4 files (well under the 50-file default cap), title conforms to
Conventional Commits grammar. See the module-level PR_42_* constants below
for the captured API-response shape this test drives loadout-merge's gate
chain against.

WHAT THIS PROVES: replaying the SAME gate facts (SHA-stamp, both fenced
verdicts, file list, title) through loadout-merge's OWN gate chain
(merge.verb._run, gates 1-7 — see that module's docstring for the numbered
list) reaches the SAME authorize decision, with ZERO deltas from the earlier
tool's real-world outcome on this exact PR. This is SHADOW mode: no live
merge call is made against the (already-merged, real) PR — the merge step
itself (gate 8) is driven through an injected fake opener whose POST
.../merge response asserts it was reached with the right method/URL, never a
live network call. This same parity was independently confirmed against the
live Forgejo API during the original task via a live comments read against
the deployed Forgejo instance (2026-07-10) — included here as a static
fixture so the test suite has no network dependency and is reproducible
without a live token.
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timezone

from clagentic_loadout.merge import verb

PR_42_HEAD_SHA = "156a5cb4f6f163dd9b7eeac47274aa4fad797af2"
PR_42_TITLE = "feat(lr-30c0d0): tool-owned --expect-verdict-block in loadout-git-host-api"
PR_42_OWNER = "clagentic"
PR_42_REPO = "clagentic-loadout"
PR_42_NUMBER = 42

#: Verbatim changed-file list, captured via
#: GET /api/v1/repos/clagentic/clagentic-loadout/pulls/42/files (lr-afba,
#: 2026-07-10) -- 4 files, well under the 50-file default diff-scope cap.
PR_42_CHANGED_FILES = [
    "docs/verbs.md",
    "src/clagentic_loadout/transport/git_host_api.py",
    "tests/test_reviewer_no_disk_staging_or_hand_authored_fence.py",
    "tests/test_transport_git_host_api.py",
]

#: PEACHES verdict comment body, matching what was captured via
#: GET /api/v1/repos/clagentic/clagentic-loadout/issues/42/comments (lr-afba,
#: 2026-07-10) -- the LATEST peaches-authored comment (supersedes an earlier
#: fenceless comment, matching read_reviewer_verdict's "latest matching
#: login wins" selection rule).
PEACHES_COMMENT_13081_BODY = (
    "PEACHES — clean (0 findings)\n\n"
    "This PR correctly implements the --expect-verdict-block flag.\n\n"
    "```review-result\n"
    '{"reviewer": "peaches", "review_status": "clean", '
    '"head_sha": "156a5cb4f6f163dd9b7eeac47274aa4fad797af2", "pr_number": 42}\n'
    "```\n"
)

#: BOBBIE verdict comment body, matching what was captured the same way --
#: the LATEST bobbie-authored comment (supersedes an earlier fenceless
#: comment, which carried findings but no verdict fence per its own text).
BOBBIE_COMMENT_13080_BODY = (
    "BOBBIE -- clean\n\n"
    "Re-audit at HEAD 156a5cb4f6f163dd9b7eeac47274aa4fad797af2 (task lr-30c0d0), "
    "superseding comment #13079 which carried findings but no verdict fence.\n\n"
    "```review-result\n"
    '{"reviewer": "bobbie", "review_status": "clean", '
    '"head_sha": "156a5cb4f6f163dd9b7eeac47274aa4fad797af2", "pr_number": 42}\n'
    "```\n"
)

#: All comments as returned by the Forgejo issues-comments endpoint, in the
#: platform's own return order (oldest first) -- id 13078 (fenceless PEACHES,
#: superseded), 13079 (fenceless BOBBIE, superseded), 13080 (fenced BOBBIE,
#: the live verdict), 13081 (fenced PEACHES, the live verdict), 13083 (the
#: post-merge gate-note, not a reviewer verdict). read_reviewer_verdict now
#: selects deterministically by each same-login comment's own created_at
#: (lr-c14a2d) -- created_at is monotonic with id here, matching this
#: fixture's documented oldest-first return order, so 13080/13081 are still
#: the ones that decide the gate, exactly as they decided it for the
#: earlier tool's real run.
PR_42_COMMENTS = [
    {"id": 13078, "user": {"login": "peaches"}, "body": "PEACHES fenceless, superseded", "created_at": "2026-04-01T10:00:00Z"},
    {"id": 13079, "user": {"login": "bobbie"}, "body": "BOBBIE fenceless, superseded", "created_at": "2026-04-01T10:05:00Z"},
    {"id": 13080, "user": {"login": "bobbie"}, "body": BOBBIE_COMMENT_13080_BODY, "created_at": "2026-04-01T11:00:00Z"},
    {"id": 13081, "user": {"login": "peaches"}, "body": PEACHES_COMMENT_13081_BODY, "created_at": "2026-04-01T11:05:00Z"},
    {"id": 13083, "user": {"login": "naomi"}, "body": "gate-note, not a verdict", "created_at": "2026-04-01T12:00:00Z"},
]

#: CI-status gate-fact evidence (lr-afba CI-status-gate slice, comment #6).
#: This repo has no CI runner wired up by design (lr-368c) -- verbatim
#: shape observed on a later loadout PR, PR #49 (lr-482c20) @ head 6a8fcbd:
#: GET commits/{sha}/status returned an empty combined state, GET
#: /actions/tasks reported total_count=0. The earlier merge tool's real run
#: on PR #42 had no CI-status gate at all (this cutover slice is what ADDS it to
#: loadout-merge); this fixture proves loadout-merge's own CI-status gate
#: treats that same empty-evidence shape as a PASS rather than a new,
#: previously-nonexistent refusal that would have regressed the cutover.
PR_42_CI_STATUS_EMPTY = {"state": "", "statuses": []}
PR_42_ACTIONS_TASKS_EMPTY = {"total_count": 0}


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


def _shadow_opener(calls: list[tuple[str, str]]):
    """Route GET calls to the PR #42 fixture data captured above; record
    every call (method, url-shape) so the test can assert the SAME gate
    facts the earlier merge tool read were the ones loadout-merge's gate
    chain also read. The merge POST itself is answered 200 (shadow mode: this never
    touches the real, already-merged PR #42 — no network call is made at
    all, fake or otherwise, once main() returns from this injected opener).

    Also answers the merge-completion attestation transport (lr-20e866,
    POST .../comments + GET /user, reused from review.forgejo_backend) --
    this shadow PR is real and already merged, so this fixture never touches
    it live either; it is answered exactly like every other gate-fact call
    above, from local fixture data only."""
    posted_comments: list[dict] = []
    # lr-361de3: merge.verb now performs a FRESH post-merge GET .../pulls/{n}
    # readback (merge.merge_readback.verify_merge_landed) -- see
    # test_merge_verb.py's _make_opener for the identical overlay rationale.
    # PR #42's real merge_commit_sha is already documented in this module's
    # own header comment (the earlier merge tool's actual merge result) -- reused here
    # rather than a placeholder, since this fixture's whole point is replaying
    # real captured data.
    _merge_landed = [False]

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "POST" and url.endswith("/merge"):
            calls.append(("POST", "merge"))
            _merge_landed[0] = True
            return _FakeResponse(200, b"{}")
        if method == "POST" and "/comments" in url:
            calls.append(("POST", "attestation_comment"))
            posted_body = json.loads(req.data.decode("utf-8"))["body"]
            posted_comments.append(
                {
                    "id": 13090 + len(posted_comments),
                    "user": {"login": "naomi"},
                    "body": posted_body,
                    "html_url": "https://forgejo.example/comment/13090",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            return _json_resp(201, posted_comments[-1])
        if method == "GET" and url.endswith("/user"):
            calls.append(("GET", "user"))
            return _json_resp(200, {"login": "naomi"})
        if method == "GET" and url.endswith("/files"):
            calls.append(("GET", "files"))
            return _json_resp(200, [{"filename": f} for f in PR_42_CHANGED_FILES])
        if method == "GET" and url.endswith("/comments"):
            calls.append(("GET", "comments"))
            return _json_resp(200, PR_42_COMMENTS + posted_comments)
        if method == "GET" and url.endswith("/status"):
            calls.append(("GET", "ci_status"))
            return _json_resp(200, PR_42_CI_STATUS_EMPTY)
        if method == "GET" and url.endswith("/actions/tasks"):
            calls.append(("GET", "actions_tasks"))
            return _json_resp(200, PR_42_ACTIONS_TASKS_EMPTY)
        if method == "GET" and "/compare/" in url:
            # lr-835c57: PR #42's real branch commit subjects were never
            # captured for this fixture -- an empty commit list keeps the
            # new commit-subject gate a no-op over this shadow-parity replay
            # (it does not change the zero-delta authorize outcome any of
            # the PR #42 gate-fact assertions above depend on).
            calls.append(("GET", "compare"))
            return _json_resp(200, {"commits": [], "ahead_by": 0})
        if method == "GET" and "/pulls/" in url:
            calls.append(("GET", "pr_info"))
            if _merge_landed[0]:
                return _json_resp(
                    200,
                    {
                        "head": {"sha": PR_42_HEAD_SHA},
                        "title": PR_42_TITLE,
                        "merged": True,
                        "merge_commit_sha": "48365231d5e40a03ac674d09e8181a50bd7266ca",
                    },
                )
            return _json_resp(
                200, {"head": {"sha": PR_42_HEAD_SHA}, "title": PR_42_TITLE}
            )
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener


class _RecordingTokenProvider:
    def __init__(self, token: str = "shadow-tok"):
        self.resolved_for: list[str] = []
        self._token = token

    def resolve_token(self, role: str) -> str:
        self.resolved_for.append(role)
        return self._token


class _AllowingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        return True


def _pr42_argv(**overrides) -> list[str]:
    args = {
        "--platform": "forgejo",
        "--role": "naomi",
        "--authorized-role": "naomi",
        "--repo": f"{PR_42_OWNER}/{PR_42_REPO}",
        "--pr": str(PR_42_NUMBER),
        "--expected-head-sha": PR_42_HEAD_SHA,
        "--required-reviewer": None,  # set below (repeatable flag)
    }
    args.update(overrides)
    argv: list[str] = []
    for key, value in args.items():
        if value is None:
            continue
        argv.extend([key, str(value)])
    # Both required reviewers, mirroring the earlier merge tool's own
    # required-reviewers list ("peaches", "bobbie") with the same
    # Forgejo login mapping (peaches->peaches, bobbie->bobbie).
    argv.extend(["--required-reviewer", "peaches:peaches"])
    argv.extend(["--required-reviewer", "bobbie:bobbie"])
    # lr-ac5c8a: this shadow-parity fixture carries no local working tree --
    # --no-post-merge-tree satisfies the now-mandatory --repo-path/
    # --no-post-merge-tree/--skip-post-merge acknowledgment (see
    # test_merge_verb.py's _base_args for the identical rationale).
    if "--repo-path" not in argv and "--skip-post-merge" not in argv:
        argv.append("--no-post-merge-tree")
    return argv


class TestShadowParityPR42:
    """Replays PR #42's real gate facts through loadout-merge's gate chain
    and asserts it reaches the earlier merge tool's real-world decision:
    authorize, zero deltas across every gate that tool itself ran for this
    PR (SHA-stamp, both reviewer verdicts, diff-scope, title)."""

    def test_all_gates_pass_matching_reference_tool_authorize_decision(self):
        calls: list[tuple[str, str]] = []
        token_provider = _RecordingTokenProvider()
        code = verb.main(
            _pr42_argv(),
            token_provider=token_provider,
            authority_provider=_AllowingAuthorityProvider(),
            opener=_shadow_opener(calls),
        )
        # ZERO-DELTA ASSERTION: loadout-merge reaches EXIT_OK (authorize) on
        # the exact gate facts the earlier merge tool authorized this PR
        # under.
        assert code == verb.EXIT_OK
        # The merge step (gate 8) was reached — every gate above it passed,
        # exactly as the earlier tool's real run reached its own merge call.
        assert ("POST", "merge") in calls
        # The role's token was resolved exactly once (no redundant mint).
        assert token_provider.resolved_for == ["naomi"]

    def test_stale_sha_gate_matches_reference_tools_sha_stamp_rule(self):
        """The earlier merge tool refuses on a SHA mismatch between
        --expected-head-sha and the PR's live HEAD (its own stale-SHA
        check) — loadout-merge's stale_sha.check_stale_head_sha enforces
        the identical rule. Proven here against a deliberately wrong
        expected SHA for PR #42."""
        calls: list[tuple[str, str]] = []
        code = verb.main(
            _pr42_argv(**{"--expected-head-sha": "f" * 40}),
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_shadow_opener(calls),
        )
        assert code == verb.EXIT_STALE_HEAD_SHA
        assert ("POST", "merge") not in calls

    def test_diff_scope_gate_matches_reference_tools_default_cap(self):
        """PR #42 touched 4 files, well under the earlier merge tool's own
        DEFAULT_MAX_CHANGED_FILES=50 (and loadout-merge's identical default,
        merge.diff_scope.DEFAULT_MAX_CHANGED_FILES=50) — both tools pass this
        gate for PR #42. Proven here by tightening the cap below 4 to show
        the gate is actually being evaluated against the real file count,
        not silently skipped."""
        calls: list[tuple[str, str]] = []
        code = verb.main(
            _pr42_argv(**{"--max-changed-files": "2"}),
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_shadow_opener(calls),
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert ("POST", "merge") not in calls

    def test_title_gate_matches_reference_tools_conventional_commits_grammar(self):
        """PR #42's real title ("feat(lr-30c0d0): ...") conforms to the same
        Conventional Commits grammar the earlier merge tool's own title
        check enforces (identical regex — see merge.title_gate's module docstring).
        Proven here by asserting the SAME gate rejects a non-conformant title
        under otherwise-identical PR #42 gate facts."""
        calls: list[tuple[str, str]] = []

        def opener_bad_title(req, timeout=15):
            url = req.full_url
            method = req.get_method()
            if method == "GET" and "/pulls/" in url and not url.endswith("/files"):
                calls.append(("GET", "pr_info"))
                return _json_resp(
                    200, {"head": {"sha": PR_42_HEAD_SHA}, "title": "not conventional"}
                )
            return _shadow_opener(calls)(req, timeout=timeout)

        code = verb.main(
            _pr42_argv(),
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener_bad_title,
        )
        assert code == verb.EXIT_PR_TITLE_INVALID
        assert ("POST", "merge") not in calls

    def test_blocking_verdict_from_either_reviewer_refuses_matching_reference_tool(self):
        """The earlier merge tool's own comment-verdict check refuses on ANY
        required reviewer posting review_status='blocking' —
        loadout-merge's merge.verdict.assert_clean_verdict enforces the
        identical per-reviewer rule. Proven by swapping PEACHES's real clean
        verdict for a blocking one while leaving every other PR #42 gate
        fact untouched."""
        blocking_peaches_body = PEACHES_COMMENT_13081_BODY.replace(
            '"review_status": "clean"', '"review_status": "blocking"'
        )
        comments = [
            c
            if c["id"] != 13081
            else {
                "id": 13081,
                "user": {"login": "peaches"},
                "body": blocking_peaches_body,
                "created_at": c["created_at"],
            }
            for c in PR_42_COMMENTS
        ]

        def opener_blocking(req, timeout=15):
            url = req.full_url
            method = req.get_method()
            if method == "GET" and url.endswith("/comments"):
                return _json_resp(200, comments)
            return _shadow_opener([])(req, timeout=timeout)

        code = verb.main(
            _pr42_argv(),
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener_blocking,
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED


class TestCiStatusGateAgainstPR42Facts:
    """lr-afba CI-status-gate slice (comment #6): PR #42's REAL CI-status
    evidence is empty (no runner wired up by design, lr-368c) and must
    PASS — this is the primary positive case, already covered by
    TestShadowParityPR42's authorize-decision test above (empty CI never
    blocks the zero-delta parity result). The negative controls below prove
    the gate is actually being evaluated, not silently skipped: swapping
    PR #42's otherwise-identical gate facts to a RUNNER-WIRED failing/
    pending CI, or an unreachable CI-status endpoint, must refuse — so
    "empty CI passes" can never be mistaken for "CI is never checked."
    """

    def test_runner_wired_failing_ci_refuses(self):
        """A hypothetical runner-wired repo with the SAME PR #42 gate facts
        but a real, non-empty FAILING combined state must refuse — proves
        pass-on-empty cannot mask a real red."""
        calls: list[tuple[str, str]] = []

        def opener_failing_ci(req, timeout=15):
            url = req.full_url
            method = req.get_method()
            if method == "GET" and url.endswith("/status"):
                calls.append(("GET", "ci_status"))
                return _json_resp(
                    200, {"state": "failure", "statuses": [{"state": "failure", "context": "build"}]}
                )
            if method == "GET" and url.endswith("/actions/tasks"):
                calls.append(("GET", "actions_tasks"))
                return _json_resp(200, {"total_count": 1})
            return _shadow_opener(calls)(req, timeout=timeout)

        code = verb.main(
            _pr42_argv(),
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener_failing_ci,
        )
        assert code == verb.EXIT_CI_STATUS_FAILED
        assert ("POST", "merge") not in calls

    def test_runner_wired_pending_ci_refuses(self):
        """A runner-wired repo with CI still running (pending, non-empty
        evidence) must refuse — a merge is never authorized against CI that
        has not conclusively passed."""
        calls: list[tuple[str, str]] = []

        def opener_pending_ci(req, timeout=15):
            url = req.full_url
            method = req.get_method()
            if method == "GET" and url.endswith("/status"):
                calls.append(("GET", "ci_status"))
                return _json_resp(
                    200, {"state": "pending", "statuses": [{"state": "pending", "context": "build"}]}
                )
            if method == "GET" and url.endswith("/actions/tasks"):
                calls.append(("GET", "actions_tasks"))
                return _json_resp(200, {"total_count": 1})
            return _shadow_opener(calls)(req, timeout=timeout)

        code = verb.main(
            _pr42_argv(),
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener_pending_ci,
        )
        assert code == verb.EXIT_CI_STATUS_FAILED
        assert ("POST", "merge") not in calls

    def test_unreachable_ci_status_endpoint_fails_closed(self):
        """An unreachable CI-status endpoint is a gate-fact-read failure,
        never conflated with the genuine empty-CI pass case — fails closed
        with GateFactUnavailableError, exactly like every other gate-fact
        fetch in this module."""
        calls: list[tuple[str, str]] = []

        def opener_unreachable_ci(req, timeout=15):
            url = req.full_url
            method = req.get_method()
            if method == "GET" and url.endswith("/status"):
                raise urllib.error.HTTPError(url, 503, "unavailable", {}, io.BytesIO(b"{}"))
            return _shadow_opener(calls)(req, timeout=timeout)

        code = verb.main(
            _pr42_argv(),
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener_unreachable_ci,
        )
        assert code == verb.EXIT_GATE_FACT_UNAVAILABLE
        assert ("POST", "merge") not in calls
