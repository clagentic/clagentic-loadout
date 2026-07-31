"""test_review_login_config.py — unit tests for
clagentic_loadout.review.login_config (lr-0a03c3).

Covers the DEPLOYMENT-TIER (user-level config.yaml, never repo-local)
`review: reviewer_logins:` per-role override map: absence at every level
returns None (falls through to merge.reviewer_login's platform-aware
derivation), a configured role resolves its override, an unconfigured role
still returns None even when the map has other entries, and malformed
shapes degrade to "no override" rather than raising (additive-tier
contract, mirrors transport.github_app_config).
"""

from __future__ import annotations

import yaml

from clagentic_loadout.review.login_config import (
    CONFIG_KEY_REVIEWER_LOGINS,
    CONFIG_SECTION_REVIEW,
    load_reviewer_login_override,
)


def _write_user_config(tmp_path, content: dict) -> None:
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(content), encoding="utf-8")


class TestAbsence:
    def test_no_config_file_returns_none(self, tmp_path):
        assert load_reviewer_login_override("reviewer", config_root=tmp_path) is None

    def test_no_review_section_returns_none(self, tmp_path):
        _write_user_config(tmp_path, {"credentials": {}})
        assert load_reviewer_login_override("reviewer", config_root=tmp_path) is None

    def test_no_reviewer_logins_key_returns_none(self, tmp_path):
        _write_user_config(tmp_path, {"review": {"some_other_key": True}})
        assert load_reviewer_login_override("reviewer", config_root=tmp_path) is None

    def test_role_absent_from_configured_map_returns_none(self, tmp_path):
        _write_user_config(
            tmp_path, {"review": {"reviewer_logins": {"security": "sec-bot"}}}
        )
        assert load_reviewer_login_override("reviewer", config_root=tmp_path) is None


class TestPresent:
    def test_configured_role_resolves(self, tmp_path):
        _write_user_config(
            tmp_path,
            {"review": {"reviewer_logins": {"reviewer": "clagentic-reviewer-bot"}}},
        )
        assert (
            load_reviewer_login_override("reviewer", config_root=tmp_path)
            == "clagentic-reviewer-bot"
        )

    def test_multiple_roles_independent(self, tmp_path):
        _write_user_config(
            tmp_path,
            {
                "review": {
                    "reviewer_logins": {
                        "reviewer": "reviewer-bot",
                        "security": "security-bot",
                    }
                }
            },
        )
        assert load_reviewer_login_override("reviewer", config_root=tmp_path) == "reviewer-bot"
        assert load_reviewer_login_override("security", config_root=tmp_path) == "security-bot"

    def test_coexists_with_other_user_level_sections(self, tmp_path):
        _write_user_config(
            tmp_path,
            {
                "credentials": {"token_provider_forgejo": "static"},
                "github_app": {"slug": "some-app"},
                "builder_identity": {"name": "bot", "email": "bot@example.com"},
                "review": {"reviewer_logins": {"reviewer": "reviewer-bot"}},
            },
        )
        assert load_reviewer_login_override("reviewer", config_root=tmp_path) == "reviewer-bot"


class TestMalformedDegradesToNone:
    def test_reviewer_logins_not_a_mapping_returns_none(self, tmp_path):
        _write_user_config(tmp_path, {"review": {"reviewer_logins": ["reviewer-bot"]}})
        assert load_reviewer_login_override("reviewer", config_root=tmp_path) is None

    def test_non_string_login_value_returns_none(self, tmp_path):
        _write_user_config(
            tmp_path, {"review": {"reviewer_logins": {"reviewer": 123}}}
        )
        assert load_reviewer_login_override("reviewer", config_root=tmp_path) is None

    def test_empty_string_login_value_returns_none(self, tmp_path):
        _write_user_config(
            tmp_path, {"review": {"reviewer_logins": {"reviewer": "  "}}}
        )
        assert load_reviewer_login_override("reviewer", config_root=tmp_path) is None


class TestNoRepoLocalTier:
    def test_no_repo_root_parameter(self):
        import inspect

        sig = inspect.signature(load_reviewer_login_override)
        assert "repo_root" not in sig.parameters


class TestConstants:
    def test_section_and_key(self):
        assert CONFIG_SECTION_REVIEW == "review"
        assert CONFIG_KEY_REVIEWER_LOGINS == "reviewer_logins"
