"""push.forgejo_backend — Forgejo PR create/update.

Wave B slice 3 (lr-09ca, tome #688). Reuses transport.git_host_api.request()
for the actual HTTP call (redirect-guarded opener, Content-Type ownership,
write-method fail-on-non-2xx) rather than rolling a second urllib client —
the same reuse discipline review.forgejo_backend already established for
the review-post verb. Response-body parsing reuses
transport.git_host_api.parse_json_body() (post-Wave-B extraction, lr-e1f9) —
the same tolerant raw-bytes-to-dict parse merge.forgejo_backend's write-path
callers need, previously duplicated locally in both modules.
"""

from __future__ import annotations

import json
from typing import Any

from clagentic_loadout.push.errors import PrOpenError
from clagentic_loadout.transport import git_host_api


def create_pr(
    api_base: str,
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
    """Create a Forgejo PR and return its number.

    Raises PrOpenError on any non-2xx response or a response missing the
    'number' field.
    """
    payload = {"head": head, "base": base, "title": title, "body": body}
    body_bytes = json.dumps(payload).encode("utf-8")
    try:
        status, raw = git_host_api.request(
            api_base,
            "POST",
            f"/api/v1/repos/{owner}/{repo}/pulls",
            token,
            body_bytes=body_bytes,
            opener=opener,
        )
    except git_host_api.GitHostApiError as exc:
        # git_host_api.request() raises before returning a status on a
        # write-method non-2xx (see that module's own docstring) -- the
        # exc.code is the error's own exit code (EXIT_CURL_FAILED), never the
        # HTTP status; the raw status rides on exc.http_status (None for a
        # network-level failure), which the caller needs to tell a redundant
        # create (409/422) from a genuine failure.
        raise PrOpenError(
            f"Forgejo PR creation failed for {owner}/{repo}: {exc}",
            status_code=exc.http_status,
        ) from exc

    resp = git_host_api.parse_json_body(raw)
    pr_number = resp.get("number")
    if not pr_number:
        raise PrOpenError(
            f"Forgejo PR creation response missing 'number' field for {owner}/{repo}",
            status_code=status,
        )
    return int(pr_number)


#: Page size and page ceiling for the open-PR scan. Forgejo's pulls list has
#: no head/base filter, so the scan filters client-side; a repository with
#: more open PRs than the ceiling cannot be confirmed and fails closed.
_LOOKUP_PAGE_SIZE = 50
_LOOKUP_MAX_PAGES = 20


def find_open_prs(
    api_base: str,
    owner: str,
    repo: str,
    *,
    token: str,
    head: str,
    base: str,
    opener=None,
) -> list[int]:
    """Return the numbers of open Forgejo PRs whose head branch is *head* and
    whose base branch is *base* (read-only).

    Raises PrOpenError on any non-2xx response, an unexpected response shape,
    a network failure, or when the open-PR list is longer than the scan
    ceiling (the answer could not be confirmed).
    """
    matches: list[int] = []
    for page in range(1, _LOOKUP_MAX_PAGES + 1):
        try:
            status, raw = git_host_api.request(
                api_base,
                "GET",
                f"/api/v1/repos/{owner}/{repo}/pulls?state=open"
                f"&limit={_LOOKUP_PAGE_SIZE}&page={page}",
                token,
                opener=opener,
            )
        except git_host_api.GitHostApiError as exc:
            raise PrOpenError(
                f"Forgejo open-PR lookup failed for {owner}/{repo}: {exc}"
            ) from exc
        if status < 200 or status >= 300:
            raise PrOpenError(
                f"Forgejo open-PR lookup returned HTTP {status} for {owner}/{repo}",
                status_code=status,
            )
        try:
            items = json.loads(raw.decode("utf-8")) if raw else []
        except (ValueError, UnicodeDecodeError) as exc:
            raise PrOpenError(
                f"Forgejo open-PR lookup returned unparseable JSON for {owner}/{repo}"
            ) from exc
        if not isinstance(items, list):
            raise PrOpenError(
                f"Forgejo open-PR lookup returned a non-list body for {owner}/{repo}"
            )
        for item in items:
            if not isinstance(item, dict):
                continue
            item_head = item.get("head") if isinstance(item.get("head"), dict) else {}
            item_base = item.get("base") if isinstance(item.get("base"), dict) else {}
            number = item.get("number")
            # A fork PR can carry the same branch name and base; only a PR whose
            # head lives in this repository holds the pushed commits. A missing
            # or unreadable head repo is not a match.
            head_repo = item_head.get("repo") if isinstance(item_head.get("repo"), dict) else {}
            if (
                item_head.get("ref") == head
                and item_base.get("ref") == base
                and head_repo.get("full_name") == f"{owner}/{repo}"
                and number
            ):
                matches.append(int(number))
        if len(items) < _LOOKUP_PAGE_SIZE:
            return matches
    raise PrOpenError(
        f"Forgejo open-PR lookup for {owner}/{repo} exceeded {_LOOKUP_MAX_PAGES} "
        f"pages of open PRs; could not confirm"
    )


def get_pr_body(
    api_base: str,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    token: str,
    opener=None,
) -> str:
    """Fetch the CURRENT body of an existing Forgejo PR (lr-2500b7 append
    mode): a GET immediately preceding the append-mode PATCH in
    push.verb._run_update_pr, so append concatenates onto the ACTUAL current
    body rather than a caller-assumed one.

    Returns "" if the PR has no body (Forgejo returns null/absent for an
    empty body) — never None, so a caller can always concatenate onto the
    result without a None-check.

    Raises PrOpenError on any non-2xx response or network failure, mirroring
    update_pr's own error translation.
    """
    try:
        status, raw = git_host_api.request(
            api_base,
            "GET",
            f"/api/v1/repos/{owner}/{repo}/pulls/{pr_number}",
            token,
            opener=opener,
        )
    except git_host_api.GitHostApiError as exc:
        raise PrOpenError(
            f"Forgejo PR body read failed for PR #{pr_number} in {owner}/{repo}: {exc}"
        ) from exc
    # GET is a read method (git_host_api's own _WRITE_METHODS excludes it),
    # so request() returns a non-2xx status here rather than raising --
    # unlike create_pr/update_pr above, which only ever see a
    # GitHostApiError for a write-method failure. This status check is what
    # actually fails closed on a 404/etc. GET response.
    if status < 200 or status >= 300:
        raise PrOpenError(
            f"Forgejo PR body read returned HTTP {status} for PR #{pr_number} in {owner}/{repo}",
            status_code=status,
        )
    resp = git_host_api.parse_json_body(raw)
    return resp.get("body") or ""


def get_pr_head_sha(
    api_base: str,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    token: str,
    opener=None,
) -> str:
    """Fetch the CURRENT head commit SHA of an existing Forgejo PR, so a
    caller can confirm a local checkout is the PR's head before claiming
    anything about it.

    Returns "" when the response carries no head SHA. Raises PrOpenError on
    any non-2xx response or network failure.
    """
    try:
        status, raw = git_host_api.request(
            api_base,
            "GET",
            f"/api/v1/repos/{owner}/{repo}/pulls/{pr_number}",
            token,
            opener=opener,
        )
    except git_host_api.GitHostApiError as exc:
        raise PrOpenError(
            f"Forgejo PR head read failed for PR #{pr_number} in {owner}/{repo}: {exc}"
        ) from exc
    if status < 200 or status >= 300:
        raise PrOpenError(
            f"Forgejo PR head read returned HTTP {status} for PR #{pr_number} in {owner}/{repo}",
            status_code=status,
        )
    resp = git_host_api.parse_json_body(raw)
    head = resp.get("head")
    sha = head.get("sha") if isinstance(head, dict) else None
    return sha if isinstance(sha, str) else ""


def update_pr(
    api_base: str,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    token: str,
    title: str | None = None,
    body: str | None = None,
    opener=None,
) -> None:
    """Update an existing Forgejo PR's title/body via PATCH — no push, no
    re-authoring, no merge. At least one of title/body must be given
    (validated by the caller).

    WHOLE-FIELD REPLACE (lr-2500b7): *body*, when given, unconditionally
    REPLACES the PR's existing body — this function performs no read, no
    append, no merge of old/new content. Append semantics are a distinct
    caller-side operation (see get_pr_body + push.verb's --append-body mode),
    not a mode of this function, so this PATCH-only contract stays a single
    unambiguous behavior regardless of caller."""
    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if body is not None:
        payload["body"] = body
    body_bytes = json.dumps(payload).encode("utf-8")
    try:
        git_host_api.request(
            api_base,
            "PATCH",
            f"/api/v1/repos/{owner}/{repo}/pulls/{pr_number}",
            token,
            body_bytes=body_bytes,
            opener=opener,
        )
    except git_host_api.GitHostApiError as exc:
        raise PrOpenError(
            f"Forgejo PR update failed for PR #{pr_number} in {owner}/{repo}: {exc}"
        ) from exc


__all__ = ["create_pr", "find_open_prs", "get_pr_body", "get_pr_head_sha", "update_pr"]
