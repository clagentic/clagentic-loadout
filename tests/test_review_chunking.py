"""Tests for review.chunking: deterministic, line-bounded diff splitting."""

from __future__ import annotations

import pytest

from clagentic_loadout.review.chunking import plan_chunks
from tests._review_cli_support import make_diff


def test_empty_diff_yields_no_chunks():
    assert plan_chunks("", 100) == []


def test_small_files_are_grouped_into_one_chunk():
    chunks = plan_chunks(make_diff({"a.py": 3, "b.py": 3}), 100)

    assert len(chunks) == 1
    assert chunks[0].files == ("a.py", "b.py")
    assert chunks[0].index == 1


def test_files_that_do_not_fit_together_get_their_own_chunks():
    chunks = plan_chunks(make_diff({"a.py": 6, "b.py": 6, "c.py": 6}), 14)

    assert [c.files for c in chunks] == [("a.py",), ("b.py",), ("c.py",)]
    assert [c.index for c in chunks] == [1, 2, 3]
    assert all(c.lines <= 14 for c in chunks)


def test_oversized_file_splits_on_hunk_boundaries_with_the_header_repeated():
    header = ["diff --git a/big.py b/big.py", "--- a/big.py", "+++ b/big.py"]
    hunk_one = ["@@ -1,1 +1,6 @@"] + [f"+one {i}" for i in range(5)]
    hunk_two = ["@@ -9,1 +14,6 @@"] + [f"+two {i}" for i in range(5)]
    diff = "\n".join(header + hunk_one + hunk_two) + "\n"

    chunks = plan_chunks(diff, 10)

    assert len(chunks) == 2
    for chunk in chunks:
        assert chunk.text.startswith("diff --git a/big.py b/big.py")
        assert chunk.lines <= 10
    assert "+one 0" in chunks[0].text and "+two 0" not in chunks[0].text
    assert "+two 0" in chunks[1].text


def test_oversized_hunk_splits_on_line_boundaries_and_keeps_every_line():
    diff = make_diff({"big.py": 40})

    chunks = plan_chunks(diff, 12)

    assert len(chunks) > 1
    assert all(c.lines <= 12 for c in chunks)
    joined = "\n".join(c.text for c in chunks)
    for i in range(1, 41):
        assert f"+line {i} of big.py\n" in joined + "\n"
    assert any("continued from the previous chunk" in c.text for c in chunks[1:])


def test_tiny_split_tail_is_merged_into_the_following_chunk():
    # big.py splits into a full 60-line piece and an 8-line tail (below the
    # bound // 6 floor of 10); the tail joins small.py rather than being
    # reviewed alone.
    chunks = plan_chunks(make_diff({"big.py": 58, "small.py": 6}), 60)

    assert len(chunks) == 2
    assert chunks[0].lines == 60
    assert chunks[1].files == ("big.py", "small.py")
    assert chunks[1].lines <= 60


def test_planning_is_deterministic():
    diff = make_diff({"a.py": 30, "b.py": 7, "c.py": 22})

    assert plan_chunks(diff, 15) == plan_chunks(diff, 15)


def test_exotic_line_separators_inside_content_do_not_split_lines():
    exotic = "+a\x0cb\x0bc\rd e f\x85g"
    diff = "\n".join(
        ["diff --git a/x b/x", "--- a/x", "+++ b/x", "@@ -0,0 +1,2 @@", exotic, "+tail"]
    ) + "\n"

    chunks = plan_chunks(diff, 100)

    assert len(chunks) == 1
    assert chunks[0].lines == 6
    assert chunks[0].text == diff


def test_continued_hunk_pieces_carry_real_headers_with_recomputed_offsets():
    # Old side: 3 context + 4 removed = 7 lines from 10; new side: 3 context + 5
    # added = 8 lines from 20.
    body = [" c1", " c2", " c3", "-r1", "-r2", "-r3", "-r4", "+a1", "+a2", "+a3", "+a4", "+a5"]
    diff = "\n".join(
        ["diff --git a/x b/x", "--- a/x", "+++ b/x", "@@ -10,7 +20,8 @@ def fn():"] + body
    ) + "\n"

    chunks = plan_chunks(diff, 9)

    headers = [
        line for chunk in chunks for line in chunk.text.splitlines() if line.startswith("@@")
    ]
    assert len(headers) == 3
    assert headers[0].startswith("@@ -10,5 +20,3 @@ def fn():")
    assert headers[1].startswith("@@ -15,2 +23,3 @@")
    # A piece with no old-side lines names the line BEFORE its position.
    assert headers[2].startswith("@@ -16,0 +26,2 @@")
    # Every piece's declared counts match the lines it actually carries.
    for chunk in chunks:
        lines = chunk.text.splitlines()
        declared = [ln for ln in lines if ln.startswith("@@")][0].split()
        old = sum(1 for ln in lines if ln[:1] in (" ", "-") and not ln.startswith("---"))
        new = sum(1 for ln in lines if ln[:1] in (" ", "+") and not ln.startswith("+++"))
        assert int(declared[1].split(",")[1]) == old
        assert int(declared[2].split(",")[1]) == new


def test_non_positive_bound_is_rejected():
    with pytest.raises(ValueError):
        plan_chunks("diff --git a/x b/x\n", 0)
