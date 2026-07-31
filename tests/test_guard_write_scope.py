"""test_guard_write_scope.py — role-keyed Write/Edit scope enforcement
(lr-fd279d, port of the reference deployment's guard-scope.py; lr-5a8d
epic, slice 1 PATTERN-SETTER).

Conformance (CLAUDE.md rule 6a): synthetic roles/paths only, no LORE
present, no real agent names, no real machine paths.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.guard.write_scope import (
    WriteRole,
    WriteScopeConfig,
    WriteScopeMode,
    check_write_call,
    check_write_scope,
    resolve_write_scope_mode,
)


# ---------------------------------------------------------------------------
# WriteRole is a closed, role-keyed enum — no agent names anywhere.
# ---------------------------------------------------------------------------


class TestWriteRoleIsRoleKeyed:
    def test_members_are_role_names_not_agent_names(self):
        names = {member.value for member in WriteRole}
        assert names == {"scoped", "merge_gate", "lead", "read_only"}

    def test_no_known_agent_name_leaks_into_enum_values(self):
        # Conformance rule 6a: invented role names only, never a specific
        # deployment's identity roster.
        forbidden = {"amos", "naomi", "peaches", "bobbie", "miller", "drummer", "prax", "holden"}
        for member in WriteRole:
            assert member.value not in forbidden


# ---------------------------------------------------------------------------
# WriteScopeMode resolution
# ---------------------------------------------------------------------------


class TestResolveWriteScopeMode:
    def test_allow_all_true_resolves_allow_all(self):
        config = WriteScopeConfig(allow_all=True)
        assert resolve_write_scope_mode(config) is WriteScopeMode.ALLOW_ALL

    def test_double_star_allowed_paths_is_allow_all_backcompat(self):
        config = WriteScopeConfig(allowed_paths=("**",))
        assert resolve_write_scope_mode(config) is WriteScopeMode.ALLOW_ALL

    def test_multi_entry_with_double_star_is_not_allow_all(self):
        config = WriteScopeConfig(allowed_paths=("**", "src/*"))
        assert resolve_write_scope_mode(config) is WriteScopeMode.CONTRACT

    def test_explicit_allowed_paths_is_contract_mode(self):
        config = WriteScopeConfig(allowed_paths=("src/**", "tests/**"))
        assert resolve_write_scope_mode(config) is WriteScopeMode.CONTRACT

    def test_empty_config_is_fail_closed(self):
        config = WriteScopeConfig()
        assert resolve_write_scope_mode(config) is WriteScopeMode.FAIL_CLOSED

    def test_empty_allowed_paths_list_is_fail_closed(self):
        config = WriteScopeConfig(allowed_paths=())
        assert resolve_write_scope_mode(config) is WriteScopeMode.FAIL_CLOSED


# ---------------------------------------------------------------------------
# check_write_scope — allow_all mode
# ---------------------------------------------------------------------------


class TestCheckWriteScopeAllowAll:
    def test_allow_all_permits_deep_path(self, tmp_path):
        config = WriteScopeConfig(allow_all=True)
        ok, reason = check_write_scope(
            str(tmp_path / "some" / "deep" / "nested" / "file.py"), tmp_path, config
        )
        assert ok is True, reason

    def test_allow_all_permits_top_level_file(self, tmp_path):
        config = WriteScopeConfig(allow_all=True)
        ok, reason = check_write_scope(str(tmp_path / "README.md"), tmp_path, config)
        assert ok is True, reason

    def test_allow_all_blocked_path_still_denies(self, tmp_path):
        config = WriteScopeConfig(allow_all=True, blocked_paths=("secrets/*",))
        ok, reason = check_write_scope(
            str(tmp_path / "secrets" / "api_key.txt"), tmp_path, config
        )
        assert ok is False
        assert "blocked_paths" in reason

    def test_allow_all_non_blocked_path_allowed_when_blocked_present(self, tmp_path):
        config = WriteScopeConfig(allow_all=True, blocked_paths=("secrets/*",))
        ok, reason = check_write_scope(str(tmp_path / "src" / "build.py"), tmp_path, config)
        assert ok is True, reason


# ---------------------------------------------------------------------------
# check_write_scope — contract mode (explicit allowed_paths)
# ---------------------------------------------------------------------------


class TestCheckWriteScopeContract:
    def test_in_scope_path_allowed(self, tmp_path):
        config = WriteScopeConfig(allowed_paths=("src/**", "tests/**"))
        ok, reason = check_write_scope(str(tmp_path / "src" / "widget.py"), tmp_path, config)
        assert ok is True, reason

    def test_out_of_scope_path_denied(self, tmp_path):
        config = WriteScopeConfig(allowed_paths=("src/**",))
        ok, reason = check_write_scope(str(tmp_path / "docs" / "notes.md"), tmp_path, config)
        assert ok is False
        assert "allowed_paths" in reason

    def test_blocked_wins_over_contract_match(self, tmp_path):
        config = WriteScopeConfig(
            allowed_paths=("src/**",), blocked_paths=("src/secrets/**",)
        )
        ok, reason = check_write_scope(
            str(tmp_path / "src" / "secrets" / "key.pem"), tmp_path, config
        )
        assert ok is False
        assert "blocked_paths" in reason


# ---------------------------------------------------------------------------
# check_write_scope — fail-closed mode
# ---------------------------------------------------------------------------


class TestCheckWriteScopeFailClosed:
    def test_empty_config_denies_any_path(self, tmp_path):
        config = WriteScopeConfig()
        ok, reason = check_write_scope(str(tmp_path / "anything.py"), tmp_path, config)
        assert ok is False

    def test_fail_closed_denial_is_actionable(self, tmp_path):
        config = WriteScopeConfig()
        ok, reason = check_write_scope(str(tmp_path / "x.py"), tmp_path, config)
        assert ok is False
        assert "allow_all" in reason
        assert "allowed_paths" in reason


# ---------------------------------------------------------------------------
# check_write_scope — outside project root
# ---------------------------------------------------------------------------


class TestCheckWriteScopeOutsideRoot:
    def test_path_outside_root_denied_even_under_allow_all(self, tmp_path):
        config = WriteScopeConfig(allow_all=True)
        outside = tmp_path.parent / "outside-file.txt"
        ok, reason = check_write_scope(str(outside), tmp_path, config)
        assert ok is False
        assert "outside project root" in reason


# ---------------------------------------------------------------------------
# check_write_call — role dispatch
# ---------------------------------------------------------------------------


class TestCheckWriteCallRoleDispatch:
    def test_merge_gate_role_always_denied(self, tmp_path):
        ok, reason = check_write_call(WriteRole.MERGE_GATE, str(tmp_path / "f.py"))
        assert ok is False
        assert "merge-authority" in reason or "release" in reason

    def test_lead_role_always_denied(self, tmp_path):
        ok, reason = check_write_call(WriteRole.LEAD, str(tmp_path / "f.py"))
        assert ok is False
        assert "read-only" in reason

    def test_read_only_role_always_denied(self, tmp_path):
        ok, reason = check_write_call(WriteRole.READ_ONLY, str(tmp_path / "f.py"))
        assert ok is False
        assert "no Write/Edit capability" in reason

    def test_scoped_role_without_config_raises(self, tmp_path):
        with pytest.raises(ValueError):
            check_write_call(WriteRole.SCOPED, str(tmp_path / "f.py"), project_root=tmp_path)

    def test_scoped_role_without_project_root_raises(self, tmp_path):
        config = WriteScopeConfig(allow_all=True)
        with pytest.raises(ValueError):
            check_write_call(WriteRole.SCOPED, str(tmp_path / "f.py"), config=config)

    def test_scoped_role_with_config_delegates_to_check_write_scope(self, tmp_path):
        config = WriteScopeConfig(allow_all=True)
        ok, reason = check_write_call(
            WriteRole.SCOPED, str(tmp_path / "f.py"), project_root=tmp_path, config=config
        )
        assert ok is True, reason

    def test_scoped_role_denies_out_of_scope(self, tmp_path):
        config = WriteScopeConfig(allowed_paths=("src/**",))
        ok, reason = check_write_call(
            WriteRole.SCOPED,
            str(tmp_path / "docs" / "x.md"),
            project_root=tmp_path,
            config=config,
        )
        assert ok is False


# ---------------------------------------------------------------------------
# fnmatch depth semantics pin (both '*' and '**' cross '/' — carried over
# from the reference deployment's lr-cec6 regression, still true here since
# this module also uses stdlib fnmatch).
# ---------------------------------------------------------------------------


class TestFnmatchDepthSemantics:
    def test_single_star_allowed_path_matches_nested_file(self, tmp_path):
        config = WriteScopeConfig(allowed_paths=("src/*",))
        ok, _ = check_write_scope(
            str(tmp_path / "src" / "nested" / "deep" / "file.py"), tmp_path, config
        )
        assert ok is True

    def test_double_star_allowed_path_matches_nested_file(self, tmp_path):
        config = WriteScopeConfig(allowed_paths=("src/**",))
        ok, _ = check_write_scope(
            str(tmp_path / "src" / "nested" / "deep" / "file.py"), tmp_path, config
        )
        assert ok is True
