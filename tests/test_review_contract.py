"""test_review_contract.py — tests for clagentic_loadout.review.contract
(lr-412f, Wave B slice 2).

Coverage:
  - validate_review_body_stdin_content: empty / non-JSON / non-object /
    missing-body / empty-string-body all raise ReviewBodyStdinEmptyError; a
    well-formed body returns the extracted string.
  - VerifiedReview is a frozen, comparable value shape.
  - ReviewBackend is a runtime-checkable Protocol both backends satisfy.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.review.contract import (
    ReviewBackend,
    VerifiedReview,
    validate_review_body_stdin_content,
    validate_review_findings_body_stdin_content,
)
from clagentic_loadout.review.errors import ReviewBodyStdinEmptyError
from clagentic_loadout.review.forgejo_backend import ForgejoReviewBackend
from clagentic_loadout.review.github_backend import GithubReviewBackend


class TestValidateReviewBodyStdinContent:
    def test_well_formed_body_returns_extracted_string(self):
        assert validate_review_body_stdin_content(b'{"body": "LGTM"}') == "LGTM"

    def test_empty_bytes_rejected(self):
        with pytest.raises(ReviewBodyStdinEmptyError, match="empty input"):
            validate_review_body_stdin_content(b"")

    def test_non_json_rejected(self):
        with pytest.raises(ReviewBodyStdinEmptyError, match="valid JSON"):
            validate_review_body_stdin_content(b"not json at all")

    def test_non_object_json_rejected(self):
        with pytest.raises(ReviewBodyStdinEmptyError, match="JSON OBJECT"):
            validate_review_body_stdin_content(b"[1, 2, 3]")

    def test_missing_body_key_rejected(self):
        with pytest.raises(ReviewBodyStdinEmptyError, match="non-empty 'body'"):
            validate_review_body_stdin_content(b'{"not_body": "x"}')

    def test_empty_body_string_rejected(self):
        with pytest.raises(ReviewBodyStdinEmptyError, match="non-empty 'body'"):
            validate_review_body_stdin_content(b'{"body": "   "}')

    def test_non_string_body_rejected(self):
        with pytest.raises(ReviewBodyStdinEmptyError, match="non-empty 'body'"):
            validate_review_body_stdin_content(b'{"body": 123}')


class TestVerifiedReview:
    def test_is_frozen_and_comparable(self):
        a = VerifiedReview(id=1, url="http://x", login="bot[bot]")
        b = VerifiedReview(id=1, url="http://x", login="bot[bot]")
        assert a == b
        with pytest.raises(AttributeError):
            a.id = 2  # type: ignore[misc]


class TestReviewBackendProtocol:
    def test_github_backend_satisfies_protocol(self):
        backend = GithubReviewBackend("tok")
        assert isinstance(backend, ReviewBackend)

    def test_forgejo_backend_satisfies_protocol(self):
        backend = ForgejoReviewBackend("tok", git_host_base="http://git-host.example.com")
        assert isinstance(backend, ReviewBackend)


class TestValidateReviewFindingsBodyStdinContent:
    """--verdict-findings route (lr-c26110): stdin carries NO 'body' field
    at all -- only review_status + a structured findings list."""

    def test_well_formed_returns_status_and_findings(self):
        payload = (
            b'{"review_status":"blocking","findings":[{"file":"a.py",'
            b'"line":1,"rule_id":"E1","message":"m"}]}'
        )
        status, findings = validate_review_findings_body_stdin_content(payload)
        assert status == "blocking"
        assert findings == [{"file": "a.py", "line": 1, "rule_id": "E1", "message": "m"}]

    def test_clean_with_empty_findings_list_is_valid(self):
        status, findings = validate_review_findings_body_stdin_content(
            b'{"review_status":"clean","findings":[]}'
        )
        assert status == "clean"
        assert findings == []

    def test_empty_bytes_rejected(self):
        with pytest.raises(ReviewBodyStdinEmptyError, match="empty input"):
            validate_review_findings_body_stdin_content(b"")

    def test_non_json_rejected(self):
        with pytest.raises(ReviewBodyStdinEmptyError, match="valid JSON"):
            validate_review_findings_body_stdin_content(b"not json")

    def test_non_object_json_rejected(self):
        with pytest.raises(ReviewBodyStdinEmptyError, match="JSON OBJECT"):
            validate_review_findings_body_stdin_content(b"[1, 2]")

    def test_invalid_review_status_rejected(self):
        with pytest.raises(ReviewBodyStdinEmptyError, match="review_status"):
            validate_review_findings_body_stdin_content(
                b'{"review_status":"maybe","findings":[]}'
            )

    def test_missing_findings_key_rejected(self):
        with pytest.raises(ReviewBodyStdinEmptyError, match="findings"):
            validate_review_findings_body_stdin_content(b'{"review_status":"clean"}')

    def test_findings_not_a_list_rejected(self):
        with pytest.raises(ReviewBodyStdinEmptyError, match="findings"):
            validate_review_findings_body_stdin_content(
                b'{"review_status":"clean","findings":"not-a-list"}'
            )

    def test_finding_entry_not_an_object_rejected(self):
        with pytest.raises(ReviewBodyStdinEmptyError, match="findings\\[0\\]"):
            validate_review_findings_body_stdin_content(
                b'{"review_status":"clean","findings":["not-an-object"]}'
            )

    def test_finding_missing_required_field_rejected(self):
        with pytest.raises(ReviewBodyStdinEmptyError, match="findings\\[0\\]"):
            validate_review_findings_body_stdin_content(
                b'{"review_status":"clean","findings":[{"file":"a.py","line":1,"rule_id":"E1"}]}'
            )

    def test_a_body_field_is_simply_ignored_not_treated_as_prose(self):
        # The route deliberately has no 'body'/prose slot -- an extra 'body'
        # key present in the caller's JSON is not an error and is never
        # consulted; only review_status + findings are extracted.
        status, findings = validate_review_findings_body_stdin_content(
            b'{"body":"a foreign narrative","review_status":"clean","findings":[]}'
        )
        assert status == "clean"
        assert findings == []
