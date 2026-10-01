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


def test_non_positive_bound_is_rejected():
    with pytest.raises(ValueError):
        plan_chunks("diff --git a/x b/x\n", 0)
