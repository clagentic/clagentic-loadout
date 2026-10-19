"""Per-platform fake hosts for the merge verb: one URL-routing opener factory per
platform, each of which refuses a URL shaped for the other platform. A test that
passes against one of them proves that platform's backend was the one reached."""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timezone

from tests._support.ci_gate import check_run
from tests._support.fakes import FakeResponse, json_resp, paged

DEFAULT_HEAD_SHA = "a" * 40


def forgejo_opener(
    *, pr_info=None, files=None, comments=None, merge_status=200, ci_state="",
    ci_run_total_count=0, branch_commits=None,
):
    """Understands ONLY Forgejo-shaped URLs -- an unexpected (e.g. GitHub-
    shaped) call raises AssertionError, proving the forgejo backend was the
    one actually dispatched to.

    CI-status defaults to the no-runner-by-design empty shape. A non-empty
    ci_state gets a matching single HEAD-scoped commit-status entry: emptiness
    is decided by the status count alone, so a caller asserting a non-empty
    combined state must also carry a real status entry.

    /actions/tasks is intentionally NOT wired to any handler here (the CI
    status fetch no longer queries it) -- ci_run_total_count is accepted for
    call-site parity with github_opener but is inert on the Forgejo side; if
    the fetch regresses and starts calling that endpoint again, this opener's
    fall-through AssertionError will catch it."""
    pr_info = pr_info if pr_info is not None else {"head": {"sha": DEFAULT_HEAD_SHA}, "title": "feat: a change"}
    files = files if files is not None else ["a.py"]
    comments = comments if comments is not None else []
    # Comments are ordered by creation time, so a fixture entry that omits it
    # gets one that is monotonic with its id.
    comments = [
        c if "created_at" in c else {**c, "created_at": f"2026-01-01T00:00:{c.get('id', 0):02d}Z"}
        for c in comments
    ]
    ci_statuses = [{"state": ci_state, "context": "build"}] if ci_state else []
    # An empty branch-commit list is a no-op for every gate that reads it.
    branch_commits = branch_commits if branch_commits is not None else []

    # The merge-completion attestation is posted after a successful merge. It is
    # kept apart from `comments` so a POST here never contaminates the
    # reviewer-verdict fixture list.
    posted_comments: list[dict] = []
    # The merge verb re-reads the pull request after merging; this flag makes
    # that readback report the merge as landed.
    _merge_landed = [False]

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        # /api/v1/user (the attestation transport's bot-login resolution) is the
        # one Forgejo-shaped endpoint with no /repos/ segment.
        assert "/api/v1/repos/" in url or url.endswith("/api/v1/user"), (
            f"expected Forgejo-shaped URL, got: {url}"
        )
        if method == "POST" and url.endswith("/merge"):
            if merge_status in (200, 204):
                _merge_landed[0] = True
                return FakeResponse(merge_status, b"{}")
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
            return json_resp(201, posted_comments[-1])
        if method == "GET" and url.endswith("/user"):
            return json_resp(200, {"login": "loadout-merger"})
        if method == "GET" and url.endswith("/files"):
            return json_resp(200, [{"filename": f} for f in files])
        if method == "GET" and url.split("?")[0].endswith("/comments"):
            return json_resp(200, paged(url, comments + posted_comments))
        if method == "GET" and url.endswith("/status"):
            return json_resp(200, {"state": ci_state, "statuses": ci_statuses})
        if method == "GET" and "/compare/" in url:
            return json_resp(200, {"commits": branch_commits, "ahead_by": len(branch_commits)})
        if method == "GET" and "/pulls/" in url:
            if _merge_landed[0]:
                return json_resp(
                    200, {**pr_info, "merged": True, "merge_commit_sha": "e" * 40}
                )
            return json_resp(200, pr_info)
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener


def github_opener(
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

    CI-status defaults to the no-runner-by-design empty shape, mirroring
    forgejo_opener. The attestation transport posts to issues/{pr}/comments (an
    issue comment, not a pull-request review). *comments* (the gate's pre-seeded
    reviewer-verdict fixture) and posted_comments (what the attestation POST
    appends) are served together, mirroring forgejo_opener.
    """
    pr_info = pr_info if pr_info is not None else {"head": {"sha": DEFAULT_HEAD_SHA}, "title": "feat: a change"}
    files = files if files is not None else ["a.py"]
    comments = comments if comments is not None else []
    comments = [
        c if "created_at" in c else {**c, "created_at": f"2026-01-01T00:00:{c.get('id', 0):02d}Z"}
        for c in comments
    ]
    ci_statuses = [{"state": ci_state, "context": "ci"}] if ci_state else []
    branch_commits = branch_commits if branch_commits is not None else []

    # After a successful merge the attestation path resolves its own login (GET
    # /user: no app slug is configured for this fixture's caller), reads the
    # comments for a dedupe check, POSTs, then reads them back.
    posted_comments: list[dict] = []
    _merge_landed = [False]

    def opener(req, timeout=30):
        url = req.full_url
        method = req.get_method()
        assert "api.github.com" in url, f"expected GitHub-shaped URL, got: {url}"
        if method == "PUT" and url.endswith("/merge"):
            if merge_status == 200:
                if merged:
                    _merge_landed[0] = True
                return json_resp(200, {"merged": merged, "message": "" if merged else "not mergeable"})
            raise urllib.error.HTTPError(
                url, merge_status, "err", {}, io.BytesIO(json.dumps({"message": "refused"}).encode("utf-8"))
            )
        if method == "GET" and url.endswith("/user"):
            return json_resp(200, {"login": "loadout-merger"})
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
            return json_resp(200, posted_comments[-1])
        if method == "GET" and url.endswith("/files"):
            return json_resp(200, [{"filename": f} for f in files])
        if method == "GET" and url.split("?")[0].endswith("/comments"):
            return json_resp(200, paged(url, comments + posted_comments))
        if method == "GET" and url.endswith("/status"):
            return json_resp(200, {"state": ci_state, "statuses": ci_statuses})
        if method == "GET" and url.endswith("/check-runs"):
            runs = [check_run(f"run-{i}", run_id=i + 1, suite_id=1) for i in range(ci_run_total_count)]
            return json_resp(200, {"total_count": ci_run_total_count, "check_runs": runs})
        if method == "GET" and "/compare/" in url:
            return json_resp(200, {"commits": branch_commits, "ahead_by": len(branch_commits)})
        if method == "GET" and "/pulls/" in url:
            if _merge_landed[0]:
                return json_resp(
                    200, {**pr_info, "merged": True, "merge_commit_sha": "e" * 40}
                )
            return json_resp(200, pr_info)
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener
