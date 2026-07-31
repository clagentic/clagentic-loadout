"""test_transport_github_app_config.py — tests for
clagentic_loadout.transport.github_app_config (lr-d31e, lr-d72d).

Coverage:
  - env var (CLAGENTIC_LOADOUT_GITHUB_APP_SLUG) resolves the slug directly.
  - user-level config-file-only selection (<config_root>/config.yaml
    `github_app.slug` key), same precedence shape as
    transport.provider_config's `credentials:` tier.
  - precedence: env var wins over the user-level config-file value.
  - unconfigured (neither source) raises GithubAppSlugNotConfiguredError,
    naming both config seams in the message.
  - malformed/absent config file degrades to "no signal" rather than
    raising, matching provider_config's treatment of an optional config
    file.
  - lr-d72d: per-caller `github_app.slugs.<caller>` map, full precedence
    (env > per-caller > global fallback), a caller with no matching entry
    falling through to the global slug, and omitting `caller` entirely
    staying byte-identical to pre-lr-d72d behavior.
"""

from __future__ import annotations

import yaml

from clagentic_loadout.transport.github_app_config import (
    CONFIG_KEY_CALLERS,
    CONFIG_KEY_SLUG,
    CONFIG_KEY_SLUGS,
    CONFIG_SECTION_GITHUB_APP,
    GITHUB_APP_SLUG_ENV_VAR,
    USER_CONFIG_FILENAME,
    GithubAppSlugNotConfiguredError,
    read_configured_callers,
    resolve_github_app_slug,
)

import pytest


class TestEnvVarSelection:
    def test_env_var_resolves_slug(self, tmp_path):
        slug = resolve_github_app_slug(
            config_root=tmp_path, env={GITHUB_APP_SLUG_ENV_VAR: "my-review-app"}
        )
        assert slug == "my-review-app"

    def test_env_var_whitespace_only_treated_as_unset(self, tmp_path):
        with pytest.raises(GithubAppSlugNotConfiguredError):
            resolve_github_app_slug(
                config_root=tmp_path, env={GITHUB_APP_SLUG_ENV_VAR: "   "}
            )


class TestConfigFileOnlySelection:
    """The github_app config tier reads the USER-LEVEL config root
    (<config_root>/config.yaml), mirroring provider_config's credentials
    tier precedence shape exactly."""

    def test_user_level_config_file_selects_slug(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump({CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_SLUG: "config-file-app"}})
        )
        slug = resolve_github_app_slug(config_root=tmp_path, env={})
        assert slug == "config-file-app"

    def test_missing_config_file_raises_not_configured(self, tmp_path):
        with pytest.raises(GithubAppSlugNotConfiguredError):
            resolve_github_app_slug(config_root=tmp_path, env={})

    def test_config_file_present_but_no_github_app_section(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(yaml.safe_dump({"credentials": {}}))
        with pytest.raises(GithubAppSlugNotConfiguredError):
            resolve_github_app_slug(config_root=tmp_path, env={})

    def test_malformed_config_file_degrades_to_not_configured(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text("not: valid: yaml: [")
        with pytest.raises(GithubAppSlugNotConfiguredError):
            resolve_github_app_slug(config_root=tmp_path, env={})

    def test_config_file_slug_empty_string_treated_as_unset(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump({CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_SLUG: ""}})
        )
        with pytest.raises(GithubAppSlugNotConfiguredError):
            resolve_github_app_slug(config_root=tmp_path, env={})


class TestPrecedenceEnvOverConfigFile:
    def test_env_wins_over_user_level_config_file(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump({CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_SLUG: "config-file-app"}})
        )
        slug = resolve_github_app_slug(
            config_root=tmp_path, env={GITHUB_APP_SLUG_ENV_VAR: "env-app"}
        )
        assert slug == "env-app"


class TestUnconfiguredErrorMessage:
    def test_error_names_both_config_seams(self, tmp_path):
        with pytest.raises(GithubAppSlugNotConfiguredError) as excinfo:
            resolve_github_app_slug(config_root=tmp_path, env={})
        message = str(excinfo.value)
        assert GITHUB_APP_SLUG_ENV_VAR in message
        assert CONFIG_SECTION_GITHUB_APP in message
        assert CONFIG_KEY_SLUG in message


class TestPerCallerSlugMap:
    """lr-d72d: github_app.slugs.<caller> — optional per-caller mapping,
    single global slug as fallback, env var still wins over both."""

    def test_caller_with_matching_entry_resolves_per_caller_slug(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump(
                {
                    CONFIG_SECTION_GITHUB_APP: {
                        CONFIG_KEY_SLUGS: {
                            "reviewer": "reviewer-app",
                            "security": "security-app",
                        }
                    }
                }
            )
        )
        slug = resolve_github_app_slug(caller="reviewer", config_root=tmp_path, env={})
        assert slug == "reviewer-app"

        slug = resolve_github_app_slug(caller="security", config_root=tmp_path, env={})
        assert slug == "security-app"

    def test_caller_with_no_matching_entry_falls_back_to_global_slug(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump(
                {
                    CONFIG_SECTION_GITHUB_APP: {
                        CONFIG_KEY_SLUG: "global-app",
                        CONFIG_KEY_SLUGS: {"reviewer": "reviewer-app"},
                    }
                }
            )
        )
        slug = resolve_github_app_slug(caller="merger", config_root=tmp_path, env={})
        assert slug == "global-app"

    def test_no_caller_supplied_uses_global_slug_unchanged(self, tmp_path):
        """Omitting `caller` entirely is byte-identical to pre-lr-d72d
        behavior even when a `slugs` map is present in config."""
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump(
                {
                    CONFIG_SECTION_GITHUB_APP: {
                        CONFIG_KEY_SLUG: "global-app",
                        CONFIG_KEY_SLUGS: {"reviewer": "reviewer-app"},
                    }
                }
            )
        )
        slug = resolve_github_app_slug(config_root=tmp_path, env={})
        assert slug == "global-app"

    def test_env_var_wins_over_per_caller_entry(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump(
                {CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_SLUGS: {"reviewer": "reviewer-app"}}}
            )
        )
        slug = resolve_github_app_slug(
            caller="reviewer",
            config_root=tmp_path,
            env={GITHUB_APP_SLUG_ENV_VAR: "env-app"},
        )
        assert slug == "env-app"

    def test_slugs_present_but_not_a_mapping_degrades_to_no_signal(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump(
                {
                    CONFIG_SECTION_GITHUB_APP: {
                        CONFIG_KEY_SLUG: "global-app",
                        CONFIG_KEY_SLUGS: "not-a-mapping",
                    }
                }
            )
        )
        slug = resolve_github_app_slug(caller="reviewer", config_root=tmp_path, env={})
        assert slug == "global-app"

    def test_caller_empty_string_treated_as_no_caller(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump(
                {
                    CONFIG_SECTION_GITHUB_APP: {
                        CONFIG_KEY_SLUG: "global-app",
                        CONFIG_KEY_SLUGS: {"": "empty-caller-app"},
                    }
                }
            )
        )
        slug = resolve_github_app_slug(caller="", config_root=tmp_path, env={})
        assert slug == "global-app"

    def test_unconfigured_error_message_names_caller_and_both_seams(self, tmp_path):
        with pytest.raises(GithubAppSlugNotConfiguredError) as excinfo:
            resolve_github_app_slug(caller="reviewer", config_root=tmp_path, env={})
        message = str(excinfo.value)
        assert "reviewer" in message
        assert GITHUB_APP_SLUG_ENV_VAR in message
        assert CONFIG_KEY_SLUGS in message
        assert CONFIG_KEY_SLUG in message


class TestReadConfiguredCallers:
    """lr-46a83a: github_app.callers — the deployment's own declaration of
    the caller key-space github_app.slugs is keyed by (never assumed to be
    provisioning.roles' bare-role-name taxonomy)."""

    def test_absent_key_returns_none(self, tmp_path):
        assert read_configured_callers(config_root=tmp_path) is None

    def test_absent_github_app_section_returns_none(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(yaml.safe_dump({"credentials": {}}))
        assert read_configured_callers(config_root=tmp_path) is None

    def test_declared_callers_list_returned(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump(
                {CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_CALLERS: ["amos", "peaches", "bobbie"]}}
            )
        )
        assert read_configured_callers(config_root=tmp_path) == ["amos", "peaches", "bobbie"]

    def test_not_a_list_returns_none(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump({CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_CALLERS: "not-a-list"}})
        )
        assert read_configured_callers(config_root=tmp_path) is None

    def test_non_string_entry_returns_none(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump({CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_CALLERS: ["amos", 42]}})
        )
        assert read_configured_callers(config_root=tmp_path) is None

    def test_blank_entry_returns_none(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump({CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_CALLERS: ["amos", "  "]}})
        )
        assert read_configured_callers(config_root=tmp_path) is None

    def test_empty_list_is_a_deliberate_no_callers_declaration(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text(
            yaml.safe_dump({CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_CALLERS: []}})
        )
        assert read_configured_callers(config_root=tmp_path) == []

    def test_malformed_config_file_degrades_to_none(self, tmp_path):
        config_path = tmp_path / USER_CONFIG_FILENAME
        config_path.write_text("not: valid: yaml: [")
        assert read_configured_callers(config_root=tmp_path) is None
