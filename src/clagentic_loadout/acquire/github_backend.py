"""acquire.github_backend — GitHub-side PR content-acquisition transport.

lr-c17040. Shares its GitHub HTTP transport shaping with review/push/merge's
own github_backend modules via transport.github_client.request_json()
(the same redirect-hardened opener, API base, and request/response
plumbing) — this module keeps its own endpoint shapes and response-field
extraction local, matching that extraction's established split.

Endpoint shapes (real, documented GitHub REST API):
  - GET /repos/{owner}/{repo}/pulls/{pull_number}
        Accept: application/vnd.github+json
        -> {"base": {"sha": ...}, "head": {"sha": ...}, ...}
  - GET /repos/{owner}/{repo}/pulls/{pull_number}
        Accept: application/vnd.github.v3.diff
        -> raw unified-diff text for the whole PR (base_sha..head_sha), NOT
           JSON — same endpoint, different Accept header, per GitHub's own
           "media types" convention for PRs
           (https://docs.github.com/rest/pulls/pulls#custom-media-types).
  - GET /repos/{owner}/{repo}/pulls/{pull_number}/files
        -> [{"filename", "status", "patch", ...}, ...] — `patch` (a
           per-file unified-diff hunk) IS present here, unlike Gitea/
           Forgejo's equivalent endpoint.
  - GET /repos/{owner}/{repo}/contents/{path}?ref={sha}
        -> {"content": "<base64>", "encoding": "base64", ...} for a text
           file; used only when include_file_contents=True (scanner-staging
           path, lr-c17040 comment #1).
"""

from __future__ import annotations

import base64
import urllib.parse

from clagentic_loadout.acquire.contract import AcquiredPr, ChangedFile
from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.transport.github_client import GITHUB_API_BASE, request_json
from clagentic_loadout.transport.redirect_guard import no_redirect_opener

#: Accept header requesting the raw unified diff instead of the ordinary
#: JSON PR representation — same PR endpoint, GitHub's own documented
#: media-type switch.
_DIFF_ACCEPT = "application/vnd.github.v3.diff"


def _github_get(
    url: str,
    token: str,
    *,
    accept: str = "application/vnd.github+json",
    opener=None,
):
    return request_json(
        "GET",
        url,
        token,
        accept=accept,
        parse_mode="content_type",
        opener=opener,
        opener_factory=no_redirect_opener,
    )


def _get_pr_info(owner: str, repo: str, pr_number: int, token: str, *, opener=None) -> dict:
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    status, body = _github_get(url, token, opener=opener)
    if status != 200 or not isinstance(body, dict):
        raise AcquireFetchError(
            f"cannot read PR #{pr_number} in {owner}/{repo}: HTTP {status}"
        )
    return body


def _get_diff_text(owner: str, repo: str, pr_number: int, token: str, *, opener=None) -> str:
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    status, body = _github_get(url, token, accept=_DIFF_ACCEPT, opener=opener)
    if status != 200:
        raise AcquireFetchError(
            f"cannot read diff for PR #{pr_number} in {owner}/{repo}: HTTP {status}"
        )
    return body if isinstance(body, str) else ""


def _get_changed_files(
    owner: str, repo: str, pr_number: int, token: str, *, opener=None
) -> list[ChangedFile]:
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/files"
    status, body = _github_get(url, token, opener=opener)
    if status != 200 or not isinstance(body, list):
        raise AcquireFetchError(
            f"cannot read changed-file list for PR #{pr_number} in "
            f"{owner}/{repo}: HTTP {status}"
        )
    return [
        ChangedFile(
            filename=f.get("filename", "<unknown>"),
            status=f.get("status", ""),
            patch=f.get("patch", "") or "",
        )
        for f in body
        if isinstance(f, dict)
    ]


def _get_file_content(
    owner: str, repo: str, filepath: str, ref: str, token: str, *, opener=None
) -> str:
    """Fetch one file's post-change content at *ref* (base64-decoded).

    Returns "" on a 404 (the file was deleted in this PR) or on an
    undecodable/binary response, mirroring the Forgejo backend's contract
    exactly.
    """
    quoted_path = urllib.parse.quote(filepath, safe="/")
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{quoted_path}?ref={urllib.parse.quote(ref)}"
    status, body = _github_get(url, token, opener=opener)
    if status == 404:
        return ""
    if status != 200 or not isinstance(body, dict):
        raise AcquireFetchError(
            f"cannot read content of {filepath!r} at {ref!r} in "
            f"{owner}/{repo}: HTTP {status}"
        )
    encoded = body.get("content", "")
    if not encoded or body.get("encoding") != "base64":
        return ""
    try:
        return base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def fetch_pr_content(
    owner: str,
    repo: str,
    pr_number: int,
    token: str,
    *,
    include_file_contents: bool = False,
    opener=None,
) -> AcquiredPr:
    """Fetch one PR's diff/content from the GitHub API — never a local
    working tree. See acquire.contract.AcquireBackend.fetch_pr_content for
    the full contract.
    """
    pr_info = _get_pr_info(owner, repo, pr_number, token, opener=opener)
    base = pr_info.get("base", {}) if isinstance(pr_info.get("base"), dict) else {}
    head = pr_info.get("head", {}) if isinstance(pr_info.get("head"), dict) else {}
    base_sha = base.get("sha", "") or ""
    head_sha = head.get("sha", "") or ""

    diff_text = _get_diff_text(owner, repo, pr_number, token, opener=opener)
    changed_files = _get_changed_files(owner, repo, pr_number, token, opener=opener)

    if include_file_contents:
        changed_files = [
            cf
            if cf.status == "removed"
            else ChangedFile(
                filename=cf.filename,
                status=cf.status,
                patch=cf.patch,
                content=_get_file_content(
                    owner, repo, cf.filename, head_sha, token, opener=opener
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


class GithubAcquireBackend:
    """AcquireBackend Protocol implementation for GitHub.

    Constructed with a resolved token — this class does not own token
    minting, mirroring every other GitHub backend's shape in this package.
    `opener` injects a urllib opener's .open callable for tests.
    """

    def __init__(self, token: str, *, opener=None) -> None:
        self._token = token
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
            owner,
            repo,
            pr_number,
            self._token,
            include_file_contents=include_file_contents,
            opener=self._opener,
        )


__all__ = [
    "GithubAcquireBackend",
    "fetch_pr_content",
]
