"""test_merge_title_gate.py — tests for clagentic_loadout.merge.title_gate
(lr-885f, Wave B slice 4; extended lr-6067 for the pr_number-optional
grammar predicate reused by push.verb).

Coverage:
  - Conformant titles (every type, with/without scope, with breaking marker)
    pass.
  - Non-conformant titles raise TitleInvalidError.
  - skip=True bypasses the check entirely, even for a badly-formed title.
  - Regression (lr-6067): the merge path's call shape (a real int
    pr_number) behaves IDENTICALLY to before the pr_number: Optional[int]
    refactor -- same exception type, same PR-number clause in the message.
  - is_conventional_title: the pure predicate extracted for push.verb's
    PR-open-time check (no PR number, no exception) agrees with
    check_pr_title's pass/fail verdict on every case above.
  - check_pr_title(pr_number=None): the PR-open-time call shape (no PR
    number yet) omits the PR-number clause and never fabricates one.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.errors import TitleInvalidError
from clagentic_loadout.merge.title_gate import check_pr_title, is_conventional_title

CONFORMANT_TITLES = [
    "feat: add a thing",
    "fix(lr-1234): correct a bug",
    "fix!: breaking change before colon",
    "docs: update readme",
    "refactor(module): reorganize",
    "perf: speed up hot path",
    "test: add coverage",
    "build: bump dependency",
    "ci: adjust pipeline",
    "chore: routine maintenance",
]

NON_CONFORMANT_TITLES = [
    "not a conventional title",
    "Feat: wrong case type",
    "feat:no space after colon",
    "feat:",
    "randomtype: bad type token",
    "feat(lr-273d)(lr-7a6e): two parenthesised scopes",  # PR #35 repro
]


class TestCheckPrTitle:
    @pytest.mark.parametrize("title", CONFORMANT_TITLES)
    def test_conformant_titles_pass(self, title):
        check_pr_title(title, 1, "owner", "repo")  # no raise

    @pytest.mark.parametrize("title", NON_CONFORMANT_TITLES)
    def test_non_conformant_titles_raise(self, title):
        with pytest.raises(TitleInvalidError) as exc_info:
            check_pr_title(title, 1, "owner", "repo")
        assert title in str(exc_info.value)

    def test_skip_bypasses_even_a_bad_title(self):
        check_pr_title("this is not conventional at all", 1, "owner", "repo", skip=True)  # no raise


class TestCheckPrTitleMergePathRegression:
    """lr-6067: the pr_number: Optional[int] refactor must not change the
    merge gate's own call shape or output -- merge.verb always passes a
    real int PR number, unchanged by this task."""

    def test_real_pr_number_still_named_in_error_message(self):
        with pytest.raises(TitleInvalidError) as exc_info:
            check_pr_title("bad title", 42, "some-owner", "some-repo")
        message = str(exc_info.value)
        assert "PR #42 in some-owner/some-repo" in message
        assert "bad title" in message

    def test_real_pr_number_conformant_title_still_passes(self):
        check_pr_title("feat(lr-6067): still passes", 42, "some-owner", "some-repo")  # no raise

    def test_skip_still_bypasses_with_a_real_pr_number(self):
        check_pr_title("not conventional", 42, "some-owner", "some-repo", skip=True)  # no raise


class TestCheckPrTitlePrNumberNone:
    """lr-6067: the PR-open-time call shape (push.verb) -- no PR number
    exists yet, so the caller passes None. Never a fabricated placeholder
    number."""

    def test_conformant_title_passes_with_no_pr_number(self):
        check_pr_title("feat(lr-6067): pr not open yet", None, "some-owner", "some-repo")  # no raise

    def test_non_conformant_title_raises_without_a_fake_pr_number(self):
        with pytest.raises(TitleInvalidError) as exc_info:
            check_pr_title("feat(lr-273d)(lr-7a6e): two scopes", None, "some-owner", "some-repo")
        message = str(exc_info.value)
        assert "feat(lr-273d)(lr-7a6e): two scopes" in message
        assert "some-owner/some-repo" in message
        # No PR-number clause fabricated -- "PR #" (the merge-path clause
        # prefix) must not appear anywhere in a None-pr_number message.
        assert "PR #" not in message

    def test_skip_bypasses_with_no_pr_number(self):
        check_pr_title("not conventional", None, "some-owner", "some-repo", skip=True)  # no raise


class TestIsConventionalTitle:
    """lr-6067: the pure predicate extracted for callers (push.verb) that
    need a pass/fail verdict without a PR number or an exception."""

    @pytest.mark.parametrize("title", CONFORMANT_TITLES)
    def test_conformant_titles_return_true(self, title):
        assert is_conventional_title(title) is True

    @pytest.mark.parametrize("title", NON_CONFORMANT_TITLES)
    def test_non_conformant_titles_return_false(self, title):
        assert is_conventional_title(title) is False
