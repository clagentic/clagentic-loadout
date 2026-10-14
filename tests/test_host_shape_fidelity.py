"""Host-shape fidelity: the tree-listing fakes follow the documented Forgejo and
GitHub contracts, the current readers pass against every modelled shape, and a
reader that pages by the `truncated` flag alone fails against them.

All data is synthetic. The point of the last group is that the fakes can fail a
reader the way a real host does: a fake that every reader passes proves nothing.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.acquire.forgejo_backend import ForgejoAcquireBackend
from clagentic_loadout.acquire.github_backend import GithubAcquireBackend
from clagentic_loadout.transport import git_host_api
from tests._support.host_fakes import (
    TRUNCATED_ENTRIES_REMAIN,
    TRUNCATED_LISTING_PAGED,
    TRUNCATED_MODELS,
    TRUNCATED_PAGE_FULL,
    forgejo_tree_opener,
    forgejo_tree_page,
    github_tree_opener,
)

_OWNER = "some-owner"
_REPO = "some-repo"
_REF = "a" * 40
_BASE = "http://git-host.example.com"

#: Sizes around the page boundary, including exact multiples of the page size.
_SIZES = [0, 1, 999, 1000, 1001, 1371, 2000, 2500]


class _Request:
    def __init__(self, url: str):
        self.full_url = url


def _entries(count: int) -> list[dict]:
    return [{"path": f"dir{i % 7}/file{i}.txt", "type": "blob"} for i in range(count)]


def _forgejo(opener) -> ForgejoAcquireBackend:
    return ForgejoAcquireBackend("tok", git_host_base=_BASE, opener=opener)


def _read(opener) -> list[str]:
    return _forgejo(opener).fetch_tree_paths(owner=_OWNER, repo=_REPO, ref=_REF)


def _flag_paged_read(opener) -> list[str]:
    """A reader that pages by the `truncated` flag alone and treats any page
    without a tree list as an incomplete listing."""
    paths: list[str] = []
    for page in range(1, 101):
        status, raw = git_host_api.request(
            _BASE,
            "GET",
            f"/api/v1/repos/{_OWNER}/{_REPO}/git/trees/{_REF}"
            f"?recursive=true&per_page=1000&page={page}",
            "tok",
            opener=opener,
        )
        assert status == 200
        body = git_host_api.decode_json_body(raw)
        if not isinstance(body.get("tree"), list):
            raise AcquireFetchError("the tree was not a complete listing")
        paths.extend(e["path"] for e in body["tree"] if e["type"] != "tree")
        if not body["truncated"]:
            return paths
    raise AcquireFetchError("the tree was truncated")


class TestForgejoFakeFollowsTheDocumentedContract:
    def test_total_count_counts_every_entry_and_is_repeated_on_each_page(self):
        entries = _entries(2500)
        pages = [forgejo_tree_page(entries, per_page=1000, page=p) for p in (1, 2, 3)]

        assert [p["total_count"] for p in pages] == [2500, 2500, 2500]
        assert [len(p["tree"]) for p in pages] == [1000, 1000, 500]

    def test_a_page_past_the_end_is_tree_null_and_not_truncated(self):
        for model in TRUNCATED_MODELS:
            page = forgejo_tree_page(_entries(10), per_page=10, page=2, truncated_model=model)
            assert page["tree"] is None
            assert page["truncated"] is False
            assert page["total_count"] == 10

    def test_an_empty_tree_is_tree_null_on_page_one(self):
        page = forgejo_tree_page([], per_page=1000, page=1)
        assert page["tree"] is None and page["total_count"] == 0

    def test_the_truncated_models_differ_only_where_the_documentation_leaves_room(self):
        def truncated(model, count, page):
            return forgejo_tree_page(
                _entries(count), per_page=1000, page=page, truncated_model=model
            )["truncated"]

        assert truncated(TRUNCATED_ENTRIES_REMAIN, 2000, 2) is False
        assert truncated(TRUNCATED_PAGE_FULL, 2000, 2) is True
        assert truncated(TRUNCATED_LISTING_PAGED, 1371, 2) is True
        assert truncated(TRUNCATED_ENTRIES_REMAIN, 1371, 2) is False
        assert truncated(TRUNCATED_PAGE_FULL, 1371, 2) is False

    def test_a_page_size_above_the_host_cap_is_clamped(self):
        opener = forgejo_tree_opener(_entries(130), max_per_page=50)

        response = opener(
            _Request(f"{_BASE}/api/v1/x/git/trees/s?recursive=true&per_page=1000&page=1")
        )

        assert len(json.loads(response.read())["tree"]) == 50

    def test_any_other_endpoint_fails_the_test(self):
        with pytest.raises(AssertionError, match="unexpected request"):
            forgejo_tree_opener([])(_Request(f"{_BASE}/api/v1/repos/o/r/pulls/1"))


class TestCurrentForgejoReaderAgainstEveryModelledShape:
    @pytest.mark.parametrize("model", TRUNCATED_MODELS)
    @pytest.mark.parametrize("count", _SIZES)
    def test_every_entry_is_read_once_in_order(self, model, count):
        entries = _entries(count)

        paths = _read(forgejo_tree_opener(entries, truncated_model=model))

        assert paths == [e["path"] for e in entries]

    @pytest.mark.parametrize("model", TRUNCATED_MODELS)
    def test_a_host_that_caps_the_page_size_below_the_request_is_paged_to_the_end(self, model):
        entries = _entries(130)
        seen: list[str] = []

        paths = _read(
            forgejo_tree_opener(entries, max_per_page=50, truncated_model=model, seen=seen)
        )

        assert paths == [e["path"] for e in entries]
        assert len(seen) == 3

    @pytest.mark.parametrize("model", TRUNCATED_MODELS)
    def test_it_stops_at_total_count_without_asking_for_a_page_past_the_end(self, model):
        seen: list[str] = []

        _read(forgejo_tree_opener(_entries(2000), truncated_model=model, seen=seen))

        pages = [
            int(urllib.parse.parse_qs(urllib.parse.urlparse(u).query)["page"][0]) for u in seen
        ]
        assert pages == [1, 2]


class TestAReaderThatPagesByTheTruncatedFlagAloneFailsOnLargeTrees:
    """The fakes must be able to fail a reader. This reader is correct against a
    host that sets `truncated` exactly when entries remain and wrong against the
    other two documented-compatible shapes."""

    def test_it_is_correct_against_a_host_that_flags_exactly_the_non_final_pages(self):
        entries = _entries(1371)

        assert _flag_paged_read(forgejo_tree_opener(entries)) == [e["path"] for e in entries]

    @pytest.mark.parametrize("count", [1001, 1371, 2000, 2500])
    def test_it_fails_when_every_page_of_a_multi_page_listing_is_flagged(self, count):
        opener = forgejo_tree_opener(_entries(count), truncated_model=TRUNCATED_LISTING_PAGED)

        with pytest.raises(AcquireFetchError, match="not a complete listing"):
            _flag_paged_read(opener)

    @pytest.mark.parametrize("count", [2000, 3000])
    def test_it_fails_when_a_full_final_page_is_flagged(self, count):
        opener = forgejo_tree_opener(_entries(count), truncated_model=TRUNCATED_PAGE_FULL)

        with pytest.raises(AcquireFetchError, match="not a complete listing"):
            _flag_paged_read(opener)

    def test_a_tree_that_fits_one_page_is_read_correctly_under_every_model(self):
        for model in TRUNCATED_MODELS:
            entries = _entries(999)
            assert _flag_paged_read(forgejo_tree_opener(entries, truncated_model=model)) == [
                e["path"] for e in entries
            ]


class TestGithubFakeFollowsTheDocumentedContract:
    def test_the_whole_listing_comes_back_whatever_paging_parameters_are_sent(self):
        seen: list[str] = []
        entries = _entries(5000)
        backend = GithubAcquireBackend("tok", opener=github_tree_opener(entries, seen=seen))

        paths = backend.fetch_tree_paths(owner=_OWNER, repo=_REPO, ref=_REF)

        assert paths == [e["path"] for e in entries]
        assert len(seen) == 1
        assert "page=" not in seen[0]

    def test_a_listing_over_the_host_cap_is_reported_truncated_and_refused(self):
        backend = GithubAcquireBackend(
            "tok", opener=github_tree_opener(_entries(10), truncated=True)
        )

        with pytest.raises(AcquireFetchError, match="truncated"):
            backend.fetch_tree_paths(owner=_OWNER, repo=_REPO, ref=_REF)
