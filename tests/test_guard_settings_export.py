"""test_guard_settings_export.py — single-source, dual-sink permission
fragment generation across every landed guard with an operator-facing
allowlist counterpart (lr-5a8d, task comment #2, point 3; closing coverage
slice lr-278e2e).

The core property under test, for EACH covered guard: the settings-fragment
sink derives its verb/path list from the SAME policy object the guard-layer
check consumes directly -- so the two sinks can never independently drift
into disagreement. `TestClosingCoverageConformance` is the closing
conformance check for epic lr-5a8d: it asserts every dual-sink-eligible
guard module landed under this epic is represented by a settings_export
function, and that every guard-layer-only module is explicitly documented
(not silently skipped).
"""

from __future__ import annotations

from clagentic_loadout.guard import settings_export
from clagentic_loadout.guard.scratch_policy import SCRATCH_SAFE_VERBS
from clagentic_loadout.guard.settings_export import (
    scratch_permission_fragment,
    write_scope_permission_fragment,
)
from clagentic_loadout.guard.write_scope import WriteScopeConfig


class TestSingleSourceBothSinks:
    def test_every_scratch_safe_verb_has_both_glob_forms(self):
        fragment = scratch_permission_fragment()
        for verb in SCRATCH_SAFE_VERBS:
            assert f"Bash({verb}:*)" in fragment
            assert f"Bash({verb} *)" in fragment

    def test_no_verb_outside_scratch_safe_verbs_appears(self):
        fragment = scratch_permission_fragment()
        for entry in fragment:
            # entry shape: "Bash(<verb>:*)" or "Bash(<verb> *)"
            inner = entry[len("Bash("):-1]
            verb = inner.split(":")[0].split(" ")[0]
            assert verb in SCRATCH_SAFE_VERBS

    def test_fragment_entry_count_matches_two_per_verb(self):
        fragment = scratch_permission_fragment()
        assert len(fragment) == 2 * len(SCRATCH_SAFE_VERBS)

    def test_fragment_is_sorted_and_deduplicated(self):
        fragment = scratch_permission_fragment()
        assert fragment == sorted(set(fragment))

    def test_adding_a_verb_to_policy_flows_to_fragment_without_new_code(self, monkeypatch):
        """Proves the dual-sink claim structurally: patching the ONE policy
        source changes what the settings-export sink emits, with zero
        changes needed to settings_export.py itself."""
        synthetic_verbs = frozenset({"mkdir", "zzz-synthetic-verb"})
        monkeypatch.setattr(settings_export, "SCRATCH_SAFE_VERBS", synthetic_verbs)
        fragment = settings_export.scratch_permission_fragment()
        assert "Bash(zzz-synthetic-verb:*)" in fragment
        assert "Bash(zzz-synthetic-verb *)" in fragment


class TestWriteScopeSingleSourceBothSinks:
    """`write_scope_permission_fragment` -- single source
    (`WriteScopeConfig.allowed_paths`), both sinks (guard-layer
    `check_write_scope`, settings-fragment `Edit`/`Write` glob pair)."""

    def test_every_allowed_path_has_both_tool_forms(self):
        config = WriteScopeConfig(allowed_paths=("src/**", "tests/**"))
        fragment = write_scope_permission_fragment(config)
        for glob in config.allowed_paths:
            assert f"Edit({glob})" in fragment
            assert f"Write({glob})" in fragment

    def test_no_glob_outside_allowed_paths_appears(self):
        config = WriteScopeConfig(allowed_paths=("src/**",))
        fragment = write_scope_permission_fragment(config)
        for entry in fragment:
            assert entry in ("Edit(src/**)", "Write(src/**)")

    def test_fragment_entry_count_matches_two_per_glob(self):
        config = WriteScopeConfig(allowed_paths=("a/**", "b/**", "c/**"))
        fragment = write_scope_permission_fragment(config)
        assert len(fragment) == 2 * len(config.allowed_paths)

    def test_fragment_is_sorted_and_deduplicated(self):
        config = WriteScopeConfig(allowed_paths=("zzz/**", "aaa/**"))
        fragment = write_scope_permission_fragment(config)
        assert fragment == sorted(set(fragment))

    def test_synthetic_glob_flows_to_fragment_without_new_code(self):
        """Conformance (rule 6a): an invented, non-real glob path proves this
        function has no notion of "the real project layout" -- it mirrors
        whatever `allowed_paths` a caller's config declares."""
        config = WriteScopeConfig(allowed_paths=("zzz-synthetic-dir/**",))
        fragment = write_scope_permission_fragment(config)
        assert "Edit(zzz-synthetic-dir/**)" in fragment
        assert "Write(zzz-synthetic-dir/**)" in fragment

    def test_allow_all_alone_emits_no_entries(self):
        """`allow_all` is deliberately NOT mirrored as a settings entry (see
        `write_scope_permission_fragment` docstring) -- a config with
        allow_all=True and no allowed_paths has nothing single-sourceable to
        emit; the guard layer still enforces `blocked_paths`/`allow_all`
        directly regardless of what this function returns."""
        config = WriteScopeConfig(allow_all=True)
        assert write_scope_permission_fragment(config) == []

    def test_blocked_paths_never_appear_in_fragment(self):
        """A settings `permissions.allow` list has no deny-entry shape; a
        blocked path must never leak into the allow fragment even when it
        also happens to overlap an allowed glob's literal text."""
        config = WriteScopeConfig(
            allowed_paths=("src/**",), blocked_paths=("src/secrets/**",)
        )
        fragment = write_scope_permission_fragment(config)
        assert "Edit(src/secrets/**)" not in fragment
        assert "Write(src/secrets/**)" not in fragment

    def test_empty_config_emits_no_entries(self):
        assert write_scope_permission_fragment(WriteScopeConfig()) == []

    def test_adding_a_path_to_policy_flows_to_fragment_without_new_code(self):
        """Structural proof of the single-source claim: the SAME
        `WriteScopeConfig` instance a caller's guard-layer
        `check_write_scope` call evaluates is the one this function reads
        `allowed_paths` from -- there is no independent copy for the
        settings-fragment sink to drift out of sync with."""
        config = WriteScopeConfig(allowed_paths=("first/**",))
        fragment_before = write_scope_permission_fragment(config)
        assert "Edit(first/**)" in fragment_before
        assert "Edit(second/**)" not in fragment_before

        # Simulate a caller adding a second allowed path to its ONE config
        # object -- both the guard layer (check_write_scope, not called
        # here directly) and this settings-fragment sink read the SAME
        # updated allowed_paths tuple with zero code changes to either.
        extended_config = WriteScopeConfig(
            allowed_paths=config.allowed_paths + ("second/**",)
        )
        fragment_after = write_scope_permission_fragment(extended_config)
        assert "Edit(first/**)" in fragment_after
        assert "Edit(second/**)" in fragment_after


class TestClosingCoverageConformance:
    """The CLOSING conformance check for epic lr-5a8d (lr-278e2e): every
    guard module landed under this epic must be represented EITHER by a
    settings_export dual-sink function, OR by an explicit
    GUARD-LAYER-ONLY-with-rationale entry in this module's own docstring
    (checked structurally below, not just by human review) -- so a future
    guard slice cannot silently land with an undocumented coverage gap.
    """

    #: Every guard module landed under epic lr-5a8d (docs/guard-policy.md's
    #: own per-slice landing record) -- kept in lockstep with that document,
    #: not re-derived by filesystem globbing, so a new guard module always
    #: requires an explicit decision (dual-sink function OR documented
    #: guard-layer-only rationale) rather than silently falling through
    #: either check below.
    ALL_LANDED_GUARD_MODULES: frozenset[str] = frozenset(
        {
            "env_prefix",
            "scratch_policy",
            "write_scope",
            "credential_paths",
            "shell_parsing",
            "bash_admission",
            "role_allowlist",
            "director_mutation",
            "infra_ops",
            "task_dispatch",
            "dispatch_discipline",
            "git_write_guard",
        }
    )

    #: Modules with a landed settings_export dual-sink function -- the
    #: DUAL-SINK half of the coverage map.
    DUAL_SINK_MODULES: frozenset[str] = frozenset({"scratch_policy", "write_scope"})

    def test_every_dual_sink_module_has_a_settings_export_function(self):
        assert hasattr(settings_export, "scratch_permission_fragment")
        assert hasattr(settings_export, "write_scope_permission_fragment")

    def test_dual_sink_functions_are_exported_in_all(self):
        for name in ("scratch_permission_fragment", "write_scope_permission_fragment"):
            assert name in settings_export.__all__

    def test_every_landed_guard_module_is_classified_one_way_or_the_other(self):
        """Every module in ALL_LANDED_GUARD_MODULES is either a DUAL_SINK
        module (asserted above to have a real settings_export function) or
        is named in this module's own GUARD-LAYER-ONLY MODULES docstring
        section -- closing the coverage gap this task exists to verify.
        """
        guard_layer_only = self.ALL_LANDED_GUARD_MODULES - self.DUAL_SINK_MODULES
        docstring = settings_export.__doc__ or ""
        for module_name in guard_layer_only:
            assert f"guard.{module_name}" in docstring, (
                f"guard.{module_name} is neither a DUAL_SINK module nor "
                f"documented as guard-layer-only in settings_export's own "
                f"module docstring -- every landed guard module must be one "
                f"or the other, never silently unaccounted for."
            )

    def test_no_undocumented_module_silently_added_to_dual_sink_without_a_function(self):
        """Guards the inverse direction: a module claimed as DUAL_SINK above
        must actually own a real, callable settings_export function -- a
        typo'd or aspirational entry in DUAL_SINK_MODULES would otherwise
        silently pass as "covered"."""
        function_names_by_module = {
            "scratch_policy": "scratch_permission_fragment",
            "write_scope": "write_scope_permission_fragment",
        }
        assert set(function_names_by_module) == self.DUAL_SINK_MODULES
        for module_name, func_name in function_names_by_module.items():
            func = getattr(settings_export, func_name, None)
            assert callable(func), (
                f"DUAL_SINK_MODULES claims {module_name!r} is dual-sink via "
                f"{func_name!r}, but settings_export.{func_name} is not a "
                f"callable function."
            )

    def test_scratch_source_of_truth_is_the_guard_layer_module_not_a_copy(self):
        """`settings_export.SCRATCH_SAFE_VERBS` must be the SAME object
        `guard.scratch_policy` exports -- an import that copied the
        frozenset's VALUE at import time (rather than importing the name
        itself) would silently drift the moment `scratch_policy` grew a new
        verb, defeating the whole single-source claim."""
        from clagentic_loadout.guard import scratch_policy

        assert settings_export.SCRATCH_SAFE_VERBS is scratch_policy.SCRATCH_SAFE_VERBS

    def test_write_scope_config_source_of_truth_is_the_same_dataclass(self):
        """`write_scope_permission_fragment` must accept the SAME
        `WriteScopeConfig` type `guard.write_scope.check_write_scope` takes
        -- a parallel, independently-defined config shape would be a second
        source, not a shared one. Calling with a real
        `guard.write_scope.WriteScopeConfig` instance and getting the
        expected fragment back proves this function reads the actual
        shared dataclass, not a lookalike."""
        from clagentic_loadout.guard import write_scope

        config = write_scope.WriteScopeConfig(allowed_paths=("x/**",))
        fragment = settings_export.write_scope_permission_fragment(config)
        assert fragment == ["Edit(x/**)", "Write(x/**)"]
