"""The tree and file reads both acquire transports expose
(acquire.contract.PolicyFileBackend), and review.policy_reader over them.
Mocked HTTP throughout."""

from __future__ import annotations

import base64
import json
import urllib.parse

import pytest

from clagentic_loadout.acquire.contract import PolicyFileBackend
from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.acquire.forgejo_backend import ForgejoAcquireBackend
from clagentic_loadout.acquire.github_backend import GithubAcquireBackend
from clagentic_loadout.review.policy_reader import HostPolicyReader, PolicyReadError
from clagentic_loadout.review.policy_source import DISPOSITIONS_PATH, read_policy_from

_OWNER = "some-owner"
_REPO = "some-repo"
_REF = "a" * 40
_GIT_HOST_BASE = "http://git-host.example.com"


class _Response:
    def __init__(self, status: int, body: bytes, content_type: str = "application/json"):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json(payload, status=200) -> _Response:
    return _Response(status, json.dumps(payload).encode("utf-8"))


def _file(text: bytes) -> dict:
    return {"type": "file", "encoding": "base64", "content": base64.b64encode(text).decode()}


def _server(tree_pages, files, *, seen=None):
    """Serves git-trees (one entry of *tree_pages* per page, the last page not
    truncated unless it says so) and contents for both transports."""

    def opener(req, timeout=30):
        url = req.full_url
        if seen is not None:
            seen.append(url)
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if "/git/trees/" in parsed.path:
            page = int(query.get("page", ["1"])[0])
            entries, truncated = tree_pages[page - 1]
            return _json({"tree": entries, "truncated": truncated})
        if "/contents/" in parsed.path:
            name = urllib.parse.unquote(parsed.path.split("/contents/", 1)[1])
            if name not in files:
                return _Response(404, b"{}")
            return _json(files[name])
        raise AssertionError(f"unexpected request: {url}")

    return opener


def _backends(opener):
    return {
        "github": GithubAcquireBackend("tok", opener=opener),
        "forgejo": ForgejoAcquireBackend("tok", git_host_base=_GIT_HOST_BASE, opener=opener),
    }


def _blob(path: str) -> dict:
    return {"path": path, "type": "blob"}


@pytest.fixture(params=["github", "forgejo"])
def platform(request):
    return request.param


def _backend(platform, tree_pages, files, seen=None):
    return _backends(_server(tree_pages, files, seen=seen))[platform]


def test_both_transports_satisfy_the_protocol():
    for backend in _backends(None).values():
        assert isinstance(backend, PolicyFileBackend)


def test_the_tree_lists_files_and_leaves_out_directories(platform):
    pages = [([_blob("a.py"), {"path": "src", "type": "tree"}, _blob("src/b.py")], False)]

    backend = _backend(platform, pages, {})

    assert backend.fetch_tree_paths(owner=_OWNER, repo=_REPO, ref=_REF) == ["a.py", "src/b.py"]


def test_a_truncated_tree_is_a_failure(platform):
    backend = _backend(platform, [([_blob("a.py")], True)] * 200, {})

    with pytest.raises(AcquireFetchError, match="truncated"):
        backend.fetch_tree_paths(owner=_OWNER, repo=_REPO, ref=_REF)


def test_a_tree_response_of_the_wrong_shape_is_a_failure(platform):
    def opener(req, timeout=30):
        return _json({"unexpected": True})

    with pytest.raises(AcquireFetchError):
        _backends(opener)[platform].fetch_tree_paths(owner=_OWNER, repo=_REPO, ref=_REF)


def test_a_file_is_returned_as_bytes(platform):
    backend = _backend(platform, [([], False)], {"x.yaml": _file(b"a: 1\n")})

    assert backend.fetch_file_bytes(owner=_OWNER, repo=_REPO, path="x.yaml", ref=_REF) == b"a: 1\n"


def test_a_missing_file_is_a_failure_not_an_empty_file(platform):
    backend = _backend(platform, [([], False)], {})

    with pytest.raises(AcquireFetchError, match="HTTP 404"):
        backend.fetch_file_bytes(owner=_OWNER, repo=_REPO, path="x.yaml", ref=_REF)


@pytest.mark.parametrize(
    "body", [{"type": "file", "encoding": "none", "content": ""}, {"type": "dir"}], ids=str
)
def test_a_file_not_returned_inline_is_a_failure(platform, body):
    backend = _backend(platform, [([], False)], {"x.yaml": body})

    with pytest.raises(AcquireFetchError, match="not returned inline"):
        backend.fetch_file_bytes(owner=_OWNER, repo=_REPO, path="x.yaml", ref=_REF)


def test_forgejo_follows_tree_pages_until_the_listing_is_complete():
    seen: list[str] = []
    pages = [([_blob("a.py")], True), ([_blob("b.py")], True), ([_blob("c.py")], False)]
    backend = _backend("forgejo", pages, {}, seen=seen)

    paths = backend.fetch_tree_paths(owner=_OWNER, repo=_REPO, ref=_REF)

    assert paths == ["a.py", "b.py", "c.py"]
    assert len(seen) == 3


def _forgejo_paged_server(entries, *, per_page=1000, seen=None):
    """Forgejo's documented git/trees paging: total_count on every page,
    truncated true on a non-final page, and a page past the end answered as
    HTTP 200 with tree null and truncated false."""

    def opener(req, timeout=30):
        if seen is not None:
            seen.append(req.full_url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
        page = int(query["page"][0])
        chunk = entries[(page - 1) * per_page : page * per_page]
        return _json(
            {
                "sha": _REF,
                "tree": chunk or None,
                "truncated": page * per_page < len(entries),
                "page": page,
                "total_count": len(entries),
            }
        )

    return opener


def _synthetic_tree(count):
    return [_blob(f"dir{i % 7}/file{i}.txt") for i in range(count)]


def test_forgejo_reads_a_tree_larger_than_one_page_by_total_count():
    seen: list[str] = []
    entries = _synthetic_tree(1371)
    backend = _backends(_forgejo_paged_server(entries, seen=seen))["forgejo"]

    paths = backend.fetch_tree_paths(owner=_OWNER, repo=_REPO, ref=_REF)

    assert paths == [e["path"] for e in entries]
    assert len(seen) == 2


def test_forgejo_reads_a_tree_that_fits_one_page_in_one_request():
    seen: list[str] = []
    entries = _synthetic_tree(435)
    backend = _backends(_forgejo_paged_server(entries, seen=seen))["forgejo"]

    assert len(backend.fetch_tree_paths(owner=_OWNER, repo=_REPO, ref=_REF)) == 435
    assert len(seen) == 1


def test_forgejo_counts_directory_entries_toward_total_count():
    entries = [{"path": "src", "type": "tree"}, _blob("src/a.py")]
    backend = _backends(_forgejo_paged_server(entries, per_page=1))["forgejo"]

    assert backend.fetch_tree_paths(owner=_OWNER, repo=_REPO, ref=_REF) == ["src/a.py"]


def test_forgejo_reads_an_empty_tree_as_empty():
    backend = _backends(_forgejo_paged_server([]))["forgejo"]

    assert backend.fetch_tree_paths(owner=_OWNER, repo=_REPO, ref=_REF) == []


def test_forgejo_tree_null_before_total_count_is_reached_is_a_failure():
    def opener(req, timeout=30):
        return _json({"tree": None, "truncated": False, "page": 1, "total_count": 5})

    with pytest.raises(AcquireFetchError, match="not a complete listing"):
        _backends(opener)["forgejo"].fetch_tree_paths(owner=_OWNER, repo=_REPO, ref=_REF)


def test_forgejo_tree_that_never_reaches_total_count_is_a_failure():
    def opener(req, timeout=30):
        return _json({"tree": [_blob("a.py")], "truncated": True, "total_count": 10**6})

    with pytest.raises(AcquireFetchError, match="truncated"):
        _backends(opener)["forgejo"].fetch_tree_paths(owner=_OWNER, repo=_REPO, ref=_REF)


def test_github_reads_the_tree_in_one_unpaged_request():
    seen: list[str] = []
    backend = _backend("github", [(_synthetic_tree(1371), False)], {}, seen=seen)

    assert len(backend.fetch_tree_paths(owner=_OWNER, repo=_REPO, ref=_REF)) == 1371
    assert len(seen) == 1
    assert "page=" not in seen[0]


def test_the_reader_turns_a_fetch_failure_into_a_named_read_error(platform):
    reader = HostPolicyReader(_backend(platform, [([], False)], {}), owner=_OWNER, repo=_REPO)

    with pytest.raises(PolicyReadError, match=r"cannot read x\.yaml at aaaaaaaaaaaa"):
        reader.read_text(_REF, "x.yaml")


def test_the_reader_refuses_a_file_over_the_size_bound(platform):
    big = _file(b"x" * (512 * 1024 + 1))
    reader = HostPolicyReader(
        _backend(platform, [([], False)], {"x.yaml": big}), owner=_OWNER, repo=_REPO
    )

    with pytest.raises(PolicyReadError, match="larger than"):
        reader.read_text(_REF, "x.yaml")


def test_policy_is_read_through_the_host_reader(platform):
    disposition = (
        "dispositions:\n"
        '  - path: "a.py"\n    rule_id: R1\n    kind: false_positive\n'
        '    rationale: "checked"\n    by: a-maintainer\n    at: 2026-09-01\n'
    )
    pages = [([_blob("a.py"), _blob(DISPOSITIONS_PATH)], False)]
    backend = _backend(platform, pages, {DISPOSITIONS_PATH: _file(disposition.encode())})

    policy = read_policy_from(
        HostPolicyReader(backend, owner=_OWNER, repo=_REPO), _REF, visibility="private"
    )

    assert [d.rule_id for d in policy.dispositions] == ["R1"]
    assert policy.degraded == ()


_MALFORMED_ENTRIES = [
    "not-an-object",
    {"type": "blob"},
    {"path": 7, "type": "blob"},
    {"path": "a.py"},
    {"path": "a.py", "type": None},
]


@pytest.mark.parametrize("entry", _MALFORMED_ENTRIES, ids=repr)
def test_a_malformed_tree_entry_is_a_failure_not_a_skip(platform, entry):
    pages = [([_blob("a.py"), entry], False)]
    backend = _backend(platform, pages, {})

    with pytest.raises(AcquireFetchError, match="malformed entry"):
        backend.fetch_tree_paths(owner=_OWNER, repo=_REPO, ref=_REF)


def test_a_malformed_tree_entry_degrades_the_policy_naming_the_cause(platform):
    pages = [([_blob(DISPOSITIONS_PATH), {"type": "blob"}], False)]
    backend = _backend(platform, pages, {DISPOSITIONS_PATH: _file(b"dispositions: []\n")})

    policy = read_policy_from(
        HostPolicyReader(backend, owner=_OWNER, repo=_REPO), _REF, visibility="private"
    )

    assert policy.degraded
    assert "malformed entry" in " ".join(str(d) for d in policy.degraded)
