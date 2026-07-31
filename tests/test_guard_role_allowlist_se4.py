"""test_guard_role_allowlist_se4.py — INFRA (host-operator) role wiring in
`guard.role_allowlist` (lr-6f61aa, sub-epic lr-19ae42 sub-slice SE4, epic
lr-5a8d Wave C — THIS SLICE COMPLETES lr-19ae42, all four sub-slices now
landed).

Conformance (CLAUDE.md rule 6a): synthetic verb names, paths, and roles
only -- no real agent names, no LORE present, no real machine identifiers.

Tests move with their subjects (CLAUDE.md rule 6) -- this file is scoped to
the SE4 addition: `BashRole.INFRA`, `check_infra_command`, and
`check_bash_call`'s INFRA dispatch branch. `guard.infra_ops`'s own admission
logic is covered directly in test_guard_infra_ops.py.
"""

from __future__ import annotations

import re

from clagentic_loadout.guard.infra_ops import InfraOpsConfig, InfraOpWrapper
from clagentic_loadout.guard.role_allowlist import (
    BashRole,
    check_bash_call,
    check_infra_command,
)


def _install_binary_wrapper() -> InfraOpWrapper:
    return InfraOpWrapper(
        verb_name="infra-install-binary",
        required_flags=("--host", "--package", "--version"),
    )


# ---------------------------------------------------------------------------
# BashRole.INFRA is role-keyed, not agent-named.
# ---------------------------------------------------------------------------


class TestBashRoleInfraIsRoleKeyed:
    def test_infra_member_present(self):
        names = {member.value for member in BashRole}
        assert "infra" in names

    def test_no_known_agent_name_leaks_into_enum_values(self):
        forbidden = {
            "amos", "naomi", "peaches", "bobbie", "miller", "drummer",
            "prax", "avasarala", "holden", "director", "ashford",
        }
        for member in BashRole:
            assert member.value not in forbidden

    def test_all_eight_roles_present_lr_19ae42_complete(self):
        # This slice completes lr-19ae42 (all four sub-slices landed):
        # BUILDER/MERGER (SE1), REVIEWER/SECURITY/ANALYSIS/RESEARCH/
        # PLANNING_READER (SE2), LEAD (SE3), INFRA (SE4).
        names = {member.value for member in BashRole}
        assert names == {
            "builder", "merger", "reviewer", "security", "analysis",
            "research", "planning_reader", "lead", "infra",
        }


# ---------------------------------------------------------------------------
# check_infra_command — thin composition wrapper.
# ---------------------------------------------------------------------------


class TestCheckInfraCommand:
    def test_admits_configured_op_wrapper(self):
        cfg = InfraOpsConfig(op_wrappers=(_install_binary_wrapper(),))
        ok, reason = check_infra_command(
            "infra-install-binary --host h --package p --version 1",
            config=cfg,
        )
        assert ok is True, reason

    def test_denies_unconfigured_command(self):
        ok, reason = check_infra_command("rm -rf /")
        assert ok is False

    def test_none_config_still_admits_fixed_lore_subset(self):
        ok, reason = check_infra_command("lore observe finding")
        assert ok is True, reason

    def test_denies_git(self):
        ok, reason = check_infra_command("git push --force")
        assert ok is False


# ---------------------------------------------------------------------------
# check_bash_call dispatch.
# ---------------------------------------------------------------------------


class TestCheckBashCallInfraDispatch:
    def test_dispatches_to_check_infra_command(self):
        cfg = InfraOpsConfig(op_wrappers=(_install_binary_wrapper(),))
        ok, reason = check_bash_call(
            BashRole.INFRA,
            "infra-install-binary --host h --package p --version 1",
            infra_config=cfg,
        )
        assert ok is True, reason

    def test_dispatch_denies_unconfigured_command(self):
        ok, reason = check_bash_call(BashRole.INFRA, "git push --force")
        assert ok is False

    def test_infra_role_never_raises_value_error(self):
        # BashRole.INFRA is now a registered checker -- check_bash_call
        # must not fall through to the "no registered checker" ValueError.
        ok, reason = check_bash_call(BashRole.INFRA, "lore search x")
        assert ok is True, reason

    def test_other_role_params_ignored_for_infra_dispatch(self):
        cfg = InfraOpsConfig(op_wrappers=(_install_binary_wrapper(),))
        ok, reason = check_bash_call(
            BashRole.INFRA,
            "infra-install-binary --host h --package p --version 1",
            infra_config=cfg,
            lead_identity_label="unused-for-infra",
            scanner_patterns=(re.compile(r"^unused$"),),
        )
        assert ok is True, reason
