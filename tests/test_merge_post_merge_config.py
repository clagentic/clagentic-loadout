"""test_merge_post_merge_config.py — unit tests for
clagentic_loadout.merge.post_merge_config (lr-77d6, lr-3812, lr-d95cdb).

Covers the repo-local `.clagentic/loadout/config.yaml` `merge:
post_merge_steps:` config surface: absence at every level is a no-op ([]), a
present list is parsed and validated (malformed steps raise at LOAD time,
before any step executes), and the file lives under the
DEFAULT_CONFIG_RELATIVE_PATH convention shared with wait.config /
provisioning.roles. Legacy-path fallback (.loadout/config.yaml, lr-446c35)
coverage lives in TestLegacyPathFallback below. `sync_tree_after_merge`
(lr-d95cdb, default-on tree-sync-after-merge config key) coverage lives in
TestResolveSyncTreeAfterMerge below.
"""

from __future__ import annotations

import pytest
import yaml

from clagentic_loadout.merge.post_merge import PostMergeConfigError
from clagentic_loadout.merge.post_merge_config import (
    CONFIG_KEY_ENFORCE_MERGE_SHAPE,
    CONFIG_KEY_ENFORCE_SINGLE_VERDICT_FENCE,
    CONFIG_KEY_GIT_WORKING_TREE,
    CONFIG_KEY_MODEL_ATTESTATION_DENYLIST,
    CONFIG_KEY_POST_MERGE_STEP_TIMEOUT_SECONDS,
    CONFIG_KEY_POST_MERGE_STEPS,
    CONFIG_KEY_REQUIRE_MODEL_ATTESTATION,
    CONFIG_KEY_SYNC_TREE_AFTER_MERGE,
    CONFIG_SECTION_MERGE,
    DEFAULT_CONFIG_RELATIVE_PATH,
    DEFAULT_ENFORCE_MERGE_SHAPE,
    DEFAULT_ENFORCE_SINGLE_VERDICT_FENCE,
    DEFAULT_POST_MERGE_STEP_TIMEOUT_SECONDS,
    DEFAULT_REQUIRE_MODEL_ATTESTATION,
    DEFAULT_SYNC_TREE_AFTER_MERGE,
    find_crew_yaml_files_declaring_post_merge_steps,
    load_post_merge_steps,
    post_merge_steps_key_declared,
    resolve_enforce_merge_shape,
    resolve_enforce_single_verdict_fence,
    resolve_git_working_tree,
    resolve_model_attestation_denylist,
    resolve_post_merge_step_timeout_seconds,
    resolve_require_model_attestation,
    resolve_sync_tree_after_merge,
)


def _write_config(tmp_path, content: dict) -> None:
    config_dir = tmp_path / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(yaml.safe_dump(content), encoding="utf-8")


class TestAbsence:
    def test_no_repo_root_returns_empty(self):
        assert load_post_merge_steps(None) == []

    def test_no_config_file_returns_empty(self, tmp_path):
        assert load_post_merge_steps(tmp_path) == []

    def test_config_file_with_no_merge_section_returns_empty(self, tmp_path):
        _write_config(tmp_path, {"wait": {"scoped_test_patterns": ["^go test"]}})
        assert load_post_merge_steps(tmp_path) == []

    def test_merge_section_with_no_post_merge_steps_key_returns_empty(self, tmp_path):
        _write_config(tmp_path, {"merge": {"some_other_key": True}})
        assert load_post_merge_steps(tmp_path) == []


class TestPresentSteps:
    def test_valid_steps_parsed(self, tmp_path):
        _write_config(
            tmp_path,
            {
                "merge": {
                    "post_merge_steps": [
                        {
                            "cmd": "scripts/install.sh --git-host-base-url https://git.example.com",
                            "description": "self-install after merge",
                            "on_failure": "fail",
                        }
                    ]
                }
            },
        )
        steps = load_post_merge_steps(tmp_path)
        assert len(steps) == 1
        assert steps[0]["on_failure"] == "fail"
        assert "install.sh" in steps[0]["cmd"]

    def test_list_form_cmd_round_trips(self, tmp_path):
        _write_config(
            tmp_path,
            {"merge": {"post_merge_steps": [{"cmd": ["scripts/install.sh", "--editable"]}]}},
        )
        steps = load_post_merge_steps(tmp_path)
        assert steps[0]["cmd"] == ["scripts/install.sh", "--editable"]

    def test_custom_config_relative_path_honored(self, tmp_path):
        alt_dir = tmp_path / "alt"
        alt_dir.mkdir()
        (alt_dir / "custom.yaml").write_text(
            yaml.safe_dump({"merge": {"post_merge_steps": [{"cmd": "true"}]}}),
            encoding="utf-8",
        )
        steps = load_post_merge_steps(
            tmp_path, config_relative_path="alt/custom.yaml"
        )
        assert len(steps) == 1


class TestMalformedConfigRaisesAtLoadTime:
    def test_merge_section_not_a_mapping_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": "not-a-mapping"})
        with pytest.raises(PostMergeConfigError):
            load_post_merge_steps(tmp_path)

    def test_post_merge_steps_not_a_list_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_steps": "run-it"}})
        with pytest.raises(PostMergeConfigError):
            load_post_merge_steps(tmp_path)

    def test_shell_operator_in_configured_step_raises(self, tmp_path):
        _write_config(
            tmp_path,
            {"merge": {"post_merge_steps": [{"cmd": "git fetch && git switch --detach X"}]}},
        )
        with pytest.raises(PostMergeConfigError, match="shell operator"):
            load_post_merge_steps(tmp_path)

    def test_missing_cmd_key_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_steps": [{"description": "no cmd"}]}})
        with pytest.raises(PostMergeConfigError):
            load_post_merge_steps(tmp_path)

    def test_malformed_yaml_raises(self, tmp_path):
        config_dir = tmp_path / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text("merge: [unterminated", encoding="utf-8")
        with pytest.raises(PostMergeConfigError):
            load_post_merge_steps(tmp_path)

    def test_non_mapping_top_level_document_raises(self, tmp_path):
        config_dir = tmp_path / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text(yaml.safe_dump(["a", "b"]), encoding="utf-8")
        with pytest.raises(PostMergeConfigError):
            load_post_merge_steps(tmp_path)


class TestModuleConstants:
    def test_default_relative_path_matches_convention(self):
        assert DEFAULT_CONFIG_RELATIVE_PATH == ".clagentic/loadout/config.yaml"

    def test_section_and_key_names(self):
        assert CONFIG_SECTION_MERGE == "merge"
        assert CONFIG_KEY_POST_MERGE_STEPS == "post_merge_steps"
        assert CONFIG_KEY_GIT_WORKING_TREE == "git_working_tree"
        assert CONFIG_KEY_SYNC_TREE_AFTER_MERGE == "sync_tree_after_merge"

    def test_sync_tree_after_merge_defaults_on(self):
        assert DEFAULT_SYNC_TREE_AFTER_MERGE is True


class TestResolveGitWorkingTree:
    """lr-93d718: the OPTIONAL `merge.git_working_tree` knob that lets
    tree_sync's git-tree target diverge from the config-root `--repo-path`
    (the wrapper-layout regression -- config at the wrapper, `.git` at a
    subdirectory of it). Absent by default: every one of these absence cases
    must return None, i.e. "target --repo-path itself, unchanged.\""""

    def test_no_repo_root_returns_none(self):
        assert resolve_git_working_tree(None) is None

    def test_no_config_file_returns_none(self, tmp_path):
        assert resolve_git_working_tree(tmp_path) is None

    def test_no_merge_section_returns_none(self, tmp_path):
        _write_config(tmp_path, {"wait": {"scoped_test_patterns": ["^go test"]}})
        assert resolve_git_working_tree(tmp_path) is None

    def test_merge_section_without_the_key_returns_none(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_steps": [{"cmd": "true"}]}})
        assert resolve_git_working_tree(tmp_path) is None

    def test_key_present_resolves_relative_to_config_root(self, tmp_path):
        _write_config(tmp_path, {"merge": {"git_working_tree": "repo"}})
        resolved = resolve_git_working_tree(tmp_path)
        assert resolved == tmp_path / "repo"

    def test_key_present_alongside_post_merge_steps(self, tmp_path):
        _write_config(
            tmp_path,
            {
                "merge": {
                    "git_working_tree": "repo",
                    "post_merge_steps": [{"cmd": "true"}],
                }
            },
        )
        # config discovery (load_post_merge_steps) stays anchored at the
        # config root -- unaffected by the working-tree knob's own value.
        assert resolve_git_working_tree(tmp_path) == tmp_path / "repo"
        steps = load_post_merge_steps(tmp_path)
        assert len(steps) == 1

    def test_merge_section_not_a_mapping_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": "not-a-mapping"})
        with pytest.raises(PostMergeConfigError):
            resolve_git_working_tree(tmp_path)

    def test_non_string_value_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"git_working_tree": 42}})
        with pytest.raises(PostMergeConfigError, match="git_working_tree"):
            resolve_git_working_tree(tmp_path)

    def test_empty_string_value_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"git_working_tree": "   "}})
        with pytest.raises(PostMergeConfigError, match="git_working_tree"):
            resolve_git_working_tree(tmp_path)

    def test_legacy_path_fallback_honored(self, tmp_path, capsys):
        legacy_dir = tmp_path / ".loadout"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text(
            yaml.safe_dump({"merge": {"git_working_tree": "repo"}}),
            encoding="utf-8",
        )
        resolved = resolve_git_working_tree(tmp_path)
        assert resolved == tmp_path / "repo"
        assert "deprecated" in capsys.readouterr().err

    def test_nested_subpath_still_resolves_within_config_root(self, tmp_path):
        # A legitimate nested subpath -- not just a single path component --
        # must still resolve fine, since it stays within the config root.
        _write_config(tmp_path, {"merge": {"git_working_tree": "nested/repo"}})
        resolved = resolve_git_working_tree(tmp_path)
        assert resolved == tmp_path / "nested" / "repo"

    def test_parent_escape_raises(self, tmp_path):
        # bobbie.sast.5 (lr-93d718): a `..`-escape must never redirect
        # tree_sync's git subprocess cwd outside the config root.
        _write_config(tmp_path, {"merge": {"git_working_tree": "../../etc"}})
        with pytest.raises(PostMergeConfigError, match="escapes the config root"):
            resolve_git_working_tree(tmp_path)

    def test_absolute_path_raises(self, tmp_path):
        # bobbie.sast.5 (lr-93d718): an absolute path is rejected outright,
        # never silently treated as relative-to-root.
        _write_config(tmp_path, {"merge": {"git_working_tree": "/etc"}})
        with pytest.raises(PostMergeConfigError, match="absolute path"):
            resolve_git_working_tree(tmp_path)


class TestResolveSyncTreeAfterMerge:
    """lr-d95cdb: the `merge.sync_tree_after_merge` config key -- defaults ON
    (True) at every absence level, same replace-not-merge convention
    `merge.gate_config`'s own keys already use."""

    def test_no_repo_root_defaults_on(self):
        assert resolve_sync_tree_after_merge(None) is DEFAULT_SYNC_TREE_AFTER_MERGE

    def test_no_config_file_defaults_on(self, tmp_path):
        assert resolve_sync_tree_after_merge(tmp_path) is True

    def test_no_merge_section_defaults_on(self, tmp_path):
        _write_config(tmp_path, {"wait": {"scoped_test_patterns": ["^go test"]}})
        assert resolve_sync_tree_after_merge(tmp_path) is True

    def test_merge_section_without_the_key_defaults_on(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_steps": [{"cmd": "true"}]}})
        assert resolve_sync_tree_after_merge(tmp_path) is True

    def test_explicit_true_is_honored(self, tmp_path):
        _write_config(tmp_path, {"merge": {"sync_tree_after_merge": True}})
        assert resolve_sync_tree_after_merge(tmp_path) is True

    def test_explicit_false_is_honored(self, tmp_path):
        _write_config(tmp_path, {"merge": {"sync_tree_after_merge": False}})
        assert resolve_sync_tree_after_merge(tmp_path) is False

    def test_key_present_alongside_post_merge_steps(self, tmp_path):
        _write_config(
            tmp_path,
            {
                "merge": {
                    "sync_tree_after_merge": False,
                    "post_merge_steps": [{"cmd": "true"}],
                }
            },
        )
        assert resolve_sync_tree_after_merge(tmp_path) is False
        # config discovery (load_post_merge_steps) stays unaffected by this
        # knob's own value.
        assert len(load_post_merge_steps(tmp_path)) == 1

    def test_merge_section_not_a_mapping_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": "not-a-mapping"})
        with pytest.raises(PostMergeConfigError):
            resolve_sync_tree_after_merge(tmp_path)

    def test_non_bool_value_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"sync_tree_after_merge": "yes"}})
        with pytest.raises(PostMergeConfigError, match="sync_tree_after_merge"):
            resolve_sync_tree_after_merge(tmp_path)

    def test_malformed_yaml_raises(self, tmp_path):
        config_dir = tmp_path / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text("merge: [unterminated", encoding="utf-8")
        with pytest.raises(PostMergeConfigError):
            resolve_sync_tree_after_merge(tmp_path)


class TestResolveEnforceMergeShape:
    """lr-14f704 item 3: `merge: enforce_merge_shape:` -- default False
    (warn-only), a repo opts into hard-failing a detected requested-vs-actual
    merge-shape mismatch. Mirrors TestResolveSyncTreeAfterMerge's own
    absence/explicit/malformed coverage shape exactly (same `merge:` section,
    same replace-not-merge convention)."""

    def test_default_is_false(self):
        assert DEFAULT_ENFORCE_MERGE_SHAPE is False

    def test_no_repo_root_defaults_off(self):
        assert resolve_enforce_merge_shape(None) is False

    def test_no_config_file_defaults_off(self, tmp_path):
        assert resolve_enforce_merge_shape(tmp_path) is False

    def test_no_merge_section_defaults_off(self, tmp_path):
        _write_config(tmp_path, {"wait": {"scoped_test_patterns": ["^go test"]}})
        assert resolve_enforce_merge_shape(tmp_path) is False

    def test_merge_section_without_the_key_defaults_off(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_steps": [{"cmd": "true"}]}})
        assert resolve_enforce_merge_shape(tmp_path) is False

    def test_explicit_true_is_honored(self, tmp_path):
        _write_config(tmp_path, {"merge": {"enforce_merge_shape": True}})
        assert resolve_enforce_merge_shape(tmp_path) is True

    def test_explicit_false_is_honored(self, tmp_path):
        _write_config(tmp_path, {"merge": {"enforce_merge_shape": False}})
        assert resolve_enforce_merge_shape(tmp_path) is False

    def test_key_present_alongside_other_merge_keys(self, tmp_path):
        _write_config(
            tmp_path,
            {
                "merge": {
                    "enforce_merge_shape": True,
                    "sync_tree_after_merge": False,
                    "post_merge_steps": [{"cmd": "true"}],
                }
            },
        )
        assert resolve_enforce_merge_shape(tmp_path) is True
        assert resolve_sync_tree_after_merge(tmp_path) is False
        assert len(load_post_merge_steps(tmp_path)) == 1


class TestResolveEnforceSingleVerdictFence:
    """lr-5260f9: `merge: enforce_single_verdict_fence:` -- default True
    (hard refusal on a reviewer-verdict comment body carrying more than one
    fenced ```review-result``` block), a repo OPTS OUT to fall back to the
    pre-existing merge.verdict.read_reviewer_verdict last-fence-wins parse
    for legacy multi-fence comments it cannot immediately clean up.
    ENFORCE-BY-DEFAULT / CONFIG-GATED OPT-OUT -- deliberately the INVERSE
    of TestResolveEnforceMergeShape's own WARN-BY-DEFAULT trade-off (per
    BOBBIE/PEACHES's blocking finding on PR #142: no known-good caller of
    this gate can still be producing a multi-fence body once the producer
    refusal ships, so there is nobody left for a permissive default to
    protect). Resolution mechanics (absence/explicit/malformed/coexistence
    coverage shape) still mirror TestResolveEnforceMergeShape exactly --
    only the DEFAULT direction differs."""

    def test_default_is_true(self):
        assert DEFAULT_ENFORCE_SINGLE_VERDICT_FENCE is True

    def test_no_repo_root_defaults_on(self):
        assert resolve_enforce_single_verdict_fence(None) is True

    def test_no_config_file_defaults_on(self, tmp_path):
        assert resolve_enforce_single_verdict_fence(tmp_path) is True

    def test_no_merge_section_defaults_on(self, tmp_path):
        _write_config(tmp_path, {"wait": {"scoped_test_patterns": ["^go test"]}})
        assert resolve_enforce_single_verdict_fence(tmp_path) is True

    def test_merge_section_without_the_key_defaults_on(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_steps": [{"cmd": "true"}]}})
        assert resolve_enforce_single_verdict_fence(tmp_path) is True

    def test_explicit_true_is_honored(self, tmp_path):
        _write_config(tmp_path, {"merge": {"enforce_single_verdict_fence": True}})
        assert resolve_enforce_single_verdict_fence(tmp_path) is True

    def test_explicit_false_opts_out(self, tmp_path):
        _write_config(tmp_path, {"merge": {"enforce_single_verdict_fence": False}})
        assert resolve_enforce_single_verdict_fence(tmp_path) is False

    def test_non_bool_value_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"enforce_single_verdict_fence": "true"}})
        with pytest.raises(PostMergeConfigError, match="must be a bool"):
            resolve_enforce_single_verdict_fence(tmp_path)

    def test_non_mapping_merge_section_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": ["bad"]})
        with pytest.raises(PostMergeConfigError, match="must be a mapping"):
            resolve_enforce_single_verdict_fence(tmp_path)

    def test_key_present_alongside_every_other_merge_key(self, tmp_path):
        # No collision against any existing merge: section key. Uses the
        # explicit opt-out (False) here deliberately -- the interesting
        # coexistence case is a repo that turns THIS key off while every
        # other merge: key keeps its own independent value, proving the
        # keys don't cross-influence each other's resolution.
        _write_config(
            tmp_path,
            {
                "merge": {
                    "enforce_merge_shape": True,
                    "enforce_single_verdict_fence": False,
                    "sync_tree_after_merge": False,
                    "post_merge_steps": [{"cmd": "true"}],
                }
            },
        )
        assert resolve_enforce_merge_shape(tmp_path) is True
        assert resolve_enforce_single_verdict_fence(tmp_path) is False
        assert resolve_sync_tree_after_merge(tmp_path) is False
        assert len(load_post_merge_steps(tmp_path)) == 1


class TestEnforceSingleVerdictFenceConstants:
    def test_key_name(self):
        assert CONFIG_KEY_ENFORCE_SINGLE_VERDICT_FENCE == "enforce_single_verdict_fence"


class TestResolvePostMergeStepTimeoutSeconds:
    """lr-d6e52b: `merge: post_merge_step_timeout_seconds:` -- default None
    (no bound at all), a repo opts into a repo-wide fallback bound for any
    ORDINARY step that does not set its own `timeout_seconds`. Mirrors
    TestResolveEnforceMergeShape's own absence/explicit/malformed coverage
    shape (same `merge:` section, same replace-not-merge convention)."""

    def test_default_is_none(self):
        assert DEFAULT_POST_MERGE_STEP_TIMEOUT_SECONDS is None

    def test_key_name(self):
        assert CONFIG_KEY_POST_MERGE_STEP_TIMEOUT_SECONDS == "post_merge_step_timeout_seconds"

    def test_no_repo_root_defaults_none(self):
        assert resolve_post_merge_step_timeout_seconds(None) is None

    def test_no_config_file_defaults_none(self, tmp_path):
        assert resolve_post_merge_step_timeout_seconds(tmp_path) is None

    def test_no_merge_section_defaults_none(self, tmp_path):
        _write_config(tmp_path, {"wait": {"scoped_test_patterns": ["^go test"]}})
        assert resolve_post_merge_step_timeout_seconds(tmp_path) is None

    def test_merge_section_without_the_key_defaults_none(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_steps": [{"cmd": "true"}]}})
        assert resolve_post_merge_step_timeout_seconds(tmp_path) is None

    def test_explicit_int_is_honored(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_step_timeout_seconds": 120}})
        assert resolve_post_merge_step_timeout_seconds(tmp_path) == 120

    def test_explicit_float_is_honored(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_step_timeout_seconds": 45.5}})
        assert resolve_post_merge_step_timeout_seconds(tmp_path) == 45.5

    def test_key_present_alongside_other_merge_keys(self, tmp_path):
        _write_config(
            tmp_path,
            {
                "merge": {
                    "post_merge_step_timeout_seconds": 90,
                    "enforce_merge_shape": True,
                    "post_merge_steps": [{"cmd": "true"}],
                }
            },
        )
        assert resolve_post_merge_step_timeout_seconds(tmp_path) == 90
        assert resolve_enforce_merge_shape(tmp_path) is True
        assert len(load_post_merge_steps(tmp_path)) == 1

    def test_merge_section_not_a_mapping_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": "not-a-mapping"})
        with pytest.raises(PostMergeConfigError):
            resolve_post_merge_step_timeout_seconds(tmp_path)

    def test_non_numeric_value_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_step_timeout_seconds": "soon"}})
        with pytest.raises(PostMergeConfigError, match="post_merge_step_timeout_seconds"):
            resolve_post_merge_step_timeout_seconds(tmp_path)

    def test_bool_value_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_step_timeout_seconds": True}})
        with pytest.raises(PostMergeConfigError, match="post_merge_step_timeout_seconds"):
            resolve_post_merge_step_timeout_seconds(tmp_path)

    def test_zero_value_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_step_timeout_seconds": 0}})
        with pytest.raises(PostMergeConfigError, match="post_merge_step_timeout_seconds"):
            resolve_post_merge_step_timeout_seconds(tmp_path)

    def test_negative_value_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_step_timeout_seconds": -5}})
        with pytest.raises(PostMergeConfigError, match="post_merge_step_timeout_seconds"):
            resolve_post_merge_step_timeout_seconds(tmp_path)

    def test_malformed_yaml_raises(self, tmp_path):
        config_dir = tmp_path / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text("merge: [unterminated", encoding="utf-8")
        with pytest.raises(PostMergeConfigError):
            resolve_post_merge_step_timeout_seconds(tmp_path)

    def test_merge_section_not_a_mapping_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": "not-a-mapping"})
        with pytest.raises(PostMergeConfigError):
            resolve_enforce_merge_shape(tmp_path)

    def test_non_bool_value_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"enforce_merge_shape": "yes"}})
        with pytest.raises(PostMergeConfigError, match=CONFIG_KEY_ENFORCE_MERGE_SHAPE):
            resolve_enforce_merge_shape(tmp_path)

    def test_malformed_yaml_raises(self, tmp_path):
        config_dir = tmp_path / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text("merge: [unterminated", encoding="utf-8")
        with pytest.raises(PostMergeConfigError):
            resolve_enforce_merge_shape(tmp_path)


class TestResolveRequireModelAttestation:
    """lr-95543d: `merge: require_model_attestation:` -- OPT-IN, default
    False. Mirrors TestResolveEnforceMergeShape's own absence/explicit/
    malformed coverage shape (same `merge:` section, same replace-not-merge
    convention, same warn-by-default-direction rationale)."""

    def test_default_is_false(self):
        assert DEFAULT_REQUIRE_MODEL_ATTESTATION is False

    def test_key_name(self):
        assert CONFIG_KEY_REQUIRE_MODEL_ATTESTATION == "require_model_attestation"

    def test_no_repo_root_defaults_false(self):
        assert resolve_require_model_attestation(None) is False

    def test_no_config_file_defaults_false(self, tmp_path):
        assert resolve_require_model_attestation(tmp_path) is False

    def test_no_merge_section_defaults_false(self, tmp_path):
        _write_config(tmp_path, {"wait": {"scoped_test_patterns": ["^go test"]}})
        assert resolve_require_model_attestation(tmp_path) is False

    def test_merge_section_without_the_key_defaults_false(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_steps": [{"cmd": "true"}]}})
        assert resolve_require_model_attestation(tmp_path) is False

    def test_explicit_true_is_honored(self, tmp_path):
        _write_config(tmp_path, {"merge": {"require_model_attestation": True}})
        assert resolve_require_model_attestation(tmp_path) is True

    def test_explicit_false_is_honored(self, tmp_path):
        _write_config(tmp_path, {"merge": {"require_model_attestation": False}})
        assert resolve_require_model_attestation(tmp_path) is False

    def test_merge_section_not_a_mapping_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": "not-a-mapping"})
        with pytest.raises(PostMergeConfigError, match="must be a mapping"):
            resolve_require_model_attestation(tmp_path)

    def test_non_bool_value_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"require_model_attestation": "yes"}})
        with pytest.raises(PostMergeConfigError, match="must be a bool"):
            resolve_require_model_attestation(tmp_path)

    def test_key_present_alongside_every_other_merge_key(self, tmp_path):
        _write_config(
            tmp_path,
            {
                "merge": {
                    "enforce_merge_shape": True,
                    "require_model_attestation": True,
                    "sync_tree_after_merge": False,
                    "post_merge_steps": [{"cmd": "true"}],
                }
            },
        )
        assert resolve_enforce_merge_shape(tmp_path) is True
        assert resolve_require_model_attestation(tmp_path) is True
        assert resolve_sync_tree_after_merge(tmp_path) is False
        assert len(load_post_merge_steps(tmp_path)) == 1


class TestResolveModelAttestationDenylist:
    """lr-95543d: `merge: model_attestation_denylist:` -- OPTIONAL,
    additional case-insensitive denylist terms for
    merge.model_attestation.assert_model_attested, ON TOP of that
    function's own built-in bare-tier-alias/no-digit-shape check."""

    def test_key_name(self):
        assert CONFIG_KEY_MODEL_ATTESTATION_DENYLIST == "model_attestation_denylist"

    def test_no_repo_root_defaults_empty(self):
        assert resolve_model_attestation_denylist(None) == frozenset()

    def test_no_config_file_defaults_empty(self, tmp_path):
        assert resolve_model_attestation_denylist(tmp_path) == frozenset()

    def test_no_merge_section_defaults_empty(self, tmp_path):
        _write_config(tmp_path, {"wait": {"scoped_test_patterns": ["^go test"]}})
        assert resolve_model_attestation_denylist(tmp_path) == frozenset()

    def test_merge_section_without_the_key_defaults_empty(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_steps": [{"cmd": "true"}]}})
        assert resolve_model_attestation_denylist(tmp_path) == frozenset()

    def test_explicit_list_is_honored(self, tmp_path):
        _write_config(
            tmp_path,
            {"merge": {"model_attestation_denylist": ["deprecated-model-v1", "old-fallback"]}},
        )
        assert resolve_model_attestation_denylist(tmp_path) == frozenset(
            {"deprecated-model-v1", "old-fallback"}
        )

    def test_merge_section_not_a_mapping_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": "not-a-mapping"})
        with pytest.raises(PostMergeConfigError, match="must be a mapping"):
            resolve_model_attestation_denylist(tmp_path)

    def test_non_list_value_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"model_attestation_denylist": "not-a-list"}})
        with pytest.raises(PostMergeConfigError, match="model_attestation_denylist"):
            resolve_model_attestation_denylist(tmp_path)

    def test_non_string_entry_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"model_attestation_denylist": [1, 2]}})
        with pytest.raises(PostMergeConfigError, match="model_attestation_denylist"):
            resolve_model_attestation_denylist(tmp_path)

    def test_empty_string_entry_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"model_attestation_denylist": [""]}})
        with pytest.raises(PostMergeConfigError, match="model_attestation_denylist"):
            resolve_model_attestation_denylist(tmp_path)


class TestLegacyPathFallback:
    """Transitional back-compat (lr-446c35): a repo that has not yet
    migrated off .loadout/config.yaml is still read, with a one-line
    deprecation warning to stderr. Removed after the fleet migration
    (lr-a645aa)."""

    def test_legacy_path_is_read_when_new_path_absent(self, tmp_path, capsys):
        legacy_dir = tmp_path / ".loadout"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text(
            yaml.safe_dump({"merge": {"post_merge_steps": [{"cmd": "true"}]}}),
            encoding="utf-8",
        )

        steps = load_post_merge_steps(tmp_path)

        assert len(steps) == 1
        stderr = capsys.readouterr().err
        assert "deprecated" in stderr
        assert stderr.count("\n") == 1

    def test_new_path_wins_when_both_present(self, tmp_path, capsys):
        legacy_dir = tmp_path / ".loadout"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text(
            yaml.safe_dump({"merge": {"post_merge_steps": [{"cmd": "legacy"}]}}),
            encoding="utf-8",
        )
        _write_config(tmp_path, {"merge": {"post_merge_steps": [{"cmd": "new"}]}})

        steps = load_post_merge_steps(tmp_path)

        assert steps[0]["cmd"] == "new"


class TestPostMergeStepsKeyDeclared:
    """lr-f9a01b followup: distinguishes MISSING key from EXPLICITLY EMPTY
    list -- earns its keep at the new merge-time .crew/*.yaml cross-check
    call site (a repo that wrote post_merge_steps: [] deliberately must
    never be warned about an unrelated stale .crew/*.yaml mention), even
    though no PRE-EXISTING call site of load_post_merge_steps needed it."""

    def test_no_repo_root_returns_false(self):
        assert post_merge_steps_key_declared(None) is False

    def test_no_config_file_returns_false(self, tmp_path):
        assert post_merge_steps_key_declared(tmp_path) is False

    def test_no_merge_section_returns_false(self, tmp_path):
        _write_config(tmp_path, {"wait": {"scoped_test_patterns": ["^go test"]}})
        assert post_merge_steps_key_declared(tmp_path) is False

    def test_merge_section_without_key_returns_false(self, tmp_path):
        _write_config(tmp_path, {"merge": {"some_other_key": True}})
        assert post_merge_steps_key_declared(tmp_path) is False

    def test_explicit_empty_list_returns_true(self, tmp_path):
        """The distinction this function exists for: an explicit []
        counts as declared, even though load_post_merge_steps also
        returns [] for this exact config."""
        _write_config(tmp_path, {"merge": {"post_merge_steps": []}})
        assert post_merge_steps_key_declared(tmp_path) is True
        assert load_post_merge_steps(tmp_path) == []

    def test_non_empty_list_returns_true(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_steps": [{"cmd": "true"}]}})
        assert post_merge_steps_key_declared(tmp_path) is True

    def test_malformed_merge_section_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": "not-a-mapping"})
        with pytest.raises(PostMergeConfigError, match="merge"):
            post_merge_steps_key_declared(tmp_path)


class TestFindCrewYamlFilesDeclaringPostMergeSteps:
    """lr-f9a01b: the shared .crew/*.yaml scan both doctor.checks.
    check_dead_crew_post_merge_config and merge.verb._run's step-10
    warning call -- one scan, two surfaces, never divergent."""

    def _write_crew_yaml(self, repo_root, filename: str, text: str) -> None:
        crew_dir = repo_root / ".crew"
        crew_dir.mkdir(parents=True, exist_ok=True)
        (crew_dir / filename).write_text(text, encoding="utf-8")

    def test_no_repo_root_returns_empty(self):
        assert find_crew_yaml_files_declaring_post_merge_steps(None) == []

    def test_no_crew_dir_returns_empty(self, tmp_path):
        assert find_crew_yaml_files_declaring_post_merge_steps(tmp_path) == []

    def test_crew_yaml_with_no_mention_returns_empty(self, tmp_path):
        self._write_crew_yaml(tmp_path, "amos.yaml", "schema_version: 1\n")
        assert find_crew_yaml_files_declaring_post_merge_steps(tmp_path) == []

    def test_top_level_mention_is_found(self, tmp_path):
        self._write_crew_yaml(
            tmp_path, "amos.yaml", "post_merge_steps:\n  - cmd: 'make install'\n"
        )
        result = find_crew_yaml_files_declaring_post_merge_steps(tmp_path)
        assert result == [str(tmp_path / ".crew" / "amos.yaml")]

    def test_nested_merge_section_mention_is_found(self, tmp_path):
        self._write_crew_yaml(
            tmp_path,
            "naomi.yaml",
            "merge:\n  post_merge_steps:\n    - cmd: 'make deploy'\n",
        )
        result = find_crew_yaml_files_declaring_post_merge_steps(tmp_path)
        assert result == [str(tmp_path / ".crew" / "naomi.yaml")]

    def test_malformed_yaml_is_skipped(self, tmp_path):
        self._write_crew_yaml(tmp_path, "amos.yaml", "not: valid: yaml: [\n")
        assert find_crew_yaml_files_declaring_post_merge_steps(tmp_path) == []

    def test_multiple_files_sorted(self, tmp_path):
        self._write_crew_yaml(
            tmp_path, "naomi.yaml", "post_merge_steps:\n  - cmd: 'b'\n"
        )
        self._write_crew_yaml(
            tmp_path, "amos.yaml", "post_merge_steps:\n  - cmd: 'a'\n"
        )
        result = find_crew_yaml_files_declaring_post_merge_steps(tmp_path)
        assert result == [
            str(tmp_path / ".crew" / "amos.yaml"),
            str(tmp_path / ".crew" / "naomi.yaml"),
        ]
