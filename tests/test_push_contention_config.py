"""test_push_contention_config.py — unit tests for
clagentic_loadout.push.contention_config (lr-78a584).

Follows the same absence/present/malformed coverage shape as
test_push_cleanliness_config.py (this module's own direct precedent for the
config-loading pattern, sharing the same `push:` top-level section).
"""

from __future__ import annotations

import pytest
import yaml

from clagentic_loadout.push.contention_config import (
    CONFIG_KEY_CONTENTION_CHECK,
    CONFIG_KEY_IN_FLIGHT_BRANCH_PATTERN,
    CONFIG_SECTION_PUSH,
    DEFAULT_CONFIG_RELATIVE_PATH,
    DEFAULT_IN_FLIGHT_BRANCH_PATTERN,
    InvalidContentionConfigError,
    load_contention_config,
)


def _write_config(tmp_path, content: dict) -> None:
    config_dir = tmp_path / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(yaml.safe_dump(content), encoding="utf-8")


class TestDefaultsAreDisabled:
    """Hard acceptance criterion: absent config is DEFAULT OFF, proven by a
    test rather than asserted (lr-78a584 comment #1)."""

    def test_no_repo_root_returns_disabled_default(self):
        cfg = load_contention_config(None)
        assert cfg.enabled is False
        assert cfg.branch_pattern == DEFAULT_IN_FLIGHT_BRANCH_PATTERN
        assert cfg.source == "default"

    def test_no_config_file_returns_disabled_default(self, tmp_path):
        cfg = load_contention_config(tmp_path)
        assert cfg.enabled is False

    def test_config_file_with_no_push_section_returns_disabled_default(self, tmp_path):
        _write_config(tmp_path, {"merge": {"authorized_roles": ["merger"]}})
        cfg = load_contention_config(tmp_path)
        assert cfg.enabled is False

    def test_push_section_with_no_contention_check_key_returns_disabled_default(self, tmp_path):
        _write_config(tmp_path, {"push": {"scratch_patterns": ["a-*"]}})
        cfg = load_contention_config(tmp_path)
        assert cfg.enabled is False


class TestExplicitOptIn:
    def test_contention_check_true_enables(self, tmp_path):
        _write_config(tmp_path, {"push": {"contention_check": True}})
        cfg = load_contention_config(tmp_path)
        assert cfg.enabled is True
        assert cfg.branch_pattern == DEFAULT_IN_FLIGHT_BRANCH_PATTERN
        assert cfg.source != "default"

    def test_contention_check_false_stays_disabled(self, tmp_path):
        _write_config(tmp_path, {"push": {"contention_check": False}})
        cfg = load_contention_config(tmp_path)
        assert cfg.enabled is False

    def test_custom_branch_pattern_overrides_default(self, tmp_path):
        _write_config(
            tmp_path,
            {"push": {"contention_check": True, "in_flight_branch_pattern": r"^wip/"}},
        )
        cfg = load_contention_config(tmp_path)
        assert cfg.branch_pattern == r"^wip/"

    def test_custom_config_relative_path_honored(self, tmp_path):
        alt_dir = tmp_path / "alt"
        alt_dir.mkdir()
        (alt_dir / "custom.yaml").write_text(
            yaml.safe_dump({"push": {"contention_check": True}}), encoding="utf-8"
        )
        cfg = load_contention_config(tmp_path, config_relative_path="alt/custom.yaml")
        assert cfg.enabled is True


class TestMalformedConfigRaises:
    def test_contention_check_not_a_bool_raises(self, tmp_path):
        _write_config(tmp_path, {"push": {"contention_check": "yes"}})
        with pytest.raises(InvalidContentionConfigError):
            load_contention_config(tmp_path)

    def test_in_flight_branch_pattern_not_a_string_raises(self, tmp_path):
        _write_config(
            tmp_path, {"push": {"contention_check": True, "in_flight_branch_pattern": 5}}
        )
        with pytest.raises(InvalidContentionConfigError):
            load_contention_config(tmp_path)

    def test_in_flight_branch_pattern_empty_string_raises(self, tmp_path):
        _write_config(
            tmp_path, {"push": {"contention_check": True, "in_flight_branch_pattern": ""}}
        )
        with pytest.raises(InvalidContentionConfigError):
            load_contention_config(tmp_path)

    def test_in_flight_branch_pattern_invalid_regex_raises(self, tmp_path):
        _write_config(
            tmp_path,
            {"push": {"contention_check": True, "in_flight_branch_pattern": "["}},
        )
        with pytest.raises(InvalidContentionConfigError):
            load_contention_config(tmp_path)

    def test_malformed_yaml_raises(self, tmp_path):
        config_dir = tmp_path / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text("push: [unterminated", encoding="utf-8")
        with pytest.raises(InvalidContentionConfigError):
            load_contention_config(tmp_path)


class TestModuleConstants:
    def test_default_relative_path_matches_convention(self):
        assert DEFAULT_CONFIG_RELATIVE_PATH == ".clagentic/loadout/config.yaml"

    def test_section_and_key_names(self):
        assert CONFIG_SECTION_PUSH == "push"
        assert CONFIG_KEY_CONTENTION_CHECK == "contention_check"
        assert CONFIG_KEY_IN_FLIGHT_BRANCH_PATTERN == "in_flight_branch_pattern"


class TestLegacyPathFallback:
    """Transitional back-compat (lr-446c35): a repo that has not yet
    migrated off .loadout/config.yaml is still read, mirroring every other
    section-owning loader in this package."""

    def test_legacy_path_is_read_when_new_path_absent(self, tmp_path, capsys):
        legacy_dir = tmp_path / ".loadout"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text(
            yaml.safe_dump({"push": {"contention_check": True}}), encoding="utf-8"
        )

        cfg = load_contention_config(tmp_path)

        assert cfg.enabled is True
        assert cfg.source == str(legacy_dir / "config.yaml")
