"""Git-host fakes that follow the documented contracts of the Forgejo and GitHub
tree-listing endpoints, built from the public API documentation and synthetic
entries only. A fake that models only what the code under test happens to use
cannot fail the way a real host does, so each behaviour a host is documented to
have is a parameter here, and the tests that matter run against all of them.

Forgejo `GET /repos/{owner}/{repo}/git/trees/{sha}?recursive=true&per_page=&page=`
  - paged; `total_count` is the number of entries (directories included) across
    every page and is repeated on each page
  - a page past the end is HTTP 200 with `tree: null`, `truncated: false`
  - `truncated` is documented only as "the listing was cut", so it is modelled
    three ways (see `TRUNCATED_*`): exactly when entries remain after the page,
    whenever the page came back full (the last page of a tree whose size is an
    exact multiple of the page size claims there is more), and on every page of
    a listing that needs more than one page
  - the page size is capped by the host; a larger `per_page` is clamped

GitHub `GET /repos/{owner}/{repo}/git/trees/{sha}?recursive=1`
  - not paged: `page` and `per_page` are ignored and the whole listing comes
    back, `truncated` true only when the host's entry cap was exceeded
"""

from __future__ import annotations

import urllib.parse

from tests._support.fakes import json_resp

__all__ = [
    "TRUNCATED_ENTRIES_REMAIN",
    "TRUNCATED_LISTING_PAGED",
    "TRUNCATED_MODELS",
    "TRUNCATED_PAGE_FULL",
    "forgejo_tree_opener",
    "forgejo_tree_page",
    "github_tree_opener",
]

#: `truncated` is true exactly when entries remain after this page.
TRUNCATED_ENTRIES_REMAIN = "entries_remain"
#: `truncated` is true whenever this page holds as many entries as were asked for.
TRUNCATED_PAGE_FULL = "page_full"
#: `truncated` is true on every page of a listing larger than one page.
TRUNCATED_LISTING_PAGED = "listing_paged"
TRUNCATED_MODELS = (TRUNCATED_ENTRIES_REMAIN, TRUNCATED_PAGE_FULL, TRUNCATED_LISTING_PAGED)

_SHA = "a" * 40


def forgejo_tree_page(
    entries: list[dict],
    *,
    per_page: int,
    page: int,
    truncated_model: str = TRUNCATED_ENTRIES_REMAIN,
) -> dict:
    """The body Forgejo returns for one page of a recursive tree listing."""
    start = (max(page, 1) - 1) * per_page
    chunk = entries[start : start + per_page]
    if truncated_model == TRUNCATED_ENTRIES_REMAIN:
        truncated = start + per_page < len(entries)
    elif truncated_model == TRUNCATED_PAGE_FULL:
        truncated = len(chunk) == per_page
    elif truncated_model == TRUNCATED_LISTING_PAGED:
        truncated = bool(chunk) and len(entries) > per_page
    else:
        raise ValueError(f"unknown truncated model {truncated_model!r}")
    return {
        "sha": _SHA,
        "url": "",
        "tree": chunk or None,
        "truncated": truncated,
        "page": page,
        "total_count": len(entries),
    }


def forgejo_tree_opener(
    entries: list[dict],
    *,
    max_per_page: int = 1000,
    truncated_model: str = TRUNCATED_ENTRIES_REMAIN,
    seen: list[str] | None = None,
):
    """A Forgejo host serving *entries* from its tree endpoint. Every request
    URL is appended to *seen*; any other endpoint fails the test."""

    def opener(req, timeout=30):
        url = req.full_url
        if seen is not None:
            seen.append(url)
        parsed = urllib.parse.urlparse(url)
        if "/git/trees/" not in parsed.path:
            raise AssertionError(f"unexpected request: {url}")
        query = urllib.parse.parse_qs(parsed.query)
        per_page = min(int(query.get("per_page", [str(max_per_page)])[0]), max_per_page)
        page = int(query.get("page", ["1"])[0])
        return json_resp(
            200,
            forgejo_tree_page(
                entries, per_page=per_page, page=page, truncated_model=truncated_model
            ),
        )

    return opener


def github_tree_opener(
    entries: list[dict], *, truncated: bool = False, seen: list[str] | None = None
):
    """A GitHub host serving *entries* from its tree endpoint in one response,
    whatever paging parameters the request carries."""

    def opener(req, timeout=30):
        url = req.full_url
        if seen is not None:
            seen.append(url)
        if "/git/trees/" not in urllib.parse.urlparse(url).path:
            raise AssertionError(f"unexpected request: {url}")
        return json_resp(
            200, {"sha": _SHA, "url": "", "tree": entries, "truncated": truncated}
        )

    return opener
