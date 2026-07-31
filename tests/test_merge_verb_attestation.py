"""test_merge_verb_attestation.py — CLI-level coverage for the merge-
completion attestation comment (lr-20e866).

Covers, on BOTH platforms (Forgejo + GitHub) per the task's hard
requirement:
  - a successful merge posts exactly one attestation comment/review via the
    backend's existing POST-and-verify comment transport, AFTER merge_pr
    succeeds and BEFORE post-merge steps.
  - the posted body carries the expected content (tool identity, gated/
    merged SHA, required-reviewer logins, CI disposition), rendered as a
    markdown field/value table (lr-0b77dd) -- table-structure assertions
    themselves live in test_merge_attestation.py, this file only checks
    that the expected field VALUES made it into the posted body.
  - FAIL-OPEN: a failing attestation POST (network error / non-2xx) never
    changes the verb's exit code or fails the merge that already succeeded.

NO real network call, NO real git, NO Date-dependence anywhere in this file
-- everything is driven through an injected opener + injected token/
authority providers, matching test_merge_verb.py's own discipline.
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


class _AllowingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        return True


class _FakeResponse:
    def __init__(self, status: int, body: bytes, content_type: str = "application/json"):
        self.status = status
        self._body = body
        # content_type-mode parsing (review.github_backend._github_request)
        # JSON-decodes a success body only when the response carries a
        # Content-Type header containing "json" -- see
        # test_review_github_backend.py's own _FakeResponse for the
        # identical shape this mirrors.
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
    # lr-ac5c8a: this file exercises the attestation transport, never
    # post_merge_steps, and none of its fixtures carry a local working tree
    # -- --no-post-merge-tree satisfies the now-mandatory --repo-path/
    # --no-post-merge-tree/--skip-post-merge acknowledgment (see
    # test_merge_verb.py's _base_args for the identical rationale).
    if "--repo-path" not in argv and "--skip-post-merge" not in argv:
        argv.append("--no-post-merge-tree")
    return argv


def _forgejo_opener(
    *,
    pr_info=None,
    files=None,
    comments=None,
    merge_status=200,
    ci_state="",
    attestation_post_error: int | None = None,
    attestation_network_error: bool = False,
):
    """Forgejo-shaped opener that ALSO answers the attestation transport
    (POST issues/<pr>/comments, GET /api/v1/user, GET issues/<pr>/comments
    readback -- review.forgejo_backend.post_and_verify_comment, reused
    unchanged from the review-post verb).

    `attestation_post_error` makes the attestation POST itself return a
    non-2xx status (proving fail-open on a ReviewPostError-shaped failure).
    `attestation_network_error` makes the attestation POST raise a network
    error directly (proving fail-open on an unwrapped transport failure).
    Both are mutually exclusive with a normal successful post; every other
    gate-fact call in this fixture is unaffected either way.
    """
    pr_info = pr_info if pr_info is not None else {"head": {"sha": _FULL_SHA}, "title": "feat: a change"}
    files = files if files is not None else ["a.py"]
    comments = comments if comments is not None else []
    # lr-c14a2d: backfill created_at (monotonic with id) on any comments
    # fixture entry that omits it, so pre-existing fixtures keep exercising
    # the same gate outcome under read_reviewer_verdict's now-mandatory
    # created_at requirement -- see test_merge_verb.py's _make_opener for
    # the identical rationale.
    comments = [
        c if "created_at" in c else {**c, "created_at": f"2026-01-01T00:00:{c.get('id', 0):02d}Z"}
        for c in comments
    ]
    ci_statuses = [{"state": ci_state, "context": "build"}] if ci_state else []
    posted_comments: list[dict] = []
    # lr-361de3: see test_merge_verb.py's _make_opener for the identical
    # post-merge-readback overlay rationale.
    _merge_landed = [False]

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "POST" and url.endswith("/merge"):
            if merge_status in (200, 204):
                _merge_landed[0] = True
                return _FakeResponse(merge_status, b"{}")
            raise urllib.error.HTTPError(url, merge_status, "err", {}, io.BytesIO(b"{}"))
        if method == "POST" and "/comments" in url:
            if attestation_network_error:
                raise urllib.error.URLError("connection refused (test fixture)")
            if attestation_post_error is not None:
                raise urllib.error.HTTPError(
                    url, attestation_post_error, "err", {}, io.BytesIO(b"{}")
                )
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
            # lr-835c57: empty branch-commit list -- this file exercises the
            # attestation transport, not the commit-subject gate, so a no-op
            # response keeps every pre-existing fixture reaching its own
            # asserted outcome.
            return _json_resp(200, {"commits": [], "ahead_by": 0})
        if method == "GET" and "/pulls/" in url:
            if _merge_landed[0]:
                return _json_resp(
                    200, {**pr_info, "merged": True, "merge_commit_sha": "e" * 40}
                )
            return _json_resp(200, pr_info)
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener, posted_comments


def _github_opener(
    *,
    pr_info=None,
    files=None,
    comments=None,
    merge_status=200,
    merged=True,
    ci_state="",
    ci_run_total_count=0,
    attestation_post_error: int | None = None,
    attestation_network_error: bool = False,
):
    """GitHub-shaped opener that ALSO answers the attestation transport
    (resolve_own_login's GET /user, the pre-post dedupe readback, the POST
    itself, and the mandatory post-POST readback -- review.github_backend.
    post_and_verify_review, reused unchanged from the review-post verb).

    lr-71f467: the attestation transport now posts to issues/{pr}/comments
    (an ISSUE COMMENT), not /pulls/{pr}/reviews (a native PR review) -- see
    review.github_backend's "VERDICT-TRANSPORT PARITY" docstring section.
    The gate's own comment-fetch (merge.github_backend.fetch_comments) reads
    the SAME endpoint, so *comments* (the gate's pre-seeded reviewer-verdict
    fixture) and *posted_comments* (what the attestation POST appends) now
    share one list, exactly like _forgejo_opener already does.
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
    posted_comments: list[dict] = []
    # lr-361de3: see _forgejo_opener's identical rationale above.
    _merge_landed = [False]

    def opener(req, timeout=30):
        url = req.full_url
        method = req.get_method()
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
            if attestation_network_error:
                raise urllib.error.URLError("connection refused (test fixture)")
            if attestation_post_error is not None:
                raise urllib.error.HTTPError(
                    url, attestation_post_error, "err", {},
                    io.BytesIO(json.dumps({"message": "refused"}).encode("utf-8")),
                )
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
            # lr-835c57: see _forgejo_opener's identical rationale above.
            return _json_resp(200, {"commits": [], "ahead_by": 0})
        if method == "GET" and "/pulls/" in url:
            if _merge_landed[0]:
                return _json_resp(
                    200, {**pr_info, "merged": True, "merge_commit_sha": "e" * 40}
                )
            return _json_resp(200, pr_info)
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener, posted_comments


class TestAttestationPostedOnBothPlatformsHappyPath:
    """Hard requirement: BOTH platforms, not a Forgejo-only slice."""

    def test_forgejo_merge_posts_attestation_comment(self):
        opener, posted_comments = _forgejo_opener(
            pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: ship it"},
        )
        argv = _base_args("forgejo")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK
        assert len(posted_comments) == 1
        assert "Merged via clagentic-loadout" in posted_comments[0]["body"]
        assert _FULL_SHA in posted_comments[0]["body"]

    def test_github_merge_posts_attestation_comment(self):
        opener, posted_comments = _github_opener(
            pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: ship it"},
        )
        argv = _base_args("github")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK
        assert len(posted_comments) == 1
        assert "Merged via clagentic-loadout" in posted_comments[0]["body"]
        assert _FULL_SHA in posted_comments[0]["body"]


class TestAttestationContentReflectsGateFacts:
    def test_forgejo_attestation_carries_required_reviewer_logins_and_ci_disposition(self):
        block = build_verdict_block("some-reviewer", "clean", _FULL_SHA, 1)
        opener, posted_comments = _forgejo_opener(
            pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: ship it"},
            comments=[{"id": 1, "user": {"login": "reviewer-login"}, "body": block}],
            ci_state="success",
        )
        argv = _base_args(
            "forgejo",
            **{
                "--expected-head-sha": _FULL_SHA,
                "--required-reviewer": "some-reviewer:reviewer-login",
            },
        )
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK
        body = posted_comments[0]["body"]
        assert "reviewer-login" in body
        assert "success" in body

    def test_no_required_reviewers_omits_reviews_line(self):
        # lr-b6da32: no reviewer-verdict gate configured for this invocation
        # -- the Reviews line is omitted entirely, never a "(none required)"
        # placeholder that misreads as "unreviewed".
        opener, posted_comments = _forgejo_opener(
            pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: ship it"},
        )
        argv = _base_args("forgejo")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK
        assert "Reviews" not in posted_comments[0]["body"]
        assert "(none required)" not in posted_comments[0]["body"]


class TestAttestationWorkItemLines:
    """lr-eb22f3: --task-id flows through to the posted attestation body,
    and the issue number is parsed back out of the PR's OWN body (the
    'Closes #NN' trailer), never from a lore field."""

    def test_task_id_flows_through_to_posted_attestation(self):
        opener, posted_comments = _forgejo_opener(
            pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: ship it", "body": ""},
        )
        argv = _base_args("forgejo", **{"--task-id": "lr-eb22f3"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK
        assert "lr-eb22f3" in posted_comments[0]["body"]

    def test_no_task_id_omits_task_id_line(self):
        opener, posted_comments = _forgejo_opener(
            pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: ship it", "body": ""},
        )
        argv = _base_args("forgejo")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK
        assert "task_id" not in posted_comments[0]["body"]

    def test_issue_number_parsed_from_pr_body_closes_trailer(self):
        opener, posted_comments = _forgejo_opener(
            pr_info={
                "head": {"sha": _FULL_SHA},
                "title": "feat: ship it",
                "body": "some description\n\nCloses #99\n",
            },
        )
        argv = _base_args("forgejo")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK
        assert "#99" in posted_comments[0]["body"]

    def test_no_closes_trailer_omits_issue_line(self):
        opener, posted_comments = _forgejo_opener(
            pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: ship it", "body": "no trailer here"},
        )
        argv = _base_args("forgejo")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK
        assert "Issue" not in posted_comments[0]["body"]

    def test_both_ids_present_on_github_platform_too(self):
        opener, posted_comments = _github_opener(
            pr_info={
                "head": {"sha": _FULL_SHA},
                "title": "feat: ship it",
                "body": "desc\n\nCloses #7\n",
            },
        )
        argv = _base_args("github", **{"--task-id": "lr-eb22f3"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK
        body = posted_comments[0]["body"]
        assert "lr-eb22f3" in body
        assert "#7" in body


class TestAttestationIsLoreFree:
    def test_posted_body_carries_no_lore_or_crew_vocabulary(self):
        opener, posted_comments = _forgejo_opener(
            pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: ship it"},
        )
        argv = _base_args("forgejo")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK
        lowered = posted_comments[0]["body"].lower()
        for forbidden in ("lore", "crew", "archivist", "sentinel"):
            assert forbidden not in lowered


class TestAttestationFailsOpen:
    """FAIL-OPEN (lr-20e866): the merge already succeeded by the time the
    attestation POST is attempted -- neither a non-2xx response nor a raw
    network error from the attestation transport may change the verb's exit
    code. Proven on BOTH platforms."""

    def test_forgejo_attestation_post_non_2xx_does_not_fail_the_verb(self):
        opener, _ = _forgejo_opener(
            pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: ship it"},
            attestation_post_error=500,
        )
        argv = _base_args("forgejo")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK

    def test_forgejo_attestation_network_error_does_not_fail_the_verb(self):
        opener, _ = _forgejo_opener(
            pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: ship it"},
            attestation_network_error=True,
        )
        argv = _base_args("forgejo")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK

    def test_github_attestation_post_non_2xx_does_not_fail_the_verb(self):
        opener, _ = _github_opener(
            pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: ship it"},
            attestation_post_error=500,
        )
        argv = _base_args("github")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK

    def test_github_attestation_network_error_does_not_fail_the_verb(self):
        opener, _ = _github_opener(
            pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: ship it"},
            attestation_network_error=True,
        )
        argv = _base_args("github")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_OK

    def test_attestation_never_attempted_when_merge_itself_fails(self):
        # A merge-execution failure must never even reach the attestation
        # step -- the merge did not succeed, so there is nothing to attest.
        opener, posted_comments = _forgejo_opener(merge_status=500)
        argv = _base_args("forgejo")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=opener,
        )
        assert code == verb.EXIT_MERGE_FAILED
        assert posted_comments == []
