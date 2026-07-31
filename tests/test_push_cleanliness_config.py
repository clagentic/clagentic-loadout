"""test_push_cleanliness_config.py — unit tests for
clagentic_loadout.push.cleanliness_config (lr-d7a8).

Covers the repo-local `.clagentic/loadout/config.yaml` `push:
scratch_patterns:` config surface, following the same absence/present/
malformed coverage shape as test_merge_post_merge_config.py and
test_wait_config.py. Legacy-path fallback (.loadout/config.yaml, lr-446c35)
coverage lives in TestLegacyPathFallback below.
"""

from __future__ import annotations

import pytest
import yaml

from clagentic_loadout.push.cleanliness_config import (
    CONFIG_KEY_SCRATCH_PATTERNS,
    CONFIG_SECTION_PUSH,
    DEFAULT_CONFIG_RELATIVE_PATH,
    DEFAULT_SCRATCH_PATTERNS,
    InvalidCleanlinessConfigError,
    load_scratch_patterns,
    match_scratch_pattern,
)


def _write_config(tmp_path, content: dict) -> None:
    config_dir = tmp_path / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(yaml.safe_dump(content), encoding="utf-8")


class TestDefaults:
    def test_no_repo_root_returns_default(self):
        cfg = load_scratch_patterns(None)
        assert cfg.patterns == tuple(DEFAULT_SCRATCH_PATTERNS)
        assert cfg.source == "default"

    def test_no_config_file_returns_default(self, tmp_path):
        cfg = load_scratch_patterns(tmp_path)
        assert cfg.patterns == tuple(DEFAULT_SCRATCH_PATTERNS)
        assert cfg.source == "default"

    def test_config_file_with_no_push_section_returns_default(self, tmp_path):
        _write_config(tmp_path, {"wait": {"scoped_test_patterns": ["^go test"]}})
        cfg = load_scratch_patterns(tmp_path)
        assert cfg.patterns == tuple(DEFAULT_SCRATCH_PATTERNS)

    def test_push_section_with_no_scratch_patterns_key_returns_default(self, tmp_path):
        _write_config(tmp_path, {"push": {"some_other_key": True}})
        cfg = load_scratch_patterns(tmp_path)
        assert cfg.patterns == tuple(DEFAULT_SCRATCH_PATTERNS)


class TestOverride:
    def test_custom_patterns_override_default(self, tmp_path):
        _write_config(tmp_path, {"push": {"scratch_patterns": ["my-litter-*"]}})
        cfg = load_scratch_patterns(tmp_path)
        assert cfg.patterns == ("my-litter-*",)
        assert cfg.source != "default"

    def test_custom_config_relative_path_honored(self, tmp_path):
        alt_dir = tmp_path / "alt"
        alt_dir.mkdir()
        (alt_dir / "custom.yaml").write_text(
            yaml.safe_dump({"push": {"scratch_patterns": ["alt-*"]}}),
            encoding="utf-8",
        )
        cfg = load_scratch_patterns(tmp_path, config_relative_path="alt/custom.yaml")
        assert cfg.patterns == ("alt-*",)


class TestMalformedConfigRaises:
    def test_scratch_patterns_not_a_list_raises(self, tmp_path):
        _write_config(tmp_path, {"push": {"scratch_patterns": "not-a-list"}})
        with pytest.raises(InvalidCleanlinessConfigError):
            load_scratch_patterns(tmp_path)

    def test_scratch_patterns_with_non_string_entry_raises(self, tmp_path):
        _write_config(tmp_path, {"push": {"scratch_patterns": ["ok-*", 5]}})
        with pytest.raises(InvalidCleanlinessConfigError):
            load_scratch_patterns(tmp_path)

    def test_malformed_yaml_raises(self, tmp_path):
        config_dir = tmp_path / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text("push: [unterminated", encoding="utf-8")
        with pytest.raises(InvalidCleanlinessConfigError):
            load_scratch_patterns(tmp_path)


class TestMatchScratchPattern:
    @pytest.mark.parametrize(
        "filename",
        [
            "pr-body-lr-1234.txt",
            ".loadout/.scratch-pr-body-lr-77d6-followup.txt",
            "notes.scratch.md",
            "output.diff-check.tmp",
            "HANDOFF.md",
            "docs/HANDOFF.md",
            # lr-8c7fe1: .homecheck-* files leaked into a repo's working tree
            # (untracked, uncaught by *scratch* — leading-dot homecheck probe
            # files are a distinct shape) before this pattern was added.
            ".loadout/.homecheck-lr-52d7.txt",
        ],
    )
    def test_default_patterns_match_expected_scratch_names(self, filename):
        assert match_scratch_pattern(filename, tuple(DEFAULT_SCRATCH_PATTERNS)) is not None

    def test_ordinary_source_file_does_not_match(self):
        assert match_scratch_pattern("src/clagentic_loadout/push/verb.py", tuple(DEFAULT_SCRATCH_PATTERNS)) is None

    def test_returns_the_matched_pattern(self):
        assert match_scratch_pattern("pr-body-lr-1234.txt", tuple(DEFAULT_SCRATCH_PATTERNS)) == "pr-body-*"

    def test_no_match_returns_none(self):
        assert match_scratch_pattern("README.md", ("only-this-*",)) is None


class TestModuleConstants:
    def test_default_relative_path_matches_convention(self):
        assert DEFAULT_CONFIG_RELATIVE_PATH == ".clagentic/loadout/config.yaml"

    def test_section_and_key_names(self):
        assert CONFIG_SECTION_PUSH == "push"
        assert CONFIG_KEY_SCRATCH_PATTERNS == "scratch_patterns"


class TestLegacyPathFallback:
    """Transitional back-compat (lr-446c35): a repo that has not yet
    migrated off .loadout/config.yaml is still read, with a one-line
    deprecation warning to stderr. Removed after the fleet migration
    (lr-a645aa)."""

    def test_legacy_path_is_read_when_new_path_absent(self, tmp_path, capsys):
        legacy_dir = tmp_path / ".loadout"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text(
            yaml.safe_dump({"push": {"scratch_patterns": ["legacy-*"]}}),
            encoding="utf-8",
        )

        cfg = load_scratch_patterns(tmp_path)

        assert cfg.patterns == ("legacy-*",)
        assert cfg.source == str(legacy_dir / "config.yaml")
        stderr = capsys.readouterr().err
        assert "deprecated" in stderr
        assert stderr.count("\n") == 1

    def test_new_path_wins_when_both_present(self, tmp_path, capsys):
        legacy_dir = tmp_path / ".loadout"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text(
            yaml.safe_dump({"push": {"scratch_patterns": ["legacy-*"]}}),
            encoding="utf-8",
        )
        _write_config(tmp_path, {"push": {"scratch_patterns": ["new-*"]}})

        cfg = load_scratch_patterns(tmp_path)

        assert cfg.patterns == ("new-*",)
        assert capsys.readouterr().err == ""
