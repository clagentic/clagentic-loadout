"""test_guard_dispatch_discipline.py — warn-only in-session-edit dispatch
discipline (lr-59dd37, port of the reference deployment's guard-dispatch.py;
lr-5a8d epic Wave C).

Conformance (CLAUDE.md rule 6a): synthetic paths only, no LORE present, no
real agent name hardcoded anywhere in the module under test.
"""

from __future__ import annotations

from clagentic_loadout.guard.dispatch_discipline import (
    DispatchGuidance,
    check_dispatch_discipline,
    is_trivial_path,
)


# ---------------------------------------------------------------------------
# is_trivial_path
# ---------------------------------------------------------------------------


class TestIsTrivialPath:
    def test_markdown_file_is_trivial(self):
        assert is_trivial_path("/some/project/README.md") is True

    def test_uppercase_markdown_extension_is_trivial(self):
        assert is_trivial_path("/some/project/NOTES.MD") is True

    def test_gitignore_is_trivial(self):
        assert is_trivial_path("/some/project/.gitignore") is True

    def test_gitattributes_is_trivial(self):
        assert is_trivial_path("/some/project/.gitattributes") is True

    def test_license_bare_is_trivial(self):
        assert is_trivial_path("/some/project/LICENSE") is True

    def test_license_with_suffix_is_trivial(self):
        assert is_trivial_path("/some/project/LICENSE.txt") is True

    def test_docs_dir_segment_is_trivial(self):
        assert is_trivial_path("/some/project/docs/guard-policy.md".replace(".md", "")) is True

    def test_crew_dir_segment_is_trivial(self):
        assert is_trivial_path("/some/project/.crew/amos.yaml") is True

    def test_lore_dir_segment_is_trivial(self):
        assert is_trivial_path("/some/project/.lore/codex.md".replace(".md", "")) is True

    def test_source_file_is_not_trivial(self):
        assert is_trivial_path("/some/project/src/module.py") is False

    def test_script_file_is_not_trivial(self):
        assert is_trivial_path("/some/project/scripts/deploy.sh") is False

    def test_malformed_path_input_is_not_trivial(self):
        assert is_trivial_path("\x00bad") is False


# ---------------------------------------------------------------------------
# check_dispatch_discipline
# ---------------------------------------------------------------------------


_GUIDANCE = DispatchGuidance(
    builder_role_label="the builder role",
    redirect_instructions="Dispatch this edit to the builder role instead.",
    override_env_var_name="EXAMPLE_INSESSION_EDIT",
)


class TestCheckDispatchDiscipline:
    def test_override_active_suppresses_warning(self):
        result = check_dispatch_discipline(
            "/repo/src/module.py",
            is_named_agent=False,
            guidance=_GUIDANCE,
            override_active=True,
        )
        assert result is None

    def test_named_agent_suppresses_warning(self):
        result = check_dispatch_discipline(
            "/repo/src/module.py",
            is_named_agent=True,
            guidance=_GUIDANCE,
        )
        assert result is None

    def test_trivial_path_suppresses_warning(self):
        result = check_dispatch_discipline(
            "/repo/README.md",
            is_named_agent=False,
            guidance=_GUIDANCE,
        )
        assert result is None

    def test_build_territory_edit_from_non_agent_warns(self):
        result = check_dispatch_discipline(
            "/repo/src/module.py",
            is_named_agent=False,
            guidance=_GUIDANCE,
        )
        assert result is not None
        assert "/repo/src/module.py" in result
        assert "the builder role" in result
        assert "Dispatch this edit to the builder role instead." in result

    def test_override_env_var_name_surfaced_in_message(self):
        result = check_dispatch_discipline(
            "/repo/src/module.py",
            is_named_agent=False,
            guidance=_GUIDANCE,
        )
        assert "EXAMPLE_INSESSION_EDIT=1" in result

    def test_no_override_env_var_name_omits_suppression_line(self):
        guidance = DispatchGuidance(
            builder_role_label="the builder role",
            redirect_instructions="Dispatch it.",
        )
        result = check_dispatch_discipline(
            "/repo/src/module.py", is_named_agent=False, guidance=guidance
        )
        assert result is not None
        assert "To suppress this warning" not in result

    def test_never_returns_a_deny_shaped_value(self):
        # This module has no deny outcome at all (module docstring point 2)
        # — the only return type is str | None, never a (bool, str) tuple.
        result = check_dispatch_discipline(
            "/repo/src/module.py", is_named_agent=False, guidance=_GUIDANCE
        )
        assert isinstance(result, str)

# Source-level anonymization conformance (no fixed agent-name literal in the
# module itself) is enforced repo-wide by tests/test_anonymization_guard.py,
# not duplicated here.
