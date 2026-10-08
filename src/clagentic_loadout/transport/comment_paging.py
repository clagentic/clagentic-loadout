"""transport.comment_paging -- the one issue/PR comment reader per platform,
used by every loadout site that lists a PR's comments.

Why this exists: GitHub returns a single bounded page (``per_page=30`` by
default, oldest first) from ``GET .../issues/{n}/comments``. A reader that
issues one bare GET only ever sees the OLDEST page, so on a PR with more
comments than one page (a) a post-and-verify readback cannot find the comment
it just posted, and (b) a merge gate picks an older reviewer verdict as the
current one. Every comment reader goes through this module instead.

Per-platform contract:
  - GitHub paginates via ``Link``: ``per_page=100``, ``page=N``. The list is
    complete on (a) a parsed empty ``[]`` page, or (b) a ``Link`` header
    present without ``rel="next"``. With no ``Link`` header paging continues
    (a short page proves nothing) until an empty page. If an earlier page
    advertised ``rel="next"`` and a later page carries no ``Link`` header at
    all, ``CommentPageLinkLostError`` fails closed. The ``Link`` URL is never
    followed (the bearer token only goes to URLs this module built); it is
    purely an end-of-list signal. A page that adds no new comment id fails
    closed (``CommentPageRepeatError``), and at most ``MAX_PAGES`` pages are
    read (``CommentPageCapError``); the list is never silently truncated.
  - Forgejo returns the full list in one response: the endpoint accepts no
    ``page`` or ``limit`` parameter, so exactly one GET is made with no query
    string. A non-200 status, an empty body, unparseable JSON or a non-list
    body fails closed; a parsed ``[]`` is a PR with no comments.
  - Output is de-duplicated by ``id`` and, when every comment carries an
    integer ``id``, ordered by it -- ids are monotonic on both platforms, so
    "latest" never depends on API ordering. Comments without integer ids keep
    their received order.

The return shape is the plain ``list`` of comment dicts the single-page
readers returned before.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

#: Hard page cap for the GitHub walk.
MAX_PAGES = 100

#: GitHub allows up to 100 per page.
GITHUB_PER_PAGE = 100

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


class CommentPageEmptyBodyError(CommentPageShapeError):
    """A page answered HTTP 200 with no body; a healthy server sends ``[]``."""

    def __init__(self, page: int) -> None:
        CommentListError.__init__(
            self, f"comments page {page} returned HTTP 200 with an empty body"
        )
        self.page = page


class CommentPageLinkLostError(CommentListError):
    """An earlier page advertised a next page but this one carries no Link
    header, so completeness can no longer be proven (fail closed)."""

    def __init__(self, page: int) -> None:
        super().__init__(
            f"comments page {page} carried no Link header after an earlier "
            f"page advertised a next page; refusing to act on a possibly "
            f"truncated list"
        )
        self.page = page


class CommentPageCapError(CommentListError):
    """The list was still going after MAX_PAGES pages (fail closed)."""

    def __init__(self, max_pages: int, per_page: int) -> None:
        super().__init__(
            f"comment list exceeded the {max_pages}-page cap "
            f"({max_pages * per_page} comments at {per_page} per page); "
            f"refusing to act on a possibly truncated list"
        )
        self.max_pages = max_pages


class CommentPageRepeatError(CommentPageCapError):
    """A GitHub page added no new comments: the server is not advancing (it
    ignores the page parameter), so the list cannot be proven complete (fail
    closed)."""

    def __init__(self, page: int, limit: int) -> None:
        CommentListError.__init__(
            self,
            f"comment pagination stalled at page {page}: it returned only "
            f"comments already seen (requested limit {limit}); refusing to "
            f"act on a possibly truncated list",
        )
        self.max_pages = page
        self.page = page


def _comment_key(comment: Any) -> str:
    """Identity of a comment for repeat detection: its id, else its content."""
    if isinstance(comment, dict) and comment.get("id") is not None:
        return f"id:{comment['id']}"
    return "raw:" + json.dumps(comment, sort_keys=True, default=str)


def _absorb(page_items: list[Any], seen: set[str], collected: list[Any]) -> int:
    """Append the not-yet-seen comments of a page to *collected*; return how
    many were new."""
    fresh = 0
    for item in page_items:
        key = _comment_key(item)
        if key in seen:
            continue
        seen.add(key)
        collected.append(item)
        fresh += 1
    return fresh


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
    seen: set[str] = set()
    link_expected = False
    for page in range(1, max_pages + 1):
        status, body, headers = fetch_page(f"{base}?per_page={GITHUB_PER_PAGE}&page={page}")
        if status != 200:
            raise CommentPageStatusError(status, page)
        if not isinstance(body, list):
            raise CommentPageShapeError(page)
        # A parsed empty list is the only end signal that needs no header.
        if not body:
            return _finalize(collected)
        if _absorb(body, seen, collected) == 0:
            raise CommentPageRepeatError(page, GITHUB_PER_PAGE)
        link = headers.get("link", "") if headers else ""
        if link:
            if not _LINK_NEXT_RE.search(link):
                return _finalize(collected)
            link_expected = True
        elif link_expected:
            raise CommentPageLinkLostError(page)
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
) -> list[dict[str, Any]]:
    """Return every comment on a Forgejo issue/PR with one GET.

    Forgejo's issue-comments endpoint takes no ``page`` or ``limit``
    parameter (only ``since``/``before``) and always answers with the whole
    list, so exactly one request is made and it carries no query string.

    *request_fn* is ``transport.git_host_api.request`` (passed in so this
    module has no import cycle with it); its own errors (network failure,
    refused redirect) propagate unchanged.
    """
    status, raw = request_fn(
        api_base,
        "GET",
        f"/api/v1/repos/{owner}/{repo}/issues/{number}/comments",
        token,
        opener=opener,
    )
    if status != 200:
        raise CommentPageStatusError(status, 1)
    if not raw:
        raise CommentPageEmptyBodyError(1)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CommentPageParseError(1, str(exc)) from exc
    if not isinstance(parsed, list):
        raise CommentPageShapeError(1)
    return _finalize(parsed)


__all__ = [
    "GITHUB_PER_PAGE",
    "MAX_PAGES",
    "CommentListError",
    "CommentPageCapError",
    "CommentPageEmptyBodyError",
    "CommentPageLinkLostError",
    "CommentPageParseError",
    "CommentPageRepeatError",
    "CommentPageShapeError",
    "CommentPageStatusError",
    "list_forgejo_issue_comments",
    "list_github_issue_comments",
]
