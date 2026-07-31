"""test_push_identity_config.py — unit tests for
clagentic_loadout.push.identity_config (lr-0a03c3).

Covers the DEPLOYMENT-TIER (user-level config.yaml, never repo-local)
`builder_identity:` section: absence at every level returns (None, None),
a well-formed section round-trips name/email, and malformed shapes (missing
key, empty string, non-string) raise InvalidBuilderIdentityConfigError.
"""

from __future__ import annotations

import pytest
import yaml

from clagentic_loadout.push.identity_config import (
    CONFIG_KEY_EMAIL,
    CONFIG_KEY_NAME,
    CONFIG_SECTION_BUILDER_IDENTITY,
    InvalidBuilderIdentityConfigError,
    load_builder_identity,
)


def _write_user_config(tmp_path, content: dict) -> None:
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(content), encoding="utf-8")


class TestAbsence:
    def test_no_config_root_file_returns_none_none(self, tmp_path):
        assert load_builder_identity(config_root=tmp_path) == (None, None)

    def test_no_builder_identity_section_returns_none_none(self, tmp_path):
        _write_user_config(tmp_path, {"credentials": {}})
        assert load_builder_identity(config_root=tmp_path) == (None, None)

    def test_empty_builder_identity_section_returns_none_none(self, tmp_path):
        _write_user_config(tmp_path, {"builder_identity": {}})
        assert load_builder_identity(config_root=tmp_path) == (None, None)


class TestPresent:
    def test_well_formed_section_round_trips(self, tmp_path):
        _write_user_config(
            tmp_path,
            {
                "builder_identity": {
                    "name": "clagentic-builder[bot]",
                    "email": "123+clagentic-builder[bot]@users.noreply.example.com",
                }
            },
        )
        name, email = load_builder_identity(config_root=tmp_path)
        assert name == "clagentic-builder[bot]"
        assert email == "123+clagentic-builder[bot]@users.noreply.example.com"

    def test_coexists_with_credentials_and_github_app_sections(self, tmp_path):
        _write_user_config(
            tmp_path,
            {
                "credentials": {"token_provider_forgejo": "static"},
                "github_app": {"slug": "some-app"},
                "builder_identity": {"name": "bot", "email": "bot@example.com"},
            },
        )
        assert load_builder_identity(config_root=tmp_path) == ("bot", "bot@example.com")


class TestMalformed:
    def test_missing_email_raises(self, tmp_path):
        _write_user_config(tmp_path, {"builder_identity": {"name": "bot"}})
        with pytest.raises(InvalidBuilderIdentityConfigError, match="email"):
            load_builder_identity(config_root=tmp_path)

    def test_missing_name_raises(self, tmp_path):
        _write_user_config(tmp_path, {"builder_identity": {"email": "bot@example.com"}})
        with pytest.raises(InvalidBuilderIdentityConfigError, match="name"):
            load_builder_identity(config_root=tmp_path)

    def test_empty_string_name_raises(self, tmp_path):
        _write_user_config(
            tmp_path, {"builder_identity": {"name": "  ", "email": "bot@example.com"}}
        )
        with pytest.raises(InvalidBuilderIdentityConfigError, match="name"):
            load_builder_identity(config_root=tmp_path)

    def test_non_string_email_raises(self, tmp_path):
        _write_user_config(
            tmp_path, {"builder_identity": {"name": "bot", "email": 123}}
        )
        with pytest.raises(InvalidBuilderIdentityConfigError, match="email"):
            load_builder_identity(config_root=tmp_path)


class TestNoRepoLocalTier:
    def test_module_has_no_repo_root_parameter(self):
        # Structural assertion (lr-0818-class reasoning): this module's
        # public read function accepts no repo_root at all -- there is no
        # call shape that could even be tempted to add a repo-local tier
        # later without a deliberate signature change.
        import inspect

        sig = inspect.signature(load_builder_identity)
        assert "repo_root" not in sig.parameters


class TestConstants:
    def test_section_and_keys(self):
        assert CONFIG_SECTION_BUILDER_IDENTITY == "builder_identity"
        assert CONFIG_KEY_NAME == "name"
        assert CONFIG_KEY_EMAIL == "email"
