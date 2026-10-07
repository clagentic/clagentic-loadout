"""Delta review of a file whose diff is split across chunks: each chunk is
asked only about the open findings its own hunks cover, and a finding no
chunk covers is carried forward as still open."""

from __future__ import annotations

import json
import subprocess

from clagentic_loadout.acquire.contract import AcquiredPr
from clagentic_loadout.review.chunking import plan_chunks
from clagentic_loadout.review.delta import (
    DeltaContext,
    carried_findings,
    findings_for_chunk,
    render_delta_note,
)
from clagentic_loadout.review.run_pipeline import RESULT_COMPLETE, run_review
from tests._support.review_chunk import profile

SINCE = "1" * 40
HEAD = "2" * 40


def _hunk(start: int) -> list[str]:
    return [f"@@ -{start},2 +{start},2 @@", " context", "-old line", "+new line"]


def _diff(name: str, starts: list[int]) -> str:
    lines = [f"diff --git a/{name} b/{name}", "index 111..222 100644", f"--- a/{name}", f"+++ b/{name}"]
    for start in starts:
        lines.extend(_hunk(start))
    return "\n".join(lines) + "\n"


def _finding(file: str, line: int, rule: str = "R1") -> dict:
    return {"file": file, "line": line, "rule_id": rule, "severity": "blocking", "message": f"at {line}"}


def _context(*findings: dict) -> DeltaContext:
    return DeltaContext(since_head=SINCE, open_findings=tuple(findings))


def _split_chunks():
    chunks = plan_chunks(_diff("big.py", [1, 50, 100]), 8)
    assert [c.files for c in chunks] == [("big.py",)] * 3
    return chunks


def test_a_chunk_knows_the_hunks_it_holds():
    chunks = _split_chunks()

    assert [[(s.file, s.first, s.last) for s in c.hunks] for c in chunks] == [
        [("big.py", 1, 2)],
        [("big.py", 50, 51)],
        [("big.py", 100, 101)],
    ]
    assert chunks[1].covers_prior_line("big.py", 51)
    assert not chunks[1].covers_prior_line("big.py", 52)
    assert not chunks[1].covers_prior_line("other.py", 51)


def test_a_chunk_holding_several_files_attributes_each_hunk_to_its_own_file():
    chunk = plan_chunks(_diff("a.py", [3]) + _diff("b.py", [40]), 600)[0]

    assert [(s.file, s.first) for s in chunk.hunks] == [("a.py", 3), ("b.py", 40)]


def test_a_split_hunk_gets_a_span_per_piece():
    long_hunk = ["@@ -0,0 +1,20 @@"] + [f"+added {i}" for i in range(20)]
    diff = "\n".join(
        ["diff --git a/x.py b/x.py", "index 1..2 100644", "--- a/x.py", "+++ b/x.py", *long_hunk]
    ) + "\n"

    chunks = plan_chunks(diff, 12)

    assert len(chunks) > 1
    assert all(chunk.hunks for chunk in chunks)
    spans = [s for chunk in chunks for s in chunk.hunks]
    covered = {line for s in spans for line in range(s.first, s.last + 1)}
    assert covered == set(range(1, 21))
    # Each added line is held by exactly the chunk that carries it.
    assert chunks[0].hunks[0].first == 1 and chunks[-1].hunks[0].first > 1
    # A pure addition has no old lines; every piece anchors at the old line it follows.
    assert {(s.old_first, s.old_last) for s in spans} == {(1, 1)}


def test_a_finding_on_a_split_file_is_listed_only_in_the_chunk_whose_hunks_cover_it():
    chunks = _split_chunks()
    context = _context(_finding("big.py", 1), _finding("big.py", 50), _finding("big.py", 101))

    listed = [[f["line"] for f in findings_for_chunk(context, c)] for c in chunks]

    assert listed == [[1], [50], [101]]


def test_a_finding_on_a_file_held_whole_is_listed_only_in_the_chunk_holding_it():
    chunks = plan_chunks(_diff("big.py", [1, 50, 100]) + _diff("small.py", [1]), 8)
    context = _context(_finding("small.py", 1), _finding("untouched.py", 3))

    listed = [[f["file"] for f in findings_for_chunk(context, c)] for c in chunks]

    holders = [i for i, c in enumerate(chunks) if "small.py" in c.files]
    assert len(holders) == 1
    for index, files in enumerate(listed):
        assert files == (["small.py"] if index in holders else [])
    # The finding on a file the diff does not hold is carried, never dropped.
    assert [f["file"] for f in carried_findings(context, {"big.py", "small.py"}, chunks)] == [
        "untouched.py"
    ]


def test_a_finding_on_a_file_no_chunk_holds_is_carried_even_when_reported_touched():
    chunks = plan_chunks(_diff("a.py", [1]), 600)
    context = _context(_finding("ghost.py", 4))

    assert [f["file"] for f in carried_findings(context, {"ghost.py"}, chunks)] == ["ghost.py"]


def test_a_span_keeps_each_side_of_its_hunk_apart_never_an_old_new_union():
    diff = "\n".join(
        [
            "diff --git a/m.py b/m.py", "index 1..2 100644", "--- a/m.py", "+++ b/m.py",
            "@@ -300,7 +420,9 @@",
            *[" ctx"] * 7, "+add one", "+add two",
        ]
    ) + "\n"
    chunk = plan_chunks(diff, 600)[0]

    assert [(s.first, s.last) for s in chunk.hunks] == [(420, 428)]
    assert [(s.old_first, s.old_last) for s in chunk.hunks] == [(300, 306)]
    assert chunk.covers_prior_line("m.py", 300) and chunk.covers_prior_line("m.py", 306)
    assert not chunk.covers_prior_line("m.py", 299) and not chunk.covers_prior_line("m.py", 307)
    assert not chunk.covers_prior_line("m.py", 420)


def test_a_pure_deletion_is_anchored_at_the_line_it_sits_next_to():
    diff = "\n".join(
        [
            "diff --git a/d.py b/d.py", "index 1..2 100644", "--- a/d.py", "+++ b/d.py",
            "@@ -10,2 +9,0 @@", "-gone", "-gone too",
        ]
    ) + "\n"
    chunk = plan_chunks(diff, 600)[0]

    assert [(s.first, s.last) for s in chunk.hunks] == [(9, 9)]


def test_an_insertion_above_a_finding_shifts_head_numbering_but_the_finding_is_still_listed():
    # 5 lines were inserted above the prior-reviewed code: old lines 10-11 are
    # now head lines 15-16. The finding keeps the prior head's numbering (10).
    diff = _diff_with_hunks("s.py", ["@@ -10,2 +15,2 @@", " context", "-old line", "+new line"])
    chunk = plan_chunks(diff, 600)[0]
    finding = _finding("s.py", 10)
    context = _context(finding)

    assert findings_for_chunk(context, chunk) == (finding,)
    assert carried_findings(context, {"s.py"}, [chunk]) == []


def test_a_finding_the_old_side_does_not_cover_is_carried_even_if_its_number_is_a_new_side_line():
    diff = _diff_with_hunks("s.py", ["@@ -10,2 +15,2 @@", " context", "-old line", "+new line"])
    chunk = plan_chunks(diff, 600)[0]
    shifted_number = _finding("s.py", 15)
    context = _context(shifted_number)

    assert findings_for_chunk(context, chunk) == ()
    assert carried_findings(context, {"s.py"}, [chunk]) == [shifted_number]


def test_a_pure_addition_is_anchored_at_the_old_line_it_follows():
    diff = _diff_with_hunks("s.py", ["@@ -20,0 +21,2 @@", "+added one", "+added two"])
    chunk = plan_chunks(diff, 600)[0]

    assert [(s.old_first, s.old_last) for s in chunk.hunks] == [(20, 20)]
    assert chunk.covers_prior_line("s.py", 20) and not chunk.covers_prior_line("s.py", 21)


def _diff_with_hunks(name: str, hunk_lines: list[str]) -> str:
    header = [f"diff --git a/{name} b/{name}", "index 111..222 100644", f"--- a/{name}", f"+++ b/{name}"]
    return "\n".join(header + hunk_lines) + "\n"


def test_a_split_file_finding_in_the_gap_between_old_and_new_starts_is_carried():
    def hunk(old: int, new: int) -> list[str]:
        return [f"@@ -{old},2 +{new},2 @@", " context", "-old line", "+new line"]

    diff = "\n".join(
        [
            "diff --git a/s.py b/s.py", "index 1..2 100644", "--- a/s.py", "+++ b/s.py",
            *hunk(300, 420), *hunk(500, 620),
        ]
    ) + "\n"
    chunks = plan_chunks(diff, 8)
    assert [c.files for c in chunks] == [("s.py",)] * 2
    gap = _finding("s.py", 350)
    context = _context(gap)

    assert [findings_for_chunk(context, c) for c in chunks] == [(), ()]
    assert carried_findings(context, {"s.py"}, chunks) == [gap]


def test_a_finding_outside_the_hunks_of_an_unsplit_touched_file_is_not_listed_and_is_carried():
    chunks = plan_chunks(_diff("small.py", [1]), 600)
    assert len(chunks) == 1
    outside = _finding("small.py", 99)
    inside = _finding("small.py", 1, rule="R2")
    context = _context(outside, inside)

    assert findings_for_chunk(context, chunks[0]) == (inside,)
    assert carried_findings(context, {"small.py"}, chunks) == [outside]


def test_the_listing_cap_still_counts_by_position_in_the_whole_list():
    chunks = plan_chunks(_diff("other.py", [1]) + _diff("big.py", [1, 50, 100]), 8)
    many = [_finding("other.py", 1, rule=f"R{i}") for i in range(1, 60)]
    context = _context(*many, _finding("big.py", 1))
    first = next(c for c in chunks if "other.py" in c.files)

    listed = findings_for_chunk(context, first)

    # big.py:1 sits at position 59, past the cap of 50: never shown, so carried.
    assert all(f["file"] == "other.py" for f in listed) and len(listed) == 50
    assert [f["line"] for f in carried_findings(context, {"big.py"}, chunks) if f["file"] == "big.py"] == [1]


def test_a_finding_no_chunk_covers_is_carried_not_dropped():
    chunks = _split_chunks()
    covered = _finding("big.py", 50)
    uncovered = _finding("big.py", 70)
    untouched = _finding("other.py", 5)
    context = _context(covered, uncovered, untouched)

    carried = carried_findings(context, {"big.py"}, chunks)

    assert [(f["file"], f["line"]) for f in carried] == [("big.py", 70), ("other.py", 5)]


def test_without_chunks_carrying_is_decided_by_touched_files_alone():
    context = _context(_finding("big.py", 70), _finding("other.py", 5))

    carried = carried_findings(context, {"big.py"})

    assert [f["file"] for f in carried] == ["other.py"]


def test_the_note_lists_exactly_the_findings_it_is_given():
    context = _context(_finding("big.py", 1), _finding("big.py", 50))

    note = render_delta_note(context, [_finding("big.py", 50)])

    assert "big.py:50" in note and "big.py:1 " not in note
    assert "not listed" not in note
    assert render_delta_note(context).count("\n- ") == 2


def test_each_chunk_prompt_of_a_delta_run_lists_only_the_findings_it_can_judge(tmp_path):
    prompts: list[str] = []

    def runner(argv, *, input, capture_output, timeout, cwd):
        prompts.append(input.decode("utf-8"))
        return subprocess.CompletedProcess(argv, 0, b"[]", b"")

    diff = _diff("big.py", [1, 50, 100])
    acquired = AcquiredPr(
        owner="o", repo="r", pr_number=1, base_sha=SINCE, head_sha=HEAD, diff_text=diff
    )
    context = _context(_finding("big.py", 1), _finding("big.py", 50), _finding("big.py", 70))
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    outcome = run_review(
        acquired, profile(fallback=False, chunk_lines=8), run_dir,
        emit=lambda *a, **k: None, runner=runner, delta=context,
    )

    assert outcome.result == RESULT_COMPLETE
    assert len(prompts) == 3
    judged = [[line for line in ("big.py:1 ", "big.py:50 ", "big.py:70 ") if line in p] for p in prompts]
    assert judged == [["big.py:1 "], ["big.py:50 "], []]
    merged = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))["findings"]
    # The uncovered finding stays open in the merged result.
    assert ("big.py", 70) in {(f["file"], f["line"]) for f in merged}
