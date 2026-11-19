"""The behaviour a pull request removes is matched against the ledger of its sealed
spec at merge: each item with no ledger line is named in the merge output, and the
whole list is a row of the completion attestation. Nothing else changes: the merge
proceeds with the same outcome either way."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clagentic_loadout.interface.consumers import CONSUMERS_FILE_ENV_VAR
from clagentic_loadout.merge import verb
from clagentic_loadout.merge.removed_notice import collect_removed_notices
from clagentic_loadout.merge.verdict import build_verdict_block
from clagentic_loadout.spec import model
from clagentic_loadout.spec import store as spec_store
from tests._support.merge_verb import (
    FULL_SHA,
    OTHER_FULL_SHA,
    AllowingAuthorityProvider,
    RecordingTokenProvider,
    base_args,
    make_opener,
)
from tests._gate_repo import commit_files, git, init_gate_repo
from tests._support.tree_host import tree_host_opener
from tests.test_merge_interface_notice import FakeReader

BASE_SHA = OTHER_FULL_SHA
HEAD_SHA = FULL_SHA
SPEC_HEAD = "c" * 40
TESTS_PATH = "tests/test_a.py"
BASE_TESTS = "def test_kept():\n    pass\n\n\ndef test_dropped():\n    pass\n"
HEAD_TESTS = "def test_kept():\n    pass\n"
DROPPED = "tests/test_a.py::test_dropped"
DISPOSITIONS = {
    "replaced_by": ({"kind": "replaced_by", "replacement": "tests/test_b.py::test_new"},
                    "replaced by tests/test_b.py::test_new"),
    "moved_to": ({"kind": "moved_to", "repo": "some-owner/other-repo", "task": "task-7"},
                 "moved to some-owner/other-repo (task-7)"),
    "retired_by": ({"kind": "retired_by", "decision": "the case is obsolete"},
                   "retired by the case is obsolete"),
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.delenv(CONSUMERS_FILE_ENV_VAR, raising=False)
    monkeypatch.setenv("CLAGENTIC_LOADOUT_SPECS_DIR", str(tmp_path / "specs"))


def _seal_spec(disposition=None, *, ledger: bool = True) -> str:
    draft = model.new_draft("planner-role")
    draft["goal"] = "Remove a test."
    draft["acceptance_criteria"] = [{"id": "ac-1", "given": "g", "when": "w", "then": "t"}]
    if ledger:
        draft["removal_ledger"] = {
            "source": "test", "note": None, "base_sha": None, "head_sha": None,
            "items": [{"item": DROPPED, "kind": "test", "disposition": disposition}],
        }
    sealed = model.seal(draft, head_sha=SPEC_HEAD, sealed_at="2026-01-01T00:00:00Z")
    spec_store.write_sealed(spec_store.specs_root(), sealed)
    return sealed.spec_hash


def _removal_repo(tmp_path) -> tuple[Path, str, str]:
    """A repository whose main tip holds both tests and whose ``pr`` branch drops
    one: the repository, the base commit and the head commit."""
    repo = tmp_path / "repo"
    init_gate_repo(repo, tracked_gate={"required_reviewer_roles": []})
    git(repo, "checkout", "-q", "main")
    base = commit_files(repo, {TESTS_PATH: BASE_TESTS}, "test: add tests")
    git(repo, "checkout", "-q", "-b", "dropping")
    head = commit_files(repo, {TESTS_PATH: HEAD_TESTS}, "test: drop one")
    git(repo, "checkout", "-q", "main")
    return repo, base, head


def _merge(capsys, *, spec: str | None, head_tests: str = HEAD_TESTS, repo=None, host_requests=None):
    base_sha, head_sha = (BASE_SHA, HEAD_SHA) if repo is None else repo[1:]
    fences = []
    if spec is not None:
        fences = [
            build_verdict_block(
                "some-reviewer", "clean", head_sha, 1, None,
                {"spec_hash": spec, "spec_tags": [], "spec_coverage": [{"criterion": "ac-1", "status": "met"}]},
            )
        ]
    else:
        fences = [build_verdict_block("some-reviewer", "clean", head_sha, 1, None, {})]
    pr_info = {
        "head": {"sha": head_sha},
        "base": {"ref": "main", "sha": base_sha},
        "title": "feat: remove a test",
    }
    posted: list[str] = []
    inner = make_opener(
        pr_info=pr_info,
        files=[TESTS_PATH],
        comments=[{"id": 1, "user": {"login": "reviewer-login"}, "body": body} for body in fences],
    )
    opener = tree_host_opener(
        inner,
        {BASE_SHA: {TESTS_PATH: BASE_TESTS}, HEAD_SHA: {TESTS_PATH: head_tests}},
        posted,
        host_requests,
    )
    capsys.readouterr()
    local = {"--repo-path": str(repo[0])} if repo is not None else {}
    code = verb.main(
        base_args(
            **{"--expected-head-sha": head_sha, "--required-reviewer": "some-reviewer:reviewer-login", **local}
        ),
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=opener,
    )
    captured = capsys.readouterr()
    return code, json.loads(captured.out.strip().splitlines()[-1]), captured.err, posted


def _row(body: str) -> str:
    (line,) = [line for line in body.splitlines() if line.startswith("| Removed behaviour |")]
    return line


class TestMergeVerb:
    def test_an_item_with_no_ledger_line_is_named_and_the_merge_proceeds(self, capsys):
        code, result, err, posted = _merge(capsys, spec=_seal_spec(None))

        assert code == verb.EXIT_OK
        assert f"merge: NOTICE -- removed without a ledger line: {DROPPED} (test)" in err
        assert result["removed_undisposed"] == [DROPPED]
        assert _row(posted[0]) == f"| Removed behaviour | {DROPPED} (test): undisposed |"

    def test_a_spec_with_no_ledger_section_leaves_every_item_undisposed(self, capsys):
        code, result, err, posted = _merge(capsys, spec=_seal_spec(ledger=False))

        assert code == verb.EXIT_OK and result["removed_undisposed"] == [DROPPED]
        assert "merge: NOTICE -- removed without a ledger line" in err and "undisposed" in _row(posted[0])

    @pytest.mark.parametrize("kind", list(DISPOSITIONS))
    def test_an_item_with_a_ledger_line_is_listed_with_it_and_is_in_no_notice(self, capsys, kind):
        disposition, text = DISPOSITIONS[kind]

        code, result, err, posted = _merge(capsys, spec=_seal_spec(disposition))

        assert code == verb.EXIT_OK
        assert "removed without a ledger line" not in err and "removed_undisposed" not in result
        assert _row(posted[0]) == f"| Removed behaviour | {DROPPED} (test): {text} |"

    def test_a_change_that_removes_nothing_says_none(self, capsys):
        code, result, err, posted = _merge(capsys, spec=_seal_spec(None), head_tests=BASE_TESTS)

        assert code == verb.EXIT_OK
        assert _row(posted[0]) == "| Removed behaviour | none |"
        assert "removed_undisposed" not in result and "removed without" not in err

    def test_a_pull_request_with_no_spec_hash_in_its_fences_names_the_item_and_says_why(self, capsys):
        code, result, err, posted = _merge(capsys, spec=None)

        assert code == verb.EXIT_OK and result["removed_undisposed"] == [DROPPED]
        assert "merge: NOTE -- no sealed spec hash in the reviewer fences" in err

    def test_a_spec_that_is_not_in_the_store_is_a_note_and_the_item_is_undisposed(self, capsys):
        code, result, err, _ = _merge(capsys, spec="e" * 64)

        assert code == verb.EXIT_OK and result["removed_undisposed"] == [DROPPED]
        assert "merge: NOTE -- spec eeeeeeeeeeee was not read" in err

    def test_a_computation_that_raises_still_merges_with_a_note_and_a_row_saying_so(self, capsys, monkeypatch):
        def explode(**_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(verb, "collect_removed_notices", explode)

        code, result, err, posted = _merge(capsys, spec=_seal_spec(None))

        assert code == verb.EXIT_OK and "removed_undisposed" not in result
        assert "merge: NOTE -- removed behaviour not computed: RuntimeError: boom" in err
        assert _row(posted[0]) == "| Removed behaviour | not computed: RuntimeError: boom |"

    def test_with_a_repo_path_holding_both_commits_the_host_is_not_read(self, capsys, tmp_path):
        host_requests: list[str] = []

        code, result, _, _ = _merge(
            capsys, spec=_seal_spec(None), repo=_removal_repo(tmp_path), host_requests=host_requests
        )

        assert code == verb.EXIT_OK and result["removed_undisposed"] == [DROPPED]
        assert host_requests == []

    def test_the_outcome_is_the_same_with_and_without_a_removal(self, capsys):
        without = _merge(capsys, spec=_seal_spec(None), head_tests=BASE_TESTS)
        with_removal = _merge(capsys, spec=_seal_spec(None))

        assert without[0] == with_removal[0] == verb.EXIT_OK
        assert len(without[3]) == len(with_removal[3]) == 1


def _collect(readers, *, base=BASE_SHA, head=HEAD_SHA, hashes=()):
    return collect_removed_notices(
        changed_files=[TESTS_PATH],
        make_readers=lambda: readers,
        base_sha=base,
        head_sha=head,
        fence_spec_hashes=list(hashes),
    )


class TestCollect:
    def _reader(self, **revisions):
        return FakeReader({BASE_SHA: {TESTS_PATH: BASE_TESTS}, HEAD_SHA: {TESTS_PATH: HEAD_TESTS}, **revisions})

    @pytest.mark.parametrize("missing", ["base", "head"])
    def test_a_revision_that_cannot_be_read_is_a_note_and_a_row_not_a_failure(self, missing):
        revisions = {BASE_SHA: {TESTS_PATH: BASE_TESTS}, HEAD_SHA: {TESTS_PATH: HEAD_TESTS}}
        del revisions[BASE_SHA if missing == "base" else HEAD_SHA]

        result = _collect([FakeReader(revisions)])

        assert result.lines == () and result.undisposed == ()
        assert result.attestation[0].startswith("not computed: cannot read the file list")
        assert result.notes[0].startswith("removed behaviour not computed: cannot read the file list")

    @pytest.mark.parametrize("which", ["base", "head"])
    def test_a_pull_request_that_names_no_commit_is_not_computed(self, which):
        result = _collect([self._reader()], **{which: None})

        assert result.attestation == (f"not computed: the pull request names no {which} commit",)

    def test_the_next_reader_is_tried_when_the_first_cannot_read_the_revisions(self):
        result = _collect([FakeReader({}), self._reader()])

        assert result.undisposed == (DROPPED,)

    def test_a_file_that_cannot_be_parsed_is_a_row_and_a_note_and_adds_no_item(self):
        reader = FakeReader(
            {BASE_SHA: {TESTS_PATH: BASE_TESTS}, HEAD_SHA: {TESTS_PATH: "def (:\n"}}
        )

        result = _collect([reader])

        assert result.undisposed == ()
        assert result.attestation[0] == "none" and result.attestation[1].startswith(f"not computed: {TESTS_PATH}:")
        assert result.notes[0].startswith(f"removed behaviour not computed for {TESTS_PATH}:")
