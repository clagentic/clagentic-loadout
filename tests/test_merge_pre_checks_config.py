"""test_merge_pre_checks_config.py — unit tests for
clagentic_loadout.merge.pre_checks_config (lr-0a03c3).

Covers the repo-local `.clagentic/loadout/config.yaml` `merge: pre_checks:`
config surface: absence at every level is a no-op ([]), a present list is
parsed and validated through the SAME validator `post_merge_steps` uses
(malformed steps raise at LOAD time, before any step executes), and the file
lives under the DEFAULT_CONFIG_RELATIVE_PATH convention shared with every
other section owner in this package.
"""

from __future__ import annotations

import pytest
import yaml

from clagentic_loadout.merge.post_merge import PostMergeConfigError
from clagentic_loadout.merge.pre_checks_config import (
    CONFIG_KEY_PRE_CHECKS,
    CONFIG_SECTION_MERGE,
    DEFAULT_CONFIG_RELATIVE_PATH,
    load_pre_checks,
)


def _write_config(tmp_path, content: dict) -> None:
    config_dir = tmp_path / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(yaml.safe_dump(content), encoding="utf-8")


class TestAbsence:
    def test_no_repo_root_returns_empty(self):
        assert load_pre_checks(None) == []

    def test_no_config_file_returns_empty(self, tmp_path):
        assert load_pre_checks(tmp_path) == []

    def test_config_file_with_no_merge_section_returns_empty(self, tmp_path):
        _write_config(tmp_path, {"wait": {"scoped_test_patterns": ["^go test"]}})
        assert load_pre_checks(tmp_path) == []

    def test_merge_section_with_no_pre_checks_key_returns_empty(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_steps": []}})
        assert load_pre_checks(tmp_path) == []

    def test_pre_checks_and_post_merge_steps_coexist_in_same_merge_section(self, tmp_path):
        _write_config(
            tmp_path,
            {
                "merge": {
                    "pre_checks": [{"cmd": "make lint"}],
                    "post_merge_steps": [{"cmd": "scripts/install.sh"}],
                }
            },
        )
        assert load_pre_checks(tmp_path) == [{"cmd": "make lint"}]


class TestPresentChecks:
    def test_valid_checks_parsed(self, tmp_path):
        _write_config(
            tmp_path,
            {
                "merge": {
                    "pre_checks": [
                        {
                            "cmd": "make lint",
                            "description": "lint pass, no CI runner wired up",
                            "on_failure": "fail",
                        }
                    ]
                }
            },
        )
        checks = load_pre_checks(tmp_path)
        assert len(checks) == 1
        assert checks[0]["on_failure"] == "fail"
        assert checks[0]["cmd"] == "make lint"

    def test_list_form_cmd_round_trips(self, tmp_path):
        _write_config(
            tmp_path,
            {"merge": {"pre_checks": [{"cmd": ["make", "lint"]}]}},
        )
        checks = load_pre_checks(tmp_path)
        assert checks[0]["cmd"] == ["make", "lint"]

    def test_default_on_failure_omitted_is_valid(self, tmp_path):
        _write_config(tmp_path, {"merge": {"pre_checks": [{"cmd": "make lint"}]}})
        checks = load_pre_checks(tmp_path)
        assert "on_failure" not in checks[0]

    def test_custom_config_relative_path_honored(self, tmp_path):
        alt_dir = tmp_path / "alt"
        alt_dir.mkdir()
        (alt_dir / "custom.yaml").write_text(
            yaml.safe_dump({"merge": {"pre_checks": [{"cmd": "make lint"}]}}),
            encoding="utf-8",
        )
        checks = load_pre_checks(tmp_path, config_relative_path="alt/custom.yaml")
        assert checks == [{"cmd": "make lint"}]


class TestMalformed:
    def test_non_dict_merge_section_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": ["not", "a", "mapping"]})
        with pytest.raises(PostMergeConfigError, match="must be a mapping"):
            load_pre_checks(tmp_path)

    def test_pre_checks_not_a_list_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"pre_checks": "make lint"}})
        with pytest.raises(PostMergeConfigError):
            load_pre_checks(tmp_path)

    def test_missing_cmd_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"pre_checks": [{"description": "no cmd"}]}})
        with pytest.raises(PostMergeConfigError, match="missing required key 'cmd'"):
            load_pre_checks(tmp_path)

    def test_shell_operator_token_rejected_at_load_time(self, tmp_path):
        _write_config(
            tmp_path,
            {"merge": {"pre_checks": [{"cmd": "make lint && make test"}]}},
        )
        with pytest.raises(PostMergeConfigError, match="shell operator token"):
            load_pre_checks(tmp_path)

    def test_invalid_on_failure_raises(self, tmp_path):
        _write_config(
            tmp_path,
            {"merge": {"pre_checks": [{"cmd": "make lint", "on_failure": "retry"}]}},
        )
        with pytest.raises(PostMergeConfigError, match="on_failure must be one of"):
            load_pre_checks(tmp_path)

    def test_malformed_yaml_raises(self, tmp_path):
        config_dir = tmp_path / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text("merge: [unterminated", encoding="utf-8")
        with pytest.raises(PostMergeConfigError, match="could not be read as YAML"):
            load_pre_checks(tmp_path)


class TestLegacyPathFallback:
    def test_legacy_path_read_with_warning(self, tmp_path, capsys):
        legacy_dir = tmp_path / ".loadout"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text(
            yaml.safe_dump({"merge": {"pre_checks": [{"cmd": "make lint"}]}}),
            encoding="utf-8",
        )
        checks = load_pre_checks(tmp_path)
        assert checks == [{"cmd": "make lint"}]
        assert "deprecated" in capsys.readouterr().err


class TestConstants:
    def test_section_key_is_merge(self):
        assert CONFIG_SECTION_MERGE == "merge"

    def test_pre_checks_key(self):
        assert CONFIG_KEY_PRE_CHECKS == "pre_checks"

    def test_default_config_relative_path(self):
        assert DEFAULT_CONFIG_RELATIVE_PATH == ".clagentic/loadout/config.yaml"
