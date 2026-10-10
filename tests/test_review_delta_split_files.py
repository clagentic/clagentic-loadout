"""Delta review of a file whose diff is split across chunks: each prior
finding is listed in exactly one chunk (the one whose hunks cover its line,
else the lowest chunk holding the file), and one on an untouched file is
carried forward as still open."""

from __future__ import annotations

import json
import subprocess

from clagentic_loadout.acquire.contract import AcquiredPr
from clagentic_loadout.review.chunking import plan_chunks
from clagentic_loadout.review.delta import (
    DeltaContext,
    account_for_prior,
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


def test_a_finding_on_a_split_file_is_listed_in_exactly_one_chunk_the_one_covering_its_line():
    chunks = _split_chunks()
    context = _context(_finding("big.py", 1), _finding("big.py", 50), _finding("big.py", 101))

    listed = [[f["line"] for f in findings_for_chunk(context, c, chunks)] for c in chunks]

    assert listed == [[1], [50], [101]]


def test_an_uncovered_finding_on_a_split_file_is_listed_in_the_lowest_chunk_holding_the_file():
    chunks = plan_chunks(_diff("other.py", [1]) + _diff("big.py", [1, 50, 100]), 8)
    holders = [c.index for c in chunks if "big.py" in c.files]
    assert len(holders) > 1
    context = _context(_finding("big.py", 75))

    listed = [c.index for c in chunks if findings_for_chunk(context, c, chunks)]

    assert listed == [holders[0]]
    assert carried_findings(context, {"big.py", "other.py"}) == []


def test_a_second_report_of_a_prior_finding_from_another_chunk_is_not_absorbed():
    chunks = _split_chunks()
    prior = _finding("big.py", 50)
    context = _context(prior)
    owner_report = {**prior, "line": 51}
    other_report = {**prior, "line": 101}

    accounting = account_for_prior(context, {"big.py"}, [owner_report, other_report], chunks)

    # Only the owner's report is paired; the other stays a reported finding of
    # its own (a visible duplicate at worst) rather than being dropped.
    assert [(p["line"], r["line"]) for p, r in accounting.kept] == [(50, 51)]
    assert not hasattr(accounting, "restatements")
    assert accounting.resolved == [] and accounting.carried == [] and accounting.left_open == []
    assert [c.index for c in chunks if findings_for_chunk(context, c, chunks)] == [2]


def test_every_prior_finding_on_a_split_file_is_accounted_for_exactly_once():
    context = _context(_finding("big.py", 1), _finding("big.py", 75), _finding("big.py", 101, "R2"))

    accounting = account_for_prior(context, {"big.py"}, [_finding("big.py", 3), _finding("big.py", 60)])

    assert accounting.kept_count + len(accounting.carried) + len(accounting.resolved) == 3
    assert len(accounting.kept) == 2 and [f["line"] for f in accounting.left_open] == [101]


def test_a_finding_on_a_file_held_whole_is_listed_only_in_the_chunk_holding_it():
    chunks = plan_chunks(_diff("big.py", [1, 50, 100]) + _diff("small.py", [1]), 8)
    context = _context(_finding("small.py", 1), _finding("untouched.py", 3))

    listed = [[f["file"] for f in findings_for_chunk(context, c, chunks)] for c in chunks]

    holders = [i for i, c in enumerate(chunks) if "small.py" in c.files]
    assert len(holders) == 1
    for index, files in enumerate(listed):
        assert files == (["small.py"] if index in holders else [])
    # The finding on a file the diff does not hold is carried, never dropped.
    assert [f["file"] for f in carried_findings(context, {"big.py", "small.py"})] == [
        "untouched.py"
    ]


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

    assert findings_for_chunk(context, chunk, [chunk]) == (finding,)
    assert carried_findings(context, {"s.py"}) == []


def test_a_finding_the_old_side_does_not_cover_is_rejudged_not_carried():
    diff = _diff_with_hunks("s.py", ["@@ -10,2 +15,2 @@", " context", "-old line", "+new line"])
    chunk = plan_chunks(diff, 600)[0]
    elsewhere = _finding("s.py", 15)
    context = _context(elsewhere)

    # The file is in the delta, so the reviewer judges it wherever the fix is.
    assert findings_for_chunk(context, chunk, [chunk]) == (elsewhere,)
    assert carried_findings(context, {"s.py"}) == []


def test_a_pure_addition_is_anchored_at_the_old_line_it_follows():
    diff = _diff_with_hunks("s.py", ["@@ -20,0 +21,2 @@", "+added one", "+added two"])
    chunk = plan_chunks(diff, 600)[0]

    assert [(s.old_first, s.old_last) for s in chunk.hunks] == [(20, 20)]
    assert chunk.covers_prior_line("s.py", 20) and not chunk.covers_prior_line("s.py", 21)


def _diff_with_hunks(name: str, hunk_lines: list[str]) -> str:
    header = [f"diff --git a/{name} b/{name}", "index 111..222 100644", f"--- a/{name}", f"+++ b/{name}"]
    return "\n".join(header + hunk_lines) + "\n"


def test_a_split_file_finding_in_the_gap_between_old_and_new_starts_is_rejudged_once():
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

    assert [findings_for_chunk(context, c, chunks) for c in chunks] == [(gap,), ()]
    assert carried_findings(context, {"s.py"}) == []


def test_a_finding_outside_the_hunks_of_an_unsplit_touched_file_is_listed_not_carried():
    chunks = plan_chunks(_diff("small.py", [1]), 600)
    assert len(chunks) == 1
    outside = _finding("small.py", 99)
    inside = _finding("small.py", 1, rule="R2")
    context = _context(outside, inside)

    assert findings_for_chunk(context, chunks[0], chunks) == (outside, inside)
    assert carried_findings(context, {"small.py"}) == []


def test_the_listing_cap_still_counts_by_position_in_the_whole_list():
    chunks = plan_chunks(_diff("other.py", [1]) + _diff("big.py", [1, 50, 100]), 8)
    many = [_finding("other.py", 1, rule=f"R{i}") for i in range(1, 60)]
    context = _context(*many, _finding("big.py", 1))
    first = next(c for c in chunks if "other.py" in c.files)

    listed = findings_for_chunk(context, first, chunks)

    # big.py:1 sits at position 59, past the cap of 50: never shown, so carried.
    assert all(f["file"] == "other.py" for f in listed) and len(listed) == 50
    assert [f["line"] for f in carried_findings(context, {"big.py"}) if f["file"] == "big.py"] == [1]


def test_carrying_is_decided_by_touched_files_alone():
    context = _context(_finding("big.py", 70), _finding("big.py", 50), _finding("other.py", 5))

    carried = carried_findings(context, {"big.py"})

    assert [f["file"] for f in carried] == ["other.py"]


def test_the_note_lists_exactly_the_findings_it_is_given():
    context = _context(_finding("big.py", 1), _finding("big.py", 50))

    note = render_delta_note(context, [_finding("big.py", 50)])

    assert "big.py:50" in note and "big.py:1 " not in note
    assert "not listed" not in note
    assert render_delta_note(context).count("\n- ") == 2


def test_each_chunk_prompt_of_a_delta_run_lists_the_findings_on_its_file(tmp_path):
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
    tokens = ("big.py:1 ", "big.py:50 ", "big.py:70 ")
    judged = [[token for token in tokens if token in p] for p in prompts]
    # Line 70 is covered by no hunk, so the lowest chunk holding the file owns it.
    assert judged == [[tokens[0], tokens[2]], [tokens[1]], []]
    document = json.loads((run_dir / "findings.json").read_text(encoding="utf-8"))
    # The reviewer answered [] for every chunk, which resolves nothing: all
    # three stay open, none carried, and each is accounted for exactly once.
    assert document["resolved"] == [] and document["carried_count"] == 0
    assert sorted(f["prior_line"] for f in document["findings"]) == [1, 50, 70]
    assert document["prior_open_count"] == 3 and document["kept_count"] == 3
