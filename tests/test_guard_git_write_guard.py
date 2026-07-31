"""test_guard_git_write_guard.py — git/PR write-operation hard-deny for a
non-agent session (lr-59dd37, port of the reference deployment's
crew-git-guard.py; lr-5a8d epic Wave C).

Conformance (CLAUDE.md rule 6a): synthetic hosts/verbs only, no LORE
present, no real forge hostname or agent name hardcoded anywhere in the
module under test.
"""

from __future__ import annotations

from clagentic_loadout.guard.git_write_guard import (
    GitWriteDenyContext,
    GitWriteGuardConfig,
    build_default_forge_patterns,
    check_git_write_call,
    classify_git_write_command,
)


# ---------------------------------------------------------------------------
# classify_git_write_command — basic shapes
# ---------------------------------------------------------------------------


class TestClassifyGitWriteCommandBasicShapes:
    def test_git_push_classified(self):
        is_write, reason = classify_git_write_command("git push origin main", GitWriteGuardConfig())
        assert is_write is True
        assert reason == "git push"

    def test_git_status_not_classified(self):
        is_write, _ = classify_git_write_command("git status", GitWriteGuardConfig())
        assert is_write is False

    def test_gh_pr_create_classified(self):
        is_write, reason = classify_git_write_command("gh pr create --title x", GitWriteGuardConfig())
        assert is_write is True
        assert reason == "gh pr create"

    def test_gh_pr_merge_classified(self):
        is_write, reason = classify_git_write_command("gh pr merge 42", GitWriteGuardConfig())
        assert is_write is True
        assert reason == "gh pr merge"

    def test_gh_pr_list_not_classified(self):
        is_write, _ = classify_git_write_command("gh pr list", GitWriteGuardConfig())
        assert is_write is False

    def test_caller_push_verb_pattern_classified(self):
        import re

        config = GitWriteGuardConfig(
            push_verb_patterns=(re.compile(r"(?:^|\s|/)example-land\.py(?:\s|$)"),)
        )
        is_write, reason = classify_git_write_command("python3 /opt/example-land.py", config)
        assert is_write is True
        assert "sanctioned-landing-tool" in reason

    def test_sanctioned_verb_pattern_never_classified(self):
        import re

        config = GitWriteGuardConfig(
            sanctioned_verb_patterns=(re.compile(r"example_push_tool\.py"),)
        )
        is_write, _ = classify_git_write_command(
            "python3 /opt/example_push_tool.py --agent builder", config
        )
        assert is_write is False

    def test_sanctioned_verb_pattern_wins_even_with_git_push_present(self):
        import re

        config = GitWriteGuardConfig(
            sanctioned_verb_patterns=(re.compile(r"example_push_tool\.py"),)
        )
        # The sanctioned tool's own internal git-push invocation must not
        # self-deny (reference: crew_push.py itself runs `git push` under
        # the hood, and is unconditionally exempted first).
        is_write, _ = classify_git_write_command(
            "python3 /opt/example_push_tool.py && git push", config
        )
        assert is_write is False


class TestClassifyGitWriteCommandForgeHostPatterns:
    def test_github_pull_create_post_classified(self):
        cmd = 'curl -X POST https://api.github.com/repos/example/repo/pulls -d "{}"'
        is_write, reason = classify_git_write_command(cmd, GitWriteGuardConfig())
        assert is_write is True
        assert reason == "PR creation via curl"

    def test_github_pull_create_get_not_classified(self):
        cmd = "curl https://api.github.com/repos/example/repo/pulls/5"
        is_write, _ = classify_git_write_command(cmd, GitWriteGuardConfig())
        assert is_write is False

    def test_github_pull_merge_classified_regardless_of_method(self):
        cmd = "curl -X PUT https://api.github.com/repos/example/repo/pulls/5/merge"
        is_write, reason = classify_git_write_command(cmd, GitWriteGuardConfig())
        assert is_write is True
        assert reason == "PR merge via curl"

    def test_caller_forge_host_pattern_create_classified(self):
        create_patterns, merge_patterns = build_default_forge_patterns(
            ("example-forge\\.internal-test",)
        )
        config = GitWriteGuardConfig(
            forge_pr_create_patterns=create_patterns,
            forge_pr_merge_patterns=merge_patterns,
        )
        cmd = 'curl -X POST https://example-forge.internal-test/api/v1/repos/o/r/pulls -d "{}"'
        is_write, reason = classify_git_write_command(cmd, config)
        assert is_write is True
        assert reason == "PR creation via curl"

    def test_caller_forge_host_pattern_merge_classified(self):
        create_patterns, merge_patterns = build_default_forge_patterns(
            ("example-forge\\.internal-test",)
        )
        config = GitWriteGuardConfig(
            forge_pr_create_patterns=create_patterns,
            forge_pr_merge_patterns=merge_patterns,
        )
        cmd = "curl https://example-forge.internal-test/api/v1/repos/o/r/pulls/9/merge"
        is_write, reason = classify_git_write_command(cmd, config)
        assert is_write is True
        assert reason == "PR merge via curl"

    def test_no_forge_host_patterns_configured_only_github_generic_applies(self):
        config = GitWriteGuardConfig()
        cmd = 'curl -X POST https://example-forge.internal-test/api/v1/repos/o/r/pulls'
        is_write, _ = classify_git_write_command(cmd, config)
        assert is_write is False


# ---------------------------------------------------------------------------
# Quote-aware scanning: a forbidden token inside a quoted narrative argv
# span is data, not a command to classify.
# ---------------------------------------------------------------------------


class TestClassifyGitWriteCommandQuoteAwareness:
    def test_git_push_inside_quoted_narrative_not_classified(self):
        cmd = 'lore task create --description "narrative: git push was used"'
        is_write, _ = classify_git_write_command(cmd, GitWriteGuardConfig())
        assert is_write is False

    def test_unquoted_git_push_alongside_quoted_narrative_still_classified(self):
        cmd = 'lore task create --description "narrative text" ; git push'
        is_write, reason = classify_git_write_command(cmd, GitWriteGuardConfig())
        assert is_write is True
        assert reason == "git push"


# ---------------------------------------------------------------------------
# ANSI-C fragmentation cannot EVADE classification (module docstring point
# 5 — the mirror-image hardening of role_allowlist's bare-verb-grant gate:
# here the risk is a fragmented write op silently passing as "not a write
# op", not a fragmented deny silently passing a bare-verb GRANT).
# ---------------------------------------------------------------------------


class TestAnsiCFragmentationCannotEvadeClassification:
    def test_resolvable_ansi_c_fragmented_git_push_still_classified(self):
        cmd = "git $'push' origin main"
        is_write, reason = classify_git_write_command(cmd, GitWriteGuardConfig())
        assert is_write is True
        assert reason == "git push"

    def test_resolvable_ansi_c_fragmented_full_phrase_still_classified(self):
        cmd = "git $'push origin main'"
        is_write, reason = classify_git_write_command(cmd, GitWriteGuardConfig())
        assert is_write is True
        assert reason == "git push"

    def test_unresolvable_ansi_c_span_fails_closed_and_still_classifies(self):
        # An unrecognized ANSI-C escape (\c is not in the supported set)
        # makes normalize_shell_words return None. Post-lr-59dd37-followup
        # (BOBBIE PR #120 comment 15918), this classifier no longer falls
        # back to a masked raw-scan on ANSI-C ambiguity (see
        # TestAnsiCCollateralMaskingCannotEvadeClassification for why that
        # fallback was unsafe) -- it fails closed and classifies as a write
        # op outright whenever an ANSI-C opener is present anywhere and
        # normalization failed. The security property this test guards
        # (is_write is True -- the command still denies) is unchanged; only
        # the internal reason text changed from "git push" to the
        # deny-on-ambiguity explanation.
        cmd = "git push $'\\c bogus'"
        is_write, reason = classify_git_write_command(cmd, GitWriteGuardConfig())
        assert is_write is True
        assert "ANSI-C" in reason

    def test_harmless_ansi_c_span_does_not_false_positive(self):
        cmd = "git $'status'"
        is_write, _ = classify_git_write_command(cmd, GitWriteGuardConfig())
        assert is_write is False


# ---------------------------------------------------------------------------
# ANSI-C COLLATERAL MASKING cannot evade classification (pre-merge
# security-audit finding, follow-up to lr-59dd37): a resolvable ANSI-C
# write-verb span ("git $'push'") plus a SECOND, UNRELATED malformed ANSI-C
# span elsewhere in the same command ("$'\c'") makes
# shell_parsing.normalize_shell_words fail
# GLOBALLY for the whole command. The bug this class regresses: the classifier
# previously fell back to a masking scan (shell_parsing.mask_quoted_spans,
# with zero ANSI-C awareness) that blanked the FIRST, perfectly resolvable
# $'push' span as if it were isolated narrative data -- hiding the real verb
# and returning "not a write op" for an actual `git push`. The fix fails
# closed instead: any normalization failure with an ANSI-C opener present
# anywhere classifies as a write op outright.
# ---------------------------------------------------------------------------


class TestAnsiCCollateralMaskingCannotEvadeClassification:
    def test_resolvable_push_plus_unrelated_malformed_span_still_denies(self):
        # $'push' alone decodes cleanly; $'\c bogus' elsewhere does not,
        # poisoning normalize_shell_words for the WHOLE command. Must still
        # classify as a write op (fail closed), never silently pass through.
        cmd = "git $'push' origin main ; echo $'\\c bogus'"
        is_write, reason = classify_git_write_command(cmd, GitWriteGuardConfig())
        assert is_write is True
        assert "ANSI-C" in reason

    def test_resolvable_push_plus_unrelated_malformed_span_denied_for_non_agent(self):
        cmd = "git $'push' origin main ; echo $'\\c bogus'"
        ok, reason = check_git_write_call(
            cmd, is_named_agent=False, config=GitWriteGuardConfig(),
            deny_context=_DENY_CONTEXT,
        )
        assert ok is False
        assert "BLOCKED" in reason

    def test_malformed_span_with_no_write_verb_at_all_still_fails_closed(self):
        # No resolvable write verb anywhere, but an unrelated malformed ANSI-C
        # span is still present -- deny-on-ambiguity means this classifies as
        # a write op too (the classifier cannot prove it is NOT one).
        cmd = "echo hello ; echo $'\\c bogus'"
        is_write, reason = classify_git_write_command(cmd, GitWriteGuardConfig())
        assert is_write is True
        assert "ANSI-C" in reason

    def test_sanctioned_verb_still_exempts_even_under_ansi_c_ambiguity(self):
        # The sanctioned-tool escape hatch (crew_push.py's own internal git
        # push) must still self-exempt even when an unrelated malformed
        # ANSI-C span elsewhere would otherwise trigger the fail-closed path.
        import re

        config = GitWriteGuardConfig(
            sanctioned_verb_patterns=(re.compile(r"example_push_tool\.py"),)
        )
        cmd = "python3 /opt/example_push_tool.py ; echo $'\\c bogus'"
        is_write, _ = classify_git_write_command(cmd, config)
        assert is_write is False

    def test_plain_benign_command_still_classifies_benign(self):
        # Confirms the fix has not widened the deny surface for ordinary,
        # unambiguous commands with no ANSI-C quoting at all.
        is_write, _ = classify_git_write_command("git status", GitWriteGuardConfig())
        assert is_write is False

    def test_plain_unbalanced_quote_with_no_ansi_c_opener_still_raw_fallback(self):
        # Normalization failure with NO ANSI-C opener present at all (plain
        # unbalanced bare-quote nesting) still falls back to the raw,
        # unmasked head -- not the new fail-closed ANSI-C path -- and an
        # unquoted git push earlier in the string is still visible to it.
        cmd = "git push ; echo 'unterminated"
        is_write, reason = classify_git_write_command(cmd, GitWriteGuardConfig())
        assert is_write is True
        assert reason == "git push"


# ---------------------------------------------------------------------------
# check_git_write_call — top-level entry point
# ---------------------------------------------------------------------------


_DENY_CONTEXT = GitWriteDenyContext(
    project_label="example-project (synthetic)",
    push_redirect_instructions="Dispatch the push/PR to the builder role.",
    merge_redirect_instructions="Dispatch the merge to the merger role.",
    override_env_var_name="EXAMPLE_INSESSION_GIT",
)


class TestCheckGitWriteCall:
    def test_non_write_command_allowed(self):
        ok, reason = check_git_write_call(
            "git status", is_named_agent=False, config=GitWriteGuardConfig(),
            deny_context=_DENY_CONTEXT,
        )
        assert ok is True
        assert reason == ""

    def test_named_agent_write_command_allowed(self):
        ok, reason = check_git_write_call(
            "git push", is_named_agent=True, config=GitWriteGuardConfig(),
            deny_context=_DENY_CONTEXT,
        )
        assert ok is True

    def test_non_agent_write_command_denied(self):
        ok, reason = check_git_write_call(
            "git push", is_named_agent=False, config=GitWriteGuardConfig(),
            deny_context=_DENY_CONTEXT,
        )
        assert ok is False
        assert "git push" in reason
        assert "Dispatch the push/PR to the builder role." in reason

    def test_merge_reason_uses_merge_redirect(self):
        ok, reason = check_git_write_call(
            "gh pr merge 1", is_named_agent=False, config=GitWriteGuardConfig(),
            deny_context=_DENY_CONTEXT,
        )
        assert ok is False
        assert "Dispatch the merge to the merger role." in reason

    def test_override_active_allows_write_from_non_agent(self):
        ok, reason = check_git_write_call(
            "git push", is_named_agent=False, config=GitWriteGuardConfig(),
            deny_context=_DENY_CONTEXT, override_active=True,
        )
        assert ok is True
        assert reason == ""

    def test_override_env_var_name_surfaced_in_deny_message(self):
        ok, reason = check_git_write_call(
            "git push", is_named_agent=False, config=GitWriteGuardConfig(),
            deny_context=_DENY_CONTEXT,
        )
        assert ok is False
        assert "EXAMPLE_INSESSION_GIT=1" in reason

    def test_no_override_env_var_name_omits_bypass_line(self):
        deny_context = GitWriteDenyContext(
            project_label="p",
            push_redirect_instructions="x",
            merge_redirect_instructions="y",
        )
        ok, reason = check_git_write_call(
            "git push", is_named_agent=False, config=GitWriteGuardConfig(),
            deny_context=deny_context,
        )
        assert ok is False
        assert "To bypass this guard" not in reason

# Source-level anonymization conformance (no fixed agent-name/operator-host
# literal in the module itself) is enforced repo-wide by
# tests/test_anonymization_guard.py, not duplicated here.
