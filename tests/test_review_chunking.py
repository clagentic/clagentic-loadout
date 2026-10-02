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


def test_a_split_never_orphans_a_no_newline_marker():
    body = ["+l1", "+l2", "+l3", "+l4", "\\ No newline at end of file", "+l5"]
    diff = "\n".join(
        ["diff --git a/x b/x", "--- a/x", "+++ b/x", "@@ -0,0 +1,5 @@"] + body
    ) + "\n"

    chunks = plan_chunks(diff, 8)

    assert len(chunks) > 1
    assert all(c.lines <= 8 for c in chunks)
    for chunk in chunks:
        lines = chunk.text.splitlines()
        after_header = lines[lines.index(next(ln for ln in lines if ln.startswith("@@"))) + 1]
        assert not after_header.startswith("\\")
    joined = "\n".join(c.text for c in chunks)
    assert "+l4\n\\ No newline at end of file\n" in joined


def test_a_marker_is_carried_along_when_a_piece_holds_one_line():
    body = ["+l1", "\\ No newline at end of file", "+l2"]
    diff = "\n".join(
        ["diff --git a/x b/x", "--- a/x", "+++ b/x", "@@ -0,0 +1,2 @@"] + body
    ) + "\n"

    chunks = plan_chunks(diff, 5)

    joined = "\n".join(c.text for c in chunks)
    assert "+l1\n\\ No newline at end of file\n" in joined


def test_a_diff_without_git_headers_becomes_one_unnamed_chunk():
    diff = "--- a/p.txt\n+++ b/p.txt\n@@ -1 +1 @@\n-old\n+new\n"

    chunks = plan_chunks(diff, 100)

    assert len(chunks) == 1
    assert chunks[0].text == diff
    assert chunks[0].files == ("(diff without file headers)",)


def test_a_large_diff_without_git_headers_is_split_and_loses_no_line():
    body = [f"+added {i}" for i in range(30)]
    diff = "\n".join(["--- a/p.txt", "+++ b/p.txt", "@@ -0,0 +1,30 @@"] + body) + "\n"

    chunks = plan_chunks(diff, 10)

    assert len(chunks) > 1
    joined = "\n".join(c.text for c in chunks)
    assert all(f"+added {i}\n" in joined + "\n" for i in range(30))


@pytest.mark.parametrize("blank", ["\n", "\n\n", "  \n"])
def test_a_whitespace_only_diff_yields_no_chunks(blank):
    assert plan_chunks(blank, 100) == []


def test_a_tiny_bound_keeps_the_marker_with_its_line_even_one_line_over():
    body = ["+l1", "\\ No newline at end of file", "+l2"]
    diff = "\n".join(
        ["diff --git a/x b/x", "--- a/x", "+++ b/x", "@@ -0,0 +1,2 @@"] + body
    ) + "\n"

    chunks = plan_chunks(diff, 5)

    # The 3-line file header leaves a 2-line budget: one hunk header plus one
    # body line. The piece carrying the marker needs a second body line, so
    # it is the documented single line over the bound, never more.
    assert max(c.lines for c in chunks) == 5 + 1
    for chunk in chunks:
        lines = chunk.text.splitlines()
        marker_at = [i for i, ln in enumerate(lines) if ln.startswith("\\")]
        assert all(lines[i - 1].startswith("+") for i in marker_at)


def test_malformed_hunk_header_is_kept_not_replaced_with_invented_offsets():
    diff = make_diff({"big.py": 20}).replace("@@ -0,0 +1,20 @@", "@@ garbage @@")

    chunks = plan_chunks(diff, 10)

    headers = [
        line for chunk in chunks for line in chunk.text.splitlines() if line.startswith("@@")
    ]
    assert len(headers) > 1
    assert headers[0] == "@@ garbage @@"
    assert all(h.startswith("@@ garbage @@") for h in headers)
    assert not any(h.startswith("@@ -") for h in headers)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("diff --git a/dir b/file.py b/dir b/file.py", "dir b/file.py"),
        ('diff --git "a/sp ace.py" "b/sp ace.py"', "sp ace.py"),
        ("diff --git a/old.py b/new.py", "new.py"),
        ("diff --git a/plain.py b/plain.py", "plain.py"),
    ],
)
def test_file_names_survive_odd_headers(header, expected):
    diff = "\n".join([header, "--- a/x", "+++ b/x", "@@ -0,0 +1 @@", "+a"]) + "\n"

    assert plan_chunks(diff, 100)[0].files == (expected,)


def test_non_positive_bound_is_rejected():
    with pytest.raises(ValueError):
        plan_chunks("diff --git a/x b/x\n", 0)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ('diff --git "a/tab\\there.py" "b/tab\\there.py"', "tab\there.py"),
        ('diff --git "a/q\\"uote.py" "b/q\\"uote.py"', 'q"uote.py'),
        ('diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"', "café.py"),
        ('diff --git a/plain.py "b/we\\tird.py"', "we\tird.py"),
        ('diff --git "a/we\\tird.py" b/plain.py', "plain.py"),
        ('diff --git "a/back\\\\slash.py" "b/back\\\\slash.py"', "back\\slash.py"),
    ],
)
def test_c_quoted_paths_are_decoded(header, expected):
    diff = "\n".join([header, "--- a/x", "+++ b/x", "@@ -0,0 +1 @@", "+a"]) + "\n"

    assert plan_chunks(diff, 100)[0].files == (expected,)


def test_a_multi_file_diff_without_git_headers_is_split_per_file():
    diff = "\n".join(
        [
            "--- a/one.txt\t2026-01-01",
            "+++ b/one.txt\t2026-01-02",
            "@@ -1 +1 @@",
            "-old one",
            "+new one",
            "--- a/two.txt",
            "+++ b/two.txt",
            "@@ -1 +1 @@",
            "-old two",
            "+new two",
            "--- a/gone.txt",
            "+++ /dev/null",
            "@@ -1 +0,0 @@",
            "-removed",
        ]
    ) + "\n"

    chunks = plan_chunks(diff, 5)

    assert [c.files for c in chunks] == [("one.txt",), ("two.txt",), ("gone.txt",)]
    # Each file's headers stay with its own hunk, never inside another's body.
    for chunk in chunks:
        lines = chunk.text.splitlines()
        for position, line in enumerate(lines):
            if line.startswith("+++ "):
                assert lines[position - 1].startswith("--- ")


def test_a_removed_line_that_looks_like_a_header_does_not_split_a_plain_diff():
    diff = "\n".join(
        ["--- a/p.txt", "+++ b/p.txt", "@@ -1,2 +1,2 @@", "--- not a header", "+++ nor this", " keep"]
    ) + "\n"

    chunks = plan_chunks(diff, 100)

    assert len(chunks) == 1
    assert chunks[0].text == diff
