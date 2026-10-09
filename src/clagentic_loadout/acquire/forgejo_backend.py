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
  - GET  /api/v1/repos/{owner}/{repo}/compare/{a}...{b}
        -> {"total_commits": n, "commits": [...]} — commits only, no diff;
           used in both directions to tell a strict fast-forward apart.
  - GET  /{owner}/{repo}/compare/{a}...{b}.diff
        -> raw net unified diff (the web route; the API tree has no
           equivalent). Used by fetch_range_diff only.
  - GET  /api/v1/repos/{owner}/{repo}/contents/{filepath}?ref={sha}
        -> {"content": "<base64>", "encoding": "base64", ...} for a text
           file; used only when include_file_contents=True (scanner-staging
           path, lr-c17040 comment #1).
"""

from __future__ import annotations

import base64
import urllib.parse
from typing import Any

from clagentic_loadout.acquire.contract import AcquiredPr, ChangedFile, RangeDiff, pr_text
from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.transport import git_host_api


def _parse_json(raw: bytes, expect: type, what: str) -> Any:
    """Parse a 200 response body as JSON of type *expect*, or raise
    AcquireFetchError. Every JSON read in this module goes through here so a
    malformed, empty, or wrong-shaped body is a fetch failure, never a raw
    parser error, an AttributeError on a wrong-typed value, or a silent
    empty result that reads as "no data"."""
    try:
        parsed = git_host_api.decode_json_body(raw)
    except ValueError as exc:
        raise AcquireFetchError(f"{what} returned a body that is not valid JSON") from exc
    if not isinstance(parsed, expect):
        raise AcquireFetchError(
            f"{what} returned JSON of the wrong shape (expected a {expect.__name__}, "
            f"got {type(parsed).__name__})"
        )
    return parsed


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
    return _parse_json(raw, dict, f"PR #{pr_number} in {owner}/{repo}")


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
    body = (
        _parse_json(raw, list, f"changed-file list for PR #{pr_number} in {owner}/{repo}")
        if raw
        else []
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
    body = _parse_json(raw, dict, f"content of {filepath!r} at {ref!r} in {owner}/{repo}")
    encoded = body.get("content", "")
    if not encoded or body.get("encoding") != "base64":
        return ""
    try:
        return base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def _compare_commit_count(
    git_host_base: str, token: str, owner: str, repo: str, base: str, head: str, *, opener=None
) -> int:
    """Number of commits reachable from *head* but not from *base*."""
    try:
        status, raw = git_host_api.request(
            git_host_base,
            "GET",
            f"/api/v1/repos/{owner}/{repo}/compare/{base}...{head}",
            token,
            opener=opener,
        )
    except git_host_api.GitHostApiError as exc:
        raise AcquireFetchError(
            f"cannot compare {base[:12]}...{head[:12]} in {owner}/{repo}: {exc}"
        ) from exc
    if status != 200:
        raise AcquireFetchError(
            f"cannot compare {base[:12]}...{head[:12]} in {owner}/{repo}: HTTP {status}"
        )
    total = _parse_json(
        raw, dict, f"compare {base[:12]}...{head[:12]} in {owner}/{repo}"
    ).get("total_commits")
    if isinstance(total, bool) or not isinstance(total, int):
        raise AcquireFetchError(
            f"compare {base[:12]}...{head[:12]} in {owner}/{repo} returned no commit count"
        )
    return total


def fetch_range_diff(
    git_host_base: str,
    token: str,
    owner: str,
    repo: str,
    base_sha: str,
    head_sha: str,
    *,
    opener=None,
) -> RangeDiff:
    """Net diff between two commits. The API's compare route reports commits
    only, so the relation is derived from commit counts in both directions
    (a strict fast-forward has commits one way and none the other) and the
    net diff comes from the web compare route's `.diff` form, which the same
    token authorises."""
    forward = _compare_commit_count(
        git_host_base, token, owner, repo, base_sha, head_sha, opener=opener
    )
    backward = _compare_commit_count(
        git_host_base, token, owner, repo, head_sha, base_sha, opener=opener
    )
    if forward == 0 or backward != 0:
        return RangeDiff(base_sha=base_sha, head_sha=head_sha, fast_forward=False)
    try:
        status, raw = git_host_api.request(
            git_host_base,
            "GET",
            f"/{owner}/{repo}/compare/{base_sha}...{head_sha}.diff",
            token,
            opener=opener,
        )
    except git_host_api.GitHostApiError as exc:
        raise AcquireFetchError(
            f"cannot read the diff of {base_sha[:12]}...{head_sha[:12]} in "
            f"{owner}/{repo}: {exc}"
        ) from exc
    if status != 200:
        raise AcquireFetchError(
            f"cannot read the diff of {base_sha[:12]}...{head_sha[:12]} in "
            f"{owner}/{repo}: HTTP {status}"
        )
    return RangeDiff(
        base_sha=base_sha,
        head_sha=head_sha,
        fast_forward=True,
        diff_text=raw.decode("utf-8", errors="replace"),
    )


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
        title=pr_text(pr_info, "title"),
        body=pr_text(pr_info, "body"),
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

    def fetch_range_diff(
        self, *, owner: str, repo: str, base_sha: str, head_sha: str
    ) -> RangeDiff:
        return fetch_range_diff(
            self._git_host_base,
            self._token,
            owner,
            repo,
            base_sha,
            head_sha,
            opener=self._opener,
        )


__all__ = [
    "ForgejoAcquireBackend",
    "fetch_pr_content",
    "fetch_range_diff",
]
