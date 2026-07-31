"""test_guard_task_dispatch.py — role-keyed Task/Agent dispatch admission
(lr-59dd37, port of the reference deployment's guard-task.py; lr-5a8d epic
Wave C).

Conformance (CLAUDE.md rule 6a): synthetic roles/subagent types only, no
LORE present, no real agent names hardcoded as expected values (BashRole
values are reused product vocabulary from role_allowlist, not agent names).
"""

from __future__ import annotations

import pytest

from clagentic_loadout.guard.role_allowlist import BashRole
from clagentic_loadout.guard.task_dispatch import (
    TaskDispatchConfig,
    check_lead_agent_dispatch,
    check_task_dispatch,
)


# ---------------------------------------------------------------------------
# check_task_dispatch — role-keyed allowlist
# ---------------------------------------------------------------------------


class TestCheckTaskDispatchRoleGrants:
    def test_allowed_subagent_type_admitted(self):
        config = TaskDispatchConfig(
            role_grants={BashRole.BUILDER: frozenset({"builder-helper", "research-tool"})}
        )
        ok, reason = check_task_dispatch(BashRole.BUILDER, "research-tool", config)
        assert ok is True
        assert reason == ""

    def test_disallowed_subagent_type_denied(self):
        config = TaskDispatchConfig(
            role_grants={BashRole.BUILDER: frozenset({"research-tool"})}
        )
        ok, reason = check_task_dispatch(BashRole.BUILDER, "merge-authority", config)
        assert ok is False
        assert "merge-authority" in reason
        assert "research-tool" in reason

    def test_empty_grant_set_denies_with_leaf_node_message(self):
        config = TaskDispatchConfig(role_grants={BashRole.MERGER: frozenset()})
        ok, reason = check_task_dispatch(BashRole.MERGER, "anything", config)
        assert ok is False
        assert "leaf node" in reason

    def test_empty_subagent_type_against_empty_grant_denies(self):
        config = TaskDispatchConfig(role_grants={BashRole.SECURITY: frozenset()})
        ok, reason = check_task_dispatch(BashRole.SECURITY, "", config)
        assert ok is False

    def test_unmapped_role_raises_value_error(self):
        config = TaskDispatchConfig(role_grants={BashRole.BUILDER: frozenset({"x"})})
        with pytest.raises(ValueError):
            check_task_dispatch(BashRole.MERGER, "x", config)

    def test_default_config_has_no_grants(self):
        config = TaskDispatchConfig()
        with pytest.raises(ValueError):
            check_task_dispatch(BashRole.BUILDER, "x", config)


# ---------------------------------------------------------------------------
# check_lead_agent_dispatch — director/lead crew-role denylist (not a
# role-keyed branch; a separate concern per module docstring point 3)
# ---------------------------------------------------------------------------


class TestCheckLeadAgentDispatch:
    def test_empty_subagent_type_allowed(self):
        ok, reason = check_lead_agent_dispatch("", frozenset({"invented-role-a"}))
        assert ok is True
        assert reason == ""

    def test_non_crew_subagent_type_allowed(self):
        ok, reason = check_lead_agent_dispatch(
            "generic-utility", frozenset({"invented-role-a", "invented-role-b"})
        )
        assert ok is True

    def test_named_crew_role_denied(self):
        ok, reason = check_lead_agent_dispatch(
            "invented-role-a", frozenset({"invented-role-a", "invented-role-b"})
        )
        assert ok is False
        assert "invented-role-a" in reason

    def test_case_sensitive_match_only(self):
        # A near-miss casing is not treated as a match — this module never
        # does its own case-folding (the caller's own registry decides its
        # own case convention).
        ok, _ = check_lead_agent_dispatch(
            "INVENTED-ROLE-A", frozenset({"invented-role-a"})
        )
        assert ok is True

    def test_empty_crew_role_names_never_denies(self):
        ok, reason = check_lead_agent_dispatch("anything-at-all", frozenset())
        assert ok is True
        assert reason == ""

    def test_caller_supplied_role_name_flows_through_to_deny_message(self):
        # A caller's own role-name string legitimately appears in the
        # returned message — it is caller-supplied data, not a hardcoded
        # literal in this module's source. Source-level anonymization
        # conformance (no fixed agent-name literal in the module itself) is
        # enforced repo-wide by tests/test_anonymization_guard.py, not
        # duplicated here.
        ok, reason = check_lead_agent_dispatch(
            "invented-role-a", frozenset({"invented-role-a"})
        )
        assert ok is False
        assert "invented-role-a" in reason
