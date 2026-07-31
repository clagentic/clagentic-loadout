"""test_push_issue_link.py — tests for clagentic_loadout.push.issue_link
(lr-09ca, Wave B slice 3).
"""

from __future__ import annotations

import pytest

from clagentic_loadout.push.errors import MissingIssueLinkError
from clagentic_loadout.push.issue_link import (
    enforce_issue_link,
    normalize_closes_trailer,
    normalize_task_trailer,
    parse_closes_issue_number,
)


class TestNormalizeClosesTrailer:
    def test_appends_trailer_when_absent(self):
        result = normalize_closes_trailer("some body text", 42)
        assert "Closes #42" in result

    def test_no_change_when_trailer_already_present(self):
        body = "some body text\n\nCloses #42\n"
        assert normalize_closes_trailer(body, 42) == body

    def test_case_insensitive_keyword_recognized(self):
        body = "fixes the bug\n\ncloses #42\n"
        assert normalize_closes_trailer(body, 42) == body

    def test_does_not_overwrite_trailer_for_different_issue(self):
        body = "some body\n\nCloses #99\n"
        result = normalize_closes_trailer(body, 42)
        # Existing trailer for a different issue is left untouched -- the
        # mismatch is caught by enforce_issue_link, not silently rewritten.
        assert "Closes #99" in result
        assert "Closes #42" not in result

    def test_separator_when_body_does_not_end_in_newline(self):
        result = normalize_closes_trailer("no trailing newline", 7)
        assert result == "no trailing newline\n\nCloses #7\n"

    def test_separator_when_body_ends_in_newline(self):
        result = normalize_closes_trailer("has trailing newline\n", 7)
        assert result == "has trailing newline\n\nCloses #7\n"


class TestParseClosesIssueNumber:
    """lr-eb22f3: the read-back side merge.attestation relies on -- parsing
    the issue number out of a PR body's own trailer, never from a lore
    field."""

    def test_returns_number_when_trailer_present(self):
        assert parse_closes_issue_number("body\n\nCloses #42\n") == 42

    def test_returns_none_when_absent(self):
        assert parse_closes_issue_number("body with no trailer") is None

    def test_case_insensitive_keyword(self):
        assert parse_closes_issue_number("body\n\ncloses #7\n") == 7

    def test_empty_body_returns_none(self):
        assert parse_closes_issue_number("") is None


class TestNormalizeTaskTrailer:
    """lr-eb22f3: the write-side 'Task: <id>' trailer -- kept grammar-
    identical to release.dispatch._TASK_TRAILER_RE so the two sides agree."""

    def test_appends_trailer_when_absent(self):
        result = normalize_task_trailer("some body text", "lr-eb22f3")
        assert "Task: lr-eb22f3" in result

    def test_no_change_when_trailer_already_present(self):
        body = "some body text\n\nTask: lr-eb22f3\n"
        assert normalize_task_trailer(body, "lr-eb22f3") == body

    def test_does_not_overwrite_trailer_for_different_task_id(self):
        body = "some body\n\nTask: lr-other\n"
        result = normalize_task_trailer(body, "lr-eb22f3")
        assert "Task: lr-other" in result
        assert "Task: lr-eb22f3" not in result

    def test_separator_when_body_does_not_end_in_newline(self):
        result = normalize_task_trailer("no trailing newline", "lr-1")
        assert result == "no trailing newline\n\nTask: lr-1\n"

    def test_separator_when_body_ends_in_newline(self):
        result = normalize_task_trailer("has trailing newline\n", "lr-1")
        assert result == "has trailing newline\n\nTask: lr-1\n"

    def test_coexists_with_closes_trailer(self):
        body = normalize_closes_trailer("some body", 42)
        body = normalize_task_trailer(body, "lr-eb22f3")
        assert "Closes #42" in body
        assert "Task: lr-eb22f3" in body


class TestEnforceIssueLink:
    def test_none_issue_number_always_allowed(self):
        enforce_issue_link("no trailer at all", None)  # no raise

    def test_matching_trailer_passes(self):
        enforce_issue_link("body\n\nCloses #42\n", 42)  # no raise

    def test_missing_trailer_raises(self):
        with pytest.raises(MissingIssueLinkError):
            enforce_issue_link("body with no trailer", 42)

    def test_mismatched_trailer_raises(self):
        with pytest.raises(MissingIssueLinkError):
            enforce_issue_link("body\n\nCloses #99\n", 42)

    def test_error_message_names_the_expected_issue_number(self):
        with pytest.raises(MissingIssueLinkError) as exc_info:
            enforce_issue_link("body", 42)
        assert "42" in str(exc_info.value)
