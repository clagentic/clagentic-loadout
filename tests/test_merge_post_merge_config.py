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

import subprocess

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
    CONFIG_KEY_FOREIGN_CONFIG_GLOBS,
    find_foreign_config_files_declaring_post_merge_steps,
    load_post_merge_steps,
    load_post_merge_steps_from_git_sha,
    post_merge_steps_key_declared,
    resolve_enforce_merge_shape,
    resolve_enforce_single_verdict_fence,
    resolve_foreign_config_globs,
    resolve_git_tree_relative_config_paths,
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

    def test_custom_legacy_relative_path_honored_for_wrapper_hop_root_resolution(
        self, tmp_path
    ):
        """lr-cd3644 fold-in #3 (PR #30 re-review finding D): a CALLER-
        supplied *legacy_relative_path* (mirroring every other resolver's
        own override knob) must be honored for BOTH the config-file read
        AND the config-ROOT re-derivation this function performs afterward
        -- before this fix, the root re-derivation hardcoded the module-
        level LEGACY_CONFIG_RELATIVE_PATH constant directly, ignoring a
        caller's own override, which is observable ONLY in the lr-18f46a
        bounded-wrapper-hop shape (resolve_repo_config_root's own hop):
        *repo_root* itself (the inner git tree) carries NEITHER candidate,
        so the hop must climb to the wrapper -- which carries the config
        ONLY at a CUSTOM legacy-relative path, not the module's own default
        legacy path. If the root re-derivation silently reverted to the
        hardcoded default legacy constant, the hop would look for the
        WRONG candidate at the wrapper and never find it, falling back to
        the un-hopped repo_root and returning None instead of the correct
        resolved working-tree path."""
        wrapper = tmp_path / "wrapper"
        repo = wrapper / "repo"
        repo.mkdir(parents=True)
        git_init = subprocess.run(
            ["git", "init", "--quiet"], cwd=str(repo), capture_output=True, text=True
        )
        assert git_init.returncode == 0, git_init.stderr
        custom_legacy = "custom/legacy-config.yaml"
        legacy_path = wrapper / "custom" / "legacy-config.yaml"
        legacy_path.parent.mkdir(parents=True)
        legacy_path.write_text(
            yaml.safe_dump({"merge": {"git_working_tree": "inner"}}),
            encoding="utf-8",
        )
        resolved = resolve_git_working_tree(
            repo,
            config_relative_path="custom/new-config.yaml",
            legacy_relative_path=custom_legacy,
        )
        assert resolved == wrapper / "inner"


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


def find_crew_yaml_files_declaring_post_merge_steps(repo_root):
    """The shared scan as the deployment that keeps `.crew/*.yaml` configures
    it: those files are the foreign surface."""
    return find_foreign_config_files_declaring_post_merge_steps(repo_root, [".crew/*.yaml"])


class TestResolveForeignConfigGlobs:
    def test_absent_config_is_empty_so_the_cross_check_is_off(self, tmp_path):
        assert resolve_foreign_config_globs(config_root=tmp_path) == ()

    def test_absent_key_is_empty(self, tmp_path):
        (tmp_path / "config.yaml").write_text("merge: {}\n", encoding="utf-8")
        assert resolve_foreign_config_globs(config_root=tmp_path) == ()

    def test_listed_globs_are_returned_in_order(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"merge": {CONFIG_KEY_FOREIGN_CONFIG_GLOBS: ["a/*.yaml", " b/*.yml "]}}),
            encoding="utf-8",
        )
        assert resolve_foreign_config_globs(config_root=tmp_path) == ("a/*.yaml", "b/*.yml")

    @pytest.mark.parametrize("bad", ["a/*.yaml", {"a": 1}, 3, None])
    def test_non_list_value_is_ignored(self, tmp_path, bad):
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"merge": {CONFIG_KEY_FOREIGN_CONFIG_GLOBS: bad}}), encoding="utf-8"
        )
        assert resolve_foreign_config_globs(config_root=tmp_path) == ()

    def test_bad_entries_are_dropped_and_good_ones_kept(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "merge": {
                        CONFIG_KEY_FOREIGN_CONFIG_GLOBS: [
                            "ok/*.yaml", "", "  ", 7, "/abs/*.yaml", "../up/*.yaml", "a/../b/*.yaml",
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )
        assert resolve_foreign_config_globs(config_root=tmp_path) == ("ok/*.yaml",)


class TestFindForeignConfigFilesEdgeCases:
    def test_empty_globs_scan_nothing_even_when_a_file_declares_the_key(self, tmp_path):
        foreign = tmp_path / ".crew"
        foreign.mkdir()
        (foreign / "amos.yaml").write_text("post_merge_steps: []\n", encoding="utf-8")
        assert find_foreign_config_files_declaring_post_merge_steps(tmp_path, []) == []
        assert find_foreign_config_files_declaring_post_merge_steps(tmp_path, ()) == []

    def test_overlapping_globs_report_each_file_once(self, tmp_path):
        foreign = tmp_path / "x"
        foreign.mkdir()
        (foreign / "a.yaml").write_text("post_merge_steps: []\n", encoding="utf-8")
        result = find_foreign_config_files_declaring_post_merge_steps(
            tmp_path, ["x/*.yaml", "x/a.*"]
        )
        assert result == [str(foreign / "a.yaml")]

    def test_a_directory_matching_the_glob_is_not_read(self, tmp_path):
        (tmp_path / "x" / "sub.yaml").mkdir(parents=True)
        assert find_foreign_config_files_declaring_post_merge_steps(tmp_path, ["x/*"]) == []


class TestFindCrewYamlFilesDeclaringPostMergeSteps:
    """The shared foreign-config scan both doctor.checks.
    check_dead_foreign_post_merge_config and merge.verb._run's step-10
    warning call -- one scan, two surfaces, never divergent. Exercised here
    with `.crew/*.yaml` as the configured glob."""

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


def _git(args: list[str], *, cwd) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(cwd))
    assert result.returncode == 0, f"git {args!r} failed: {result.stderr}"
    return result


def _init_git_repo(tmp_path) -> None:
    _git(["init", "-b", "main"], cwd=tmp_path)
    _git(["config", "user.email", "test@example.com"], cwd=tmp_path)
    _git(["config", "user.name", "test"], cwd=tmp_path)


def _commit_config(tmp_path, content: dict, *, message: str = "config") -> str:
    config_dir = tmp_path / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(yaml.safe_dump(content), encoding="utf-8")
    _git(["add", "."], cwd=tmp_path)
    _git(["commit", "-m", message], cwd=tmp_path)
    return _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()


class TestLoadPostMergeStepsFromGitSha:
    """lr-cd3644: load_post_merge_steps_from_git_sha reads post_merge_steps
    from the git OBJECT DATABASE at a specific commit -- via `git show
    <sha>:<path>` -- never from the working tree, so it stays correct
    regardless of what `git_tree_path`'s working tree currently has checked
    out (including a tree left checked out on a DIFFERENT, earlier commit
    than *merged_sha* itself). Returns None (not []) when the config path
    is absent from *merged_sha*'s tree entirely -- see the function's own
    docstring for why that distinction matters (a repo that never commits
    this file at all must never be compared against a fabricated [])."""

    def test_reads_steps_from_a_commit_even_when_working_tree_is_on_an_earlier_one(
        self, tmp_path
    ):
        _init_git_repo(tmp_path)
        _commit_config(tmp_path, {"merge": {"post_merge_steps": []}}, message="first")
        # Advance the branch to a SECOND commit declaring real steps...
        second_sha = _commit_config(
            tmp_path,
            {"merge": {"post_merge_steps": [{"cmd": "echo hi"}]}},
            message="second",
        )
        # ...then move the WORKING TREE back to the first commit, so a
        # filesystem-based read (load_post_merge_steps) would see zero
        # steps, while this function -- reading directly from second_sha's
        # own tree object -- must still see the one step declared there.
        _git(["checkout", "HEAD~1"], cwd=tmp_path)
        assert load_post_merge_steps(tmp_path) == []
        result = load_post_merge_steps_from_git_sha(tmp_path, second_sha)
        assert result == [{"cmd": "echo hi"}]

    def test_no_config_at_that_commit_returns_none_not_empty_list(self, tmp_path):
        # THE load-bearing distinction: absent-from-git-history is None,
        # never a fabricated [] -- a repo that never commits this file
        # (e.g. a gitignored config, this package's own dogfooding
        # convention) must never look like it "declares zero steps" when
        # compared by a caller.
        _init_git_repo(tmp_path)
        (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
        _git(["add", "."], cwd=tmp_path)
        _git(["commit", "-m", "seed, no config at all"], cwd=tmp_path)
        sha = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()
        assert load_post_merge_steps_from_git_sha(tmp_path, sha) is None

    def test_merge_section_present_but_no_steps_key_returns_empty_list(self, tmp_path):
        # Contrast with the above: the config file IS tracked here, its
        # merge: section just never mentions the key -- this is a REAL,
        # comparable "declares zero steps" resolution, so it returns [],
        # not None.
        _init_git_repo(tmp_path)
        sha = _commit_config(tmp_path, {"merge": {"some_other_key": True}})
        assert load_post_merge_steps_from_git_sha(tmp_path, sha) == []

    def test_malformed_steps_at_that_commit_raises(self, tmp_path):
        _init_git_repo(tmp_path)
        sha = _commit_config(
            tmp_path, {"merge": {"post_merge_steps": [{"on_failure": "warn"}]}}
        )
        with pytest.raises(PostMergeConfigError):
            load_post_merge_steps_from_git_sha(tmp_path, sha)

    def test_a_config_blob_that_is_not_utf8_raises_the_config_error(self, tmp_path):
        _init_git_repo(tmp_path)
        config_dir = tmp_path / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_bytes(b"merge:\n  note: \xff\xfe\n")
        _git(["add", "."], cwd=tmp_path)
        _git(["commit", "-m", "binary config"], cwd=tmp_path)
        sha = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()
        with pytest.raises(PostMergeConfigError, match="not valid UTF-8"):
            load_post_merge_steps_from_git_sha(tmp_path, sha)

    def test_merge_section_not_a_mapping_raises(self, tmp_path):
        _init_git_repo(tmp_path)
        sha = _commit_config(tmp_path, {"merge": ["not", "a", "mapping"]})
        with pytest.raises(PostMergeConfigError):
            load_post_merge_steps_from_git_sha(tmp_path, sha)

    def test_legacy_path_fallback_is_read_when_new_path_absent(self, tmp_path):
        _init_git_repo(tmp_path)
        legacy_dir = tmp_path / ".loadout"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text(
            yaml.safe_dump({"merge": {"post_merge_steps": [{"cmd": "legacy"}]}}),
            encoding="utf-8",
        )
        _git(["add", "."], cwd=tmp_path)
        _git(["commit", "-m", "legacy config only"], cwd=tmp_path)
        sha = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()
        assert load_post_merge_steps_from_git_sha(tmp_path, sha) == [{"cmd": "legacy"}]

    def test_malformed_merged_sha_fails_loud_not_silently_absent(self, tmp_path):
        """BOBBIE finding, lr-cd3644 fold-in #1 (structurally hardened by
        fold-in #3 on PR #30): a *merged_sha* that cannot even resolve to a
        commit object is a GENUINE failure, distinct from the path-absent-
        from-a-resolvable-commit's-tree shape the function's own `None`
        return is reserved for. This is now verified via `git cat-file -e
        <merged_sha>^{commit}`, checked ONCE up front before any path lookup
        is even attempted -- never by matching `git show`'s own (localized,
        ambiguous-with-path-absence) stderr text. A SHA that fails this
        existence+type check can never be reinterpreted as "path absent":
        no path has been looked up yet at the point this check runs. Must
        raise, never return None."""
        _init_git_repo(tmp_path)
        _commit_config(tmp_path, {"merge": {"post_merge_steps": [{"cmd": "true"}]}})
        with pytest.raises(PostMergeConfigError, match="cat-file"):
            load_post_merge_steps_from_git_sha(tmp_path, "not-a-valid-sha-at-all")

    def test_unresolvable_but_sha_shaped_merged_sha_fails_loud(self, tmp_path):
        """A syntactically valid 40-hex SHA that does not resolve to any
        object in this tree's object database at all must ALSO fail loud
        via the same git cat-file -e ...^{commit} existence check -- not
        merely a malformed/non-hex string (covered above). Confirms the
        up-front commit-existence gate catches an unknown-but-well-formed
        SHA the same way it catches an obviously-malformed one."""
        _init_git_repo(tmp_path)
        _commit_config(tmp_path, {"merge": {"post_merge_steps": [{"cmd": "true"}]}})
        unresolvable_sha = "a" * 40
        with pytest.raises(PostMergeConfigError, match="cat-file"):
            load_post_merge_steps_from_git_sha(tmp_path, unresolvable_sha)

    def test_non_english_locale_still_fails_loud_on_invalid_sha(
        self, tmp_path, monkeypatch
    ):
        """lr-cd3644 fold-in #3 (PEACHES re-review, PR #30): the prior
        stderr-text-matching classifier was NOT locale-independent -- git
        localizes its own diagnostic text under LC_ALL/LANG, so a non-English
        spawn environment could silently misclassify a genuine invalid-SHA
        failure as a merely-absent path. `git cat-file -e`'s exit code is
        locale-independent by construction (no stderr text is parsed at all
        for the classification decision) -- prove this holds under a non-
        English LC_ALL."""
        _init_git_repo(tmp_path)
        _commit_config(tmp_path, {"merge": {"post_merge_steps": [{"cmd": "true"}]}})
        monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
        monkeypatch.setenv("LANG", "fr_FR.UTF-8")
        with pytest.raises(PostMergeConfigError, match="cat-file"):
            load_post_merge_steps_from_git_sha(tmp_path, "not-a-valid-sha-at-all")

    def test_genuinely_absent_path_at_a_valid_commit_still_returns_none(
        self, tmp_path
    ):
        """Negative control for the fix above: a VALID commit that genuinely
        never tracked the config path at all must still return None (the
        untracked-config, gitignored-by-design shape -- see
        test_no_config_at_that_commit_returns_none_not_empty_list), never
        raise. Proves the fix narrows to invalid-SHA/genuine-failure cases
        specifically, without regressing the pre-existing absent-path
        contract."""
        _init_git_repo(tmp_path)
        (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
        _git(["add", "."], cwd=tmp_path)
        _git(["commit", "-m", "seed, no config at all"], cwd=tmp_path)
        sha = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()
        assert load_post_merge_steps_from_git_sha(tmp_path, sha) is None


class TestResolveGitTreeRelativeConfigPaths:
    """lr-cd3644 fold-in #3: re-expresses the SAME config root
    `load_post_merge_steps` resolves relative to *git_tree_path*, so
    `load_post_merge_steps_from_git_sha`'s `git show` reads the identical
    file the pre-sync read used -- see that function's own updated
    docstring for the wrapper-layout bug this closes."""

    def test_flat_layout_config_root_equals_git_tree(self, tmp_path):
        # The common case: no git_working_tree knob declared, config root
        # and git tree are the SAME directory -- both candidates resolve to
        # their own unchanged, bare relative-path defaults.
        _write_config(tmp_path, {"merge": {"post_merge_steps": [{"cmd": "true"}]}})
        config_rel, legacy_rel = resolve_git_tree_relative_config_paths(
            tmp_path, tmp_path
        )
        assert config_rel == DEFAULT_CONFIG_RELATIVE_PATH
        assert legacy_rel == ".loadout/config.yaml"

    def test_wrapper_layout_config_root_above_git_tree_yields_none(self, tmp_path):
        # THE lr-cd3644 fold-in #3 REGRESSION PROOF: config root (tmp_path)
        # carries the committed config, but the git tree is a SUBDIRECTORY
        # (tmp_path / "repo") -- the config file sits OUTSIDE that git tree
        # entirely and can never be tracked by it. Both candidates must
        # resolve to None (never a path load_post_merge_steps_from_git_sha
        # would incorrectly treat as "path absent from a comparable tree").
        _write_config(tmp_path, {"merge": {"git_working_tree": "repo"}})
        git_tree = tmp_path / "repo"
        git_tree.mkdir()
        config_rel, legacy_rel = resolve_git_tree_relative_config_paths(
            tmp_path, git_tree
        )
        assert config_rel is None
        assert legacy_rel is None

    def test_git_tree_nested_deeper_than_config_root_still_resolves(self, tmp_path):
        # A declared git_working_tree of "a/b" (nested, not just one level)
        # -- the config file still sits outside that deeper git tree, same
        # None-None contract.
        _write_config(tmp_path, {"merge": {"git_working_tree": "a/b"}})
        git_tree = tmp_path / "a" / "b"
        git_tree.mkdir(parents=True)
        config_rel, legacy_rel = resolve_git_tree_relative_config_paths(
            tmp_path, git_tree
        )
        assert config_rel is None
        assert legacy_rel is None

    def test_legacy_config_root_also_resolves_relative_to_git_tree(self, tmp_path):
        # Config root carries ONLY the legacy path -- resolve_repo_config_root
        # still finds it as the config-bearing root; the flat-layout case
        # (git tree == config root) still resolves both candidates relative
        # to that SAME root.
        legacy_dir = tmp_path / ".loadout"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text(
            yaml.safe_dump({"merge": {"post_merge_steps": [{"cmd": "true"}]}}),
            encoding="utf-8",
        )
        config_rel, legacy_rel = resolve_git_tree_relative_config_paths(
            tmp_path, tmp_path
        )
        assert config_rel == DEFAULT_CONFIG_RELATIVE_PATH
        assert legacy_rel == ".loadout/config.yaml"

    def test_wired_end_to_end_through_load_post_merge_steps_from_git_sha(
        self, tmp_path
    ):
        # End-to-end proof (mirrors the wrapper-layout regression exactly):
        # the config root's config.yaml is committed inside its OWN git
        # history (not the inner git tree's) -- resolve_git_tree_relative_
        # config_paths correctly yields (None, None) for the inner tree, and
        # load_post_merge_steps_from_git_sha correctly treats that as "not
        # comparable," returning None rather than raising or fabricating a
        # path that would try to git-show outside the inner repository.
        _write_config(tmp_path, {"merge": {"git_working_tree": "repo"}})
        inner_tree = tmp_path / "repo"
        inner_tree.mkdir()
        _init_git_repo(inner_tree)
        (inner_tree / "README.md").write_text("seed\n", encoding="utf-8")
        _git(["add", "."], cwd=inner_tree)
        _git(["commit", "-m", "seed"], cwd=inner_tree)
        sha = _git(["rev-parse", "HEAD"], cwd=inner_tree).stdout.strip()

        config_rel, legacy_rel = resolve_git_tree_relative_config_paths(
            tmp_path, inner_tree
        )
        result = load_post_merge_steps_from_git_sha(
            inner_tree,
            sha,
            config_relative_path=config_rel,
            legacy_relative_path=legacy_rel,
        )
        assert result is None
