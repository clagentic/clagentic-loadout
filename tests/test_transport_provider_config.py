"""test_transport_provider_config.py — tests for
clagentic_loadout.transport.provider_config (lr-af6e, lr-0818).

Coverage:
  - default (no env, no config file) resolves to StaticTokenProvider for
    both platforms -- unchanged behavior from before this task.
  - per-platform independence: forgejo and github can each select a
    different provider kind/command simultaneously.
  - precedence: env var wins over the user-level config-file value.
  - user-level config-file-only selection (<config_root>/config.yaml
    credentials: section) (lr-0818).
  - a repo-local .loadout/config.yaml credentials: section is REJECTED
    (never consulted for selection) and a warning is printed to stderr even
    when present (lr-0818 -- the PR #22 hostile-repo committed-config
    finding).
  - failure modes: unrecognized provider kind, "command" kind with no
    command configured.
  - no-lore conformance: this module imports nothing from lore and needs no
    lore process present to resolve any provider (asserted by these tests
    running with no LORE_* env vars and no ~/.lore path involved at all).
"""

from __future__ import annotations

import sys

import pytest
import yaml

from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.transport import provider_config
from clagentic_loadout.transport.credential_provider import (
    CommandTokenProvider,
    StaticTokenProvider,
)
from clagentic_loadout.transport.credential_provider import ResolvedToken
from clagentic_loadout.transport.provider_config import (
    CONFIG_KEY_COMMAND_EMITS_JSON_FORGEJO,
    CONFIG_KEY_COMMAND_EMITS_JSON_GITHUB,
    CONFIG_KEY_COMMAND_FORGEJO,
    CONFIG_KEY_COMMAND_GITHUB,
    CONFIG_KEY_PROVIDER_FORGEJO,
    CONFIG_KEY_PROVIDER_GITHUB,
    CONFIG_SECTION_CREDENTIALS,
    PROVIDER_KIND_COMMAND,
    PROVIDER_KIND_STATIC,
    TOKEN_COMMAND_EMITS_JSON_ENV_VAR_FORGEJO,
    TOKEN_COMMAND_EMITS_JSON_ENV_VAR_GITHUB,
    TOKEN_COMMAND_ENV_VAR_FORGEJO,
    TOKEN_COMMAND_ENV_VAR_GITHUB,
    TOKEN_PROVIDER_ENV_VAR_FORGEJO,
    TOKEN_PROVIDER_ENV_VAR_GITHUB,
    USER_CONFIG_FILENAME,
    InvalidProviderConfigError,
    resolve_platform_provider,
)

_PY = sys.executable


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    """Belt-and-suspenders isolation (lr-a7c2): every test in this module
    already pins `config_root=` explicitly on each `resolve_platform_provider`
    call it cares about, but two tests (`test_no_repo_root_no_warning`,
    `test_repo_local_credentials_section_warns_even_when_env_selects_command`)
    omit it, so `_load_credentials_section` falls through to
    `DEFAULT_USER_CONFIG_ROOT` -- the REAL `~/.config/clagentic/loadout/`
    directory. On any host that has ever written a real deployment
    config.yaml there, that read picks up a live `credentials:` section and
    breaks the two tests' assertions (verified: renaming the real config
    file makes the full suite pass; restoring it fails 2 tests).

    This autouse fixture monkeypatches the module-level default so that ANY
    call in this file that fails to pass an explicit `config_root=` still
    resolves to an empty, per-test tmp directory rather than the real user
    config -- a conformance backstop against future tests regressing into
    the same host-state leak, on top of (not instead of) pinning
    `config_root=tmp_path` explicitly at each call site."""
    isolated_root = tmp_path / "isolated-user-config-root"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", isolated_root)


# argv[1] (the first fixed marker arg baked into the configured command) --
# NOT argv[-1], since resolve_token appends the role as the FINAL arg when
# no {role} placeholder is present, which would otherwise overwrite a
# marker meant to identify which configured command actually ran.
_ECHO_CODE = "import sys; sys.stdout.write('tok-' + sys.argv[1] + '\\n')"


class TestDefaultIsStaticUnchanged:
    def test_forgejo_defaults_to_static_with_no_signal(self, tmp_path):
        provider = resolve_platform_provider(
            PLATFORM_FORGEJO, config_root=tmp_path, env={}, static_config_root=tmp_path
        )
        assert isinstance(provider, StaticTokenProvider)

    def test_github_defaults_to_static_with_no_signal(self, tmp_path):
        provider = resolve_platform_provider(
            PLATFORM_GITHUB, config_root=tmp_path, env={}, static_config_root=tmp_path
        )
        assert isinstance(provider, StaticTokenProvider)

    def test_no_repo_root_still_returns_static_default(self, tmp_path):
        provider = resolve_platform_provider(
            PLATFORM_FORGEJO, repo_root=None, config_root=tmp_path, env={}
        )
        assert isinstance(provider, StaticTokenProvider)


class TestPerPlatformIndependence:
    def test_forgejo_command_github_static_simultaneously(self, tmp_path):
        env = {
            TOKEN_PROVIDER_ENV_VAR_FORGEJO: PROVIDER_KIND_COMMAND,
            TOKEN_COMMAND_ENV_VAR_FORGEJO: f"{_PY} -c \"{_ECHO_CODE}\"",
        }
        forgejo_provider = resolve_platform_provider(
            PLATFORM_FORGEJO, config_root=tmp_path, env=env, static_config_root=tmp_path
        )
        github_provider = resolve_platform_provider(
            PLATFORM_GITHUB, config_root=tmp_path, env=env, static_config_root=tmp_path
        )
        assert isinstance(forgejo_provider, CommandTokenProvider)
        assert isinstance(github_provider, StaticTokenProvider)

    def test_both_platforms_command_with_different_commands(self, tmp_path):
        env = {
            TOKEN_PROVIDER_ENV_VAR_FORGEJO: PROVIDER_KIND_COMMAND,
            TOKEN_COMMAND_ENV_VAR_FORGEJO: f"{_PY} -c \"{_ECHO_CODE}\" forgejo-marker",
            TOKEN_PROVIDER_ENV_VAR_GITHUB: PROVIDER_KIND_COMMAND,
            TOKEN_COMMAND_ENV_VAR_GITHUB: f"{_PY} -c \"{_ECHO_CODE}\" github-marker",
        }
        forgejo_provider = resolve_platform_provider(PLATFORM_FORGEJO, config_root=tmp_path, env=env)
        github_provider = resolve_platform_provider(PLATFORM_GITHUB, config_root=tmp_path, env=env)
        assert forgejo_provider.resolve_token("role") == "tok-forgejo-marker"
        assert github_provider.resolve_token("role") == "tok-github-marker"


class TestPrecedenceEnvOverConfigFile:
    def test_env_wins_over_user_level_config_file(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump(
                {
                    CONFIG_SECTION_CREDENTIALS: {
                        CONFIG_KEY_PROVIDER_FORGEJO: PROVIDER_KIND_COMMAND,
                        CONFIG_KEY_COMMAND_FORGEJO: "some-config-file-command --role",
                    }
                }
            )
        )
        env = {TOKEN_PROVIDER_ENV_VAR_FORGEJO: PROVIDER_KIND_STATIC}
        provider = resolve_platform_provider(
            PLATFORM_FORGEJO, config_root=tmp_path, env=env, static_config_root=tmp_path
        )
        # env says static -- must win over the user-level config file's
        # "command".
        assert isinstance(provider, StaticTokenProvider)


class TestConfigFileOnlySelection:
    """The credentials config-file tier reads the USER-LEVEL config root
    (<config_root>/config.yaml), never a repo-local .loadout/config.yaml
    (lr-0818)."""

    def test_user_level_config_file_selects_command_provider(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump(
                {
                    CONFIG_SECTION_CREDENTIALS: {
                        CONFIG_KEY_PROVIDER_GITHUB: PROVIDER_KIND_COMMAND,
                        CONFIG_KEY_COMMAND_GITHUB: f'{_PY} -c "{_ECHO_CODE}" from-config',
                    }
                }
            )
        )
        provider = resolve_platform_provider(PLATFORM_GITHUB, config_root=tmp_path, env={})
        assert isinstance(provider, CommandTokenProvider)
        assert provider.resolve_token("role") == "tok-from-config"

    def test_missing_config_file_falls_back_to_static(self, tmp_path):
        provider = resolve_platform_provider(
            PLATFORM_FORGEJO, config_root=tmp_path, env={}, static_config_root=tmp_path
        )
        assert isinstance(provider, StaticTokenProvider)

    def test_config_file_present_but_no_credentials_section(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(yaml.safe_dump({"wait": {"scoped_test_patterns": []}}))
        provider = resolve_platform_provider(
            PLATFORM_FORGEJO, config_root=tmp_path, env={}, static_config_root=tmp_path
        )
        assert isinstance(provider, StaticTokenProvider)


class TestRepoLocalCredentialsRejected:
    """A repo-local .loadout/config.yaml credentials: section must never be
    consulted for provider selection, and its presence must be surfaced with
    a stderr warning rather than silently ignored (lr-0818, PR #22 audit
    finding: a cloned hostile repo's committed config must never be able to
    name the command the credential factory execs)."""

    def test_repo_local_credentials_section_ignored_for_selection(self, tmp_path):
        repo_root = tmp_path / "repo"
        config_path = repo_root / ".loadout" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            yaml.safe_dump(
                {
                    CONFIG_SECTION_CREDENTIALS: {
                        CONFIG_KEY_PROVIDER_FORGEJO: PROVIDER_KIND_COMMAND,
                        CONFIG_KEY_COMMAND_FORGEJO: "some-hostile-repo-command --role",
                    }
                }
            )
        )
        user_config_root = tmp_path / "user-config"
        # No user-level config.yaml present -- default (static) must win;
        # the repo-local "command" selection must NOT be honored.
        provider = resolve_platform_provider(
            PLATFORM_FORGEJO,
            repo_root=repo_root,
            config_root=user_config_root,
            env={},
            static_config_root=user_config_root,
        )
        assert isinstance(provider, StaticTokenProvider)

    def test_repo_local_credentials_section_warns_to_stderr(self, tmp_path, capsys):
        repo_root = tmp_path / "repo"
        config_path = repo_root / ".loadout" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            yaml.safe_dump(
                {
                    CONFIG_SECTION_CREDENTIALS: {
                        CONFIG_KEY_PROVIDER_FORGEJO: PROVIDER_KIND_COMMAND,
                        CONFIG_KEY_COMMAND_FORGEJO: "some-hostile-repo-command --role",
                    }
                }
            )
        )
        user_config_root = tmp_path / "user-config"
        resolve_platform_provider(
            PLATFORM_FORGEJO,
            repo_root=repo_root,
            config_root=user_config_root,
            env={},
            static_config_root=user_config_root,
        )
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert CONFIG_SECTION_CREDENTIALS in captured.err
        assert str(config_path) in captured.err

    def test_repo_local_credentials_section_warns_even_when_env_selects_command(self, tmp_path, capsys):
        # Even when env vars legitimately select "command" (so the resolved
        # provider IS a CommandTokenProvider), a repo-local credentials:
        # section is still an unrelated misconfiguration worth surfacing.
        repo_root = tmp_path / "repo"
        config_path = repo_root / ".loadout" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            yaml.safe_dump({CONFIG_SECTION_CREDENTIALS: {CONFIG_KEY_PROVIDER_FORGEJO: PROVIDER_KIND_COMMAND}})
        )
        env = {
            TOKEN_PROVIDER_ENV_VAR_FORGEJO: PROVIDER_KIND_COMMAND,
            TOKEN_COMMAND_ENV_VAR_FORGEJO: f"{_PY} -c \"{_ECHO_CODE}\" env-marker",
        }
        provider = resolve_platform_provider(
            PLATFORM_FORGEJO, repo_root=repo_root, config_root=tmp_path, env=env
        )
        assert isinstance(provider, CommandTokenProvider)
        assert provider.resolve_token("role") == "tok-env-marker"
        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    def test_no_repo_root_no_warning(self, tmp_path, capsys):
        provider = resolve_platform_provider(
            PLATFORM_FORGEJO,
            repo_root=None,
            config_root=tmp_path,
            env={},
            static_config_root=tmp_path,
        )
        assert isinstance(provider, StaticTokenProvider)
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_repo_local_config_with_empty_credentials_section_no_warning(self, tmp_path, capsys):
        repo_root = tmp_path / "repo"
        config_path = repo_root / ".loadout" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(yaml.safe_dump({CONFIG_SECTION_CREDENTIALS: {}}))
        provider = resolve_platform_provider(
            PLATFORM_FORGEJO,
            repo_root=repo_root,
            config_root=tmp_path,
            env={},
            static_config_root=tmp_path,
        )
        assert isinstance(provider, StaticTokenProvider)
        captured = capsys.readouterr()
        assert captured.err == ""


class TestFailureModes:
    def test_unrecognized_provider_kind_raises(self, tmp_path):
        env = {TOKEN_PROVIDER_ENV_VAR_FORGEJO: "not-a-real-kind"}
        with pytest.raises(InvalidProviderConfigError, match="not recognized"):
            resolve_platform_provider(PLATFORM_FORGEJO, config_root=tmp_path, env=env)

    def test_command_kind_with_no_command_configured_raises(self, tmp_path):
        env = {TOKEN_PROVIDER_ENV_VAR_GITHUB: PROVIDER_KIND_COMMAND}
        with pytest.raises(InvalidProviderConfigError, match="no command is configured"):
            resolve_platform_provider(PLATFORM_GITHUB, config_root=tmp_path, env=env)

    def test_unrecognized_platform_raises(self, tmp_path):
        with pytest.raises(InvalidProviderConfigError, match="not recognized"):
            resolve_platform_provider("bitbucket", config_root=tmp_path, env={})

    def test_command_kind_with_blank_command_string_raises(self, tmp_path):
        env = {
            TOKEN_PROVIDER_ENV_VAR_FORGEJO: PROVIDER_KIND_COMMAND,
            TOKEN_COMMAND_ENV_VAR_FORGEJO: "   ",
        }
        with pytest.raises(InvalidProviderConfigError, match="no command is configured"):
            resolve_platform_provider(PLATFORM_FORGEJO, config_root=tmp_path, env=env)


class TestShlexQuoting:
    def test_quoted_argument_with_spaces_splits_correctly(self, tmp_path):
        # argv[1] is the first fixed marker arg from the configured command
        # (a shlex-quoted single argument containing a space) -- not
        # argv[-1], which would be the appended role instead.
        code = "import sys; sys.stdout.write(sys.argv[1] + '\\n')"
        env = {
            TOKEN_PROVIDER_ENV_VAR_FORGEJO: PROVIDER_KIND_COMMAND,
            TOKEN_COMMAND_ENV_VAR_FORGEJO: f'{_PY} -c "{code}" "two words"',
        }
        provider = resolve_platform_provider(PLATFORM_FORGEJO, config_root=tmp_path, env=env)
        assert provider.resolve_token("role") == "two words"


_JSON_ECHO_CODE = (
    "import json, sys; "
    "sys.stdout.write(json.dumps({'token': 'tok-json', 'app_slug': sys.argv[1]}))"
)


class TestStructuredOutputOptIn:
    """lr-43c8d7: token_command_emits_json_forgejo/_github (config) and
    CLAGENTIC_LOADOUT_TOKEN_COMMAND_EMITS_JSON_FORGEJO/_GITHUB (env) --
    opt-in only, default False (byte-identical bare-token behavior)."""

    def test_default_false_returns_bare_token_provider_unaffected(self, tmp_path):
        env = {
            TOKEN_PROVIDER_ENV_VAR_FORGEJO: PROVIDER_KIND_COMMAND,
            TOKEN_COMMAND_ENV_VAR_FORGEJO: f'{_PY} -c "{_JSON_ECHO_CODE}" some-slug',
        }
        provider = resolve_platform_provider(PLATFORM_FORGEJO, config_root=tmp_path, env=env)
        # Bare-stdout read -- the whole JSON-looking string comes back as
        # the "token" itself, proving no parsing was attempted by default.
        result = provider.resolve_token("role")
        assert result == '{"token": "tok-json", "app_slug": "some-slug"}'

    def test_env_var_enables_structured_output(self, tmp_path):
        env = {
            TOKEN_PROVIDER_ENV_VAR_GITHUB: PROVIDER_KIND_COMMAND,
            TOKEN_COMMAND_ENV_VAR_GITHUB: f'{_PY} -c "{_JSON_ECHO_CODE}" verified-slug',
            TOKEN_COMMAND_EMITS_JSON_ENV_VAR_GITHUB: "true",
        }
        provider = resolve_platform_provider(PLATFORM_GITHUB, config_root=tmp_path, env=env)
        result = provider.resolve_token("role")
        assert result == ResolvedToken(token="tok-json", app_slug="verified-slug")

    def test_config_file_enables_structured_output(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump(
                {
                    CONFIG_SECTION_CREDENTIALS: {
                        CONFIG_KEY_PROVIDER_FORGEJO: PROVIDER_KIND_COMMAND,
                        CONFIG_KEY_COMMAND_FORGEJO: f'{_PY} -c "{_JSON_ECHO_CODE}" from-config',
                        CONFIG_KEY_COMMAND_EMITS_JSON_FORGEJO: True,
                    }
                }
            )
        )
        provider = resolve_platform_provider(PLATFORM_FORGEJO, config_root=tmp_path, env={})
        result = provider.resolve_token("role")
        assert result == ResolvedToken(token="tok-json", app_slug="from-config")

    def test_env_wins_over_config_file_for_emits_json_flag(self, tmp_path):
        """Same precedence rule as the provider-kind/command pair: env wins
        over the config-file value. Config says True, env says false --
        env must win, so bare-stdout is read (proven by the raw JSON string
        coming back unparsed)."""
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump(
                {
                    CONFIG_SECTION_CREDENTIALS: {
                        CONFIG_KEY_PROVIDER_FORGEJO: PROVIDER_KIND_COMMAND,
                        CONFIG_KEY_COMMAND_FORGEJO: f'{_PY} -c "{_JSON_ECHO_CODE}" x',
                        CONFIG_KEY_COMMAND_EMITS_JSON_FORGEJO: True,
                    }
                }
            )
        )
        env = {TOKEN_COMMAND_EMITS_JSON_ENV_VAR_FORGEJO: "false"}
        provider = resolve_platform_provider(PLATFORM_FORGEJO, config_root=tmp_path, env=env)
        result = provider.resolve_token("role")
        assert result == '{"token": "tok-json", "app_slug": "x"}'

    def test_unset_emits_json_key_defaults_false_with_command_config(self, tmp_path):
        """A deployment that sets token_command_forgejo but never sets the
        emits-json key at all must see the pre-existing bare-token
        behavior -- this is the common case (every deployment before this
        task, and any bring-your-own command that just prints a token)."""
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump(
                {
                    CONFIG_SECTION_CREDENTIALS: {
                        CONFIG_KEY_PROVIDER_GITHUB: PROVIDER_KIND_COMMAND,
                        CONFIG_KEY_COMMAND_GITHUB: f'{_PY} -c "{_ECHO_CODE}" plain',
                    }
                }
            )
        )
        provider = resolve_platform_provider(PLATFORM_GITHUB, config_root=tmp_path, env={})
        assert provider.resolve_token("role") == "tok-plain"


class TestNoLoreConformance:
    def test_module_has_no_lore_import(self):
        import clagentic_loadout.transport.provider_config as module

        source = module.__file__
        with open(source, encoding="utf-8") as f:
            content = f.read()
        assert "lore" not in content.lower()
