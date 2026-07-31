"""test_guard_role_allowlist_se3_pr2.py — `check_lead_command`'s mutation-
verb-deny wiring (lr-1cc4df, sub-epic lr-19ae42 sub-slice SE3 PR2, epic
lr-5a8d Wave C).

Conformance (CLAUDE.md rule 6a): synthetic verb names, paths, roles, and
identity labels only -- no real agent names, no LORE present, no real
machine identifiers.

Tests move with their subjects (CLAUDE.md rule 6) -- this file is scoped to
the SE3 PR2 addition: `LeadRoleConfig.mutation_config`, the composition
order inside `check_lead_command`, and `check_bash_call`'s LEAD dispatch
carrying it through. PR1's own coverage (identity-discipline-only,
no-config defaults) stays in test_guard_role_allowlist_se3.py, unmodified
and still passing (this PR is purely additive to that surface).
"""

from __future__ import annotations

import re

from clagentic_loadout.guard.director_mutation import (
    DirectorClagenticConfig,
    LeadMutationConfig,
)
from clagentic_loadout.guard.role_allowlist import (
    BashRole,
    LeadRoleConfig,
    check_bash_call,
    check_lead_command,
)

_RELAY_VERB_RE = re.compile(r"^relay-cli\s+conversation$")


# ---------------------------------------------------------------------------
# check_lead_command -- mutation_config wired, no director_clagentic_config.
# ---------------------------------------------------------------------------


class TestCheckLeadCommandWithMutationConfigOnly:
    def _config(self) -> LeadRoleConfig:
        return LeadRoleConfig(mutation_config=LeadMutationConfig())

    def test_git_push_denied(self):
        ok, reason = check_lead_command(
            "git push --force", identity_label="lead-x", config=self._config()
        )
        assert ok is False
        assert "lead-x" in reason

    def test_read_only_command_admitted(self):
        ok, reason = check_lead_command(
            "git status", identity_label="lead-x", config=self._config()
        )
        assert ok is True, reason


# ---------------------------------------------------------------------------
# check_lead_command -- both configs wired: mutation deny runs BEFORE the
# identity-discipline check (mirrors the reference's own main() ordering).
# ---------------------------------------------------------------------------


class TestCheckLeadCommandCompositionOrder:
    def _config(self) -> LeadRoleConfig:
        return LeadRoleConfig(
            mutation_config=LeadMutationConfig(),
            director_clagentic_config=DirectorClagenticConfig(
                relay_verb_pattern=_RELAY_VERB_RE
            ),
        )

    def test_mutation_deny_fires_before_identity_discipline_would_even_apply(self):
        # "git push" is not the relay verb shape at all -- identity
        # discipline has no opinion on it either way; this proves the
        # mutation deny is the one that actually fires.
        ok, reason = check_lead_command(
            "git push --force", identity_label="lead-x", config=self._config()
        )
        assert ok is False
        assert "git write op" in reason

    def test_identity_discipline_still_reachable_for_relay_shaped_command(self):
        # A relay-shaped command with no mutation-verb-family match at all
        # reaches the identity-discipline check exactly as PR1 intended.
        ok, reason = check_lead_command(
            "relay-cli conversation open",
            identity_label="lead-x",
            config=self._config(),
        )
        assert ok is False
        assert "--opener" in reason

    def test_relay_shaped_command_with_opener_admitted(self):
        ok, reason = check_lead_command(
            "relay-cli conversation open --opener lead-x",
            identity_label="lead-x",
            config=self._config(),
        )
        assert ok is True, reason

    def test_mutation_deny_applies_even_to_a_relay_shaped_command(self):
        # A pathological "relay-cli conversation open" wrapped in a shell
        # write-redirect must still deny via the mutation gate FIRST --
        # composition order is not merely "mutation family regexes don't
        # happen to match a relay command," it's an intentional ordering
        # guarantee (reference: "_check_director_lead_mutation runs on ALL
        # commands in a director/lead session").
        ok, reason = check_lead_command(
            "relay-cli conversation open --opener lead-x > /workspace/f.txt",
            identity_label="lead-x",
            config=self._config(),
        )
        assert ok is False
        assert "shell write redirection" in reason


# ---------------------------------------------------------------------------
# PR1 backward-compatible defaults: no config, or director_clagentic_config
# only -- still exactly PR1's documented (intentionally incomplete) posture.
# ---------------------------------------------------------------------------


class TestPr1BackwardCompatibleDefaults:
    def test_no_config_admits_everything_exactly_as_pr1(self):
        ok, reason = check_lead_command("git push --force", identity_label="lead-x")
        assert ok is True, reason

    def test_director_clagentic_config_only_still_has_no_mutation_deny(self):
        config = LeadRoleConfig(
            director_clagentic_config=DirectorClagenticConfig(
                relay_verb_pattern=_RELAY_VERB_RE
            )
        )
        ok, reason = check_lead_command(
            "git push --force", identity_label="lead-x", config=config
        )
        assert ok is True, reason


# ---------------------------------------------------------------------------
# check_bash_call dispatch carries mutation_config through.
# ---------------------------------------------------------------------------


class TestCheckBashCallLeadDispatchWithMutationConfig:
    def test_dispatches_mutation_deny_through_lead_config(self):
        config = LeadRoleConfig(mutation_config=LeadMutationConfig())
        ok, reason = check_bash_call(
            BashRole.LEAD,
            "git push --force",
            lead_config=config,
            lead_identity_label="lead-x",
        )
        assert ok is False
        assert "lead-x" in reason

    def test_read_only_command_still_admitted_through_dispatch(self):
        config = LeadRoleConfig(mutation_config=LeadMutationConfig())
        ok, reason = check_bash_call(
            BashRole.LEAD, "git log", lead_config=config
        )
        assert ok is True, reason
