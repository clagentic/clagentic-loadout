"""test_guard_director_mutation.py — `guard.director_mutation` unit tests
(lr-1cc4df, sub-epic lr-19ae42 sub-slice SE3 PR1, epic lr-5a8d Wave C).

Conformance (CLAUDE.md rule 6a): synthetic verb names, paths, roles, and
identity labels only — no real agent names, no LORE present, no real
machine identifiers.

Tests move with their subjects (CLAUDE.md rule 6) — this file is scoped to
`check_director_identity_discipline` / `DirectorClagenticConfig`, the
`_check_director_clagentic` port. The mutation-verb-family deny half
(SE3 PR2) will get its own test file when it lands, per that PR's own
task-lr-1cc4df tracking.
"""

from __future__ import annotations

import re

from clagentic_loadout.guard.director_mutation import (
    DirectorClagenticConfig,
    check_director_identity_discipline,
)

_RELAY_VERB_RE = re.compile(r"^relay-cli\s+conversation$")


def _config(**overrides) -> DirectorClagenticConfig:
    defaults = {"relay_verb_pattern": _RELAY_VERB_RE}
    defaults.update(overrides)
    return DirectorClagenticConfig(**defaults)


# ---------------------------------------------------------------------------
# Non-matching commands: this checker has no opinion on anything that isn't
# the configured relay-shaped IPC verb's conversation subcommand shape.
# ---------------------------------------------------------------------------


class TestNonMatchingCommandsAlwaysAdmitted:
    def test_short_command_admitted(self):
        ok, reason = check_director_identity_discipline(
            "git status", identity_label="lead-x", config=_config()
        )
        assert ok is True, reason

    def test_unrelated_binary_admitted(self):
        ok, reason = check_director_identity_discipline(
            "relay-cli status open --opener lead-x",
            identity_label="lead-x",
            config=_config(),
        )
        # "relay-cli status" != "relay-cli conversation" -- not this verb shape.
        assert ok is True, reason

    def test_unknown_subcommand_admitted(self):
        ok, reason = check_director_identity_discipline(
            "relay-cli conversation list",
            identity_label="lead-x",
            config=_config(),
        )
        assert ok is True, reason


# ---------------------------------------------------------------------------
# open subcommand
# ---------------------------------------------------------------------------


class TestOpenSubcommand:
    def test_opener_flag_present_admitted(self):
        ok, reason = check_director_identity_discipline(
            "relay-cli conversation open --opener lead-x",
            identity_label="lead-x",
            config=_config(),
        )
        assert ok is True, reason

    def test_missing_opener_and_no_acting_as_denied(self):
        ok, reason = check_director_identity_discipline(
            "relay-cli conversation open",
            identity_label="lead-x",
            config=_config(),
        )
        assert ok is False
        assert "--opener" in reason

    def test_acting_as_signal_admits_without_opener_flag(self):
        ok, reason = check_director_identity_discipline(
            "relay-cli conversation open",
            identity_label="lead-x",
            config=_config(relay_acting_as_env_signal=lambda: "lead-x"),
        )
        assert ok is True, reason

    def test_blank_acting_as_signal_still_denies(self):
        ok, reason = check_director_identity_discipline(
            "relay-cli conversation open",
            identity_label="lead-x",
            config=_config(relay_acting_as_env_signal=lambda: "   "),
        )
        assert ok is False

    def test_teams_context_signal_short_circuits_entirely(self):
        ok, reason = check_director_identity_discipline(
            "relay-cli conversation open",
            identity_label="lead-x",
            config=_config(teams_context_signal=lambda: True),
        )
        assert ok is True, reason

    def test_denial_message_embeds_identity_label(self):
        ok, reason = check_director_identity_discipline(
            "relay-cli conversation open",
            identity_label="lead-x",
            config=_config(),
        )
        assert ok is False
        assert "lead-x" in reason


# ---------------------------------------------------------------------------
# post subcommand
# ---------------------------------------------------------------------------


class TestPostSubcommand:
    def test_from_flag_present_admitted(self):
        ok, reason = check_director_identity_discipline(
            "relay-cli conversation post --from lead-x --body hello",
            identity_label="lead-x",
            config=_config(),
        )
        assert ok is True, reason

    def test_missing_from_flag_denied(self):
        ok, reason = check_director_identity_discipline(
            "relay-cli conversation post --body hello",
            identity_label="lead-x",
            config=_config(),
        )
        assert ok is False
        assert "--from" in reason


# ---------------------------------------------------------------------------
# close subcommand
# ---------------------------------------------------------------------------


class TestCloseSubcommand:
    def test_reason_embedding_identity_admitted(self):
        ok, reason = check_director_identity_discipline(
            'relay-cli conversation close --reason "lead-x closed: done"',
            identity_label="lead-x",
            config=_config(),
        )
        assert ok is True, reason

    def test_missing_reason_flag_denied(self):
        ok, reason = check_director_identity_discipline(
            "relay-cli conversation close",
            identity_label="lead-x",
            config=_config(),
        )
        assert ok is False
        assert "--reason" in reason

    def test_reason_without_identity_denied(self):
        ok, reason = check_director_identity_discipline(
            'relay-cli conversation close --reason "someone-else closed: done"',
            identity_label="lead-x",
            config=_config(),
        )
        assert ok is False
        assert "lead-x" in reason

    def test_reason_with_embedded_flag_like_token_captured_intact(self):
        # shlex.split must capture the whole --reason value, including an
        # internal "--word"-shaped token, rather than a naive split that
        # would stop at the first embedded flag-looking token.
        ok, reason = check_director_identity_discipline(
            'relay-cli conversation close --reason "lead-x fixed --opener wiring"',
            identity_label="lead-x",
            config=_config(),
        )
        assert ok is True, reason

    def test_unbalanced_quote_falls_back_to_conservative_tokens(self):
        # shlex.split raises ValueError on unbalanced quoting; the fallback
        # is the conservative whitespace token list (matches the reference's
        # own fallback exactly -- reference comment "fallback: best-effort
        # token list"). The fallback's --reason extraction takes the next
        # RAW whitespace token ('"lead-x', quote mark intact), which still
        # contains identity_label as a substring, so this admits -- an
        # unbalanced quote is a best-effort-parse edge case, not a security
        # boundary this function is designed to fail closed on (the
        # reference itself has the identical fallback and identical
        # substring-based identity_label check).
        ok, reason = check_director_identity_discipline(
            "relay-cli conversation close --reason \"lead-x unterminated",
            identity_label="lead-x",
            config=_config(),
        )
        assert ok is True, reason


# ---------------------------------------------------------------------------
# ANSI-C-quote evasion analysis (see function docstring): every branch here
# is a required-presence check, never a forbidden-substring scan feeding a
# bare-verb affirmative grant -- fragmenting a flag via $'...' can only make
# a deny fire MORE readily, never admit a command that would otherwise be
# denied. These tests are the regression proof of that analysis.
# ---------------------------------------------------------------------------


class TestAnsiCQuoteCannotWidenAdmission:
    def test_ansi_c_fragmented_opener_flag_still_denies(self):
        # $'--opener' is not the literal token "--opener" under a plain
        # whitespace split -- this must still deny (stricter, not laxer).
        ok, reason = check_director_identity_discipline(
            "relay-cli conversation open $'--opener' lead-x",
            identity_label="lead-x",
            config=_config(),
        )
        assert ok is False

    def test_ansi_c_reason_value_shlex_failure_falls_back_to_raw_token(self):
        # shlex does not understand the $'...' ANSI-C grammar and raises
        # ValueError on it here (an embedded ':' + space inside the ANSI-C
        # span breaks shlex's own posix-mode parse), falling back to the
        # conservative whitespace token list. The fallback's own
        # reason_text extraction (the next raw token after --reason,
        # "$'lead-x") still contains identity_label as a literal substring,
        # so this admits -- exactly the reference's own fallback semantics,
        # and still no path that WIDENS admission relative to a plainly
        # quoted equivalent (a caller who wants the identity check to see
        # the FULL reason text supplies a --reason value shlex can parse).
        ok, reason = check_director_identity_discipline(
            "relay-cli conversation close --reason $'lead-x closed: done'",
            identity_label="lead-x",
            config=_config(),
        )
        assert ok is True, reason


# ---------------------------------------------------------------------------
# DirectorClagenticConfig defaults
# ---------------------------------------------------------------------------


class TestDirectorClagenticConfigDefaults:
    def test_default_teams_context_signal_returns_false(self):
        config = DirectorClagenticConfig(relay_verb_pattern=_RELAY_VERB_RE)
        assert config.teams_context_signal() is False

    def test_default_relay_acting_as_signal_returns_empty_string(self):
        config = DirectorClagenticConfig(relay_verb_pattern=_RELAY_VERB_RE)
        assert config.relay_acting_as_env_signal() == ""

    def test_relay_verb_pattern_is_entirely_caller_supplied(self):
        # CLAUDE.md rule 1: no fixed IPC-verb literal is compiled into this
        # module itself -- check_director_identity_discipline never matches
        # against anything but the caller-supplied config.relay_verb_pattern.
        alt_pattern = re.compile(r"^totally-different-binary\s+topic$")
        config = _config(relay_verb_pattern=alt_pattern)
        ok, reason = check_director_identity_discipline(
            "totally-different-binary topic open",
            identity_label="lead-x",
            config=config,
        )
        assert ok is False
        assert "--opener" in reason
