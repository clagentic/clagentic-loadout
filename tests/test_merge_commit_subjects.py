"""test_merge_commit_subjects.py — tests for
clagentic_loadout.merge.commit_subjects (lr-835c57).

Coverage (acceptance criteria):
  - merge_method='merge' + a non-conformant 'lr-XXXX: <desc>' branch commit
    subject -> CommitSubjectInvalidError naming the offending SHA + subject
    + expected grammar.
  - merge_method='merge' + every branch subject conformant -> no raise.
  - Any merge_method other than 'merge' (e.g. squash) -> no-op regardless of
    subject content.
  - skip=True bypasses the check entirely, even on a non-conformant subject.
  - Grammar is reused from merge.title_gate.is_conventional_title, not
    forked (checked directly against the SAME conformant/non-conformant
    fixtures test_merge_title_gate.py uses).
  - A multi-line commit message is checked on its FIRST LINE only.
  - The first non-conformant subject in order is the one reported (fail on
    first offender, not a full scan).
"""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.commit_subjects import (
    REAL_MERGE_METHOD,
    check_branch_commit_subjects,
)
from clagentic_loadout.merge.errors import CommitSubjectInvalidError
from clagentic_loadout.merge.title_gate import is_conventional_title

_SHA_A = "a" * 40
_SHA_B = "b" * 40

CONFORMANT_SUBJECTS = [
    "feat: add a thing",
    "fix(lr-1234): correct a bug",
    "fix!: breaking change before colon",
    "docs: update readme",
    "chore: routine maintenance",
]

NON_CONFORMANT_SUBJECTS = [
    "lr-835c57: id-leading, no type",
    "not a conventional subject",
    "Feat: wrong case type",
]


class TestRealMergeMethodEnforced:
    @pytest.mark.parametrize("subject", CONFORMANT_SUBJECTS)
    def test_conformant_subjects_pass_on_real_merge(self, subject):
        check_branch_commit_subjects(
            [(_SHA_A, subject)], 1, "owner", "repo", merge_method=REAL_MERGE_METHOD,
        )  # no raise

    @pytest.mark.parametrize("subject", NON_CONFORMANT_SUBJECTS)
    def test_non_conformant_subject_refuses_on_real_merge(self, subject):
        with pytest.raises(CommitSubjectInvalidError) as exc_info:
            check_branch_commit_subjects(
                [(_SHA_A, subject)], 1, "owner", "repo", merge_method=REAL_MERGE_METHOD,
            )
        message = str(exc_info.value)
        assert _SHA_A in message
        assert subject in message
        assert "Conventional Commits grammar" in message

    def test_id_leading_no_type_subject_refuses(self):
        """The exact acceptance-criteria repro: 'lr-XXXX: <desc>' (ID-leading,
        no type token) on a merge_method='merge' repo must refuse."""
        with pytest.raises(CommitSubjectInvalidError) as exc_info:
            check_branch_commit_subjects(
                [(_SHA_A, "lr-835c57: fix the release cut")],
                7, "some-owner", "some-repo",
                merge_method=REAL_MERGE_METHOD,
            )
        message = str(exc_info.value)
        assert _SHA_A in message
        assert "lr-835c57: fix the release cut" in message
        assert "PR #7 in some-owner/some-repo" in message

    def test_every_subject_conformant_passes(self):
        """Acceptance criteria: every branch subject
        'type(scope): desc (lr-XXXX)' -> merges (no raise)."""
        check_branch_commit_subjects(
            [
                (_SHA_A, "feat(lr-835c57): add the commit-subject gate"),
                (_SHA_B, "test(lr-835c57): cover the commit-subject gate"),
            ],
            7, "some-owner", "some-repo",
            merge_method=REAL_MERGE_METHOD,
        )  # no raise

    def test_first_offender_reported_not_a_full_scan(self):
        with pytest.raises(CommitSubjectInvalidError) as exc_info:
            check_branch_commit_subjects(
                [
                    (_SHA_A, "feat: this one is fine"),
                    (_SHA_B, "lr-835c57: this one is not"),
                ],
                1, "owner", "repo", merge_method=REAL_MERGE_METHOD,
            )
        message = str(exc_info.value)
        assert _SHA_B in message
        assert "lr-835c57: this one is not" in message
        assert _SHA_A not in message

    def test_empty_commit_list_passes(self):
        check_branch_commit_subjects(
            [], 1, "owner", "repo", merge_method=REAL_MERGE_METHOD,
        )  # no raise


class TestSquashRepoIsNoOp:
    """Acceptance criteria: squash repo -> check is a no-op (unaffected)."""

    @pytest.mark.parametrize("merge_method", ["squash", "rebase", "anything-else"])
    def test_non_conformant_subject_never_raises_off_real_merge(self, merge_method):
        check_branch_commit_subjects(
            [(_SHA_A, "lr-835c57: this would refuse on a real merge")],
            1, "owner", "repo", merge_method=merge_method,
        )  # no raise -- no-op for any non-"merge" method


class TestSkipBypass:
    """Acceptance criteria: --skip-commit-check bypasses, logged (logging is
    the CALLER's job -- see merge.verb's own --skip-commit-check wiring)."""

    def test_skip_bypasses_even_a_non_conformant_subject_on_real_merge(self):
        check_branch_commit_subjects(
            [(_SHA_A, "lr-835c57: not conventional at all")],
            1, "owner", "repo", merge_method=REAL_MERGE_METHOD, skip=True,
        )  # no raise


class TestGrammarReusedFromTitleGate:
    """Hard constraint: is_conventional_title (merge.title_gate) is the
    SINGLE grammar source -- never forked. Every subject this module accepts
    or refuses must agree with the predicate's own verdict."""

    @pytest.mark.parametrize("subject", CONFORMANT_SUBJECTS + NON_CONFORMANT_SUBJECTS)
    def test_gate_outcome_matches_is_conventional_title_verdict(self, subject):
        raised = False
        try:
            check_branch_commit_subjects(
                [(_SHA_A, subject)], 1, "owner", "repo", merge_method=REAL_MERGE_METHOD,
            )
        except CommitSubjectInvalidError:
            raised = True
        assert raised is not is_conventional_title(subject)


class TestMultiLineCommitMessageFirstLineOnly:
    """The SUBJECT is the first line only -- a conformant subject followed
    by a non-conformant body must never fail, and vice versa (mirrors
    merge.title_gate's own single-line PR-title check, which never sees a
    body)."""

    def test_conformant_first_line_passes_regardless_of_body(self):
        check_branch_commit_subjects(
            [(_SHA_A, "feat(lr-835c57): add the gate\n\nlr-835c57: body detail")],
            1, "owner", "repo", merge_method=REAL_MERGE_METHOD,
        )  # no raise -- only the first line is checked
