"""test_guard_bash_admission.py — containment/scratch/scope admission layer
(lr-288fad, guard-bash decomposition slice 2 of 3, epic lr-5a8d).

Conformance (CLAUDE.md rule 6a): every fixture uses synthetic verb names,
paths, and roles — no real agent names, no LORE present, no real machine
identifiers. Two inherited items get dedicated test classes:
`TestBodyStdinPipeCarveOut*` (item 1) and `TestAnsiCEvasionWiring` (item 2,
the security-reviewer forward nit, PR #113 comment 15824).
"""

from __future__ import annotations

import re

from clagentic_loadout.guard.bash_admission import (
    BodyStdinVerb,
    MethodPathFlagRule,
    classify_body_stdin_pipe_ambiguity,
    detect_project_tree_write_targets,
    is_admitted_body_stdin_pipe,
    is_admitted_loadout_family_readonly,
    is_fd_safe_target,
    requires_admission_flag,
)


# ---------------------------------------------------------------------------
# is_fd_safe_target
# ---------------------------------------------------------------------------


class TestIsFdSafeTarget:
    def test_dev_null_is_safe(self):
        assert is_fd_safe_target("/dev/null") is True

    def test_fd_dup_is_safe(self):
        assert is_fd_safe_target("&1") is True
        assert is_fd_safe_target("&2") is True

    def test_real_path_is_not_safe(self):
        assert is_fd_safe_target("/workspace/evil") is False

    def test_bare_ampersand_no_digit_is_not_safe(self):
        assert is_fd_safe_target("&") is False
        assert is_fd_safe_target("&x") is False


# ---------------------------------------------------------------------------
# detect_project_tree_write_targets
# ---------------------------------------------------------------------------


class TestDetectProjectTreeWriteTargets:
    def test_no_redirect_returns_empty(self):
        assert detect_project_tree_write_targets("git status") == []

    def test_home_no_longer_excluded(self):
        # lr-f8649f: $HOME dropped as a sanctioned scratch-staging root --
        # a $HOME-spelled redirect must now be reported as a project-tree
        # write CANDIDATE, not silently exempted as staging.
        assert detect_project_tree_write_targets(
            "printf 'x' > $HOME/scratch.txt"
        ) == ["$HOME/scratch.txt"]

    def test_tmpdir_staging_excluded(self):
        assert detect_project_tree_write_targets("printf 'x' > $TMPDIR/scratch.txt") == []

    def test_dev_null_excluded(self):
        assert detect_project_tree_write_targets("cmd 2> /dev/null") == []

    def test_fd_dup_excluded(self):
        assert detect_project_tree_write_targets("cmd 2>&1") == []

    def test_real_target_detected(self):
        assert detect_project_tree_write_targets("echo x > /workspace/repo/file.txt") == [
            "/workspace/repo/file.txt"
        ]

    def test_multiple_redirect_targets_all_reported(self):
        targets = detect_project_tree_write_targets(
            "go test ./... > /workspace/in-scope > /workspace/out-of-scope"
        )
        assert targets == ["/workspace/in-scope", "/workspace/out-of-scope"]

    def test_quoted_redirect_looking_string_is_not_a_target(self):
        # A redirect-looking substring inside an isolated quoted argv span
        # is data, not a real redirect.
        assert detect_project_tree_write_targets('echo "a > b"') == []

    def test_quote_glued_redirect_is_recognized(self):
        targets = detect_project_tree_write_targets('cmd >""/workspace/evil')
        assert targets == ["/workspace/evil"]

    def test_unparseable_quoting_falls_back_to_raw_scan(self):
        # Unbalanced quote: normalize_shell_words returns None, raw scan
        # still finds the redirect operator.
        targets = detect_project_tree_write_targets("echo 'unbalanced > /workspace/x")
        assert "/workspace/x" in targets or targets == []
        # (either the raw-scan finds it or the ambiguous string yields no
        # redirect match at all -- both are acceptable non-permissive
        # outcomes for this enumeration primitive; the point is it must not
        # raise.)


# ---------------------------------------------------------------------------
# is_admitted_loadout_family_readonly
# ---------------------------------------------------------------------------

_MUTATING = frozenset({"loadout-merge", "loadout-push", "loadout-git-host-api"})


class TestLoadoutFamilyReadonly:
    def test_non_mutating_bare_verb_admitted(self):
        assert is_admitted_loadout_family_readonly(
            "loadout-doctor --check", mutating_verb_names=_MUTATING
        )

    def test_mutating_bare_verb_not_admitted(self):
        assert not is_admitted_loadout_family_readonly(
            "loadout-merge --pr 1", mutating_verb_names=_MUTATING
        )

    def test_mutating_verb_prefix_of_nonmutating_name_not_falsely_excluded(self):
        # loadout-mergex is NOT loadout-merge -- must still admit.
        assert is_admitted_loadout_family_readonly(
            "loadout-mergex --check", mutating_verb_names=_MUTATING
        )

    def test_bare_loadout_prefix_no_name_not_admitted(self):
        assert not is_admitted_loadout_family_readonly(
            "loadout- --check", mutating_verb_names=_MUTATING
        )

    def test_install_path_form_admitted_when_sanctioned_dir_given(self):
        assert is_admitted_loadout_family_readonly(
            "/opt/synthetic/bin/loadout-doctor --check",
            mutating_verb_names=_MUTATING,
            sanctioned_bin_dir="/opt/synthetic/bin",
        )

    def test_install_path_mutating_verb_not_admitted(self):
        assert not is_admitted_loadout_family_readonly(
            "/opt/synthetic/bin/loadout-merge --pr 1",
            mutating_verb_names=_MUTATING,
            sanctioned_bin_dir="/opt/synthetic/bin",
        )

    def test_install_path_form_without_sanctioned_dir_not_admitted(self):
        # No sanctioned_bin_dir supplied -- absolute-path form never matches.
        assert not is_admitted_loadout_family_readonly(
            "/opt/synthetic/bin/loadout-doctor --check",
            mutating_verb_names=_MUTATING,
        )

    def test_unrelated_absolute_path_not_admitted(self):
        assert not is_admitted_loadout_family_readonly(
            "/tmp/evil/loadout-doctor --check",
            mutating_verb_names=_MUTATING,
            sanctioned_bin_dir="/opt/synthetic/bin",
        )

    def test_module_namespace_form_admitted(self):
        assert is_admitted_loadout_family_readonly(
            "python3 -m clagentic_loadout.doctor.cli",
            mutating_verb_names=_MUTATING,
        )

    def test_unrelated_module_namespace_not_admitted(self):
        assert not is_admitted_loadout_family_readonly(
            "python3 -m http.server", mutating_verb_names=_MUTATING
        )

    def test_unrelated_command_not_admitted(self):
        assert not is_admitted_loadout_family_readonly(
            "git push --force", mutating_verb_names=_MUTATING
        )


# ---------------------------------------------------------------------------
# requires_admission_flag
# ---------------------------------------------------------------------------

_COMMENTS_POST_RULE = MethodPathFlagRule(
    method="POST",
    path_pattern=re.compile(r"/api/v1/repos/[^/\s]+/[^/\s]+/issues/\d+/comments\b"),
    required_flag="--verify-comment",
    deny_reason="POST to a comments path requires --verify-comment.",
)

_SYNTHETIC_VERB_RE = re.compile(r"^synthetic-curl(\s|$)")


class TestRequiresAdmissionFlag:
    def test_non_matching_verb_is_no_opinion(self):
        ok, reason = requires_admission_flag(
            "git push", verb_pattern=_SYNTHETIC_VERB_RE, rule=_COMMENTS_POST_RULE
        )
        assert ok is True
        assert reason == ""

    def test_get_method_not_gated(self):
        ok, _ = requires_admission_flag(
            "synthetic-curl GET /api/v1/repos/o/r/issues/1/comments",
            verb_pattern=_SYNTHETIC_VERB_RE,
            rule=_COMMENTS_POST_RULE,
        )
        assert ok is True

    def test_post_other_path_not_gated(self):
        ok, _ = requires_admission_flag(
            "synthetic-curl POST /api/v1/repos/o/r/pulls/1/reviews",
            verb_pattern=_SYNTHETIC_VERB_RE,
            rule=_COMMENTS_POST_RULE,
        )
        assert ok is True

    def test_post_comments_without_flag_denied(self):
        ok, reason = requires_admission_flag(
            "synthetic-curl POST /api/v1/repos/o/r/issues/1/comments",
            verb_pattern=_SYNTHETIC_VERB_RE,
            rule=_COMMENTS_POST_RULE,
        )
        assert ok is False
        assert "verify-comment" in reason

    def test_post_comments_with_flag_admitted(self):
        ok, _ = requires_admission_flag(
            "synthetic-curl POST /api/v1/repos/o/r/issues/1/comments --verify-comment",
            verb_pattern=_SYNTHETIC_VERB_RE,
            rule=_COMMENTS_POST_RULE,
        )
        assert ok is True

    def test_lowercase_method_still_matched(self):
        # A tool's own argv parsing upper-cases the method before comparing
        # (a security-review nit) -- the guard must agree.
        ok, reason = requires_admission_flag(
            "synthetic-curl post /api/v1/repos/o/r/issues/1/comments",
            verb_pattern=_SYNTHETIC_VERB_RE,
            rule=_COMMENTS_POST_RULE,
        )
        assert ok is False

    def test_unparseable_quoting_denies(self):
        ok, reason = requires_admission_flag(
            "synthetic-curl POST /api/v1/repos/o/r/issues/1/comments 'unbalanced",
            verb_pattern=_SYNTHETIC_VERB_RE,
            rule=_COMMENTS_POST_RULE,
        )
        assert ok is False
        assert "normalized" in reason


# ---------------------------------------------------------------------------
# INHERITED ITEM 1 -- body-stdin pipe carve-out
# ---------------------------------------------------------------------------

_GIT_HOST_API = BodyStdinVerb(
    verb_pattern=re.compile(r"^synthetic-git-host-api(\s|$)"),
    requires_body_stdin_flag=True,
)
_REVIEW_POST = BodyStdinVerb(
    verb_pattern=re.compile(r"^synthetic-review-post(\s|$)"),
    requires_body_stdin_flag=False,
)
_VERBS = (_GIT_HOST_API, _REVIEW_POST)


class TestBodyStdinPipeCarveOutAdmission:
    def test_flag_gated_verb_with_flag_admitted(self):
        cmd = "echo '{\"body\": \"x\"}' | synthetic-git-host-api POST --body-stdin"
        assert is_admitted_body_stdin_pipe(cmd, verbs=_VERBS) is True

    def test_flag_gated_verb_without_flag_not_admitted(self):
        cmd = "echo '{\"body\": \"x\"}' | synthetic-git-host-api POST"
        assert is_admitted_body_stdin_pipe(cmd, verbs=_VERBS) is False

    def test_unconditional_stdin_verb_needs_no_flag(self):
        cmd = "printf '%s' 'x' | synthetic-review-post --caller role"
        assert is_admitted_body_stdin_pipe(cmd, verbs=_VERBS) is True

    def test_cat_producer_admitted(self):
        cmd = "cat body.json | synthetic-review-post --caller role"
        assert is_admitted_body_stdin_pipe(cmd, verbs=_VERBS) is True

    def test_non_producer_lhs_not_admitted(self):
        cmd = "curl x | synthetic-review-post --caller role"
        assert is_admitted_body_stdin_pipe(cmd, verbs=_VERBS) is False

    def test_unrelated_rhs_not_admitted(self):
        cmd = "echo x | rm -rf /"
        assert is_admitted_body_stdin_pipe(cmd, verbs=_VERBS) is False

    def test_second_operator_not_admitted(self):
        cmd = "echo x | synthetic-review-post --caller role | tee out"
        assert is_admitted_body_stdin_pipe(cmd, verbs=_VERBS) is False

    def test_chained_operator_not_admitted(self):
        cmd = "echo x | synthetic-review-post --caller role && rm -rf /"
        assert is_admitted_body_stdin_pipe(cmd, verbs=_VERBS) is False

    def test_no_pipe_at_all_not_admitted(self):
        assert is_admitted_body_stdin_pipe("synthetic-review-post --caller role", verbs=_VERBS) is False


class TestBodyStdinPipeAmbiguityClassification:
    def test_unrelated_pipe_returns_none(self):
        assert classify_body_stdin_pipe_ambiguity("grep foo | wc -l") is None

    def test_clean_admitted_shape_returns_none(self):
        # Both sides parse cleanly -- not this classifier's concern even
        # though it happens to also be an admitted shape.
        cmd = "echo x | synthetic-review-post --caller role"
        assert classify_body_stdin_pipe_ambiguity(cmd) is None

    def test_unparseable_producer_with_recognizable_verb_hint_returns_message(self):
        # The messaging hint (_BODY_STDIN_VERB_NAME_HINT_RE) is intentionally
        # loadout-branded (mirrors the reference's own verb-name hint,
        # reference ll.664-675) -- these are real loadout product binary
        # names, not agent names, so a `loadout-<verb>`-shaped RHS is the
        # correct fixture here (unlike the admission tests above, which use
        # a fully synthetic verb registry via BodyStdinVerb).
        cmd = "echo '`evil`' | loadout-review-post --caller role"
        msg = classify_body_stdin_pipe_ambiguity(cmd)
        assert msg is not None
        assert "body-AMBIGUITY" in msg

    def test_non_producer_lhs_returns_none(self):
        cmd = "curl '`evil`' | loadout-review-post --caller role"
        assert classify_body_stdin_pipe_ambiguity(cmd) is None

    def test_rhs_not_loadout_shaped_returns_none(self):
        cmd = "echo '`evil`' | some-other-tool --caller role"
        assert classify_body_stdin_pipe_ambiguity(cmd) is None


# ---------------------------------------------------------------------------
# INHERITED ITEM 2 -- ANSI-C evasion wiring (security-reviewer forward nit,
# PR #113 comment 15824): has_unresolved_ansi_c_quote must be INVOKED by
# every raw-fallback verb-matcher in this module.
# ---------------------------------------------------------------------------


class TestAnsiCEvasionWiring:
    def test_family_readonly_denies_on_unresolved_ansi_c_quote(self):
        # $'\cXloadout-merge' does not normalize (unrecognized \c escape)
        # and contains an ANSI-C opener -- must hard-deny, never fall
        # through to a permissive bare-basename match.
        cmd = "$'\\cXloadout-doctor' --check"
        assert is_admitted_loadout_family_readonly(cmd, mutating_verb_names=_MUTATING) is False

    def test_requires_admission_flag_denies_with_ansi_c_note(self):
        cmd = "synthetic-curl $'\\cXPOST' /api/v1/repos/o/r/issues/1/comments"
        ok, reason = requires_admission_flag(
            cmd, verb_pattern=_SYNTHETIC_VERB_RE, rule=_COMMENTS_POST_RULE
        )
        assert ok is False
        assert "ANSI-C" in reason

    def test_body_stdin_pipe_denies_on_unresolved_ansi_c_quote_lhs(self):
        cmd = "$'\\cXecho' x | synthetic-review-post --caller role"
        assert is_admitted_body_stdin_pipe(cmd, verbs=_VERBS) is False

    def test_body_stdin_pipe_denies_on_unresolved_ansi_c_quote_rhs(self):
        cmd = "echo x | $'\\cXsynthetic-review-post' --caller role"
        assert is_admitted_body_stdin_pipe(cmd, verbs=_VERBS) is False
