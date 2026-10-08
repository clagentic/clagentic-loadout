"""transport.comment_paging -- the one paginated issue/PR comment reader per
platform, used by every loadout site that lists a PR's comments.

Why this exists: both GitHub (``per_page=30``, oldest first) and Forgejo
return a single bounded page from ``GET .../issues/{n}/comments``. A reader
that issues one bare GET only ever sees the OLDEST page, so on a PR with more
comments than one page (a) a post-and-verify readback cannot find the comment
it just posted, and (b) a merge gate picks an older reviewer verdict as the
current one. Every comment reader goes through this module instead.

Contract:
  - GitHub: ``per_page=100``, ``page=N``. Pagination ends on a short page, or
    on a ``Link`` header that carries no ``rel="next"``. The ``Link`` URL is
    never followed (the bearer token must only ever go to the URL this module
    built), so it is used purely as an end-of-list signal.
  - Forgejo: ``limit=50`` (the server maximum), ``page=N``, until a short or
    empty page.
  - Bounded: at most ``MAX_PAGES`` pages. A list that is still going at the
    cap raises ``CommentPageCapError``; it is never silently truncated.
  - Output is de-duplicated by ``id`` (comments posted mid-walk shift page
    boundaries) and, when every comment carries an integer ``id``, ordered by
    it -- ids are monotonic on both platforms, so "latest" never depends on
    API ordering. Comments without integer ids keep their received order.

The return shape is the plain ``list`` of comment dicts the single-page
readers returned before.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

#: Hard page cap shared by both platforms.
MAX_PAGES = 100

#: GitHub allows up to 100 per page.
GITHUB_PER_PAGE = 100

#: Forgejo's server-side maximum page size.
FORGEJO_PAGE_LIMIT = 50

_LINK_NEXT_RE = re.compile(r'<[^>]*>\s*;\s*rel="?next"?', re.IGNORECASE)


class CommentListError(Exception):
    """Base class for every failure reading a comment list."""


class CommentPageStatusError(CommentListError):
    """A page came back with a non-200 status."""

    def __init__(self, status: int, page: int) -> None:
        super().__init__(f"comments page {page} returned HTTP {status}")
        self.status = status
        self.page = page


class CommentPageShapeError(CommentListError):
    """A page body was not a JSON list."""

    def __init__(self, page: int) -> None:
        super().__init__(f"comments page {page} returned a non-list body")
        self.page = page


class CommentPageParseError(CommentPageShapeError):
    """A page body could not be decoded as JSON at all."""

    def __init__(self, page: int, detail: str) -> None:
        CommentListError.__init__(
            self, f"comments page {page} returned unparseable JSON: {detail}"
        )
        self.page = page
        self.detail = detail


class CommentPageCapError(CommentListError):
    """The list was still going after MAX_PAGES pages (fail closed)."""

    def __init__(self, max_pages: int, per_page: int) -> None:
        super().__init__(
            f"comment list exceeded the {max_pages}-page cap "
            f"({max_pages * per_page} comments at {per_page} per page); "
            f"refusing to act on a possibly truncated list"
        )
        self.max_pages = max_pages


def _finalize(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """De-duplicate by id (first occurrence wins) and order by integer id when
    every comment has one."""
    seen: set[Any] = set()
    unique: list[dict[str, Any]] = []
    for comment in comments:
        cid = comment.get("id") if isinstance(comment, dict) else None
        if cid is not None:
            if cid in seen:
                continue
            seen.add(cid)
        unique.append(comment)
    if unique and all(
        isinstance(c, dict) and isinstance(c.get("id"), int) and not isinstance(c.get("id"), bool)
        for c in unique
    ):
        unique.sort(key=lambda c: c["id"])
    return unique


def list_github_issue_comments(
    owner: str,
    repo: str,
    number: int | str,
    fetch_page: Callable[[str], tuple[int, Any, dict[str, str]]],
    *,
    api_base: str,
    max_pages: int = MAX_PAGES,
) -> list[dict[str, Any]]:
    """Return every comment on a GitHub issue/PR.

    *fetch_page* takes a full URL and returns ``(status, parsed_body,
    lower-cased-headers)``; the caller owns auth, redirect hardening and
    network-error translation (it is typically a closure over
    ``transport.github_client.request_json_with_headers``).
    """
    base = f"{api_base}/repos/{owner}/{repo}/issues/{number}/comments"
    collected: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        status, body, headers = fetch_page(f"{base}?per_page={GITHUB_PER_PAGE}&page={page}")
        if status != 200:
            raise CommentPageStatusError(status, page)
        if not isinstance(body, list):
            raise CommentPageShapeError(page)
        collected.extend(body)
        link = headers.get("link", "") if headers else ""
        if link:
            if not _LINK_NEXT_RE.search(link):
                return _finalize(collected)
        elif len(body) < GITHUB_PER_PAGE:
            return _finalize(collected)
    raise CommentPageCapError(max_pages, GITHUB_PER_PAGE)


def list_forgejo_issue_comments(
    request_fn: Callable[..., tuple[int, bytes]],
    api_base: str,
    owner: str,
    repo: str,
    number: int | str,
    token: str,
    *,
    opener=None,
    max_pages: int = MAX_PAGES,
) -> list[dict[str, Any]]:
    """Return every comment on a Forgejo issue/PR.

    *request_fn* is ``transport.git_host_api.request`` (passed in so this
    module has no import cycle with it); its own errors (network failure,
    refused redirect) propagate unchanged.
    """
    path = f"/api/v1/repos/{owner}/{repo}/issues/{number}/comments"
    collected: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        status, raw = request_fn(
            api_base,
            "GET",
            f"{path}?limit={FORGEJO_PAGE_LIMIT}&page={page}",
            token,
            opener=opener,
        )
        if status != 200:
            raise CommentPageStatusError(status, page)
        if not raw:
            return _finalize(collected)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CommentPageParseError(page, str(exc)) from exc
        if not isinstance(parsed, list):
            raise CommentPageShapeError(page)
        collected.extend(parsed)
        if len(parsed) < FORGEJO_PAGE_LIMIT:
            return _finalize(collected)
    raise CommentPageCapError(max_pages, FORGEJO_PAGE_LIMIT)


__all__ = [
    "FORGEJO_PAGE_LIMIT",
    "GITHUB_PER_PAGE",
    "MAX_PAGES",
    "CommentListError",
    "CommentPageCapError",
    "CommentPageParseError",
    "CommentPageShapeError",
    "CommentPageStatusError",
    "list_forgejo_issue_comments",
    "list_github_issue_comments",
]
