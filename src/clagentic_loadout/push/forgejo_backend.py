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
        # status is embedded in exc.code (the GitHostApiError's own exit
        # code, EXIT_CURL_FAILED), never the raw HTTP status, so there is no
        # HTTP status_code to attach here. Distinct from the GitHub path
        # above, which returns (status, body) even on a non-2xx and so CAN
        # attach one.
        raise PrOpenError(f"Forgejo PR creation failed for {owner}/{repo}: {exc}") from exc

    resp = git_host_api.parse_json_body(raw)
    pr_number = resp.get("number")
    if not pr_number:
        raise PrOpenError(
            f"Forgejo PR creation response missing 'number' field for {owner}/{repo}",
            status_code=status,
        )
    return int(pr_number)


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


__all__ = ["create_pr", "get_pr_body", "update_pr"]
