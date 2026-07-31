"""test_merge_gate_config.py — unit tests for clagentic_loadout.merge.gate_config
(lr-0a03c3).

Covers the repo-local `.clagentic/loadout/config.yaml` `merge:` gate-
declaration keys: `merge_requirements` (tests_pass/ci_pass/max_changed_files),
`required_reviewer_roles`, `authorized_roles`. All three are repo-tier
(committed, public-safe, role-vocabulary-only) config homes for values that
were previously CLI-flag-only on `merge.verb`.
"""

from __future__ import annotations

import pytest
import yaml

from clagentic_loadout.merge.gate_config import (
    CONFIG_KEY_AUTHORIZED_ROLES,
    CONFIG_KEY_MERGE_REQUIREMENTS,
    CONFIG_KEY_REQUIRED_REVIEWER_ROLES,
    CONFIG_SECTION_MERGE,
    DEFAULT_CONFIG_RELATIVE_PATH,
    InvalidMergeGateConfigError,
    RequiredReviewerRolesNotDeclaredError,
    load_authorized_roles,
    load_merge_requirements,
    load_required_reviewer_roles,
)


def _write_config(tmp_path, content: dict) -> None:
    config_dir = tmp_path / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(yaml.safe_dump(content), encoding="utf-8")


class TestMergeRequirementsAbsence:
    def test_no_repo_root_returns_empty(self):
        assert load_merge_requirements(None) == {}

    def test_no_config_file_returns_empty(self, tmp_path):
        assert load_merge_requirements(tmp_path) == {}

    def test_no_merge_section_returns_empty(self, tmp_path):
        _write_config(tmp_path, {"wait": {}})
        assert load_merge_requirements(tmp_path) == {}

    def test_merge_section_with_no_requirements_key_returns_empty(self, tmp_path):
        _write_config(tmp_path, {"merge": {"post_merge_steps": []}})
        assert load_merge_requirements(tmp_path) == {}


class TestMergeRequirementsPresent:
    def test_full_mapping_parsed(self, tmp_path):
        _write_config(
            tmp_path,
            {
                "merge": {
                    "merge_requirements": {
                        "tests_pass": True,
                        "ci_pass": False,
                        "max_changed_files": 25,
                    }
                }
            },
        )
        reqs = load_merge_requirements(tmp_path)
        assert reqs == {"tests_pass": True, "ci_pass": False, "max_changed_files": 25}

    def test_partial_mapping_keeps_only_declared_keys(self, tmp_path):
        _write_config(tmp_path, {"merge": {"merge_requirements": {"ci_pass": True}}})
        assert load_merge_requirements(tmp_path) == {"ci_pass": True}


class TestMergeRequirementsMalformed:
    def test_non_mapping_requirements_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"merge_requirements": ["tests_pass"]}})
        with pytest.raises(InvalidMergeGateConfigError, match="must be a mapping"):
            load_merge_requirements(tmp_path)

    def test_unknown_key_raises(self, tmp_path):
        _write_config(
            tmp_path, {"merge": {"merge_requirements": {"tests_pas": True}}}
        )
        with pytest.raises(InvalidMergeGateConfigError, match="unknown key"):
            load_merge_requirements(tmp_path)

    def test_non_bool_tests_pass_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"merge_requirements": {"tests_pass": "yes"}}})
        with pytest.raises(InvalidMergeGateConfigError, match="must be a boolean"):
            load_merge_requirements(tmp_path)

    def test_non_bool_ci_pass_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"merge_requirements": {"ci_pass": 1}}})
        with pytest.raises(InvalidMergeGateConfigError, match="must be a boolean"):
            load_merge_requirements(tmp_path)

    def test_zero_max_changed_files_raises(self, tmp_path):
        _write_config(
            tmp_path, {"merge": {"merge_requirements": {"max_changed_files": 0}}}
        )
        with pytest.raises(InvalidMergeGateConfigError, match="positive integer"):
            load_merge_requirements(tmp_path)

    def test_bool_max_changed_files_rejected(self, tmp_path):
        # bool is a subclass of int in Python -- must not silently pass as
        # a valid max_changed_files value.
        _write_config(
            tmp_path, {"merge": {"merge_requirements": {"max_changed_files": True}}}
        )
        with pytest.raises(InvalidMergeGateConfigError, match="positive integer"):
            load_merge_requirements(tmp_path)

    def test_non_int_max_changed_files_raises(self, tmp_path):
        _write_config(
            tmp_path, {"merge": {"merge_requirements": {"max_changed_files": "50"}}}
        )
        with pytest.raises(InvalidMergeGateConfigError, match="positive integer"):
            load_merge_requirements(tmp_path)

    def test_non_mapping_merge_section_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": ["bad"]})
        with pytest.raises(InvalidMergeGateConfigError, match="must be a mapping"):
            load_merge_requirements(tmp_path)


class TestRequiredReviewerRoles:
    def test_no_config_file_returns_empty_tuple(self, tmp_path):
        # No merge: section at all (no config file whatsoever) -- nothing to
        # be explicit about, shape 1 of the lr-638945 three-shape contract.
        assert load_required_reviewer_roles(tmp_path) == ()

    def test_no_merge_section_returns_empty_tuple(self, tmp_path):
        _write_config(tmp_path, {"wait": {}})
        assert load_required_reviewer_roles(tmp_path) == ()

    def test_no_repo_root_returns_empty_tuple(self):
        assert load_required_reviewer_roles(None) == ()

    def test_merge_section_present_key_omitted_raises(self, tmp_path):
        # Shape 3: merge: exists (post_merge_steps declared) but omits
        # required_reviewer_roles entirely -- must raise, not silently
        # return (), per the lr-638945 hardening.
        _write_config(tmp_path, {"merge": {"post_merge_steps": []}})
        with pytest.raises(
            RequiredReviewerRolesNotDeclaredError, match="omits.*required_reviewer_roles"
        ):
            load_required_reviewer_roles(tmp_path)

    def test_not_declared_error_is_invalid_merge_gate_config_error_subclass(self):
        assert issubclass(RequiredReviewerRolesNotDeclaredError, InvalidMergeGateConfigError)

    def test_explicit_empty_list_is_the_deliberate_opt_out(self, tmp_path):
        # Shape 2: merge: declares required_reviewer_roles explicitly as an
        # empty list -- a deliberate "no reviewer gate" opt-out, returns ()
        # without raising.
        _write_config(tmp_path, {"merge": {"required_reviewer_roles": []}})
        assert load_required_reviewer_roles(tmp_path) == ()

    def test_role_list_parsed(self, tmp_path):
        _write_config(
            tmp_path, {"merge": {"required_reviewer_roles": ["reviewer", "security"]}}
        )
        assert load_required_reviewer_roles(tmp_path) == ("reviewer", "security")

    def test_never_agent_names_is_not_enforced_but_bare_roles_work(self, tmp_path):
        # This module has no allowlist of role names (role vocabulary is a
        # convention, not a grammar this reader enforces) -- it only
        # validates SHAPE (non-empty strings).
        _write_config(tmp_path, {"merge": {"required_reviewer_roles": ["merger"]}})
        assert load_required_reviewer_roles(tmp_path) == ("merger",)

    def test_non_list_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"required_reviewer_roles": "reviewer"}})
        with pytest.raises(InvalidMergeGateConfigError, match="must be a list"):
            load_required_reviewer_roles(tmp_path)

    def test_empty_string_entry_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"required_reviewer_roles": [""]}})
        with pytest.raises(InvalidMergeGateConfigError, match="non-empty role-name"):
            load_required_reviewer_roles(tmp_path)

    def test_non_string_entry_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"required_reviewer_roles": [123]}})
        with pytest.raises(InvalidMergeGateConfigError, match="non-empty role-name"):
            load_required_reviewer_roles(tmp_path)


class TestAuthorizedRoles:
    def test_absent_returns_empty_tuple(self, tmp_path):
        assert load_authorized_roles(tmp_path) == ()

    def test_no_repo_root_returns_empty_tuple(self):
        assert load_authorized_roles(None) == ()

    def test_role_list_parsed(self, tmp_path):
        _write_config(tmp_path, {"merge": {"authorized_roles": ["merger", "lead"]}})
        assert load_authorized_roles(tmp_path) == ("merger", "lead")

    def test_non_list_raises(self, tmp_path):
        _write_config(tmp_path, {"merge": {"authorized_roles": "merger"}})
        with pytest.raises(InvalidMergeGateConfigError, match="must be a list"):
            load_authorized_roles(tmp_path)


class TestSharedSectionCoexistence:
    def test_all_merge_keys_coexist(self, tmp_path):
        _write_config(
            tmp_path,
            {
                "merge": {
                    "pre_checks": [{"cmd": "make lint"}],
                    "post_merge_steps": [{"cmd": "scripts/install.sh"}],
                    "merge_requirements": {"tests_pass": True},
                    "required_reviewer_roles": ["reviewer"],
                    "authorized_roles": ["merger"],
                }
            },
        )
        assert load_merge_requirements(tmp_path) == {"tests_pass": True}
        assert load_required_reviewer_roles(tmp_path) == ("reviewer",)
        assert load_authorized_roles(tmp_path) == ("merger",)


class TestLegacyPathFallback:
    def test_legacy_path_read_with_warning(self, tmp_path, capsys):
        legacy_dir = tmp_path / ".loadout"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text(
            yaml.safe_dump({"merge": {"authorized_roles": ["merger"]}}),
            encoding="utf-8",
        )
        assert load_authorized_roles(tmp_path) == ("merger",)
        assert "deprecated" in capsys.readouterr().err


class TestConstants:
    def test_section_key(self):
        assert CONFIG_SECTION_MERGE == "merge"

    def test_requirement_keys(self):
        assert CONFIG_KEY_MERGE_REQUIREMENTS == "merge_requirements"
        assert CONFIG_KEY_REQUIRED_REVIEWER_ROLES == "required_reviewer_roles"
        assert CONFIG_KEY_AUTHORIZED_ROLES == "authorized_roles"

    def test_default_config_relative_path(self):
        assert DEFAULT_CONFIG_RELATIVE_PATH == ".clagentic/loadout/config.yaml"
