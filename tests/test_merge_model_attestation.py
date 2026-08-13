"""test_merge_model_attestation.py — tests for
clagentic_loadout.merge.model_attestation (lr-95543d).

Coverage:
  - assert_model_attested: no-op on a blocking verdict (never enforced,
    even with model_attested missing/invalid).
  - clean verdict, model_attested missing/None -> ModelAttestationMissingError.
  - clean verdict, model_attested empty/whitespace-only ->
    ModelAttestationMissingError.
  - clean verdict, model_attested is a bare tier alias (exact word, any
    case) -> ModelAttestationInvalidError.
  - clean verdict, model_attested has no digit at all (alias-shaped, not in
    the enumerated set) -> ModelAttestationInvalidError (shape check, not
    just the enumerated alias list).
  - clean verdict, model_attested is a genuine resolved model string with a
    digit/date marker -> passes (no raise).
  - substring/case/whitespace edge cases named in the task:
      * a value CONTAINING "opus" as a sub-word of a longer allowed model
        string does NOT trip the bare-alias check (it has a digit, so it
        clears the shape gate) -- "claude-opus-4-1-20250805" passes.
      * "claude-haiku-4-5-20251001" is caught by a denylist term "haiku"
        (delimited-substring, not the bare-alias whole-value check).
      * a denylist term matching only as a RAW substring inside an
        unrelated word (e.g. "opus" inside a hypothetical
        "notopusmodel-4-1") is NOT flagged -- the delimited-match
        requirement.
  - Given the fix is reverted (assert_model_attested never called), no test
    here catches that directly since it's an integration point in
    merge.verb -- see test_merge_verb.py's own model-attestation coverage
    for the end-to-end regression lock.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.model_attestation import (
    ModelAttestationInvalidError,
    ModelAttestationMissingError,
    assert_model_attested,
)
from clagentic_loadout.merge.verdict import ReviewerVerdict

_FULL_SHA = "a" * 40


def _verdict(review_status: str, model_attested: str | None) -> ReviewerVerdict:
    return ReviewerVerdict(
        reviewer="some-reviewer",
        review_status=review_status,
        head_sha=_FULL_SHA,
        pr_number=1,
        comment_id=1,
        comment_author_login="some-reviewer-bot",
        model_attested=model_attested,
    )


class TestBlockingVerdictNeverEnforced:
    def test_blocking_with_no_model_attested_is_a_noop(self):
        verdict = _verdict("blocking", None)
        assert_model_attested(verdict, "some-reviewer")  # no raise

    def test_blocking_with_bare_alias_is_a_noop(self):
        verdict = _verdict("blocking", "opus")
        assert_model_attested(verdict, "some-reviewer")  # no raise


class TestCleanVerdictMissingAttestation:
    def test_none_raises_missing(self):
        verdict = _verdict("clean", None)
        with pytest.raises(ModelAttestationMissingError):
            assert_model_attested(verdict, "some-reviewer")

    def test_empty_string_raises_missing(self):
        verdict = _verdict("clean", "")
        with pytest.raises(ModelAttestationMissingError):
            assert_model_attested(verdict, "some-reviewer")

    def test_whitespace_only_raises_missing(self):
        verdict = _verdict("clean", "   \t  ")
        with pytest.raises(ModelAttestationMissingError):
            assert_model_attested(verdict, "some-reviewer")


class TestCleanVerdictBareTierAlias:
    def test_bare_alias_exact_match_raises_invalid(self):
        verdict = _verdict("clean", "gpt-flagship")
        with pytest.raises(ModelAttestationInvalidError):
            assert_model_attested(verdict, "some-reviewer")

    def test_bare_alias_case_insensitive(self):
        verdict = _verdict("clean", "OPUS")
        with pytest.raises(ModelAttestationInvalidError):
            assert_model_attested(verdict, "some-reviewer")

    def test_bare_alias_whitespace_stripped(self):
        verdict = _verdict("clean", "  haiku  ")
        with pytest.raises(ModelAttestationInvalidError):
            assert_model_attested(verdict, "some-reviewer")

    def test_unenumerated_alias_shaped_word_caught_by_shape_check(self):
        # Not in _BARE_TIER_ALIASES, but still has no digit/version marker
        # -- the shape check (not the enumerated set) catches it.
        verdict = _verdict("clean", "some-future-tier-name")
        with pytest.raises(ModelAttestationInvalidError):
            assert_model_attested(verdict, "some-reviewer")


class TestCleanVerdictResolvedModelStringPasses:
    def test_resolved_model_string_with_version_and_date_passes(self):
        verdict = _verdict("clean", "claude-haiku-4-5-20251001")
        assert_model_attested(verdict, "some-reviewer")  # no raise (no denylist)

    def test_resolved_model_string_containing_opus_as_subword_passes(self):
        # THE SUBSTRING TRAP (named explicitly in the task): a value
        # CONTAINING "opus" must not be rejected by a check intended for
        # the exact bare-alias fallback -- this is a real, resolved model
        # string with a version/date marker, not a bare "opus" alias.
        verdict = _verdict("clean", "claude-opus-4-1-20250805")
        assert_model_attested(verdict, "some-reviewer")  # no raise

    def test_gpt_dotted_version_passes(self):
        verdict = _verdict("clean", "gpt-5.1")
        assert_model_attested(verdict, "some-reviewer")  # no raise


class TestDenylist:
    def test_denylist_term_exact_word_match_raises_invalid(self):
        verdict = _verdict("clean", "claude-haiku-4-5-20251001")
        with pytest.raises(ModelAttestationInvalidError):
            assert_model_attested(verdict, "some-reviewer", denylist={"haiku"})

    def test_denylist_term_case_insensitive(self):
        verdict = _verdict("clean", "claude-HAIKU-4-5-20251001")
        with pytest.raises(ModelAttestationInvalidError):
            assert_model_attested(verdict, "some-reviewer", denylist={"haiku"})

    def test_denylist_full_model_string_match(self):
        verdict = _verdict("clean", "claude-haiku-4-5-20251001")
        with pytest.raises(ModelAttestationInvalidError):
            assert_model_attested(
                verdict, "some-reviewer", denylist={"claude-haiku-4-5-20251001"}
            )

    def test_denylist_term_as_raw_substring_of_unrelated_word_not_flagged(self):
        # THE FALSE-POSITIVE TRAP: "opus" must not match INSIDE a longer
        # alphanumeric run that merely contains those letters as a
        # sub-string of a different word.
        verdict = _verdict("clean", "notopusmodel-4-1-20250101")
        assert_model_attested(verdict, "some-reviewer", denylist={"opus"})  # no raise

    def test_denylist_term_matches_delimited_occurrence_mid_string(self):
        verdict = _verdict("clean", "claude-opus-4-1-20250805")
        with pytest.raises(ModelAttestationInvalidError):
            assert_model_attested(verdict, "some-reviewer", denylist={"opus"})

    def test_empty_denylist_is_a_noop_beyond_shape_check(self):
        verdict = _verdict("clean", "claude-opus-4-1-20250805")
        assert_model_attested(verdict, "some-reviewer", denylist=frozenset())  # no raise


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
