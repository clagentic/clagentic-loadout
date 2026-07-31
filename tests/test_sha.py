"""test_sha.py — unit tests for clagentic_loadout.sha (ported lr-09f9 from
an internal deployment's own tests/test_sha.py, lr-56b4 EPIC A item 2).

Coverage:
  - normalize_sha: whitespace stripped, lowercased, empty stays empty.
  - validate_sha: accepts well-formed abbreviated/full SHAs; raises
    InvalidShaError on malformed input; empty string always accepted.
  - compare_sha_values: the two named defect classes explicitly reproduced
    and fixed —
      * lr-6495 — a 7-char abbreviated SHA must compare EQUAL to the full
        40-char SHA it is a prefix of (previously false-mismatched).
      * lr-503d — a 41-char value with stray whitespace must compare EQUAL
        to the real 40-char SHA once whitespace is stripped (previously
        false-mismatched).
    Plus general correctness: exact match, true mismatch, empty-vs-empty,
    one-side-empty, case-insensitivity.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.sha import (
    InvalidShaError,
    compare_sha_values,
    normalize_sha,
    validate_sha,
)

FULL_SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"  # 40 chars, valid hex


class TestNormalizeSha:
    def test_strips_leading_trailing_whitespace(self):
        assert normalize_sha(f"  {FULL_SHA}  ") == FULL_SHA

    def test_strips_trailing_newline(self):
        assert normalize_sha(f"{FULL_SHA}\n") == FULL_SHA

    def test_lowercases(self):
        assert normalize_sha(FULL_SHA.upper()) == FULL_SHA

    def test_empty_stays_empty(self):
        assert normalize_sha("") == ""

    def test_whitespace_only_normalizes_to_empty(self):
        assert normalize_sha("   \n\t") == ""


class TestValidateSha:
    def test_full_40_char_sha_accepted(self):
        assert validate_sha(FULL_SHA) == FULL_SHA

    def test_abbreviated_7_char_sha_accepted_by_default(self):
        short = FULL_SHA[:7]
        assert validate_sha(short) == short

    def test_abbreviated_rejected_when_allow_abbreviated_false(self):
        short = FULL_SHA[:7]
        with pytest.raises(InvalidShaError):
            validate_sha(short, allow_abbreviated=False)

    def test_full_sha_accepted_when_allow_abbreviated_false(self):
        assert validate_sha(FULL_SHA, allow_abbreviated=False) == FULL_SHA

    def test_empty_string_always_accepted(self):
        assert validate_sha("") == ""
        assert validate_sha("", allow_abbreviated=False) == ""

    def test_too_short_6_chars_rejected(self):
        with pytest.raises(InvalidShaError):
            validate_sha(FULL_SHA[:6])

    def test_too_long_41_chars_rejected(self):
        with pytest.raises(InvalidShaError):
            validate_sha(FULL_SHA + "a")

    def test_non_hex_characters_rejected(self):
        with pytest.raises(InvalidShaError):
            validate_sha("g1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0")

    def test_whitespace_normalized_before_validation(self):
        """A value with stray whitespace that normalizes to a valid 40-char
        SHA passes — normalize-then-validate order (lr-503d class)."""
        assert validate_sha(f"{FULL_SHA}\n") == FULL_SHA

    def test_uppercase_normalized_before_validation(self):
        assert validate_sha(FULL_SHA.upper()) == FULL_SHA


class TestCompareShaValuesDefectClasses:
    """The two named lr-56b4 defect classes, reproduced and fixed."""

    def test_lr6495_seven_char_abbreviated_matches_full_sha(self):
        """lr-6495: a 7-char abbreviated SHA must match the full 40-char SHA
        it is a prefix of — a bare string compare previously false-mismatched
        this and false-positived a stale-gate refusal on an IDENTICAL commit."""
        short = FULL_SHA[:7]
        assert compare_sha_values(short, FULL_SHA) is True

    def test_lr6495_seven_char_abbreviated_matches_as_actual_too(self):
        """Symmetric: abbreviated value on the 'actual' side also matches."""
        short = FULL_SHA[:7]
        assert compare_sha_values(FULL_SHA, short) is True

    def test_lr6495_abbreviated_non_matching_prefix_still_mismatches(self):
        """An abbreviated SHA that is NOT a real prefix of the full SHA must
        still be reported as a mismatch — the fix must not become permissive
        of genuinely different commits."""
        wrong_short = "0000000"
        assert compare_sha_values(wrong_short, FULL_SHA) is False

    def test_lr503d_stray_whitespace_and_41_chars_matches_after_strip(self):
        """lr-503d: a captured value with a trailing newline (shell-capture
        artifact) that is otherwise the identical 40-char SHA must compare
        EQUAL once whitespace is stripped — this is the exact incident shape
        (a 41-character string: 40 hex chars + 1 stray whitespace char)."""
        stray = f"{FULL_SHA}\n"
        assert len(stray) == 41
        assert compare_sha_values(stray, FULL_SHA) is True

    def test_lr503d_leading_and_trailing_whitespace_both_sides(self):
        assert compare_sha_values(f"  {FULL_SHA}  ", f"\t{FULL_SHA}\n") is True


class TestCompareShaValuesGeneral:
    def test_exact_match(self):
        assert compare_sha_values(FULL_SHA, FULL_SHA) is True

    def test_true_mismatch_two_full_shas(self):
        other = "0" * 40
        assert compare_sha_values(FULL_SHA, other) is False

    def test_both_empty_is_true_noop(self):
        assert compare_sha_values("", "") is True

    def test_one_side_empty_is_false(self):
        assert compare_sha_values("", FULL_SHA) is False
        assert compare_sha_values(FULL_SHA, "") is False

    def test_case_insensitive_match(self):
        assert compare_sha_values(FULL_SHA.upper(), FULL_SHA) is True
