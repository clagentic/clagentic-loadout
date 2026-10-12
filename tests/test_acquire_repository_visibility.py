"""acquire: the repository visibility the host's own repository record states,
read from the PR metadata under the reader credential. Anything that is not a
positive statement of non-public resolves to public or to nothing, never to
private."""

from __future__ import annotations

import pytest

from clagentic_loadout.acquire.contract import repository_visibility
from clagentic_loadout.review.stakes import VISIBILITIES


def _pr(repo) -> dict:
    return {"base": {"sha": "a" * 40, "repo": repo}}


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        ({"private": True}, "private"),
        ({"private": False}, "public"),
        ({"private": True, "visibility": "private"}, "private"),
        ({"private": True, "visibility": "internal"}, "internal"),
        ({"visibility": "internal"}, "internal"),
        ({"visibility": "public", "private": False}, "public"),
        ({"private": True, "internal": True}, "internal"),
        ({"private": True, "internal": False}, "private"),
    ],
)
def test_a_stated_visibility_is_returned(repo, expected):
    assert repository_visibility(_pr(repo)) == expected


@pytest.mark.parametrize(
    "repo",
    [
        {"private": True, "visibility": "public"},
        {"private": False, "visibility": "private"},
        {"visibility": "public"},
    ],
    ids=str,
)
def test_any_signal_that_says_public_wins(repo):
    assert repository_visibility(_pr(repo)) == "public"


@pytest.mark.parametrize(
    "pr",
    [
        {},
        {"base": None},
        {"base": {}},
        {"base": {"repo": None}},
        {"base": {"repo": "private"}},
        {"base": {"repo": {}}},
        {"base": {"repo": {"private": "true"}}},
        {"base": {"repo": {"private": 1}}},
        {"base": {"repo": {"private": None}}},
        {"base": {"repo": {"visibility": "Private"}}},
        {"base": {"repo": {"visibility": "secret"}}},
        {"base": {"repo": {"visibility": ["private"]}}},
    ],
    ids=str,
)
def test_an_absent_or_unrecognised_record_states_nothing(pr):
    assert repository_visibility(pr) is None


@pytest.mark.parametrize(
    "repo",
    [
        {"private": True, "visibility": "secret"},
        {"private": False, "visibility": "secret"},
        {"visibility": "public", "private": "yes"},
        {"visibility": "private", "private": 1},
        {"visibility": None, "private": True},
        {"visibility": "internal", "internal": "true", "private": True},
        {"private": True, "internal": "yes"},
        {"private": False, "internal": None},
    ],
    ids=str,
)
def test_an_unrecognised_signal_voids_the_record_whatever_the_others_say(repo):
    assert repository_visibility(_pr(repo)) is None


def test_the_vocabulary_matches_the_stakes_vocabulary():
    from clagentic_loadout.acquire import contract

    assert (
        contract.VISIBILITY_PUBLIC,
        contract.VISIBILITY_PRIVATE,
        contract.VISIBILITY_INTERNAL,
    ) == ("public", "private", "internal")
    assert set(VISIBILITIES) == {"public", "private", "internal"}


def test_the_github_backend_reports_the_visibility_of_the_base_repository(monkeypatch):
    from clagentic_loadout.acquire import github_backend

    pr_info = {"base": {"sha": "a" * 40, "repo": {"private": True}}, "head": {"sha": "b" * 40}}

    def fake_get(url, token, *, accept="application/vnd.github+json", opener=None):
        if url.endswith("/files"):
            return 200, []
        if accept == "application/vnd.github.v3.diff":
            return 200, ""
        return 200, pr_info

    monkeypatch.setattr(github_backend, "_github_get", fake_get)

    assert github_backend.fetch_pr_content("o", "r", 1, "token").repo_visibility == "private"


def test_the_forgejo_backend_reports_the_visibility_of_the_base_repository(monkeypatch):
    from clagentic_loadout.acquire import forgejo_backend

    pr_info = {"base": {"sha": "a" * 40, "repo": {"private": False}}, "head": {"sha": "b" * 40}}
    monkeypatch.setattr(forgejo_backend, "_get_pr_info", lambda *a, **k: pr_info)
    monkeypatch.setattr(forgejo_backend, "_get_diff_text", lambda *a, **k: "")
    monkeypatch.setattr(forgejo_backend, "_get_changed_files", lambda *a, **k: [])

    acquired = forgejo_backend.fetch_pr_content("http://host.invalid", "token", "o", "r", 1)

    assert acquired.repo_visibility == "public"


def test_a_pr_without_a_repository_record_has_no_visibility():
    from clagentic_loadout.acquire.contract import AcquiredPr

    assert AcquiredPr("o", "r", 1, "a" * 40, "b" * 40).repo_visibility is None
