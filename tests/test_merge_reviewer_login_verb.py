"""Tests for `loadout-reviewer-login` (merge.reviewer_login_verb): role names
are caller-supplied and invented here, and every login comes from deployment
config through the same derivation the merge gate uses."""

from __future__ import annotations

import pytest

from clagentic_loadout.cli import main as umbrella_main
from clagentic_loadout.merge import reviewer_login_verb as verb
from clagentic_loadout.transport.github_app_config import GithubAppSlugNotConfiguredError

_SLUGS = {"alpha-role": "alpha-app", "beta-role": "beta-app"}


@pytest.fixture
def configured_slugs(monkeypatch):
    """Resolve github slugs from a synthetic registry, never the real user
    config or environment."""

    def resolve(*, caller=None, **_):
        if caller in _SLUGS:
            return _SLUGS[caller]
        raise GithubAppSlugNotConfiguredError(f"no slug for {caller!r}")

    monkeypatch.setattr("clagentic_loadout.merge.reviewer_login.resolve_github_app_slug", resolve)


def test_forgejo_prints_the_bare_name_as_the_login(capsys):
    code = verb.main(["--platform", "forgejo", "alpha-role", "beta-role"])

    assert code == verb.EXIT_OK
    assert capsys.readouterr().out.splitlines() == ["alpha-role", "beta-role"]


def test_github_prints_the_configured_app_slug_as_a_bot_login_in_argument_order(
    capsys, configured_slugs
):
    code = verb.main(["--platform", "github", "beta-role", "alpha-role"])

    assert code == verb.EXIT_OK
    assert capsys.readouterr().out.splitlines() == ["beta-app[bot]", "alpha-app[bot]"]


def test_an_unconfigured_name_exits_named_and_prints_no_partial_list(capsys, configured_slugs):
    code = verb.main(["--platform", "github", "alpha-role", "gamma-role"])

    captured = capsys.readouterr()
    assert code == verb.EXIT_REVIEWER_NOT_CONFIGURED
    assert captured.out == ""
    assert "gamma-role" in captured.err
    assert "github" in captured.err


def test_the_verb_uses_the_resolution_the_merge_gate_uses(monkeypatch, capsys):
    seen: list[tuple[str, str]] = []

    def fake(name, platform):
        seen.append((name, platform))
        return f"login-for-{name}"

    monkeypatch.setattr(verb, "resolve_reviewer_login", fake)

    assert verb.main(["--platform", "forgejo", "alpha-role"]) == verb.EXIT_OK
    assert seen == [("alpha-role", "forgejo")]
    assert capsys.readouterr().out == "login-for-alpha-role\n"


@pytest.mark.parametrize("bad", ["alpha-role:some-login", "../x", "a b", "_lead", ""])
def test_a_name_that_is_not_a_bare_name_is_a_usage_error(bad, capsys):
    code = verb.main(["--platform", "forgejo", "alpha-role", bad])

    captured = capsys.readouterr()
    assert code == verb.EXIT_USAGE
    assert captured.out == ""
    assert repr(bad) in captured.err


@pytest.mark.parametrize(
    "argv",
    [[], ["--platform", "forgejo"], ["alpha-role"], ["--platform", "gitlab", "alpha-role"]],
    ids=["nothing", "no-names", "no-platform", "unknown-platform"],
)
def test_missing_or_unknown_arguments_are_usage_errors(argv):
    assert verb.main(argv) == verb.EXIT_USAGE


def test_the_verb_takes_no_caller_because_it_mints_nothing():
    parser = verb._build_arg_parser()

    assert not any(
        flag in action.option_strings for action in parser._actions for flag in ("--caller", "--role")
    )


def test_the_umbrella_routes_to_the_verb(capsys):
    code = umbrella_main(["reviewer-login", "--platform", "forgejo", "alpha-role"])

    assert code == verb.EXIT_OK
    assert capsys.readouterr().out == "alpha-role\n"


def test_the_verb_never_reaches_a_credential_mint(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("loadout-reviewer-login must not resolve a token")

    monkeypatch.setattr("clagentic_loadout.transport.credential_provider.resolve_token", refuse)

    assert verb.main(["--platform", "forgejo", "alpha-role"]) == verb.EXIT_OK
