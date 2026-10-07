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
    split_files,
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
    assert chunks[1].covers("big.py", 51) and not chunks[1].covers("big.py", 52)
    assert not chunks[1].covers("other.py", 51)


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
    covered = {line for line in range(1, 21) if any(c.covers("x.py", line) for c in chunks)}
    assert covered == set(range(1, 21))
    # Each added line is covered by the chunk that actually holds it.
    assert chunks[0].covers("x.py", 1) and not chunks[-1].covers("x.py", 1)


def test_only_files_spread_over_several_chunks_are_split_files():
    chunks = plan_chunks(_diff("big.py", [1, 50, 100]) + _diff("small.py", [1]), 8)

    assert split_files(chunks) == frozenset({"big.py"})


def test_a_finding_on_a_split_file_is_listed_only_in_the_chunk_whose_hunks_cover_it():
    chunks = _split_chunks()
    context = _context(_finding("big.py", 1), _finding("big.py", 50), _finding("big.py", 101))
    split = split_files(chunks)

    listed = [[f["line"] for f in findings_for_chunk(context, c, split)] for c in chunks]

    assert listed == [[1], [50], [101]]


def test_a_finding_on_a_file_held_whole_is_listed_in_every_chunk_as_before():
    chunks = plan_chunks(_diff("big.py", [1, 50, 100]) + _diff("small.py", [1]), 8)
    context = _context(_finding("small.py", 99), _finding("untouched.py", 3))
    split = split_files(chunks)

    for chunk in chunks:
        assert len(findings_for_chunk(context, chunk, split)) == 2


def test_the_listing_cap_still_counts_by_position_in_the_whole_list():
    chunks = _split_chunks()
    many = [_finding("other.py", i) for i in range(1, 60)]
    context = _context(*many, _finding("big.py", 1))
    split = split_files(chunks)

    listed = findings_for_chunk(context, chunks[0], split)

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
