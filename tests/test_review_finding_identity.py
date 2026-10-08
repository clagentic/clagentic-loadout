"""A finding identity that survives line shifts, and a chunk working directory
that holds no sibling results. Everything here is additive: a findings file
without a fingerprint loads and matches exactly as before."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from clagentic_loadout.acquire.contract import AcquiredPr
from clagentic_loadout.review.chunking import plan_chunks
from clagentic_loadout.review.finding_identity import (
    KEY_FINGERPRINT,
    compute_fingerprint,
    drop_rereported,
    match_prior,
    same_finding,
    with_fingerprints,
)
from clagentic_loadout.review.findings_contract import validate_finding
from clagentic_loadout.review.run_pipeline import (
    CHUNK_WORKDIR_DIRNAME,
    FINDINGS_FILENAME,
    RESULT_COMPLETE,
    run_review,
)
from tests._review_cli_support import BASE_SHA, HEAD_SHA, make_diff
from tests._support.review_chunk import profile

_BODY = "def risky():\n    return eval(user_input)\n"


def _diff(path: str, body: str, start: int) -> str:
    lines = body.splitlines()
    return "\n".join(
        [
            f"diff --git a/{path} b/{path}",
            "index 1111111..2222222 100644",
            f"--- a/{path}",
            f"+++ b/{path}",
            f"@@ -0,0 +{start},{len(lines)} @@",
            *(f"+{line}" for line in lines),
        ]
    ) + "\n"


def _finding(file="m.py", line=2, rule="R1", severity="blocking", message="bad"):
    return {"file": file, "line": line, "rule_id": rule, "severity": severity, "message": message}


def _fingerprinted(diff: str, line: int) -> dict:
    chunk = plan_chunks(diff, 600)[0]
    return with_fingerprints([_finding(line=line)], chunk)[0]


def test_the_same_line_keeps_its_fingerprint_after_an_unrelated_insertion_above_it():
    before = _fingerprinted(_diff("m.py", _BODY, 1), 2)
    after = _fingerprinted(_diff("m.py", "import os\nimport sys\n" + _BODY, 1), 4)

    assert before["line"] == 2 and after["line"] == 4
    assert before[KEY_FINGERPRINT] == after[KEY_FINGERPRINT]
    assert same_finding(before, after)
    assert match_prior(after, [before]) is before


def test_re_indented_code_is_the_same_finding():
    flat = _fingerprinted(_diff("m.py", "x = 1\nreturn  eval(a)\n", 1), 2)
    indented = _fingerprinted(_diff("m.py", "x = 1\n        return eval(a)\n", 1), 2)

    assert flat[KEY_FINGERPRINT] == indented[KEY_FINGERPRINT]


def test_a_different_rule_file_or_line_text_is_a_different_finding():
    diff = _diff("m.py", _BODY, 1)
    base = _fingerprinted(diff, 2)
    chunk = plan_chunks(diff, 600)[0]
    other_rule = with_fingerprints([_finding(rule="R2")], chunk)[0]
    other_line = with_fingerprints([_finding(line=1)], chunk)[0]

    assert base[KEY_FINGERPRINT] != other_rule[KEY_FINGERPRINT]
    assert base[KEY_FINGERPRINT] != other_line[KEY_FINGERPRINT]
    assert compute_fingerprint("a.py", "R1", "x") != compute_fingerprint("b.py", "R1", "x")


def test_a_line_the_chunk_does_not_show_gets_no_fingerprint_and_keeps_its_position_identity():
    chunk = plan_chunks(_diff("m.py", _BODY, 1), 600)[0]

    out = with_fingerprints([_finding(line=99), _finding(file="other.py")], chunk)

    assert all(KEY_FINGERPRINT not in f for f in out)


def test_a_fingerprint_a_model_put_in_its_reply_is_discarded():
    chunk = plan_chunks(_diff("m.py", _BODY, 1), 600)[0]
    forged = {**_finding(), KEY_FINGERPRINT: "fp1:" + "0" * 16}

    out = with_fingerprints([forged], chunk)[0]

    assert out[KEY_FINGERPRINT] != forged[KEY_FINGERPRINT]


def test_a_findings_file_without_the_field_loads_and_matches_as_today():
    old = validate_finding(_finding(), 1, lenient_severity=True, truncate_message=False,
                           carry_fingerprint=True)
    assert old == _finding()
    assert KEY_FINGERPRINT not in old

    assert same_finding(old, _finding())
    assert not same_finding(old, _finding(line=4)), "no fingerprint: a moved line is a new finding"
    assert not same_finding(old, _finding(rule="R2"))
    assert match_prior(_finding(line=4), [old]) is None


def test_a_prior_without_a_fingerprint_still_matches_a_fingerprinted_rereport_by_position():
    chunk = plan_chunks(_diff("m.py", _BODY, 1), 600)[0]
    fresh = with_fingerprints([_finding(line=2)], chunk)[0]

    assert same_finding(_finding(line=2), fresh)
    assert not same_finding(_finding(line=5), fresh)


def test_a_fingerprint_is_carried_when_a_findings_file_is_loaded_and_ignored_in_a_reply():
    fingerprint = "fp1:" + "a" * 16
    item = {**_finding(), KEY_FINGERPRINT: fingerprint}

    assert validate_finding(item, 1, carry_fingerprint=True)[KEY_FINGERPRINT] == fingerprint
    assert KEY_FINGERPRINT not in validate_finding(item, 1)
    junk = {**_finding(), KEY_FINGERPRINT: "not-a-fingerprint"}
    assert KEY_FINGERPRINT not in validate_finding(junk, 1, carry_fingerprint=True)


def test_a_carried_finding_the_delta_re_reports_at_a_shifted_line_is_listed_once():
    prior = {**_finding(line=2), KEY_FINGERPRINT: "fp1:" + "a" * 16}
    unprinted = _finding(file="z.py", line=7)
    fresh = [{**_finding(line=4), KEY_FINGERPRINT: "fp1:" + "a" * 16}]

    assert drop_rereported([prior, unprinted], fresh) == [unprinted]
    assert drop_rereported([prior, unprinted], []) == [prior, unprinted]


def _scripted_runner(array: str, cwds: list[Path], listings: list[list[str]]):
    def run(argv, *, input, capture_output, timeout, cwd):
        cwds.append(Path(cwd))
        listings.append(sorted(p.name for p in Path(cwd).rglob("*")))
        return subprocess.CompletedProcess(argv, 0, array.encode(), b"")

    return run


def _acquired(diff: str) -> AcquiredPr:
    return AcquiredPr(
        owner="some-owner", repo="some-repo", pr_number=7, base_sha=BASE_SHA,
        head_sha=HEAD_SHA, diff_text=diff,
    )


def test_a_chunk_cannot_read_sibling_results(tmp_path):
    diff = make_diff({"a.py": 4, "b.py": 4, "c.py": 4})
    array = json.dumps(
        [{"file": "a.py", "line": 1, "rule_id": "R1", "severity": "nit", "message": "m"}]
    )
    cwds: list[Path] = []
    listings: list[list[str]] = []
    # Each file is nine diff lines, so a bound of nine puts one file in each of
    # three chunks and later chunks run beside the persisted results of earlier ones.
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    outcome = run_review(
        _acquired(diff), profile(fallback=False, chunk_lines=9, parallel=1), run_dir,
        emit=lambda *a, **k: None, runner=_scripted_runner(array, cwds, listings),
    )

    assert outcome.result == RESULT_COMPLETE
    assert len(cwds) == 3 and len(set(cwds)) == 3
    assert all(listing == [] for listing in listings), "a chunk saw files in its working directory"
    for cwd in cwds:
        assert cwd.parent == run_dir / CHUNK_WORKDIR_DIRNAME
        assert not list(cwd.glob("result-*.json"))
        assert not (cwd / FINDINGS_FILENAME).exists()
    assert list(run_dir.glob("state-*/result-*.json")), "results still persist in the state dir"


def test_the_merged_findings_file_carries_a_fingerprint_beside_the_unchanged_fields(tmp_path):
    diff = _diff("m.py", _BODY, 1)
    array = json.dumps([_finding()])
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    run_review(
        _acquired(diff), profile(fallback=False), run_dir, emit=lambda *a, **k: None,
        runner=_scripted_runner(array, [], []),
    )

    document = json.loads((run_dir / FINDINGS_FILENAME).read_text(encoding="utf-8"))
    (found,) = document["findings"]
    assert document["schema"] == "loadout.review-findings/1"
    assert {k: found[k] for k in ("file", "line", "rule_id", "severity", "message")} == _finding()
    assert found["chunk"] == 1
    assert found[KEY_FINGERPRINT].startswith("fp1:")
