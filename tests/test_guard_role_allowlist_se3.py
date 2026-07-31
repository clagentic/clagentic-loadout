"""test_guard_role_allowlist_se3.py — LEAD Bash-command allow-checker (PR1)
(lr-1cc4df, sub-epic lr-19ae42 sub-slice SE3, epic lr-5a8d Wave C).

Conformance (CLAUDE.md rule 6a): synthetic verb names, paths, roles, and
identity labels only -- no real agent names, no LORE present, no real
machine identifiers.

Tests move with their subjects (CLAUDE.md rule 6) -- this file is scoped to
the SE3 PR1 addition only (`BashRole.LEAD`, `LeadRoleConfig`,
`check_lead_command`, and `check_bash_call`'s LEAD dispatch branch). The
mutation-verb-family deny half (SE3 PR2, `_check_director_lead_mutation`'s
port) gets its own follow-up coverage when it lands in the same task.
"""

from __future__ import annotations

import re

from clagentic_loadout.guard.director_mutation import DirectorClagenticConfig
from clagentic_loadout.guard.role_allowlist import (
    BashRole,
    LeadRoleConfig,
    check_bash_call,
    check_lead_command,
)

_RELAY_VERB_RE = re.compile(r"^relay-cli\s+conversation$")


# ---------------------------------------------------------------------------
# BashRole.LEAD is role-keyed, not agent-named.
# ---------------------------------------------------------------------------


class TestBashRoleLeadIsRoleKeyed:
    def test_lead_member_present(self):
        names = {member.value for member in BashRole}
        assert "lead" in names

    def test_no_known_agent_name_leaks_into_enum_values(self):
        forbidden = {
            "amos", "naomi", "peaches", "bobbie", "miller", "drummer",
            "prax", "avasarala", "holden", "director",
        }
        for member in BashRole:
            assert member.value not in forbidden


# ---------------------------------------------------------------------------
# check_lead_command -- no config supplied (identity-discipline check never
# consulted; PR1 scope is intentionally incomplete on its own -- see the
# function's own docstring).
# ---------------------------------------------------------------------------


class TestCheckLeadCommandNoConfig:
    def test_admits_any_command_when_no_director_clagentic_config(self):
        ok, reason = check_lead_command(
            "git push --force", identity_label="lead-x"
        )
        assert ok is True, reason


# ---------------------------------------------------------------------------
# check_lead_command -- identity-discipline check wired via
# director_clagentic_config.
# ---------------------------------------------------------------------------


class TestCheckLeadCommandWithDirectorClagenticConfig:
    def _config(self) -> LeadRoleConfig:
        return LeadRoleConfig(
            director_clagentic_config=DirectorClagenticConfig(
                relay_verb_pattern=_RELAY_VERB_RE
            )
        )

    def test_opener_flag_present_admitted(self):
        ok, reason = check_lead_command(
            "relay-cli conversation open --opener lead-x",
            identity_label="lead-x",
            config=self._config(),
        )
        assert ok is True, reason

    def test_missing_opener_denied(self):
        ok, reason = check_lead_command(
            "relay-cli conversation open",
            identity_label="lead-x",
            config=self._config(),
        )
        assert ok is False
        assert "lead-x" in reason

    def test_non_relay_command_admitted_by_this_pr_scope(self):
        # PR1 scope note: the mutation-verb deny half has not landed yet,
        # so a non-relay command is admitted at this layer -- see
        # check_lead_command's own docstring "INTENTIONALLY INCOMPLETE".
        ok, reason = check_lead_command(
            "git push --force",
            identity_label="lead-x",
            config=self._config(),
        )
        assert ok is True, reason


# ---------------------------------------------------------------------------
# check_bash_call dispatch
# ---------------------------------------------------------------------------


class TestCheckBashCallLeadDispatch:
    def test_dispatches_to_check_lead_command(self):
        config = LeadRoleConfig(
            director_clagentic_config=DirectorClagenticConfig(
                relay_verb_pattern=_RELAY_VERB_RE
            )
        )
        ok, reason = check_bash_call(
            BashRole.LEAD,
            "relay-cli conversation open",
            lead_config=config,
            lead_identity_label="lead-x",
        )
        assert ok is False
        assert "lead-x" in reason

    def test_default_identity_label_is_generic_role_word(self):
        # No lead_identity_label supplied -- defaults to the bare role word
        # "lead", never a real agent/session name (CLAUDE.md rule 1).
        config = LeadRoleConfig(
            director_clagentic_config=DirectorClagenticConfig(
                relay_verb_pattern=_RELAY_VERB_RE
            )
        )
        ok, reason = check_bash_call(
            BashRole.LEAD, "relay-cli conversation open", lead_config=config
        )
        assert ok is False
        assert "lead" in reason

    def test_lead_role_never_raises_value_error(self):
        # BashRole.LEAD is now a registered checker -- check_bash_call must
        # not fall through to the "no registered checker" ValueError.
        ok, reason = check_bash_call(BashRole.LEAD, "git status")
        assert ok is True, reason
