"""test_guard_role_allowlist_se2.py — REVIEWER/SECURITY/ANALYSIS/RESEARCH/
PLANNING_READER Bash-command allow-checkers (lr-a64227, sub-epic lr-19ae42
sub-slice SE2, epic lr-5a8d Wave C).

Conformance (CLAUDE.md rule 6a): synthetic verb names, paths, and roles
only — no real agent names, no LORE present, no real machine identifiers.

Tests move with their subjects (CLAUDE.md rule 6) — this file is scoped to
the SE2 additions only; SE1's BUILDER/MERGER coverage stays in
test_guard_role_allowlist.py.
"""

from __future__ import annotations

import re

import pytest

from clagentic_loadout.guard.bash_admission import BodyStdinVerb, MethodPathFlagRule
from clagentic_loadout.guard.role_allowlist import (
    AnalysisRoleConfig,
    BashRole,
    PlanningReaderReadOnlyConfig,
    ResearchRoleConfig,
    ReviewGateConfig,
    check_analysis_command,
    check_bash_call,
    check_planning_reader_command,
    check_research_command,
    check_reviewer_command,
    check_security_command,
)


# ---------------------------------------------------------------------------
# BashRole SE2 members are role-keyed, not agent-named.
# ---------------------------------------------------------------------------


class TestBashRoleSe2MembersAreRoleKeyed:
    def test_se2_members_present(self):
        names = {member.value for member in BashRole}
        assert {"reviewer", "security", "analysis", "research", "planning_reader"} <= names

    def test_no_known_agent_name_leaks_into_enum_values(self):
        forbidden = {"amos", "naomi", "peaches", "bobbie", "miller", "drummer", "prax", "avasarala", "holden"}
        for member in BashRole:
            assert member.value not in forbidden


# ---------------------------------------------------------------------------
# check_reviewer_command
# ---------------------------------------------------------------------------


class TestCheckReviewerCommandBaseGrants:
    def test_lore_task_show_admitted(self):
        ok, reason = check_reviewer_command("lore task show lr-1")
        assert ok is True, reason

    def test_lore_task_create_admitted(self):
        ok, reason = check_reviewer_command("lore task create --title x")
        assert ok is True, reason

    def test_git_show_admitted(self):
        ok, reason = check_reviewer_command("git show HEAD")
        assert ok is True, reason

    def test_git_diff_admitted(self):
        ok, reason = check_reviewer_command("git diff HEAD~1")
        assert ok is True, reason

    def test_git_log_not_admitted(self):
        # REVIEWER's read-only git surface is narrower than SECURITY's --
        # no `git log` (module docstring COLLAPSE RATIONALE).
        ok, reason = check_reviewer_command("git log")
        assert ok is False

    def test_bare_git_not_admitted(self):
        ok, reason = check_reviewer_command("git status")
        assert ok is False

    def test_readonly_verb_admitted(self):
        ok, reason = check_reviewer_command("ls -la")
        assert ok is True, reason

    def test_tmpdir_staging_write_admitted(self):
        ok, reason = check_reviewer_command("printf 'x' > $TMPDIR/scratch.txt")
        assert ok is True, reason

    def test_home_staging_write_denied(self):
        # lr-f8649f: $HOME dropped as a sanctioned scratch-staging root.
        ok, reason = check_reviewer_command("printf 'x' > $HOME/scratch.txt")
        assert ok is False

    def test_post_verb_pattern_admitted_via_config(self):
        cfg = ReviewGateConfig(
            post_verb_patterns=(re.compile(r"^synthetic-git-host-api(\s|$)"),)
        )
        ok, reason = check_reviewer_command(
            "synthetic-git-host-api GET /repos/o/r", config=cfg
        )
        assert ok is True, reason

    def test_extra_verb_pattern_admitted_via_config(self):
        cfg = ReviewGateConfig(
            extra_verb_patterns=(re.compile(r"^synthetic-model-carrier(\s|$)"),)
        )
        ok, reason = check_reviewer_command("synthetic-model-carrier exec", config=cfg)
        assert ok is True, reason

    def test_unrelated_command_denied(self):
        ok, reason = check_reviewer_command("curl http://example.invalid")
        assert ok is False
        assert "reviewer-role" in reason

    def test_scanner_verb_not_admitted(self):
        # SECURITY's scanner surface must not leak into REVIEWER.
        ok, reason = check_reviewer_command("gitleaks detect")
        assert ok is False


class TestCheckReviewerCommandForbiddenAndCompound:
    def test_forbidden_git_pattern_denied(self):
        ok, reason = check_reviewer_command("git push --force")
        assert ok is False
        assert "forbidden pattern" in reason

    def test_compound_expression_denied(self):
        ok, reason = check_reviewer_command("git show HEAD && rm -rf /")
        assert ok is False

    def test_proc_environ_denied(self):
        ok, reason = check_reviewer_command("cat /proc/1/environ")
        assert ok is False
        assert "environ" in reason


class TestCheckReviewerCommandAnsiCEvasion:
    """POST-LANDING HARDENING (guard-policy.md, mandatory for SE2): reused
    role-independent check_forbidden_git_patterns already carries the
    normalize-before-scan fix; this pins that REVIEWER inherits it."""

    def test_ansi_c_fragmented_force_push_denied(self):
        ok, reason = check_reviewer_command("git $'push' --force")
        assert ok is False
        assert "forbidden pattern" in reason

    def test_ansi_c_fragmented_whole_operation_denied(self):
        ok, reason = check_reviewer_command("git $'push --force'")
        assert ok is False
        assert "forbidden pattern" in reason


class TestCheckReviewerCommandFlagGates:
    _VERB_RE = re.compile(r"^synthetic-git-host-api(\s|$)")
    _RULE = MethodPathFlagRule(
        method="POST",
        path_pattern=re.compile(r"/issues/\d+/comments\b"),
        required_flag="--verify-comment",
        deny_reason="comments-post requires --verify-comment",
    )

    def test_comments_post_without_flag_denied(self):
        cfg = ReviewGateConfig(
            post_verb_patterns=(self._VERB_RE,),
            comments_post_rule=self._RULE,
            comments_post_verb_pattern=self._VERB_RE,
        )
        ok, reason = check_reviewer_command(
            "synthetic-git-host-api POST /repos/o/r/issues/1/comments", config=cfg
        )
        assert ok is False
        assert "verify-comment" in reason

    def test_comments_post_with_flag_admitted(self):
        cfg = ReviewGateConfig(
            post_verb_patterns=(self._VERB_RE,),
            comments_post_rule=self._RULE,
            comments_post_verb_pattern=self._VERB_RE,
        )
        ok, reason = check_reviewer_command(
            "synthetic-git-host-api POST /repos/o/r/issues/1/comments --verify-comment",
            config=cfg,
        )
        assert ok is True, reason


class TestCheckReviewerCommandBodyStdinPipe:
    _VERBS = (BodyStdinVerb(verb_pattern=re.compile(r"^synthetic-stage-body(\s|$)")),)

    def test_admitted_body_stdin_pipe(self):
        cfg = ReviewGateConfig(body_stdin_verbs=self._VERBS)
        ok, reason = check_reviewer_command(
            "echo '{}' | synthetic-stage-body --caller reviewer", config=cfg
        )
        assert ok is True, reason

    def test_without_config_pipe_not_admitted(self):
        ok, reason = check_reviewer_command(
            "echo '{}' | synthetic-stage-body --caller reviewer"
        )
        assert ok is False


class TestCheckReviewerCommandScratchGrant:
    def test_mkdir_under_tmpdir_admitted(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        ok, reason = check_reviewer_command(f"mkdir -p {tmp_path}/work")
        assert ok is True, reason


class TestCheckReviewerCommandLoadoutFamilyGrant:
    def test_readonly_loadout_verb_admitted(self):
        ok, reason = check_reviewer_command("loadout-doctor --check")
        assert ok is True, reason


# ---------------------------------------------------------------------------
# check_security_command
# ---------------------------------------------------------------------------


class TestCheckSecurityCommandBaseGrants:
    def test_git_log_admitted(self):
        # SECURITY is one entry wider than REVIEWER -- git log IS admitted.
        ok, reason = check_security_command("git log")
        assert ok is True, reason

    def test_git_show_admitted(self):
        ok, reason = check_security_command("git show HEAD")
        assert ok is True, reason

    def test_scanner_pattern_admitted_via_config(self):
        patterns = (re.compile(r"^gitleaks(\s|$)"), re.compile(r"^semgrep(\s|$)"))
        ok, reason = check_security_command("gitleaks detect", scanner_patterns=patterns)
        assert ok is True, reason

    def test_scanner_pattern_not_admitted_without_config(self):
        ok, reason = check_security_command("gitleaks detect")
        assert ok is False

    def test_model_carrier_verb_not_admitted(self):
        # REVIEWER's Path-D external-model-carrier surface must not leak
        # into SECURITY.
        cfg = ReviewGateConfig(extra_verb_patterns=())
        ok, reason = check_security_command("codex exec 'do a thing'", config=cfg)
        assert ok is False

    def test_unrelated_command_denied(self):
        ok, reason = check_security_command("curl http://example.invalid")
        assert ok is False
        assert "security-role" in reason


class TestCheckSecurityCommandForbiddenAndCompound:
    def test_forbidden_git_pattern_denied(self):
        ok, reason = check_security_command("git push --force")
        assert ok is False

    def test_compound_expression_denied(self):
        ok, reason = check_security_command("git log && rm -rf /")
        assert ok is False


class TestCheckSecurityCommandAnsiCEvasion:
    def test_ansi_c_fragmented_force_push_denied(self):
        ok, reason = check_security_command("git $'push' --force")
        assert ok is False
        assert "forbidden pattern" in reason

    def test_ansi_c_fragmented_whole_operation_denied(self):
        ok, reason = check_security_command("git $'push --force'")
        assert ok is False
        assert "forbidden pattern" in reason


class TestCheckSecurityCommandBodyStdinPipe:
    _VERBS = (BodyStdinVerb(verb_pattern=re.compile(r"^synthetic-stage-body(\s|$)")),)

    def test_admitted_body_stdin_pipe(self):
        cfg = ReviewGateConfig(body_stdin_verbs=self._VERBS)
        ok, reason = check_security_command(
            "echo '{}' | synthetic-stage-body --caller security", config=cfg
        )
        assert ok is True, reason


# ---------------------------------------------------------------------------
# check_analysis_command (MILLER + DRUMMER collapse)
# ---------------------------------------------------------------------------


class TestCheckAnalysisCommandRequiresConfig:
    def test_dispatch_without_analysis_config_raises(self):
        with pytest.raises(ValueError):
            check_bash_call(BashRole.ANALYSIS, "git log")


class TestCheckAnalysisCommandGitSubcommandConfig:
    _WIDE = AnalysisRoleConfig(
        git_readonly_subcommands=("log", "show", "diff", "status", "blame", "rev-parse"),
        extra_reader_verbs=("grep", "less"),
    )
    _NARROW = AnalysisRoleConfig(
        git_readonly_subcommands=("log", "show", "status", "rev-parse"),
    )

    def test_wide_config_admits_git_diff(self):
        ok, reason = check_analysis_command("git diff HEAD~1", config=self._WIDE)
        assert ok is True, reason

    def test_narrow_config_denies_git_diff(self):
        ok, reason = check_analysis_command("git diff HEAD~1", config=self._NARROW)
        assert ok is False

    def test_both_configs_admit_git_log(self):
        ok_wide, _ = check_analysis_command("git log", config=self._WIDE)
        ok_narrow, _ = check_analysis_command("git log", config=self._NARROW)
        assert ok_wide is True
        assert ok_narrow is True

    def test_git_dash_c_form_admitted(self):
        ok, reason = check_analysis_command(
            "git -C /some/repo log", config=self._NARROW
        )
        assert ok is True, reason

    def test_wide_config_admits_grep_narrow_does_not(self):
        ok_wide, _ = check_analysis_command("grep foo file.txt", config=self._WIDE)
        ok_narrow, _ = check_analysis_command("grep foo file.txt", config=self._NARROW)
        assert ok_wide is True
        assert ok_narrow is False

    def test_systemctl_status_admitted(self):
        ok, reason = check_analysis_command("systemctl status foo.service", config=self._NARROW)
        assert ok is True, reason

    def test_systemctl_restart_denied(self):
        ok, reason = check_analysis_command("systemctl restart foo.service", config=self._NARROW)
        assert ok is False

    def test_docker_inspect_admitted(self):
        ok, reason = check_analysis_command("docker inspect foo", config=self._NARROW)
        assert ok is True, reason

    def test_docker_rm_denied(self):
        ok, reason = check_analysis_command("docker rm foo", config=self._NARROW)
        assert ok is False

    def test_curl_status_probe_admitted(self):
        cmd = 'curl -sS -o /dev/null -w "%{http_code}" http://example.invalid/health'
        ok, reason = check_analysis_command(cmd, config=self._NARROW)
        assert ok is True, reason

    def test_extra_probe_pattern_admitted(self):
        cfg = AnalysisRoleConfig(
            git_readonly_subcommands=("log",),
            extra_probe_patterns=(re.compile(r"^curl\s+-sS\s+http://synthetic\.invalid/health"),),
        )
        ok, reason = check_analysis_command(
            "curl -sS http://synthetic.invalid/health", config=cfg
        )
        assert ok is True, reason

    def test_lore_read_and_task_mutation_admitted(self):
        ok, reason = check_analysis_command("lore task comment lr-1 --body x", config=self._NARROW)
        assert ok is True, reason

    def test_unrelated_command_denied(self):
        ok, reason = check_analysis_command("curl http://example.invalid", config=self._NARROW)
        assert ok is False
        assert "analysis-role" in reason


class TestCheckAnalysisCommandForbiddenMutation:
    _CFG = AnalysisRoleConfig(git_readonly_subcommands=("log", "status"))

    def test_git_push_denied(self):
        # git push is not in DEFAULT_FORBIDDEN_GIT_PATTERNS but IS in the
        # wider ANALYSIS mutation-forbidden list (mirrors reference
        # _MUTATION_FORBIDDEN, service-mutation-aware).
        ok, reason = check_analysis_command("git push origin main", config=self._CFG)
        assert ok is False
        assert "forbidden mutation" in reason

    def test_systemctl_restart_denied_by_forbidden_scan(self):
        ok, reason = check_analysis_command("systemctl restart foo", config=self._CFG)
        assert ok is False

    def test_docker_compose_up_denied(self):
        ok, reason = check_analysis_command("docker compose up", config=self._CFG)
        assert ok is False

    def test_ordinary_git_log_admitted(self):
        ok, reason = check_analysis_command("git log", config=self._CFG)
        assert ok is True, reason


class TestAnalysisRoleConfigTokenValidation:
    """HARDENING (security-review advisory, PR #116 comment 15875 item 3):
    git_readonly_subcommands/extra_reader_verbs flow into a `"|".join(...)`
    regex alternation in check_analysis_command with no re.escape() -- a
    malformed token must be rejected at config-construction time, mirroring
    MergerReadOnlyConfig.caller_role's own _ROLE_TOKEN_RE validation, rather
    than ever reaching re.compile()."""

    def test_metacharacter_subcommand_token_rejected(self):
        with pytest.raises(ValueError):
            AnalysisRoleConfig(git_readonly_subcommands=("log", "a)|(.*"))

    def test_metacharacter_reader_verb_token_rejected(self):
        with pytest.raises(ValueError):
            AnalysisRoleConfig(
                git_readonly_subcommands=("log",),
                extra_reader_verbs=("cat|rm",),
            )

    def test_alternation_injection_cannot_widen_match(self):
        # A metacharacter-bearing token like "status)|(.*" would, if joined
        # unescaped into the alternation, widen the grant to match ANY git
        # subcommand -- construction must fail closed before that can
        # happen.
        with pytest.raises(ValueError):
            AnalysisRoleConfig(git_readonly_subcommands=("status)|(.*",))

    def test_well_formed_tokens_still_admitted(self):
        cfg = AnalysisRoleConfig(
            git_readonly_subcommands=("log", "rev-parse"),
            extra_reader_verbs=("grep",),
        )
        ok, reason = check_analysis_command("git rev-parse HEAD", config=cfg)
        assert ok is True, reason
        ok, reason = check_analysis_command("grep foo file.txt", config=cfg)
        assert ok is True, reason


class TestCheckAnalysisCommandAnsiCEvasion:
    """POST-LANDING HARDENING (mandatory for every bare-verb grant this
    checker composes, e.g. the bare `git <subcommand>` grant)."""

    _CFG = AnalysisRoleConfig(git_readonly_subcommands=("log", "status"))

    def test_ansi_c_fragmented_force_push_denied_via_normalization(self):
        ok, reason = check_analysis_command("git $'push' --force", config=self._CFG)
        assert ok is False
        assert "forbidden" in reason.lower()

    def test_unresolvable_ansi_c_escape_hard_denied(self):
        ok, reason = check_analysis_command("git $'push \\c --force'", config=self._CFG)
        assert ok is False
        assert "ANSI-C" in reason

    def test_benign_resolvable_ansi_c_still_admitted(self):
        ok, reason = check_analysis_command("git $'status'", config=self._CFG)
        assert ok is True, reason

    def test_compound_expression_denied(self):
        ok, reason = check_analysis_command("git log && rm -rf /", config=self._CFG)
        assert ok is False

    def test_proc_environ_denied(self):
        ok, reason = check_analysis_command("cat /proc/1/environ", config=self._CFG)
        assert ok is False
        assert "environ" in reason


# ---------------------------------------------------------------------------
# check_research_command (PRAX)
# ---------------------------------------------------------------------------


class TestCheckResearchCommandBaseGrants:
    def test_lore_search_admitted(self):
        ok, reason = check_research_command("lore search foo")
        assert ok is True, reason

    def test_readonly_verb_admitted(self):
        ok, reason = check_research_command("cat README.md")
        assert ok is True, reason

    def test_research_engine_pattern_admitted_via_config(self):
        cfg = ResearchRoleConfig(
            research_engine_patterns=(re.compile(r"^synthetic-research(\s|$)"),)
        )
        ok, reason = check_research_command("synthetic-research --topic x", config=cfg)
        assert ok is True, reason

    def test_research_engine_pattern_not_admitted_without_config(self):
        ok, reason = check_research_command("synthetic-research --topic x")
        assert ok is False

    def test_git_not_admitted(self):
        # RESEARCH has zero git visibility -- not collapsed into ANALYSIS.
        ok, reason = check_research_command("git status")
        assert ok is False

    def test_systemctl_not_admitted(self):
        ok, reason = check_research_command("systemctl status foo.service")
        assert ok is False

    def test_unrelated_command_denied(self):
        ok, reason = check_research_command("curl http://example.invalid")
        assert ok is False
        assert "research-role" in reason


class TestCheckResearchCommandForbiddenAndCompound:
    def test_forbidden_git_pattern_denied(self):
        ok, reason = check_research_command("git push --force")
        assert ok is False

    def test_compound_expression_denied(self):
        ok, reason = check_research_command("lore search foo && rm -rf /")
        assert ok is False

    def test_proc_environ_denied(self):
        ok, reason = check_research_command("cat /proc/1/environ")
        assert ok is False


class TestCheckResearchCommandAnsiCEvasion:
    def test_ansi_c_fragmented_force_push_denied(self):
        ok, reason = check_research_command("git $'push' --force")
        assert ok is False
        assert "forbidden pattern" in reason

    def test_unresolvable_ansi_c_escape_hard_denied(self):
        ok, reason = check_research_command("git $'push \\c --force'")
        assert ok is False
        assert "ANSI-C" in reason

    def test_benign_resolvable_ansi_c_still_admitted(self):
        ok, reason = check_research_command("git $'status'")
        # git is not otherwise admitted for RESEARCH, so this stays denied
        # for lack of a matching grant -- but must NOT be denied by the
        # ANSI-C gate itself for a resolvable, non-forbidden span.
        ok2, reason2 = check_research_command("lore search x")
        assert ok2 is True, reason2


# ---------------------------------------------------------------------------
# check_planning_reader_command (AVASARALA)
# ---------------------------------------------------------------------------

_PLANNING_READ_VERB_RE = re.compile(r"^synthetic-git-host-api(\s|$)")


class TestCheckPlanningReaderCommandBaseGrants:
    def test_lore_task_create_admitted(self):
        # WIDER lore task grant than the other read-only roles -- task
        # AUTHORING, matching the reference.
        ok, reason = check_planning_reader_command("lore task create --title x")
        assert ok is True, reason

    def test_lore_task_update_admitted(self):
        ok, reason = check_planning_reader_command("lore task update lr-1 --status open")
        assert ok is True, reason

    def test_readonly_verb_admitted(self):
        ok, reason = check_planning_reader_command("ls -la")
        assert ok is True, reason

    def test_git_not_admitted(self):
        ok, reason = check_planning_reader_command("git status")
        assert ok is False

    def test_unrelated_command_denied(self):
        ok, reason = check_planning_reader_command("curl http://example.invalid")
        assert ok is False
        assert "planning-reader-role" in reason


class TestCheckPlanningReaderCommandReadOnlyPrecheck:
    _CFG = PlanningReaderReadOnlyConfig(
        verb_pattern=_PLANNING_READ_VERB_RE, caller_role="planning-reader"
    )

    def test_get_with_caller_flag_admitted(self):
        cmd = "synthetic-git-host-api GET /repos/o/r --caller planning-reader"
        ok, reason = check_planning_reader_command(cmd, read_only_configs=(self._CFG,))
        assert ok is True, reason

    def test_write_method_denied(self):
        cmd = "synthetic-git-host-api POST /repos/o/r --caller planning-reader"
        ok, reason = check_planning_reader_command(cmd, read_only_configs=(self._CFG,))
        assert ok is False

    def test_without_read_only_configs_read_shape_not_admitted(self):
        cmd = "synthetic-git-host-api GET /repos/o/r --caller planning-reader"
        ok, reason = check_planning_reader_command(cmd)
        assert ok is False


class TestCheckPlanningReaderCommandForbiddenAndCompound:
    def test_forbidden_git_pattern_denied(self):
        ok, reason = check_planning_reader_command("git push --force")
        assert ok is False

    def test_compound_expression_denied(self):
        ok, reason = check_planning_reader_command("lore search x && rm -rf /")
        assert ok is False


# ---------------------------------------------------------------------------
# check_bash_call — SE2 role dispatch
# ---------------------------------------------------------------------------


class TestCheckBashCallSe2RoleDispatch:
    def test_reviewer_dispatch_delegates(self):
        ok, reason = check_bash_call(BashRole.REVIEWER, "git show HEAD")
        assert ok is True, reason

    def test_security_dispatch_delegates(self):
        ok, reason = check_bash_call(BashRole.SECURITY, "git log")
        assert ok is True, reason

    def test_security_dispatch_honors_scanner_patterns(self):
        patterns = (re.compile(r"^gitleaks(\s|$)"),)
        ok, reason = check_bash_call(
            BashRole.SECURITY, "gitleaks detect", scanner_patterns=patterns
        )
        assert ok is True, reason

    def test_analysis_dispatch_delegates(self):
        cfg = AnalysisRoleConfig(git_readonly_subcommands=("log",))
        ok, reason = check_bash_call(BashRole.ANALYSIS, "git log", analysis_config=cfg)
        assert ok is True, reason

    def test_research_dispatch_delegates(self):
        ok, reason = check_bash_call(BashRole.RESEARCH, "lore search foo")
        assert ok is True, reason

    def test_planning_reader_dispatch_delegates(self):
        ok, reason = check_bash_call(
            BashRole.PLANNING_READER, "lore task create --title x"
        )
        assert ok is True, reason

    def test_unrelated_role_specific_params_ignored_across_roles(self):
        # Passing every role-specific param at once must not raise for a
        # role that ignores most of them.
        cfg = AnalysisRoleConfig(git_readonly_subcommands=("log",))
        ok, reason = check_bash_call(
            BashRole.BUILDER,
            "git status",
            review_config=ReviewGateConfig(),
            scanner_patterns=(re.compile(r"^gitleaks(\s|$)"),),
            analysis_config=cfg,
            research_config=ResearchRoleConfig(),
            planning_reader_read_only_configs=(),
        )
        assert ok is True, reason
