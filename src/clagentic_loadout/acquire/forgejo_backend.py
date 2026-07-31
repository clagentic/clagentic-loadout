"""acquire.forgejo_backend — Forgejo-side PR content-acquisition transport.

lr-c17040. Reuses transport.git_host_api.request() for every HTTP call (the
redirect-guarded opener, non-2xx handling for GET) rather than rolling a
second urllib client — the same reuse discipline every other Forgejo-side
loadout backend (push, review, merge) already established. NEVER touches a
local working tree or shells out to `git`.

Endpoint shapes (Gitea/Forgejo REST API, verified against Gitea's route
table — Forgejo forked this behavior and has not diverged for these
endpoints):
  - GET  /api/v1/repos/{owner}/{repo}/pulls/{index}
        -> {"base": {"sha": ...}, "head": {"sha": ...}, ...}
  - GET  /api/v1/repos/{owner}/{repo}/pulls/{index}/files
        -> [{"filename", "status", ...}, ...] — NO per-file `patch` field
           (unlike GitHub's equivalent endpoint); per-file patch text is not
           available from this endpoint on Gitea/Forgejo.
  - GET  /api/v1/repos/{owner}/{repo}/pulls/{index}.diff
        -> raw unified-diff text for the whole PR (base_sha..head_sha),
           NOT JSON — the same /api/v1-prefixed route tree, authenticated
           identically to every other call here.
  - GET  /api/v1/repos/{owner}/{repo}/contents/{filepath}?ref={sha}
        -> {"content": "<base64>", "encoding": "base64", ...} for a text
           file; used only when include_file_contents=True (scanner-staging
           path, lr-c17040 comment #1).
"""

from __future__ import annotations

import base64
import json
import urllib.parse
from typing import Any

from clagentic_loadout.acquire.contract import AcquiredPr, ChangedFile
from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.transport import git_host_api


def _get_pr_info(
    git_host_base: str, token: str, owner: str, repo: str, pr_number: int, *, opener=None
) -> dict[str, Any]:
    try:
        status, raw = git_host_api.request(
            git_host_base,
            "GET",
            f"/api/v1/repos/{owner}/{repo}/pulls/{pr_number}",
            token,
            opener=opener,
        )
    except git_host_api.GitHostApiError as exc:
        raise AcquireFetchError(
            f"cannot read PR #{pr_number} in {owner}/{repo}: {exc}"
        ) from exc
    if status != 200:
        raise AcquireFetchError(
            f"cannot read PR #{pr_number} in {owner}/{repo}: HTTP {status}"
        )
    return git_host_api.parse_json_body(raw)


def _get_changed_files(
    git_host_base: str, token: str, owner: str, repo: str, pr_number: int, *, opener=None
) -> list[ChangedFile]:
    try:
        status, raw = git_host_api.request(
            git_host_base,
            "GET",
            f"/api/v1/repos/{owner}/{repo}/pulls/{pr_number}/files",
            token,
            opener=opener,
        )
    except git_host_api.GitHostApiError as exc:
        raise AcquireFetchError(
            f"cannot read changed-file list for PR #{pr_number} in "
            f"{owner}/{repo}: {exc}"
        ) from exc
    if status != 200:
        raise AcquireFetchError(
            f"cannot read changed-file list for PR #{pr_number} in "
            f"{owner}/{repo}: HTTP {status}"
        )
    body = json.loads(raw.decode("utf-8")) if raw else []
    if not isinstance(body, list):
        raise AcquireFetchError(
            f"changed-file list endpoint returned a non-list body for PR "
            f"#{pr_number} in {owner}/{repo}"
        )
    return [
        ChangedFile(filename=f.get("filename", "<unknown>"), status=f.get("status", ""))
        for f in body
        if isinstance(f, dict)
    ]


def _get_diff_text(
    git_host_base: str, token: str, owner: str, repo: str, pr_number: int, *, opener=None
) -> str:
    try:
        status, raw = git_host_api.request(
            git_host_base,
            "GET",
            f"/api/v1/repos/{owner}/{repo}/pulls/{pr_number}.diff",
            token,
            opener=opener,
        )
    except git_host_api.GitHostApiError as exc:
        raise AcquireFetchError(
            f"cannot read diff for PR #{pr_number} in {owner}/{repo}: {exc}"
        ) from exc
    if status != 200:
        raise AcquireFetchError(
            f"cannot read diff for PR #{pr_number} in {owner}/{repo}: HTTP {status}"
        )
    return raw.decode("utf-8", errors="replace")


def _get_file_content(
    git_host_base: str,
    token: str,
    owner: str,
    repo: str,
    filepath: str,
    ref: str,
    *,
    opener=None,
) -> str:
    """Fetch one file's post-change content at *ref* (base64-decoded).

    Returns "" on a 404 (the file was deleted in this PR — a common,
    expected case, not a fetch failure) or on an undecodable/binary
    response — a scanner cannot meaningfully scan binary content anyway,
    and this module never guesses at a decode.
    """
    quoted_path = urllib.parse.quote(filepath, safe="/")
    try:
        status, raw = git_host_api.request(
            git_host_base,
            "GET",
            f"/api/v1/repos/{owner}/{repo}/contents/{quoted_path}?ref={urllib.parse.quote(ref)}",
            token,
            opener=opener,
        )
    except git_host_api.GitHostApiError as exc:
        raise AcquireFetchError(
            f"cannot read content of {filepath!r} at {ref!r} in "
            f"{owner}/{repo}: {exc}"
        ) from exc
    if status == 404:
        return ""
    if status != 200:
        raise AcquireFetchError(
            f"cannot read content of {filepath!r} at {ref!r} in "
            f"{owner}/{repo}: HTTP {status}"
        )
    body = git_host_api.parse_json_body(raw)
    encoded = body.get("content", "")
    if not encoded or body.get("encoding") != "base64":
        return ""
    try:
        return base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def fetch_pr_content(
    git_host_base: str,
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    include_file_contents: bool = False,
    opener=None,
) -> AcquiredPr:
    """Fetch one PR's diff/content from the Forgejo/Gitea API — never a
    local working tree. See acquire.contract.AcquireBackend.fetch_pr_content
    for the full contract.
    """
    pr_info = _get_pr_info(git_host_base, token, owner, repo, pr_number, opener=opener)
    base = pr_info.get("base", {}) if isinstance(pr_info.get("base"), dict) else {}
    head = pr_info.get("head", {}) if isinstance(pr_info.get("head"), dict) else {}
    base_sha = base.get("sha", "") or ""
    head_sha = head.get("sha", "") or ""

    diff_text = _get_diff_text(git_host_base, token, owner, repo, pr_number, opener=opener)
    changed_files = _get_changed_files(git_host_base, token, owner, repo, pr_number, opener=opener)

    if include_file_contents:
        changed_files = [
            cf
            if cf.status == "deleted"
            else ChangedFile(
                filename=cf.filename,
                status=cf.status,
                patch=cf.patch,
                content=_get_file_content(
                    git_host_base, token, owner, repo, cf.filename, head_sha, opener=opener
                ),
            )
            for cf in changed_files
        ]

    return AcquiredPr(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
        diff_text=diff_text,
        changed_files=tuple(changed_files),
    )


class ForgejoAcquireBackend:
    """AcquireBackend Protocol implementation for Forgejo.

    Constructed with a resolved token and the Forgejo API base URL — token
    resolution stays the caller's responsibility (via
    transport.credential_provider), matching every other Forgejo backend's
    shape in this package.
    """

    def __init__(self, token: str, *, git_host_base: str, opener=None) -> None:
        self._token = token
        self._git_host_base = git_host_base
        self._opener = opener

    def fetch_pr_content(
        self,
        *,
        owner: str,
        repo: str,
        pr_number: int,
        include_file_contents: bool = False,
    ) -> AcquiredPr:
        return fetch_pr_content(
            self._git_host_base,
            self._token,
            owner,
            repo,
            pr_number,
            include_file_contents=include_file_contents,
            opener=self._opener,
        )


__all__ = [
    "ForgejoAcquireBackend",
    "fetch_pr_content",
]
