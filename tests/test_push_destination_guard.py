"""push.destination_guard: which git destinations may receive the minted token."""

from __future__ import annotations

import pytest

from clagentic_loadout.push import destination_guard
from clagentic_loadout.push.destination_guard import (
    CATEGORY_NOT_CONFIGURED,
    CATEGORY_OFF_HOST,
    CATEGORY_UNRESOLVED,
    CATEGORY_UNSUPPORTED_TRANSPORT,
    DestinationDeniedError,
    check_push_destination,
    fetch_destination_is_anchored,
)
from tests._gate_repo import git, seed_base_commit

ANCHOR = "https://git-host.example.com:3000"
GITHUB_ANCHOR = "https://github.com"


@pytest.fixture
def tree(tmp_path):
    path = tmp_path / "tree"
    seed_base_commit(path)
    return path


def _remote(tree, url, *, push_urls=(), name="origin"):
    git(tree, "remote", "add", name, url)
    for push_url in push_urls:
        git(tree, "config", "--add", f"remote.{name}.pushurl", push_url)


@pytest.mark.parametrize(
    ("url", "anchor"),
    [
        ("https://git-host.example.com:3000/o/r.git", ANCHOR),
        ("http://GIT-HOST.example.com:3000/o/r", ANCHOR),
        ("https://user@git-host.example.com:3000/o/r.git", ANCHOR),
        ("https://github.com/o/r.git", GITHUB_ANCHOR),
        ("https://github.com:443/o/r.git", GITHUB_ANCHOR),
        ("file:///srv/git/r.git", ANCHOR),
        ("/srv/git/r.git", ANCHOR),
        ("../sibling/r.git", ANCHOR),
        ("relative/dir/with:colon", ANCHOR),
    ],
)
def test_destinations_that_may_receive_the_token(tree, url, anchor):
    _remote(tree, url)

    check_push_destination("origin", anchor_url=anchor, git_cwd=tree)

    assert fetch_destination_is_anchored("origin", anchor_url=anchor, git_cwd=tree)


@pytest.mark.parametrize(
    ("url", "category"),
    [
        ("https://other.example.com:3000/o/r.git", CATEGORY_OFF_HOST),
        ("https://git-host.example.com/o/r.git", CATEGORY_OFF_HOST),
        ("https://git-host.example.com:3001/o/r.git", CATEGORY_OFF_HOST),
        ("http://git-host.example.com@127.0.0.1:3000/o/r.git", CATEGORY_OFF_HOST),
        ("http://127.0.0.1:1/o/r.git", CATEGORY_OFF_HOST),
        ("https://git-host.example.com:3000\\@127.0.0.1/o/r.git", CATEGORY_UNRESOLVED),
        ("ssh://git@git-host.example.com:3000/o/r.git", CATEGORY_UNSUPPORTED_TRANSPORT),
        ("git://git-host.example.com/o/r.git", CATEGORY_UNSUPPORTED_TRANSPORT),
        ("ftp://git-host.example.com/o/r.git", CATEGORY_UNSUPPORTED_TRANSPORT),
        ("ext::sh -c true", CATEGORY_UNSUPPORTED_TRANSPORT),
        ("http::https://git-host.example.com:3000/o/r.git", CATEGORY_UNSUPPORTED_TRANSPORT),
        ("git@git-host.example.com:o/r.git", CATEGORY_UNSUPPORTED_TRANSPORT),
        ("git-host.example.com:o/r.git", CATEGORY_UNSUPPORTED_TRANSPORT),
    ],
)
def test_destinations_that_are_refused(tree, url, category):
    _remote(tree, url)

    with pytest.raises(DestinationDeniedError) as excinfo:
        check_push_destination("origin", anchor_url=ANCHOR, git_cwd=tree)

    assert excinfo.value.category == category
    assert not fetch_destination_is_anchored("origin", anchor_url=ANCHOR, git_cwd=tree)


def test_a_name_that_is_not_a_configured_remote_is_refused(tree):
    _remote(tree, "https://git-host.example.com:3000/o/r.git")

    for value in ("https://git-host.example.com:3000/o/r.git", "missing", "."):
        with pytest.raises(DestinationDeniedError) as excinfo:
            check_push_destination(value, anchor_url=ANCHOR, git_cwd=tree)
        assert excinfo.value.category == CATEGORY_NOT_CONFIGURED
        assert not fetch_destination_is_anchored(value, anchor_url=ANCHOR, git_cwd=tree)


def test_every_pushurl_is_checked_not_just_the_first(tree):
    _remote(
        tree,
        "https://git-host.example.com:3000/o/r.git",
        push_urls=["https://git-host.example.com:3000/o/r.git", "https://other.example.com/o/r.git"],
    )

    with pytest.raises(DestinationDeniedError) as excinfo:
        check_push_destination("origin", anchor_url=ANCHOR, git_cwd=tree)

    assert excinfo.value.category == CATEGORY_OFF_HOST
    assert fetch_destination_is_anchored("origin", anchor_url=ANCHOR, git_cwd=tree)


def test_push_and_fetch_destinations_are_judged_separately(tree):
    """An off-host fetch url with an anchored push url fails the fetch check
    only; the reverse fails the push check only."""
    _remote(
        tree,
        "https://other.example.com/o/r.git",
        push_urls=["https://git-host.example.com:3000/o/r.git"],
    )

    check_push_destination("origin", anchor_url=ANCHOR, git_cwd=tree)
    assert not fetch_destination_is_anchored("origin", anchor_url=ANCHOR, git_cwd=tree)


def test_a_global_insteadof_rule_is_not_applied_when_resolving(tree, tmp_path, monkeypatch):
    """The credentialed subprocess runs without global config, so the check
    must resolve the destination the same way: a user-level rewrite that
    would hide an off-host url (or invent one) is ignored."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        '[url "https://other.example.com/"]\n\tinsteadOf = https://git-host.example.com:3000/\n'
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    _remote(tree, "https://git-host.example.com:3000/o/r.git")

    check_push_destination("origin", anchor_url=ANCHOR, git_cwd=tree)


def test_an_unparseable_anchor_is_refused(tree):
    _remote(tree, "https://git-host.example.com:3000/o/r.git")

    with pytest.raises(DestinationDeniedError) as excinfo:
        check_push_destination("origin", anchor_url="", git_cwd=tree)

    assert excinfo.value.category == CATEGORY_UNRESOLVED


def test_refusal_messages_name_a_category_never_a_value(tree):
    secret = "FAKETOKEN0123456789"
    for url in (
        f"https://user:{secret}@other.example.com/o/r.git",
        f"ssh://{secret}@other.example.com/o/r.git",
        f"/local/{secret}/r.git?token={secret}\x01",
    ):
        git(tree, "remote", "add", "probe", url)
        with pytest.raises(DestinationDeniedError) as excinfo:
            check_push_destination("probe", anchor_url=ANCHOR, git_cwd=tree)
        git(tree, "remote", "remove", "probe")
        assert secret not in str(excinfo.value)
        assert "example.com" not in str(excinfo.value)


def test_every_category_has_a_message():
    for category in (
        CATEGORY_NOT_CONFIGURED,
        CATEGORY_OFF_HOST,
        CATEGORY_UNRESOLVED,
        CATEGORY_UNSUPPORTED_TRANSPORT,
    ):
        assert str(DestinationDeniedError(category))
    assert destination_guard.__all__
