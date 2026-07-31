"""test_acquire_contract.py — tests for clagentic_loadout.acquire.contract
(lr-c17040).

Coverage:
  - ChangedFile / AcquiredPr are frozen dataclasses with the expected default
    shapes (empty string/tuple defaults).
  - AcquiredPr.changed_filenames extracts a plain filename list from the
    richer ChangedFile tuple, matching merge.diff_scope.check_diff_scope's
    expected input shape.
  - AcquireBackend is a runtime_checkable Protocol: a class implementing
    fetch_pr_content(...) satisfies isinstance(), one that does not does not.
"""

from __future__ import annotations

from clagentic_loadout.acquire.contract import AcquireBackend, AcquiredPr, ChangedFile


class TestChangedFile:
    def test_defaults(self):
        cf = ChangedFile(filename="a.py")
        assert cf.status == ""
        assert cf.patch == ""
        assert cf.content == ""

    def test_is_frozen(self):
        cf = ChangedFile(filename="a.py")
        import pytest

        with pytest.raises(AttributeError):
            cf.filename = "b.py"  # type: ignore[misc]


class TestAcquiredPr:
    def test_defaults(self):
        acquired = AcquiredPr(
            owner="some-owner", repo="some-repo", pr_number=1, base_sha="a" * 40, head_sha="b" * 40
        )
        assert acquired.diff_text == ""
        assert acquired.changed_files == ()

    def test_changed_filenames_extracts_plain_list(self):
        acquired = AcquiredPr(
            owner="o",
            repo="r",
            pr_number=1,
            base_sha="a" * 40,
            head_sha="b" * 40,
            changed_files=(
                ChangedFile(filename="a.py", status="modified"),
                ChangedFile(filename="b.py", status="added"),
            ),
        )
        assert acquired.changed_filenames == ["a.py", "b.py"]

    def test_changed_filenames_empty_when_no_files(self):
        acquired = AcquiredPr(owner="o", repo="r", pr_number=1, base_sha="a", head_sha="b")
        assert acquired.changed_filenames == []


class TestAcquireBackendProtocol:
    def test_satisfying_class_is_instance(self):
        class _Backend:
            def fetch_pr_content(self, *, owner, repo, pr_number, include_file_contents=False):
                return AcquiredPr(owner=owner, repo=repo, pr_number=pr_number, base_sha="", head_sha="")

        assert isinstance(_Backend(), AcquireBackend)

    def test_non_satisfying_class_is_not_instance(self):
        class _NotABackend:
            def some_other_method(self):
                return None

        assert not isinstance(_NotABackend(), AcquireBackend)
