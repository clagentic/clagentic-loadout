"""Payload builders and hosts for the CI-gate tests.

The check-run, commit-status and combined-status payloads follow the shapes the
GitHub REST API (`GET /commits/{sha}/check-runs`, `GET /commits/{sha}/status`)
and the Forgejo API (`GET /commits/{sha}/status`) return, with every field the
gate reads (and the neighbouring ones it must ignore) present. The host fakes
route by URL shape and make no network call."""

from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone

from tests._support.fakes import FakeResponse, json_resp, paged

__all__ = [
    "HEAD_SHA",
    "MERGED_SHA",
    "check_run",
    "combined_status",
    "forgejo_ci_opener",
    "github_ci_opener",
    "status_entry",
]

HEAD_SHA = "5e1b49a4" + "0" * 32
MERGED_SHA = "7be9a8e7" + "0" * 32


def check_run(
    name: str,
    *,
    run_id: int,
    suite_id: int,
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict:
    """One element of the `check_runs` array a commit's check-runs endpoint
    returns. `conclusion` is null until the run completes."""
    return {
        "id": run_id,
        "node_id": f"CR_{run_id}",
        "name": name,
        "head_sha": HEAD_SHA,
        "external_id": str(run_id),
        "url": f"https://api.github.com/repos/some-owner/some-repo/check-runs/{run_id}",
        "html_url": f"https://github.com/some-owner/some-repo/actions/runs/1/job/{run_id}",
        "details_url": f"https://github.com/some-owner/some-repo/actions/runs/1/job/{run_id}",
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "started_at": "2026-10-10T10:00:00Z",
        "completed_at": "2026-10-10T10:05:00Z" if status == "completed" else None,
        "output": {"title": None, "summary": None, "text": None, "annotations_count": 0},
        "check_suite": {"id": suite_id},
        "app": {"id": 15368, "slug": "github-actions", "name": "GitHub Actions"},
        "pull_requests": [],
    }


def status_entry(context: str, state: str) -> dict:
    return {
        "id": 1,
        "state": state,
        "context": context,
        "description": None,
        "target_url": None,
        "created_at": "2026-10-10T10:00:00Z",
    }


def combined_status(state: str, entries: list[dict]) -> dict:
    return {"state": state, "statuses": entries, "sha": HEAD_SHA, "total_count": len(entries)}


def _merge_json(url: str, pr_info: dict, landed: list[bool]) -> FakeResponse:
    if landed[0]:
        return json_resp(200, {**pr_info, "merged": True, "merge_commit_sha": MERGED_SHA})
    return json_resp(200, pr_info)


def github_ci_opener(
    *,
    pr_info: dict | None = None,
    check_runs: list[dict] | None = None,
    state: str = "",
    statuses: list[dict] | None = None,
    check_runs_page_size: int = 30,
    merge_calls: list | None = None,
):
    """A GitHub host. check-runs are served in pages of *check_runs_page_size*
    (the API's own default is 30) with the real `total_count`; every merge PUT
    is recorded in *merge_calls*."""
    pr_info = pr_info if pr_info is not None else {
        "head": {"sha": HEAD_SHA},
        "title": "fix: a change",
        "base": {"ref": "main", "sha": "c" * 40},
    }
    runs = check_runs if check_runs is not None else []
    entries = statuses if statuses is not None else []
    posted: list[dict] = []
    landed = [False]

    def opener(req, timeout=30):
        url = req.full_url
        method = req.get_method()
        assert "api.github.com" in url, f"expected GitHub-shaped URL, got: {url}"
        path = url.split("?")[0]
        if method == "PUT" and path.endswith("/merge"):
            landed[0] = True
            if merge_calls is not None:
                merge_calls.append(url)
            return json_resp(200, {"merged": True, "sha": MERGED_SHA, "message": "merged"})
        if method == "GET" and path.endswith("/user"):
            return json_resp(200, {"login": "loadout-merger"})
        if method == "POST" and path.endswith("/comments"):
            body = json.loads(req.data.decode("utf-8"))["body"]
            posted.append(
                {
                    "id": 8001 + len(posted),
                    "user": {"login": "loadout-merger"},
                    "body": body,
                    "html_url": "https://github.com/some-owner/some-repo/pull/1#c",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            return json_resp(200, posted[-1])
        if method == "GET" and path.endswith("/files"):
            return json_resp(200, [{"filename": "a.py"}])
        if method == "GET" and path.endswith("/comments"):
            return json_resp(200, paged(url, posted))
        if method == "GET" and path.endswith("/status"):
            return json_resp(200, combined_status(state, entries))
        if method == "GET" and path.endswith("/check-runs"):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            page = int(query.get("page", ["1"])[0])
            size = int(query.get("per_page", [str(check_runs_page_size)])[0])
            window = runs[(page - 1) * size : page * size]
            return json_resp(200, {"total_count": len(runs), "check_runs": window})
        if method == "GET" and "/compare/" in path:
            return json_resp(200, {"commits": [], "ahead_by": 0})
        if method == "GET" and "/pulls/" in path:
            return _merge_json(url, pr_info, landed)
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener


def forgejo_ci_opener(
    *,
    pr_info: dict | None = None,
    state: str = "",
    statuses: list[dict] | None = None,
    merge_calls: list | None = None,
):
    """A Forgejo host whose only CI evidence is the commit-status endpoint,
    which is where Forgejo Actions publishes job results. Any call to the
    check-runs or repo-global actions endpoints fails the test."""
    pr_info = pr_info if pr_info is not None else {
        "head": {"sha": HEAD_SHA},
        "title": "fix: a change",
        "base": {"ref": "main", "sha": "c" * 40},
    }
    entries = statuses if statuses is not None else []
    posted: list[dict] = []
    landed = [False]

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        path = url.split("?")[0]
        assert "/api/v1/" in url, f"expected Forgejo-shaped URL, got: {url}"
        assert not path.endswith("/check-runs") and not path.endswith("/actions/tasks"), url
        if method == "POST" and path.endswith("/merge"):
            landed[0] = True
            if merge_calls is not None:
                merge_calls.append(url)
            return FakeResponse(200, b"{}")
        if method == "GET" and path.endswith("/user"):
            return json_resp(200, {"login": "loadout-merger"})
        if method == "POST" and "/comments" in path:
            body = json.loads(req.data.decode("utf-8"))["body"]
            posted.append(
                {
                    "id": 9001 + len(posted),
                    "user": {"login": "loadout-merger"},
                    "body": body,
                    "html_url": "https://forgejo.example/comment/9001",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            return json_resp(201, posted[-1])
        if method == "GET" and path.endswith("/files"):
            return json_resp(200, [{"filename": "a.py"}])
        if method == "GET" and path.endswith("/comments"):
            return json_resp(200, paged(url, posted))
        if method == "GET" and path.endswith("/status"):
            return json_resp(200, combined_status(state, entries))
        if method == "GET" and "/compare/" in path:
            return json_resp(200, {"commits": [], "ahead_by": 0})
        if method == "GET" and "/pulls/" in path:
            return _merge_json(url, pr_info, landed)
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener
