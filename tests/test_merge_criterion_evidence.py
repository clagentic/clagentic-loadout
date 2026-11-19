"""Whether something that ran shows each acceptance criterion of a pull request's
sealed spec is read from the test run recorded at the exact head and reported at
merge: shown, not shown (each id absent, failed or skipped), or no evidence named.
The merge prints the rows and carries them in its completion attestation, and
proceeds: nothing here changes an outcome or an exit code."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.criterion_evidence import collect_criterion_evidence
from clagentic_loadout.merge.verdict import build_verdict_block
from clagentic_loadout.spec import model
from clagentic_loadout.spec import store as spec_store
from tests._repo_test_support import write_run
from tests._support.merge_verb import (
    FULL_SHA,
    OTHER_FULL_SHA,
    AllowingAuthorityProvider,
    RecordingTokenProvider,
    base_args,
    make_opener,
)
from tests._support.tree_host import tree_host_opener

HEAD = FULL_SHA
OTHER_HEAD = OTHER_FULL_SHA
SPEC_HEAD = "c" * 40
REPO = "some-owner/some-repo"
REMOTE = {"host": "git.example", "owner": "some-owner", "name": "some-repo"}
OLDER, NEWER = "20260101T000000Z-1", "20260102T000000Z-2"
JUNIT = (
    '<testcase classname="t.a" name="passes"/>'
    '<testcase classname="t.a" name="fails"><failure message="x"/></testcase>'
    '<testcase classname="t.a" name="skips"><skipped/></testcase>'
)
GOOD = {"junit_body": JUNIT, "head_sha": HEAD, "remote": REMOTE, "repo_path": "/repos/some-repo"}
NO_HEAD_RUN = f"no complete clean-tree run at head {HEAD} was found in the test-report store"


def _ac(cid, **evidence):
    criterion = {"id": cid, "given": "g", "when": "w", "then": "t"}
    return dict(criterion, evidence=evidence) if evidence else criterion


PASSES = _ac("ac-1", tests=["t.a::passes"])

# (criteria, runs to record as (run id, write_run overrides), spec hashes, expected line prefixes,
#  substrings the notes must hold)
CASES = {
    "shown": (
        [PASSES], [(OLDER, GOOD)], "sealed",
        ["ac-1: shown (t.a::passes passed)"], [],
    ),
    "absent, failed and skipped are each named": (
        [_ac("ac-1", tests=["t.a::passes", "t.a::gone", "t.a::fails", "t.a::skips"])],
        [(OLDER, GOOD)], "sealed",
        ["ac-1: not shown (t.a::gone absent; t.a::fails failed; t.a::skips skipped)"], [],
    ),
    "an id with a passed and a skipped case is not shown, skipped": (
        [_ac("ac-1", tests=["t.a::dup"])],
        [(OLDER, dict(GOOD, junit_body=(
            '<testcase classname="t.a" name="dup"/>'
            '<testcase classname="t.a" name="dup"><skipped/></testcase>'
        )))],
        "sealed", ["ac-1: not shown (t.a::dup skipped)"], [],
    ),
    "a pull request naming no head is a note and a line": (
        [PASSES], [(OLDER, GOOD)], "sealed",
        ["not computed: the pull request names no head commit"], ["criterion evidence not computed"],
    ),
    "a criterion with no evidence line reads no evidence named": (
        [PASSES, _ac("ac-2")], [(OLDER, GOOD)], "sealed",
        ["ac-1: shown", "ac-2: no evidence named"], [],
    ),
    "a spec with no evidence line says so": (
        [_ac("ac-1"), _ac("ac-2")], [], "sealed",
        ["the spec carries no evidence lines"], [],
    ),
    "the newest complete run at the head decides": (
        [PASSES], [(OLDER, dict(GOOD, junit_body='<testcase classname="t.a" name="passes"><failure/></testcase>')),
                   (NEWER, GOOD)], "sealed",
        ["ac-1: shown"], [],
    ),
    "a run at another head is no evidence": (
        [PASSES], [(OLDER, dict(GOOD, head_sha=OTHER_HEAD))], "sealed",
        [f"ac-1: not shown ({NO_HEAD_RUN})"], [HEAD],
    ),
    "a run on a dirty tree is no evidence": (
        [PASSES], [(OLDER, dict(GOOD, dirty=True))], "sealed",
        [f"ac-1: not shown ({NO_HEAD_RUN})"], [HEAD],
    ),
    "a run that did not complete is no evidence": (
        [PASSES], [(OLDER, dict(GOOD, state="unrunnable"))], "sealed",
        [f"ac-1: not shown ({NO_HEAD_RUN})"], [HEAD],
    ),
    "a run of another repository is no evidence": (
        [PASSES], [(OLDER, dict(GOOD, remote={**REMOTE, "name": "other-repo"}, repo_path="/repos/other"))],
        "sealed", [f"ac-1: not shown ({NO_HEAD_RUN})"], [HEAD],
    ),
    "no run at all": (
        [PASSES], [], "sealed",
        ["ac-1: not shown (reports root"], ["reports root"],
    ),
    "a run whose junit report is missing": (
        [PASSES], [(OLDER, {k: v for k, v in GOOD.items() if k != "junit_body"})], "sealed",
        [f"ac-1: not shown (run {OLDER}: no JUnit report at the path"], [f"run {OLDER}"],
    ),
    "a named run that is complete, clean, at the head and failure-free shows the criterion": (
        [_ac("ac-1", run=OLDER)], [(OLDER, dict(GOOD, junit_body=None))], "sealed",
        [f"ac-1: shown (run {OLDER} complete, no failures)"], [],
    ),
    "a named run at another head does not": (
        [_ac("ac-1", run=OLDER)], [(OLDER, dict(GOOD, head_sha=OTHER_HEAD))], "sealed",
        [f"ac-1: not shown (run {OLDER} was not taken at head {HEAD})"], [OLDER],
    ),
    "a named run with failures does not": (
        [_ac("ac-1", run=OLDER)], [(OLDER, dict(GOOD, failing=["t.a::fails"]))], "sealed",
        [f"ac-1: not shown (run {OLDER} did not record zero failures)"], [OLDER],
    ),
    "a named run that does not exist does not": (
        [_ac("ac-1", run=OLDER)], [(NEWER, GOOD)], "sealed",
        [f"ac-1: not shown (run {OLDER} could not be read"], [OLDER],
    ),
    "tests and a named run must both show": (
        [_ac("ac-1", tests=["t.a::passes"], run=NEWER)], [(OLDER, GOOD), (NEWER, dict(GOOD, dirty=True))],
        "sealed", [f"ac-1: not shown (run {NEWER} was not taken on a clean tree)"], [],
    ),
    "a spec that is not in the store is a note and a line": (
        [PASSES], [(OLDER, GOOD)], "missing",
        ["not computed: no sealed spec could be read"], ["was not read"],
    ),
    "no spec hash in the fences is a note and a line": (
        [PASSES], [(OLDER, GOOD)], "none",
        ["not computed: no sealed spec could be read"], ["no sealed spec hash"],
    ),
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAGENTIC_LOADOUT_SPECS_DIR", str(tmp_path / "specs"))
    monkeypatch.setenv("CLAGENTIC_LOADOUT_TEST_REPORTS_DIR", str(tmp_path / "reports"))


def _seal(criteria) -> str:
    draft = model.new_draft("planner-role")
    draft["goal"] = "Show each criterion."
    draft["acceptance_criteria"] = criteria
    sealed = model.seal(draft, head_sha=SPEC_HEAD, sealed_at="2026-01-01T00:00:00Z")
    spec_store.write_sealed(spec_store.specs_root(), sealed)
    return sealed.spec_hash


def _record(tmp_path, runs) -> None:
    if not runs:
        return
    (tmp_path / "reports").mkdir()
    for run_id, overrides in runs:
        write_run(tmp_path / "reports", run_id, **overrides)


#: Cases whose pull request head differs from HEAD (None: the pull request names none).
CASE_HEADS = {"a pull request naming no head is a note and a line": None}


def _collect(hashes, head=HEAD):
    return collect_criterion_evidence(head_sha=head, repo_names=[REPO], fence_spec_hashes=hashes)


@pytest.mark.parametrize("name", list(CASES))
def test_each_criterion_reads_shown_not_shown_or_no_evidence_named(tmp_path, name):
    criteria, runs, hashes, expected, note_parts = CASES[name]
    _record(tmp_path, runs)
    fence_hashes = {"sealed": [_seal(criteria)], "missing": ["e" * 64], "none": []}[hashes]

    result = _collect(fence_hashes, CASE_HEADS.get(name, HEAD))

    assert len(result.lines) == len(expected), result.lines
    for line, prefix in zip(result.lines, expected):
        assert line.startswith(prefix), line
    for part in note_parts:
        assert any(part in note for note in result.notes), (part, result.notes)


def _merge(capsys, spec: str):
    fences = [
        build_verdict_block(
            "some-reviewer", "clean", HEAD, 1, None,
            {"spec_hash": spec, "spec_tags": [],
             "spec_coverage": [{"criterion": c, "status": "met"} for c in ("ac-1", "ac-2", "ac-3")]},
        )
    ]
    inner = make_opener(
        pr_info={"head": {"sha": HEAD}, "base": {"ref": "main", "sha": OTHER_HEAD}, "title": "feat: a change"},
        files=["a.py"],
        comments=[{"id": 1, "user": {"login": "reviewer-login"}, "body": body} for body in fences],
    )
    posted: list[str] = []
    opener = tree_host_opener(
        inner, {OTHER_HEAD: {"a.py": "x = 1\n"}, HEAD: {"a.py": "x = 1\n"}}, posted
    )
    capsys.readouterr()
    code = verb.main(
        base_args(**{"--expected-head-sha": HEAD, "--required-reviewer": "some-reviewer:reviewer-login"}),
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=opener,
    )
    return code, capsys.readouterr().err, posted


def _row(body: str) -> str:
    (line,) = [line for line in body.splitlines() if line.startswith("| Criterion evidence |")]
    return line


def test_the_merge_prints_and_attests_each_criterion_and_proceeds(tmp_path, capsys):
    _record(tmp_path, [(OLDER, GOOD)])
    spec = _seal([PASSES, _ac("ac-2", tests=["t.a::fails"]), _ac("ac-3")])

    code, err, posted = _merge(capsys, spec)

    assert code == verb.EXIT_OK
    assert "merge: CRITERION EVIDENCE -- ac-1: shown (t.a::passes passed)" in err
    assert "merge: CRITERION EVIDENCE -- ac-2: not shown (t.a::fails failed)" in err
    assert "merge: CRITERION EVIDENCE -- ac-3: no evidence named" in err
    assert _row(posted[-1]) == (
        "| Criterion evidence | ac-1: shown (t.a::passes passed)<br>"
        "ac-2: not shown (t.a::fails failed)<br>ac-3: no evidence named |"
    )


def test_the_outcome_is_the_same_whatever_the_evidence_reads(tmp_path, capsys):
    no_run = _merge(capsys, _seal([PASSES]))
    _record(tmp_path, [(OLDER, GOOD)])
    with_run = _merge(capsys, _seal([PASSES, _ac("ac-2")]))

    assert no_run[0] == with_run[0] == verb.EXIT_OK
    assert len(no_run[2]) == len(with_run[2]) == 1


def test_a_computation_that_raises_still_merges_with_a_note_and_a_row_saying_so(
    tmp_path, capsys, monkeypatch
):
    def explode(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(verb, "collect_criterion_evidence", explode)

    code, err, posted = _merge(capsys, _seal([PASSES]))

    assert code == verb.EXIT_OK
    assert "merge: NOTE -- criterion evidence not computed: RuntimeError: boom" in err
    assert _row(posted[-1]) == "| Criterion evidence | not computed: RuntimeError: boom |"
