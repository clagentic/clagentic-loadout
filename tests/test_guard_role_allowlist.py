"""test_guard_role_allowlist.py — BUILDER/MERGER Bash-command allow-checkers
(lr-7feafc, sub-epic lr-19ae42 sub-slice SE1, epic lr-5a8d Wave C).

Conformance (CLAUDE.md rule 6a): synthetic verb names, paths, and roles
only — no real agent names, no LORE present, no real machine identifiers.
"""

from __future__ import annotations

import re

import pytest

from clagentic_loadout.guard.role_allowlist import (
    BashRole,
    MergerReadOnlyConfig,
    RoleAllowlistConfig,
    check_ansi_c_quote_denied,
    check_bash_call,
    check_builder_command,
    check_forbidden_git_patterns,
    check_merger_command,
    check_proc_environ_denied,
    is_admitted_merger_read_only,
)


# ---------------------------------------------------------------------------
# BashRole is role-keyed, not agent-named.
# ---------------------------------------------------------------------------


class TestBashRoleIsRoleKeyed:
    def test_se1_members_present(self):
        # BashRole is a GROWING enum by design (module docstring point 4,
        # this module's own docstring "later sub-slices (SE2/SE3/SE4) add
        # their own members to the SAME enum") -- SE2 (lr-a64227) adds five
        # more members (see test_guard_role_allowlist_se2.py). This test is
        # scoped to pinning SE1's own two members remain present, not to
        # asserting BashRole's total membership never grows.
        names = {member.value for member in BashRole}
        assert {"builder", "merger"} <= names

    def test_no_known_agent_name_leaks_into_enum_values(self):
        forbidden = {"amos", "naomi", "peaches", "bobbie", "miller", "drummer", "prax", "holden"}
        for member in BashRole:
            assert member.value not in forbidden


# ---------------------------------------------------------------------------
# Role-independent helpers (FLAGGED LOSSY COLLAPSE: the reference applies
# these identically inside both per-agent checkers).
# ---------------------------------------------------------------------------


class TestCheckForbiddenGitPatterns:
    def test_force_push_denied(self):
        ok, reason = check_forbidden_git_patterns("git push --force origin main")
        assert ok is False
        assert "git push --force" in reason

    def test_add_dash_a_denied(self):
        ok, _ = check_forbidden_git_patterns("git add -A")
        assert ok is False

    def test_reset_hard_denied(self):
        ok, _ = check_forbidden_git_patterns("git reset --hard HEAD~1")
        assert ok is False

    def test_ordinary_git_status_allowed(self):
        ok, reason = check_forbidden_git_patterns("git status")
        assert ok is True, reason

    def test_custom_forbidden_list_respected(self):
        ok, reason = check_forbidden_git_patterns(
            "widget destroy-everything", forbidden=("destroy-everything",)
        )
        assert ok is False
        assert "destroy-everything" in reason

    def test_ansi_c_fragmented_force_push_denied_via_normalization(self):
        # security-review finding, PR #115: "git $'push' --force" decodes
        # (bash ANSI-C quote-removal) to the literal joined text
        # "git push --force" -- normalize_shell_words resolves this
        # cleanly, so the (now-normalized) substring scan catches it.
        ok, reason = check_forbidden_git_patterns("git $'push' --force")
        assert ok is False
        assert "git push --force" in reason

    def test_ansi_c_fragmented_whole_operation_denied_via_normalization(self):
        ok, reason = check_forbidden_git_patterns("git $'push --force'")
        assert ok is False
        assert "git push --force" in reason


class TestCheckAnsiCQuoteDenied:
    """security-review BLOCKING finding regression coverage (PR #115),
    residual-ambiguity half: an ANSI-C span that does NOT decode cleanly at
    all (an unrecognized escape) must hard-deny rather than fall back to a
    raw scan that cannot see through the intact $'...' wrapper."""

    def test_unresolvable_ansi_c_escape_denied(self):
        ok, reason = check_ansi_c_quote_denied("git $'push \\c --force'")
        assert ok is False
        assert "ANSI-C" in reason

    def test_resolvable_ansi_c_span_not_denied_by_this_gate(self):
        # $'status' decodes cleanly (no unrecognized escape) -- this gate
        # only fires on genuinely UNRESOLVABLE ANSI-C ambiguity; the
        # resolvable-but-forbidden case is check_forbidden_git_patterns's
        # job (see TestCheckForbiddenGitPatterns above), not this gate's.
        ok, reason = check_ansi_c_quote_denied("git $'status'")
        assert ok is True, reason

    def test_plain_command_not_denied(self):
        ok, reason = check_ansi_c_quote_denied("git status")
        assert ok is True, reason


class TestCheckProcEnvironDenied:
    def test_cat_proc_pid_environ_denied(self):
        ok, reason = check_proc_environ_denied("cat /proc/1234/environ")
        assert ok is False
        assert "environ" in reason

    def test_cat_proc_star_environ_denied(self):
        ok, _ = check_proc_environ_denied("cat /proc/*/environ")
        assert ok is False

    def test_narrative_mention_in_non_file_verb_not_denied(self):
        # "lore" is not a file-reading verb this pattern anchors on.
        ok, reason = check_proc_environ_denied(
            'lore observe "saw /proc/1234/environ leaked"'
        )
        assert ok is True, reason

    def test_unrelated_cat_not_denied(self):
        ok, reason = check_proc_environ_denied("cat README.md")
        assert ok is True, reason


# ---------------------------------------------------------------------------
# check_builder_command
# ---------------------------------------------------------------------------


class TestCheckBuilderCommandBaseGrants:
    def test_git_admitted(self):
        ok, reason = check_builder_command("git status")
        assert ok is True, reason

    def test_lore_admitted(self):
        ok, reason = check_builder_command("lore task list")
        assert ok is True, reason

    def test_readonly_verb_admitted(self):
        ok, reason = check_builder_command("ls -la")
        assert ok is True, reason

    def test_bare_cat_read_admitted(self):
        ok, reason = check_builder_command("cat README.md")
        assert ok is True, reason

    def test_cat_redirect_to_tmpdir_admitted(self):
        ok, reason = check_builder_command("cat > $TMPDIR/scratch.txt")
        assert ok is True, reason

    def test_cat_redirect_to_home_denied(self):
        # lr-f8649f: $HOME dropped as a sanctioned scratch-staging root --
        # a $HOME-spelled redirect is no longer distinguishable from any
        # other non-staging absolute-path write and must be denied.
        ok, reason = check_builder_command("cat > $HOME/scratch.txt")
        assert ok is False

    def test_cat_redirect_to_arbitrary_absolute_path_denied(self):
        ok, reason = check_builder_command("cat > /workspace/evil.txt")
        assert ok is False

    def test_scoped_test_command_admitted_by_default(self):
        ok, reason = check_builder_command("python3 -m pytest tests/ -v")
        assert ok is True, reason

    def test_go_build_admitted_by_default(self):
        ok, reason = check_builder_command("go build ./...")
        assert ok is True, reason

    def test_unrelated_command_denied(self):
        ok, reason = check_builder_command("curl http://example.invalid")
        assert ok is False
        assert "builder-role" in reason


class TestCheckBuilderCommandForbiddenAndCompound:
    def test_forbidden_git_pattern_denied_before_anything_else(self):
        ok, reason = check_builder_command("git push --force")
        assert ok is False
        assert "forbidden pattern" in reason

    def test_compound_expression_denied(self):
        ok, reason = check_builder_command("git status && rm -rf /")
        assert ok is False

    def test_proc_environ_denied(self):
        ok, reason = check_builder_command("cat /proc/1/environ")
        assert ok is False
        assert "environ" in reason


class TestCheckBuilderCommandAnsiCEvasion:
    """security-review BLOCKING finding (PR #115): an ANSI-C-quote-
    fragmented forbidden git op must be DENIED for the builder role, not
    admitted through the bare `git` prefix grant."""

    def test_ansi_c_fragmented_force_push_denied_not_admitted(self):
        ok, reason = check_builder_command("git $'push' --force")
        assert ok is False
        assert "forbidden pattern" in reason

    def test_ansi_c_fragmented_whole_operation_denied_not_admitted(self):
        ok, reason = check_builder_command("git $'push --force'")
        assert ok is False
        assert "forbidden pattern" in reason

    def test_unresolvable_ansi_c_escape_hard_denied(self):
        ok, reason = check_builder_command("git $'push \\c --force'")
        assert ok is False
        assert "ANSI-C" in reason

    def test_benign_resolvable_ansi_c_still_admitted(self):
        # A resolvable, non-forbidden ANSI-C span must NOT be collaterally
        # denied -- only the genuinely forbidden/unresolvable shapes are.
        ok, reason = check_builder_command("git $'status'")
        assert ok is True, reason


class TestCheckBuilderCommandScratchGrant:
    def test_mkdir_under_tmpdir_admitted(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        ok, reason = check_builder_command(f"mkdir -p {tmp_path}/work")
        assert ok is True, reason

    def test_mv_escaping_tmpdir_denied(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        ok, reason = check_builder_command(f"mv {tmp_path}/x /workspace/y")
        assert ok is False


class TestCheckBuilderCommandLoadoutFamilyGrant:
    def test_readonly_loadout_verb_admitted(self):
        ok, reason = check_builder_command("loadout-doctor --check")
        assert ok is True, reason

    def test_mutating_loadout_verb_excluded_when_configured(self):
        cfg = RoleAllowlistConfig(
            mutating_loadout_verb_names=frozenset({"loadout-merge"})
        )
        ok, reason = check_builder_command("loadout-merge --pr 1", config=cfg)
        assert ok is False


class TestCheckBuilderCommandExtraVerbPatterns:
    def test_extra_verb_pattern_admitted(self):
        cfg = RoleAllowlistConfig(
            extra_verb_patterns=(re.compile(r"^synthetic-push(\s|$)"),)
        )
        ok, reason = check_builder_command("synthetic-push --pr", config=cfg)
        assert ok is True, reason

    def test_without_config_extra_verb_not_admitted(self):
        ok, reason = check_builder_command("synthetic-push --pr")
        assert ok is False


# ---------------------------------------------------------------------------
# check_merger_command — deliberately narrower than builder.
# ---------------------------------------------------------------------------


class TestCheckMergerCommandBaseGrants:
    def test_git_admitted(self):
        ok, reason = check_merger_command("git status")
        assert ok is True, reason

    def test_readonly_verb_admitted(self):
        ok, reason = check_merger_command("head -n 5 file.txt")
        assert ok is True, reason

    def test_tmpdir_staging_write_admitted(self):
        ok, reason = check_merger_command("printf 'x' > $TMPDIR/scratch.txt")
        assert ok is True, reason

    def test_home_staging_write_denied(self):
        # lr-f8649f: $HOME dropped as a sanctioned scratch-staging root.
        ok, reason = check_merger_command("printf 'x' > $HOME/scratch.txt")
        assert ok is False

    def test_bare_lore_not_admitted_without_extra_config(self):
        # Unlike builder, merger has no bare `lore` grant baked in — a
        # caller wires its own narrower lore subset via extra_verb_patterns.
        ok, reason = check_merger_command("lore task list")
        assert ok is False

    def test_scoped_test_command_not_admitted(self):
        # Merger deliberately does not consult scoped_test_patterns at all.
        ok, reason = check_merger_command("python3 -m pytest tests/")
        assert ok is False
        assert "merger-role" in reason

    def test_unrelated_command_denied(self):
        ok, reason = check_merger_command("curl http://example.invalid")
        assert ok is False
        assert "narrower than builder" in reason


class TestCheckMergerCommandForbiddenAndCompound:
    def test_forbidden_git_pattern_denied(self):
        ok, reason = check_merger_command("git push --force")
        assert ok is False

    def test_compound_expression_denied(self):
        ok, reason = check_merger_command("git status && rm -rf /")
        assert ok is False


class TestCheckMergerCommandAnsiCEvasion:
    """security-review BLOCKING finding (PR #115): reachable from the
    merger role too, since check_forbidden_git_patterns is the shared
    role-independent helper both check_builder_command and
    check_merger_command call."""

    def test_ansi_c_fragmented_force_push_denied_not_admitted(self):
        ok, reason = check_merger_command("git $'push' --force")
        assert ok is False
        assert "forbidden pattern" in reason

    def test_ansi_c_fragmented_whole_operation_denied_not_admitted(self):
        ok, reason = check_merger_command("git $'push --force'")
        assert ok is False
        assert "forbidden pattern" in reason

    def test_unresolvable_ansi_c_escape_hard_denied(self):
        ok, reason = check_merger_command("git $'push \\c --force'")
        assert ok is False
        assert "ANSI-C" in reason

    def test_benign_resolvable_ansi_c_still_admitted(self):
        ok, reason = check_merger_command("git $'status'")
        assert ok is True, reason


class TestCheckMergerCommandScratchGrant:
    def test_mkdir_under_tmpdir_admitted(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        ok, reason = check_merger_command(f"mkdir -p {tmp_path}/stage")
        assert ok is True, reason

    def test_mkdir_under_home_denied(self, monkeypatch, tmp_path):
        # lr-f8649f: $HOME is no longer a scratch_policy boundary at all --
        # a bare mkdir naming a real $HOME-resolved path with no $TMPDIR
        # configured (and no uid-home fallback matching this synthetic path)
        # has no boundary to resolve against, so is_scratch_contained denies.
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        ok, reason = check_merger_command(f"mkdir -p {tmp_path}/stage")
        assert ok is False


class TestCheckMergerCommandExtraVerbPatterns:
    def test_merge_verb_admitted_via_config(self):
        cfg = RoleAllowlistConfig(
            extra_verb_patterns=(re.compile(r"^synthetic-merge(\s|$)"),)
        )
        ok, reason = check_merger_command(
            "synthetic-merge --pr 1 --role merger", config=cfg
        )
        assert ok is True, reason


# ---------------------------------------------------------------------------
# is_admitted_merger_read_only / MergerReadOnlyConfig
# ---------------------------------------------------------------------------

_READ_VERB_RE = re.compile(r"^synthetic-git-host-api(\s|$)")


class TestMergerReadOnlyConfigValidation:
    def test_valid_caller_role_accepted(self):
        MergerReadOnlyConfig(verb_pattern=_READ_VERB_RE, caller_role="merger")

    def test_leading_hyphen_caller_role_rejected(self):
        with pytest.raises(ValueError):
            MergerReadOnlyConfig(verb_pattern=_READ_VERB_RE, caller_role="-merger")

    def test_metacharacter_caller_role_rejected(self):
        with pytest.raises(ValueError):
            MergerReadOnlyConfig(
                verb_pattern=_READ_VERB_RE, caller_role="merger; rm -rf /"
            )

    def test_empty_caller_role_rejected(self):
        with pytest.raises(ValueError):
            MergerReadOnlyConfig(verb_pattern=_READ_VERB_RE, caller_role="")


class TestIsAdmittedMergerReadOnly:
    _CFG = MergerReadOnlyConfig(verb_pattern=_READ_VERB_RE, caller_role="merger")

    def test_get_with_caller_flag_admitted(self):
        cmd = "synthetic-git-host-api GET /repos/o/r --caller merger"
        assert is_admitted_merger_read_only(cmd, config=self._CFG) is True

    def test_default_method_with_caller_flag_admitted(self):
        cmd = "synthetic-git-host-api /repos/o/r --caller merger"
        assert is_admitted_merger_read_only(cmd, config=self._CFG) is True

    def test_missing_caller_flag_not_admitted(self):
        cmd = "synthetic-git-host-api GET /repos/o/r"
        assert is_admitted_merger_read_only(cmd, config=self._CFG) is False

    def test_wrong_caller_value_not_admitted(self):
        cmd = "synthetic-git-host-api GET /repos/o/r --caller someone-else"
        assert is_admitted_merger_read_only(cmd, config=self._CFG) is False

    def test_write_method_denied_even_with_caller_flag(self):
        cmd = "synthetic-git-host-api POST /repos/o/r --caller merger"
        assert is_admitted_merger_read_only(cmd, config=self._CFG) is False

    def test_unrelated_verb_not_admitted(self):
        cmd = "some-other-tool GET /repos/o/r --caller merger"
        assert is_admitted_merger_read_only(cmd, config=self._CFG) is False


class TestIsAdmittedMergerReadOnlyAnsiCEvasion:
    """security-review NIT finding (PR #115): an ANSI-C-obscured write-method
    token must not defeat the read-only exclusion."""

    _CFG = MergerReadOnlyConfig(verb_pattern=_READ_VERB_RE, caller_role="merger")

    def test_ansi_c_obscured_post_denied_not_admitted(self):
        cmd = "synthetic-git-host-api $'POST' /repos/o/r --caller merger"
        assert is_admitted_merger_read_only(cmd, config=self._CFG) is False

    def test_ansi_c_obscured_delete_denied_not_admitted(self):
        cmd = "synthetic-git-host-api $'DELETE' /repos/o/r --caller merger"
        assert is_admitted_merger_read_only(cmd, config=self._CFG) is False

    def test_ansi_c_obscured_get_still_admitted(self):
        # A resolvable, non-write-method ANSI-C span must not be
        # collaterally denied.
        cmd = "synthetic-git-host-api $'GET' /repos/o/r --caller merger"
        assert is_admitted_merger_read_only(cmd, config=self._CFG) is True


class TestCheckMergerCommandReadOnlyPreCheckIntegration:
    _CFG = MergerReadOnlyConfig(verb_pattern=_READ_VERB_RE, caller_role="merger")

    def test_read_only_precheck_admitted_through_full_pipeline(self):
        cmd = "synthetic-git-host-api GET /repos/o/r --caller merger"
        ok, reason = check_merger_command(cmd, read_only_config=self._CFG)
        assert ok is True, reason

    def test_write_method_denied_through_full_pipeline(self):
        cmd = "synthetic-git-host-api POST /repos/o/r --caller merger"
        ok, reason = check_merger_command(cmd, read_only_config=self._CFG)
        assert ok is False

    def test_without_read_only_config_read_shape_not_admitted(self):
        cmd = "synthetic-git-host-api GET /repos/o/r --caller merger"
        ok, reason = check_merger_command(cmd)
        assert ok is False


# ---------------------------------------------------------------------------
# check_bash_call — role dispatch
# ---------------------------------------------------------------------------


class TestCheckBashCallRoleDispatch:
    def test_builder_dispatch_delegates(self):
        ok, reason = check_bash_call(BashRole.BUILDER, "git status")
        assert ok is True, reason

    def test_merger_dispatch_delegates(self):
        ok, reason = check_bash_call(BashRole.MERGER, "git status")
        assert ok is True, reason

    def test_merger_dispatch_honors_read_only_config(self):
        cfg = MergerReadOnlyConfig(verb_pattern=_READ_VERB_RE, caller_role="merger")
        cmd = "synthetic-git-host-api GET /repos/o/r --caller merger"
        ok, reason = check_bash_call(BashRole.MERGER, cmd, read_only_config=cfg)
        assert ok is True, reason

    def test_read_only_config_ignored_for_builder(self):
        cfg = MergerReadOnlyConfig(verb_pattern=_READ_VERB_RE, caller_role="merger")
        # Passing a MERGER-shaped read_only_config alongside BUILDER must not
        # raise -- it is simply unused for BUILDER's own checker.
        ok, reason = check_bash_call(BashRole.BUILDER, "git status", read_only_config=cfg)
        assert ok is True, reason


# ---------------------------------------------------------------------------
# Behavior-preservation pin: builder is strictly wider than merger for the
# base grant surface both share (read-only verbs, $TMPDIR staging) -- and
# merger genuinely lacks capabilities builder has (scoped-test, bare lore),
# matching the reference's documented narrower-by-design posture.
# ---------------------------------------------------------------------------


class TestBuilderMergerAsymmetry:
    def test_builder_has_scoped_test_merger_does_not(self):
        cmd = "python3 -m pytest tests/"
        builder_ok, _ = check_builder_command(cmd)
        merger_ok, _ = check_merger_command(cmd)
        assert builder_ok is True
        assert merger_ok is False

    def test_builder_has_bare_lore_merger_does_not(self):
        cmd = "lore task list"
        builder_ok, _ = check_builder_command(cmd)
        merger_ok, _ = check_merger_command(cmd)
        assert builder_ok is True
        assert merger_ok is False

    def test_both_share_readonly_and_staging_grants(self):
        for cmd in ("ls -la", "printf 'x' > $TMPDIR/f.txt"):
            builder_ok, breason = check_builder_command(cmd)
            merger_ok, mreason = check_merger_command(cmd)
            assert builder_ok is True, breason
            assert merger_ok is True, mreason

    def test_both_deny_home_staging(self):
        # lr-f8649f: $HOME is no longer a sanctioned staging root for
        # either role.
        cmd = "printf 'x' > $HOME/f.txt"
        builder_ok, _ = check_builder_command(cmd)
        merger_ok, _ = check_merger_command(cmd)
        assert builder_ok is False
        assert merger_ok is False
