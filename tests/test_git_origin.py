"""git_origin: the anchored origin parser and the tree binding built on it."""

from __future__ import annotations

import dataclasses

import pytest

from clagentic_loadout.git_origin import (
    ConfigTreeBinding,
    OriginBindingError,
    host_of,
    parse_origin_url,
    read_origin_target,
)
from tests._gate_repo import git

HOST = "git-host.example.test"


@pytest.mark.parametrize(
    "url",
    [
        f"https://{HOST}/some-owner/some-repo",
        f"https://{HOST}/some-owner/some-repo.git",
        f"https://{HOST}/some-owner/some-repo/",
        f"https://user:secret@{HOST}:3000/some-owner/some-repo.git",
        f"HTTPS://{HOST.upper()}/Some-Owner/Some-Repo.git",
        f"ssh://git@{HOST}:2222/some-owner/some-repo.git",
        f"git@{HOST}:some-owner/some-repo.git",
        f"{HOST}:some-owner/some-repo",
    ],
)
def test_ssh_and_https_forms_of_one_host_parse_to_the_same_target(url):
    target = parse_origin_url(url)

    assert target is not None
    assert (target.host, target.owner.lower(), target.name.lower()) == (
        HOST,
        "some-owner",
        "some-repo",
    )


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "/srv/local/some-owner/some-repo.git",
        "../some-owner/some-repo",
        f"https://{HOST}/some-repo",
        f"https://{HOST}/a/b/c",
        f"https://{HOST}/some-owner/some-repo?ref=x",
        f"https://{HOST}/some-owner/some-repo#frag",
        f"ftp://{HOST}/some-owner/some-repo",
        f"https://{HOST}:notaport/some-owner/some-repo",
        "https:///some-owner/some-repo",
        f"git@{HOST}:some-repo",
        f"git@{HOST}:a/b/c",
    ],
)
def test_a_shape_that_is_not_one_of_the_accepted_remotes_is_unparseable(url):
    assert parse_origin_url(url) is None


@pytest.mark.parametrize(
    "url, expected_host",
    [
        # userinfo naming the expected host must not make it the host
        (f"https://{HOST}@other.example.test/some-owner/some-repo", "other.example.test"),
        # a host that merely contains the expected one
        (f"https://not{HOST}/some-owner/some-repo", f"not{HOST}"),
        (f"https://{HOST}.other.example.test/some-owner/some-repo", f"{HOST}.other.example.test"),
    ],
)
def test_the_host_is_the_parsed_hostname_never_a_substring(url, expected_host):
    target = parse_origin_url(url)

    assert target is not None and target.host == expected_host
    assert target.host != HOST


def test_the_dot_github_org_profile_repository_parses_and_dot_segments_do_not():
    target = parse_origin_url(f"https://{HOST}/some-owner/.github.git")

    assert target is not None and target.name == ".github"
    assert parse_origin_url(f"https://{HOST}/some-owner/..") is None
    assert parse_origin_url(f"git@{HOST}:some-owner/.") is None


def test_host_of_reads_a_bare_host_or_a_url():
    assert host_of("Git-Host.Example.Test") == HOST
    assert host_of(f"http://{HOST}:3000") == HOST
    assert host_of("") == ""


def _clone(tmp_path, origin_url, name="tree"):
    tree = tmp_path / name
    tree.mkdir()
    git(tree, "init", "-q", "-b", "main")
    git(tree, "commit", "-q", "--allow-empty", "-m", "base")
    if origin_url is not None:
        git(tree, "remote", "add", "origin", origin_url)
    return tree


def test_read_origin_target_reads_origin_at_the_git_top_level_from_a_subdirectory(tmp_path):
    tree = _clone(tmp_path, f"git@{HOST}:some-owner/some-repo.git")
    (tree / "sub").mkdir()

    target = read_origin_target(tree / "sub")

    assert target.slug == "some-owner/some-repo"
    assert target.top_level == str(tree.resolve())


@pytest.mark.parametrize(
    "origin_url, needle",
    [
        (None, "no readable 'origin'"),
        ("/srv/mirror/some-owner/some-repo.git", "not a parseable remote URL"),
    ],
)
def test_an_unreadable_or_unparseable_origin_is_an_error_naming_the_tree(tmp_path, origin_url, needle):
    tree = _clone(tmp_path, origin_url)

    with pytest.raises(OriginBindingError) as info:
        read_origin_target(tree)

    assert needle in str(info.value) and str(tree.resolve()) in str(info.value)


def test_a_directory_outside_any_git_tree_is_an_error_naming_it(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(OriginBindingError, match="not inside a git work tree"):
        read_origin_target(plain)


def test_a_missing_path_is_an_error_naming_it(tmp_path):
    with pytest.raises(OriginBindingError, match="does not exist"):
        read_origin_target(tmp_path / "missing")


@pytest.mark.parametrize(
    "origin_url",
    [
        f"https://{HOST}/Some-Owner/Some-Repo.git",
        f"git@{HOST}:some-owner/some-repo.git",
        f"ssh://git@{HOST}:2222/some-owner/some-repo",
    ],
)
def test_binding_confirms_a_clone_on_the_expected_host_by_either_remote_form(tmp_path, origin_url):
    tree = _clone(tmp_path, origin_url)
    binding = ConfigTreeBinding(expected_host=f"http://{HOST}:3000", slug="some-owner/some-repo")

    assert binding.confirm(tree).host == HOST


def test_binding_refuses_the_same_owner_and_name_on_another_host_naming_both(tmp_path):
    tree = _clone(tmp_path, "https://other.example.test/some-owner/some-repo.git")
    binding = ConfigTreeBinding(expected_host=HOST, slug="some-owner/some-repo")

    with pytest.raises(OriginBindingError) as info:
        binding.confirm(tree)

    message = str(info.value)
    assert "other.example.test" in message and HOST in message
    assert "'some-owner/some-repo'" in message and "origin" in message
    assert "https://" not in message


def test_binding_refuses_a_different_repository_on_the_expected_host(tmp_path):
    tree = _clone(tmp_path, f"https://{HOST}/some-owner/another-repo")
    binding = ConfigTreeBinding(expected_host=HOST, slug="some-owner/some-repo")

    with pytest.raises(OriginBindingError, match="another-repo"):
        binding.confirm(tree)


def test_binding_reads_origin_not_the_tracking_remote(tmp_path):
    tree = _clone(tmp_path, f"https://{HOST}/some-owner/some-repo")
    git(tree, "remote", "add", "upstream", "https://other.example.test/some-owner/some-repo")
    git(tree, "config", "branch.main.remote", "upstream")
    git(tree, "config", "branch.main.merge", "refs/heads/main")
    binding = ConfigTreeBinding(expected_host=HOST, slug="some-owner/some-repo")

    assert binding.confirm(tree).host == HOST


def test_binding_refuses_when_origin_is_the_foreign_host_even_if_the_tracking_remote_is_right(tmp_path):
    tree = _clone(tmp_path, "https://other.example.test/some-owner/some-repo")
    git(tree, "remote", "add", "upstream", f"https://{HOST}/some-owner/some-repo")
    git(tree, "config", "branch.main.remote", "upstream")
    git(tree, "config", "branch.main.merge", "refs/heads/main")
    binding = ConfigTreeBinding(expected_host=HOST, slug="some-owner/some-repo")

    with pytest.raises(OriginBindingError):
        binding.confirm(tree)


def test_binding_with_no_expected_host_refuses_rather_than_passing(tmp_path):
    tree = _clone(tmp_path, f"https://{HOST}/some-owner/some-repo")

    with pytest.raises(OriginBindingError, match="names no host"):
        ConfigTreeBinding(expected_host="", slug="some-owner/some-repo").confirm(tree)


def test_the_url_echoing_helpers_and_the_target_url_field_are_gone():
    import clagentic_loadout.git_origin as git_origin

    for name in ("redact_remote_url", "describe_origin"):
        assert not hasattr(git_origin, name), name
        assert name not in git_origin.__all__
    target = parse_origin_url(f"https://user:secret@{HOST}/some-owner/some-repo.git")
    assert target is not None
    assert "url" not in {field.name for field in dataclasses.fields(target)}
    assert not hasattr(target, "url")
    assert "secret" not in repr(target)


@pytest.mark.parametrize(
    "url",
    [
        f"https://{HOST}/some-owner/some-repo?token=secret",
        f"https://{HOST}/some-owner/some-repo#secret",
        f"ssh://git@{HOST}/some-owner/some-repo?secret",
        f"git@{HOST}:some-owner/some-repo?token=secret",
        f"git@{HOST}:some-owner/some-repo#secret",
        f"git@{HOST}:some-owner/secret/some-repo",
        "secret/not-a-remote",
    ],
    ids=["https-query", "https-fragment", "ssh-query", "scp-query", "scp-fragment", "scp-path", "junk"],
)
def test_an_unparseable_origin_is_reported_without_its_text(tmp_path, url):
    tree = _clone(tmp_path, url)

    with pytest.raises(OriginBindingError) as info:
        ConfigTreeBinding(HOST, "some-owner/some-repo").confirm(tree)

    message = str(info.value)
    assert "secret" not in message
    assert "not a parseable remote URL" in message and str(tree.resolve()) in message


def test_the_binding_error_for_an_unparseable_origin_names_the_configured_host(tmp_path):
    tree = _clone(tmp_path, f"https://user:secret@{HOST}/a/b/c/d")

    with pytest.raises(OriginBindingError) as info:
        ConfigTreeBinding(HOST, "some-owner/some-repo").confirm(tree)

    assert "secret" not in str(info.value) and repr(HOST) in str(info.value)
