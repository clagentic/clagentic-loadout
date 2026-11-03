"""review.changed_lines: which lines of which files a unified diff adds."""

from __future__ import annotations

import pytest

from clagentic_loadout.review.changed_lines import DiffUnreadableError, added_lines
from tests._review_cli_support import make_diff


def test_added_lines_are_numbered_on_the_new_side_and_context_is_not_added():
    diff = (
        "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n"
        "@@ -10,4 +10,5 @@ def f():\n ctx\n-gone\n+new one\n+new two\n ctx\n ctx\n"
        "@@ -40,2 +41,2 @@\n ctx\n+swapped\n-old\n"
    )

    assert added_lines(diff) == {"m.py": frozenset({11, 12, 42})}


def test_a_new_file_adds_every_line():
    assert added_lines(make_diff({"n.py": 3})) == {"n.py": frozenset({1, 2, 3})}


def test_content_that_looks_like_file_headers_is_content():
    diff = (
        "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n"
        "@@ -1,2 +1,2 @@\n--- not a header\n+++ not a header either\n ctx\n"
    )

    assert added_lines(diff) == {"m.py": frozenset({1})}


def test_a_deleted_file_and_a_mode_only_change_add_nothing():
    diff = (
        "diff --git a/d.py b/d.py\ndeleted file mode 100644\n--- a/d.py\n+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n-x\n"
        "diff --git a/e.sh b/e.sh\nold mode 100644\nnew mode 100755\n"
    )

    assert added_lines(diff) == {}


def test_a_rename_is_keyed_by_its_new_name():
    diff = (
        "diff --git a/old.py b/new.py\nsimilarity index 90%\nrename from old.py\nrename to new.py\n"
        "--- a/old.py\n+++ b/new.py\n@@ -1,1 +1,2 @@\n ctx\n+added\n"
    )

    assert added_lines(diff) == {"new.py": frozenset({2})}


def test_a_quoted_path_is_unquoted():
    diff = 'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n@@ -0,0 +1,1 @@\n+x\n'

    assert added_lines(diff) == {"café.py": frozenset({1})}


def test_no_newline_marker_does_not_shift_numbering():
    diff = (
        "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n"
        "@@ -1,1 +1,2 @@\n-a\n\\ No newline at end of file\n+a\n+b\n"
    )

    assert added_lines(diff) == {"m.py": frozenset({1, 2})}


@pytest.mark.parametrize("text", ["", "not a diff\n+++ b/x\n@@ -1 +1 @@\n+y\n"])
def test_text_without_a_file_section_is_unreadable(text):
    with pytest.raises(DiffUnreadableError):
        added_lines(text)


def _quoted_diff(quoted: str) -> str:
    return (
        f'diff --git "a/{quoted}" "b/{quoted}"\n--- "a/{quoted}"\n+++ "b/{quoted}"\n'
        "@@ -0,0 +1,1 @@\n+x\n"
    )


@pytest.mark.parametrize(
    ("quoted", "path"),
    [
        ("x\\\\303.py", "x\\303.py"),
        ("a\\\\tb", "a\\tb"),
        ("a\\tb", "a\tb"),
        ('q\\"n\\\\', 'q"n\\'),
        ("caf\\303\\251.py", "café.py"),
        ("日本.py", "日本.py"),
        ("\\346\\227\\245.py", "日.py"),
    ],
)
def test_a_c_quoted_path_decodes_each_escape_exactly_once(quoted, path):
    assert added_lines(_quoted_diff(quoted)) == {path: frozenset({1})}


def test_a_hunk_whose_counts_are_unmet_at_the_end_of_the_text_is_unreadable():
    diff = "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1,3 +1,4 @@\n ctx\n+new\n"

    with pytest.raises(DiffUnreadableError):
        added_lines(diff)


def test_a_hunk_cut_before_the_next_file_section_is_unreadable():
    diff = (
        "diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1,3 +1,4 @@\n ctx\n+new\n"
        "diff --git a/n.py b/n.py\n--- a/n.py\n+++ b/n.py\n@@ -0,0 +1,1 @@\n+x\n"
    )

    with pytest.raises(DiffUnreadableError):
        added_lines(diff)
