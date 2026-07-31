"""push.github_backend — GitHub PR create/update.

Wave B slice 3 (lr-09ca, tome #688). Ported from the reference push
transport's GitHub PR calls (create_pr_github / update_pr_github,
lr-ea62/lr-6357). Routes HTTP through
transport.github_client.request_json() (post-Wave-B extraction, lr-e1f9) —
the shared, redirect-hardened GitHub request primitive review/push/merge's
github_backend modules all build on; see that module's docstring for what
stayed local per-verb and why. This module keeps its own PR create/update
payload shapes and PrOpenError translation — endpoint semantics genuinely
specific to the push verb, not force-fit into the shared transport call.
"""

from __future__ import annotations

import urllib.error
from typing import Any

from clagentic_loadout.push.errors import PrOpenError
from clagentic_loadout.transport.github_client import GITHUB_API_BASE, request_json
from clagentic_loadout.transport.redirect_guard import no_redirect_opener


def _github_api_request(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any],
    *,
    timeout: int = 30,
    opener=None,
) -> tuple[int, dict[str, Any]]:
    """Send a JSON request to a GitHub API endpoint. Token appears ONLY in
    the Authorization header — never in the URL, never logged.

    Thin wrapper over transport.github_client.request_json() in "strict"
    parse mode (a non-empty body is always JSON-parsed, matching this
    module's pre-extraction behavior exactly) that translates a
    network-level failure to PrOpenError — push.verb has no distinct exit
    code for "network unreachable" vs. "PR call failed", so both collapse
    to the same PrOpenError this module has always raised for that case.
    `no_redirect_opener` is imported here (passed through as
    opener_factory) so this module's own name stays monkeypatchable at
    `clagentic_loadout.push.github_backend.no_redirect_opener` for any
    future redirect-hardening coverage mirroring merge/review's own
    (test_push_github_backend.py's current redirect test drives the
    `opener` injection point directly rather than patching the factory, but
    the seam is kept consistent with the other two backends).
    """
    try:
        return request_json(
            method, url, token, payload, opener=opener, timeout=timeout,
            opener_factory=no_redirect_opener,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PrOpenError(f"network error reaching GitHub API: {exc}") from exc


def create_pr(
    owner: str,
    repo: str,
    *,
    token: str,
    head: str,
    base: str,
    title: str,
    body: str,
    opener=None,
) -> int:
    """Create a GitHub PR and return its number.

    Raises PrOpenError on any non-2xx response or a response missing
    'number'.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls"
    payload = {"head": head, "base": base, "title": title, "body": body}
    status, resp = _github_api_request("POST", url, token, payload, opener=opener)
    if status not in (200, 201):
        server_msg = resp.get("message", "") if resp else ""
        detail = f": {server_msg}" if server_msg else ""
        raise PrOpenError(
            f"GitHub PR creation returned HTTP {status} for {owner}/{repo}{detail}",
            status_code=status,
        )
    pr_number = resp.get("number")
    if not pr_number:
        raise PrOpenError(
            f"GitHub PR creation response missing 'number' field for {owner}/{repo}",
            status_code=status,
        )
    return int(pr_number)


def get_pr_body(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    token: str,
    opener=None,
) -> str:
    """Fetch the CURRENT body of an existing GitHub PR (lr-2500b7 append
    mode): a GET immediately preceding the append-mode PATCH in
    push.verb._run_update_pr, so append concatenates onto the ACTUAL current
    body rather than a caller-assumed one.

    Returns "" if the PR has no body (GitHub returns null for an empty
    body) — never None, so a caller can always concatenate onto the result
    without a None-check.

    Raises PrOpenError on any non-2xx response or network failure, mirroring
    update_pr's own error translation.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    try:
        status, resp = request_json(
            "GET", url, token, None, opener=opener, opener_factory=no_redirect_opener,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PrOpenError(f"network error reaching GitHub API: {exc}") from exc
    if status != 200:
        server_msg = resp.get("message", "") if resp else ""
        detail = f": {server_msg}" if server_msg else ""
        raise PrOpenError(
            f"GitHub PR body read returned HTTP {status} for PR #{pr_number} in {owner}/{repo}{detail}"
        )
    return (resp or {}).get("body") or ""


def update_pr(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    token: str,
    title: str | None = None,
    body: str | None = None,
    opener=None,
) -> None:
    """Update an existing GitHub PR's title/body via PATCH, using the SAME
    token create_pr already uses — no second token source, no re-mint.

    WHOLE-FIELD REPLACE (lr-2500b7): *body*, when given, unconditionally
    REPLACES the PR's existing body — this function performs no read, no
    append, no merge of old/new content. Append semantics are a distinct
    caller-side operation (see get_pr_body + push.verb's --append-body mode),
    not a mode of this function."""
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if body is not None:
        payload["body"] = body
    status, resp = _github_api_request("PATCH", url, token, payload, opener=opener)
    if status not in (200, 201):
        server_msg = resp.get("message", "") if resp else ""
        detail = f": {server_msg}" if server_msg else ""
        raise PrOpenError(
            f"GitHub PR update returned HTTP {status} for PR #{pr_number} in {owner}/{repo}{detail}"
        )


__all__ = ["GITHUB_API_BASE", "create_pr", "get_pr_body", "update_pr"]
