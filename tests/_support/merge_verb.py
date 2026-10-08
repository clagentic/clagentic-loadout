"""Shared fixtures for the merge verb's tests: token and authority providers
that record or refuse, a fake HTTP response, a URL-routing fake host opener,
and the base argv. Public names, so a test module reuses them by importing
from here and never reaches into a peer test module's underscored helpers."""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

from clagentic_loadout.transport.credential_provider import CredentialProviderError
from tests._support.fakes import (
    FakeResponse,
    RecordingTokenProvider,
    RefusingTokenProvider,
    json_resp,
)

__all__ = [
    "FULL_SHA",
    "MERGED_COMMIT_SHA",
    "OTHER_FULL_SHA",
    "AllowingAuthorityProvider",
    "DenyingAuthorityProvider",
    "MissingCredsTokenProvider",
    "RecordingTokenProvider",
    "RefusingAuthorityProvider",
    "RefusingTokenProvider",
    "base_args",
    "make_opener",
]

FULL_SHA = "a" * 40
OTHER_FULL_SHA = "b" * 40
MERGED_COMMIT_SHA = "e" * 40
_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


class MissingCredsTokenProvider:
    def resolve_token(self, role: str) -> str:
        raise CredentialProviderError("no credentials configured for this role")


class AllowingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        return True


class DenyingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        return False


class RefusingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        raise AssertionError("authority provider must not be called")


def make_opener(
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
    network call is ever made.

    CI status defaults to the no-runner-by-design empty shape (empty combined
    state, zero statuses, zero actions total_count), so a test that does not
    reason about CI reaches the gate outcome it asserts. `branch_commits`
    defaults to an empty list, which the commit-subject gate treats as a
    no-op; a test that exercises that gate passes {"sha", "commit": {"message"}}
    dicts, the shape the compare API's 'commits' field carries.

    `post_merge_readback_confirms` (default True): the verb makes a fresh
    post-merge GET of the PR to confirm merged==true with a merge commit SHA.
    This fixture tracks whether the merge POST has fired and, once it has,
    overlays `merged`/`merge_commit_sha` on later PR GETs. Set False to
    exercise the readback-failure path.
    """
    pr_info = pr_info if pr_info is not None else {"head": {"sha": FULL_SHA}, "title": "feat: a change"}
    files = files if files is not None else ["a.py"]
    comments = comments if comments is not None else []
    branch_commits = branch_commits if branch_commits is not None else []
    # The verdict reader requires a created_at on every candidate comment.
    # Fixtures build comments by id alone (id order is chronological order),
    # so a monotonic-with-id created_at is backfilled here; a fixture that
    # needs out-of-order timestamps supplies its own, which is never
    # overwritten.
    comments = [
        c
        if "created_at" in c
        else {
            **c,
            "created_at": (_EPOCH + timedelta(seconds=c.get("id", 0))).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        for c in comments
    ]
    ci_statuses = ci_statuses if ci_statuses is not None else []

    # The merge-completion attestation is posted through the review backend
    # after a successful merge. It is recorded apart from `comments` so a POST
    # never contaminates the verdict gate's own comment list, and the readback
    # sees exactly the posted body.
    posted_comments: list[dict] = []
    merge_landed = [False]

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "POST" and url.endswith("/merge"):
            if merge_status in (200, 204):
                merge_landed[0] = True
                return FakeResponse(merge_status, b"{}")
            raise urllib.error.HTTPError(url, merge_status, "err", {}, io.BytesIO(b"{}"))
        if method == "POST" and "/comments" in url:
            # created_at is taken at POST time so it always clears the
            # readback's freshness anchor, whenever the suite runs.
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
            return json_resp(201, posted_comments[-1])
        if method == "GET" and url.endswith("/user"):
            return json_resp(200, {"login": "loadout-merger"})
        if method == "GET" and url.endswith("/files"):
            return json_resp(200, [{"filename": f} for f in files])
        if method == "GET" and url.split("?")[0].endswith("/comments"):
            return json_resp(200, comments + posted_comments)
        if method == "GET" and url.endswith("/status"):
            return json_resp(200, {"state": ci_state, "statuses": ci_statuses})
        if method == "GET" and url.endswith("/actions/tasks"):
            return json_resp(200, {"total_count": ci_run_total_count})
        if method == "GET" and "/compare/" in url:
            return json_resp(200, {"commits": branch_commits, "ahead_by": len(branch_commits)})
        if method == "GET" and "/pulls/" in url:
            if merge_landed[0] and post_merge_readback_confirms:
                return json_resp(
                    200,
                    {**pr_info, "merged": True, "merge_commit_sha": MERGED_COMMIT_SHA},
                )
            return json_resp(200, pr_info)
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener


def base_args(**overrides) -> list[str]:
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
    # The gate-chain tests never run post-merge steps and carry no local
    # working tree; --no-post-merge-tree acknowledges that, satisfying the
    # mandatory --repo-path/--no-post-merge-tree/--skip-post-merge requirement
    # without changing any gate outcome.
    if "--repo-path" not in argv and "--skip-post-merge" not in argv:
        argv.append("--no-post-merge-tree")
    return argv
