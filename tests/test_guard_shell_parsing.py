"""test_guard_shell_parsing.py — shell-word normalization / compound-command
detection CORE (lr-7ff55e, guard-bash decomposition slice 1 of 3, epic
lr-5a8d).

Conformance (CLAUDE.md rule 6a): every fixture below uses synthetic
commands/paths — no real machine identifiers, no LORE present, no agent
names. `clagentic_loadout.guard.shell_parsing` is pure text processing with
no policy content, so these tests exercise structural behavior only (does a
compound operator get detected, does a quoted span get correctly masked or
spliced) — never a role/verb allow-or-deny decision, which belongs to a
later slice (lr-288fad / lr-19ae42).
"""

from __future__ import annotations

import pytest

from clagentic_loadout.guard.shell_parsing import (
    cmd_head,
    compound_check,
    decode_ansi_c_escapes,
    decode_for_match,
    detect_tmp_redirect_target,
    has_background_operator,
    has_unresolved_ansi_c_quote,
    is_safe_redirect_only,
    is_staging_redirect_target,
    mask_quoted_spans,
    normalize_shell_words,
    quote_delimited_spans,
    split_glued_redirect_operators,
    split_segments,
    unquoted_spans,
)


class TestDecodeForMatch:
    def test_single_encoding_decodes(self):
        assert decode_for_match("a%2Fb") == "a/b"

    def test_double_encoding_decodes_within_bound(self):
        assert decode_for_match("a%252Fb") == "a/b"

    def test_plus_folds_to_space(self):
        assert decode_for_match("a+b") == "a b"

    def test_stable_input_returns_unchanged(self):
        assert decode_for_match("plain text") == "plain text"

    def test_bounded_passes_does_not_hang_on_pathological_input(self):
        # Should not raise or loop forever; bounded to a few passes.
        decode_for_match("%25" * 50)


class TestUnquotedSpans:
    def test_no_quotes_single_span(self):
        assert unquoted_spans("git push") == [(0, 8)]

    def test_quoted_token_excluded(self):
        s = 'echo "hidden" tail'
        spans = unquoted_spans(s)
        text_covered = "".join(s[start:end] for start, end in spans)
        assert "hidden" not in text_covered

    def test_adjacent_unquoted_segments_around_quote(self):
        spans = unquoted_spans('a"b"c')
        assert spans == [(0, 1), (4, 5)]

    def test_empty_string_no_spans(self):
        assert unquoted_spans("") == []


class TestMaskQuotedSpans:
    def test_preserves_length(self):
        s = 'echo "a|b" && rm -rf /'
        masked = mask_quoted_spans(s)
        assert masked is not None
        assert len(masked) == len(s)

    def test_blanks_isolated_quoted_metacharacter(self):
        masked = mask_quoted_spans('grep -E "x|y" file')
        assert masked is not None
        assert "|" not in masked

    def test_unquoted_operator_preserved(self):
        masked = mask_quoted_spans('cmd1 && cmd2')
        assert masked is not None
        assert "&&" in masked

    def test_unbalanced_quote_returns_none(self):
        assert mask_quoted_spans('echo "unterminated') is None

    def test_quoted_command_substitution_dollar_paren_returns_none(self):
        assert mask_quoted_spans('echo "$(git push --force)"') is None

    def test_quoted_backtick_returns_none(self):
        assert mask_quoted_spans('echo "`git push --force`"') is None


class TestDecodeAnsiCEscapes:
    def test_simple_escapes(self):
        assert decode_ansi_c_escapes(r"a\tb\nc") == "a\tb\nc"

    def test_octal_escape(self):
        assert decode_ansi_c_escapes(r"\101") == "A"

    def test_hex_escape(self):
        assert decode_ansi_c_escapes(r"\x41") == "A"

    def test_unicode_short_escape(self):
        assert decode_ansi_c_escapes("\\u0041") == "A"

    def test_unicode_long_escape(self):
        assert decode_ansi_c_escapes(r"\U00000041") == "A"

    def test_trailing_backslash_raises(self):
        with pytest.raises(ValueError):
            decode_ansi_c_escapes("a\\")

    def test_unrecognized_escape_raises(self):
        with pytest.raises(ValueError):
            decode_ansi_c_escapes(r"\c")

    def test_hex_with_no_digits_raises(self):
        with pytest.raises(ValueError):
            decode_ansi_c_escapes(r"\x")


class TestQuoteDelimitedSpans:
    def test_bare_single_quote_span(self):
        regions = quote_delimited_spans("echo 'hi'")
        assert len(regions) == 1
        start, end, content, is_ansi_c = regions[0]
        assert content == "hi"
        assert is_ansi_c is False
        assert (start, end) == (5, 9)

    def test_ansi_c_span_consumes_leading_dollar(self):
        regions = quote_delimited_spans("$'git'")
        assert len(regions) == 1
        start, end, content, is_ansi_c = regions[0]
        assert start == 0  # the leading $ is part of the region
        assert content == "git"
        assert is_ansi_c is True

    def test_ansi_c_decodes_escapes_in_content(self):
        regions = quote_delimited_spans(r"$'a\tb'")
        assert regions[0][2] == "a\tb"

    def test_double_quote_backslash_escape_preserved_literally(self):
        # Non-ANSI-C double-quote backslash handling only unescapes the
        # quote-character escape itself; other backslashes pass through.
        regions = quote_delimited_spans('"a\\"b"')
        assert regions[0][2] == 'a"b'

    def test_unbalanced_quote_raises(self):
        with pytest.raises(ValueError):
            quote_delimited_spans("echo 'unterminated")

    def test_unresolvable_ansi_c_escape_raises(self):
        with pytest.raises(ValueError):
            quote_delimited_spans(r"$'\c'")


class TestHasUnresolvedAnsiCQuote:
    def test_detects_single_quote_opener(self):
        assert has_unresolved_ansi_c_quote("cmd $'x'") is True

    def test_detects_double_quote_opener(self):
        assert has_unresolved_ansi_c_quote('cmd $"x"') is True

    def test_no_opener_returns_false(self):
        assert has_unresolved_ansi_c_quote("cmd 'x'") is False


class TestSplitGluedRedirectOperators:
    def test_splits_glued_fd_dup_and_write(self):
        out = split_glued_redirect_operators("2>&1>/workspace/evil")
        assert out == "2>&1 >/workspace/evil"

    def test_unglued_input_unchanged_in_content(self):
        out = split_glued_redirect_operators("cmd 2>&1")
        assert "2>&1" in out

    def test_no_redirect_at_all_unchanged(self):
        assert split_glued_redirect_operators("git status") == "git status"


class TestNormalizeShellWords:
    def test_glued_quote_fragments_join_into_real_word(self):
        out = normalize_shell_words('g""it push --force')
        assert out is not None
        assert "git push --force" in out

    def test_isolated_quoted_span_blanked(self):
        out = normalize_shell_words('grep -E "x|y" file')
        assert out is not None
        assert "|" not in out

    def test_ansi_c_span_always_spliced_even_when_isolated(self):
        out = normalize_shell_words("$'git' push --force")
        assert out is not None
        assert "git push --force" in out
        assert "$" not in out

    def test_glued_redirect_split_over_full_string(self):
        out = normalize_shell_words("cmd 2>&1>/workspace/evil")
        assert out is not None
        assert "2>&1 >/workspace/evil" in out

    def test_unbalanced_quote_returns_none(self):
        assert normalize_shell_words("echo 'unterminated") is None

    def test_quoted_command_substitution_returns_none(self):
        assert normalize_shell_words('echo "$(git push --force)"') is None

    def test_unresolvable_ansi_c_escape_returns_none(self):
        assert normalize_shell_words(r"cmd $'\c'") is None


class TestSplitSegments:
    def test_no_operator_single_segment(self):
        assert split_segments("git status") == [("", "git status")]

    def test_semicolon_chain(self):
        pairs = split_segments("a; b")
        assert pairs == [("", "a"), (";", " b")]

    def test_double_ampersand_chain(self):
        pairs = split_segments("a && b")
        assert pairs == [("", "a "), ("&&", " b")]

    def test_double_pipe_chain(self):
        pairs = split_segments("a || b")
        assert pairs == [("", "a "), ("||", " b")]

    def test_single_pipe(self):
        pairs = split_segments("a | b")
        assert pairs == [("", "a "), ("|", " b")]

    def test_fd_dup_ampersand_not_treated_as_background_operator(self):
        pairs = split_segments("cmd 2>&1")
        assert pairs == [("", "cmd 2>&1")]

    def test_bare_background_ampersand_splits(self):
        pairs = split_segments("cmd &")
        assert pairs == [("", "cmd "), ("&", "")]

    def test_quoted_operator_does_not_split(self):
        pairs = split_segments('echo "a|b"')
        assert pairs == [("", 'echo "a|b"')]

    def test_unbalanced_quote_raises(self):
        with pytest.raises(ValueError):
            split_segments("echo 'unterminated")


class TestCmdHead:
    def test_no_heredoc_returned_unchanged(self):
        assert cmd_head("git status") == "git status"

    def test_heredoc_body_stripped(self):
        cmd = "cat > /tmp/f << 'EOF'\ngit push --force\nEOF"
        head = cmd_head(cmd)
        assert "git push --force" not in head
        assert head.startswith("cat > /tmp/f")


class TestIsSafeRedirectOnly:
    def test_simple_output_redirect_is_safe(self):
        assert is_safe_redirect_only("cmd > file") is True

    def test_simple_input_redirect_is_safe(self):
        assert is_safe_redirect_only("cmd < file") is True

    def test_pipe_is_not_safe(self):
        assert is_safe_redirect_only("cmd | other") is False

    def test_chain_is_not_safe(self):
        assert is_safe_redirect_only("cmd && other") is False

    def test_subshell_paren_is_not_safe(self):
        assert is_safe_redirect_only("(cmd)") is False


class TestHasBackgroundOperator:
    def test_trailing_ampersand_detected(self):
        assert has_background_operator("cmd &") is True

    def test_ampersand_followed_by_other_token_detected(self):
        assert has_background_operator("cmd & disown") is True
        assert has_background_operator("cmd & true") is True
        assert has_background_operator("cmd & sleep 60") is True

    def test_double_ampersand_not_background(self):
        assert has_background_operator("cmd1 && cmd2") is False

    def test_fd_dup_redirect_not_background(self):
        assert has_background_operator("cmd 2>&1") is False
        assert has_background_operator("cmd >&2") is False

    def test_no_ampersand_at_all(self):
        assert has_background_operator("git status") is False


class TestCompoundCheck:
    def test_simple_command_allowed(self):
        ok, reason = compound_check("git status")
        assert ok is True
        assert reason == ""

    def test_unquoted_pipe_denied(self):
        ok, reason = compound_check("cmd1 | cmd2")
        assert ok is False
        assert "compound" in reason

    def test_unquoted_double_ampersand_denied(self):
        ok, reason = compound_check("cmd1 && rm -rf /")
        assert ok is False

    def test_unquoted_semicolon_chain_denied(self):
        ok, reason = compound_check("cmd1 ; cmd2")
        assert ok is False

    def test_quoted_pipe_metacharacter_allowed(self):
        ok, reason = compound_check('grep -E "x|y" file')
        assert ok is True
        assert reason == ""

    def test_simple_redirect_allowed(self):
        ok, reason = compound_check("cmd > $HOME/out.txt")
        assert ok is True

    def test_heredoc_body_pipe_not_flagged(self):
        cmd = "cat > $HOME/f << 'EOF'\ngrep foo | wc -l\nEOF"
        ok, reason = compound_check(cmd)
        assert ok is True

    def test_trailing_background_ampersand_denied(self):
        ok, reason = compound_check("cmd &")
        assert ok is False
        assert "backgrounding" in reason

    def test_fd_dup_redirect_not_flagged_as_background(self):
        ok, reason = compound_check("cmd 2>&1")
        assert ok is True

    def test_quote_glued_pipe_metacharacter_still_denies(self):
        # g""it push denormalizes to "git push" -- an unquoted, glued
        # verb -- but a genuinely glued PIPE metacharacter should still
        # surface as a live operator once splice-joined.
        ok, reason = compound_check('cmd1 |""| cmd2')
        assert ok is False

    def test_ansi_c_quoted_verb_does_not_bypass_pipe_deny(self):
        ok, reason = compound_check("$'cmd1' | cmd2")
        assert ok is False


class TestDetectTmpRedirectTarget:
    def test_unquoted_tmp_redirect_detected(self):
        assert detect_tmp_redirect_target("echo hi > /tmp/evil") == "/tmp/evil"

    def test_append_redirect_detected(self):
        assert detect_tmp_redirect_target("echo hi >> /tmp/evil") == "/tmp/evil"

    def test_home_redirect_not_detected(self):
        assert detect_tmp_redirect_target("echo hi > $HOME/out.txt") is None

    def test_no_redirect_returns_none(self):
        assert detect_tmp_redirect_target("git status") is None

    def test_quoted_tmp_string_is_not_a_redirect_target(self):
        assert detect_tmp_redirect_target('lore observe "writes to /tmp/x"') is None

    def test_heredoc_body_tmp_string_not_detected(self):
        cmd = "cat > $HOME/f << 'EOF'\nsome text about /tmp/evil\nEOF"
        assert detect_tmp_redirect_target(cmd) is None


class TestIsStagingRedirectTarget:
    def test_bare_home_no_longer_staging(self):
        # lr-f8649f: $HOME dropped as a sanctioned scratch-staging root.
        assert is_staging_redirect_target("$HOME") is False

    def test_home_subpath_no_longer_staging(self):
        assert is_staging_redirect_target("$HOME/scratch/f.txt") is False

    def test_bare_tmpdir(self):
        assert is_staging_redirect_target("$TMPDIR") is True

    def test_tmpdir_subpath(self):
        assert is_staging_redirect_target("$TMPDIR/work/f.txt") is True

    def test_tmp_path_is_not_staging(self):
        assert is_staging_redirect_target("/tmp/evil") is False

    def test_workspace_path_is_not_staging(self):
        assert is_staging_redirect_target("/workspace/repo/f.txt") is False

    def test_home_lookalike_prefix_not_matched(self):
        # $HOMEFOO is not $HOME or $HOME/... -- must not false-positive on
        # a bare prefix match.
        assert is_staging_redirect_target("$HOMEFOO/x") is False
