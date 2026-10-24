"""The merge verb's verdict-record requirement: every gate the configuration
requires must have left a record at the exact head being merged, and a gate that
did not is not measured. Covers the acceptance cases, the transition path for a
pull request reviewed before records existed, and the records the verb writes for
its own checks."""

from __future__ import annotations

import json
import sys

import pytest
import yaml

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.verdict import build_verdict_block
from clagentic_loadout.repo_config import DEFAULT_CONFIG_RELATIVE_PATH
from clagentic_loadout.verdict_record.emit import emit_record
from clagentic_loadout.verdict_record.record import GATE_CI, gate_name
from clagentic_loadout.verdict_record.store import load_record, records_at_head, records_root
from tests._gate_repo import init_gate_repo
from tests._support.merge_verb import (
    FULL_SHA,
    OTHER_FULL_SHA,
    AllowingAuthorityProvider,
    RecordingTokenProvider,
    base_args,
    make_opener,
)
from tests._support.platform_openers import forgejo_opener, github_opener

REVIEWER_GATE = "reviewer:some-reviewer"
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-01T00:00:05Z"
_PY = sys.executable
_REVIEWER_FLAG = {"--required-reviewer": "some-reviewer:reviewer-login"}


def _run(argv, opener):
    return verb.main(
        argv,
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=opener,
    )


def _fence_comment(head=FULL_SHA, status="clean", comment_id=1):
    body = build_verdict_block("some-reviewer", status, head, 1)
    return {"id": comment_id, "user": {"login": "reviewer-login"}, "body": body}


def _opener(comments, head=FULL_SHA):
    return make_opener(
        pr_info={"head": {"sha": head}, "title": "feat: x"}, comments=comments
    )


def _reviewer_record(head=FULL_SHA, exit=0):
    return emit_record(
        gate=REVIEWER_GATE, head_sha=head, tool="test-reviewer", exit=exit,
        started=T0, finished=T1, evidence_ref="comment:1",
    )


class TestRequiredReviewerRecord:
    """Acceptance 1 to 3 for a reviewer role."""

    def test_a_reviewer_that_never_ran_is_not_measured_and_the_gate_is_named(self, capsys):
        code = _run(base_args(**_REVIEWER_FLAG), _opener(comments=[]))
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        err = capsys.readouterr().err
        assert "not-measured" in err
        assert REVIEWER_GATE in err
        assert f"expected a record at head {FULL_SHA}" in err
        assert "record found for head none" in err

    def test_a_record_for_another_commit_is_not_measured_for_this_head(self, capsys):
        _reviewer_record(head=OTHER_FULL_SHA)
        code = _run(
            base_args(**_REVIEWER_FLAG),
            _opener(comments=[_fence_comment(head=OTHER_FULL_SHA)]),
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        err = capsys.readouterr().err
        assert "not-measured" in err and REVIEWER_GATE in err
        assert f"expected a record at head {FULL_SHA}" in err
        assert f"record found for head {OTHER_FULL_SHA}" in err

    def test_a_record_and_its_fence_at_the_head_proceed(self):
        record = _reviewer_record()
        code = _run(base_args(**_REVIEWER_FLAG), _opener(comments=[_fence_comment()]))
        assert code == verb.EXIT_OK
        assert load_record(records_root(), record.id) == record

    def test_the_fence_gate_is_not_weakened_by_a_record(self):
        _reviewer_record()
        code = _run(base_args(**_REVIEWER_FLAG), _opener(comments=[]))
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_a_blocking_fence_still_refuses_beside_a_passing_record(self):
        _reviewer_record()
        code = _run(
            base_args(**_REVIEWER_FLAG), _opener(comments=[_fence_comment(status="blocking")])
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_the_not_measured_refusal_uses_the_existing_gate_refusal_exit_code(self):
        assert verb.EXIT_GATE_RESULT_BLOCKED == 24
        assert _run(base_args(**_REVIEWER_FLAG), _opener(comments=[])) == 24


class TestTransitionFromFencesToRecords:
    """A pull request reviewed by the previous release has a verdict fence at
    its head and no record."""

    def test_a_fence_at_the_exact_head_and_no_record_still_merges(self, capsys):
        assert records_at_head(records_root(), FULL_SHA).records == ()
        code = _run(base_args(**_REVIEWER_FLAG), _opener(comments=[_fence_comment()]))
        assert code == verb.EXIT_OK
        gates = {r.gate for r in records_at_head(records_root(), FULL_SHA).records}
        # The fence was read as the reviewer's record and was not stored; the
        # records the verb wrote are its own.
        assert REVIEWER_GATE not in gates and GATE_CI in gates

    def test_a_fence_at_an_older_head_is_not_measured_naming_the_gate(self, capsys):
        code = _run(
            base_args(**_REVIEWER_FLAG),
            _opener(comments=[_fence_comment(head=OTHER_FULL_SHA)]),
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        err = capsys.readouterr().err
        assert "not-measured" in err and REVIEWER_GATE in err
        assert f"expected a record at head {FULL_SHA}" in err


class TestRecordsTheVerbWritesForItsOwnChecks:
    def test_the_ci_read_is_recorded_for_the_merged_head_and_read_by_id(self):
        assert _run(base_args(), make_opener()) == verb.EXIT_OK
        (record,) = records_at_head(records_root(), FULL_SHA).records
        assert record.gate == GATE_CI
        assert record.head_sha == FULL_SHA
        assert record.tool == "loadout-merge" and record.exit == 0
        assert record.started <= record.finished
        assert record.repo == "some-owner/some-repo"
        assert load_record(records_root(), record.id) == record

    def test_pre_checks_are_recorded_one_per_check(self, tmp_path):
        steps = [
            {"cmd": [_PY, "-c", "pass"], "description": "lint pass", "on_failure": "fail"},
            {"cmd": [_PY, "-c", "pass"], "on_failure": "fail"},
        ]
        repo = init_gate_repo(tmp_path / "repo", tracked_gate={"pre_checks": steps})
        opener = make_opener(pr_info=repo.pr_info())
        assert _run(base_args(**{"--repo-path": repo.path}), opener) == verb.EXIT_OK
        gates = sorted(r.gate for r in records_at_head(records_root(), repo.head_sha).records)
        assert gates == sorted(
            [GATE_CI, gate_name("pre_check", "lint pass"), gate_name("pre_check", f"{_PY} -c pass")]
        )

    def test_skipped_pre_checks_are_not_required_or_recorded(self, tmp_path):
        steps = [{"cmd": [_PY, "-c", "pass"], "on_failure": "fail"}]
        repo = init_gate_repo(tmp_path / "repo", tracked_gate={"pre_checks": steps})
        opener = make_opener(pr_info=repo.pr_info())
        argv = base_args(**{"--repo-path": repo.path}) + ["--skip-pre-checks"]
        assert _run(argv, opener) == verb.EXIT_OK
        gates = {r.gate for r in records_at_head(records_root(), repo.head_sha).records}
        assert gates == {GATE_CI}

    def test_report_only_records_what_it_measured(self):
        assert _run(base_args() + ["--report-only"], make_opener()) == verb.EXIT_OK
        assert [r.gate for r in records_at_head(records_root(), FULL_SHA).records] == [GATE_CI]

    def test_a_failed_ci_gate_refuses_before_any_record_is_written(self):
        opener = make_opener(ci_state="failure", ci_statuses=[{"context": "build", "status": "failure"}])
        assert _run(base_args(), opener) == verb.EXIT_CI_STATUS_FAILED
        assert records_at_head(records_root(), FULL_SHA).records == ()


def _write_repo_config(repo_path, *, push_verify=None):
    config = {"merge": {"sync_tree_after_merge": False}}
    if push_verify is not None:
        config["push"] = {"verify": push_verify}
    target = repo_path / DEFAULT_CONFIG_RELATIVE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(config), encoding="utf-8")


class TestPushVerifyRecords:
    _ENTRY = {"name": "unit", "argv": [_PY, "-c", "pass"]}

    def _repo(self, tmp_path, entries):
        repo = init_gate_repo(tmp_path / "repo", tracked_gate={})
        _write_repo_config(repo.path, push_verify=entries)
        return repo

    def test_a_repository_without_push_verify_requires_none(self, tmp_path):
        repo = self._repo(tmp_path, None)
        argv = base_args(**{"--repo-path": repo.path})
        assert _run(argv, make_opener(pr_info=repo.pr_info())) == verb.EXIT_OK

    def test_a_configured_entry_with_no_record_is_not_measured_and_says_how_to_record_it(
        self, tmp_path, capsys
    ):
        repo = self._repo(tmp_path, [self._ENTRY])
        argv = base_args(**{"--repo-path": repo.path})
        assert _run(argv, make_opener(pr_info=repo.pr_info())) == verb.EXIT_GATE_RESULT_BLOCKED
        err = capsys.readouterr().err
        assert "not-measured" in err and "push.verify:unit" in err
        assert f"expected a record at head {repo.head_sha}" in err
        assert "loadout-push --update-pr --pr <n> --append-body" in err

    def test_a_string_repo_path_reads_the_push_verify_entries(self, tmp_path, capsys):
        repo = self._repo(tmp_path, [self._ENTRY])
        argv = base_args(**{"--repo-path": str(repo.path)})
        assert isinstance(argv[argv.index("--repo-path") + 1], str)
        code = _run(argv, make_opener(pr_info=repo.pr_info()))
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "push.verify:unit" in capsys.readouterr().err

    def test_a_record_at_the_head_satisfies_the_entry(self, tmp_path):
        repo = self._repo(tmp_path, [self._ENTRY])
        emit_record(
            gate="push.verify:unit", head_sha=repo.head_sha, tool="loadout-push", exit=0,
            started=T0, finished=T1,
        )
        argv = base_args(**{"--repo-path": repo.path})
        assert _run(argv, make_opener(pr_info=repo.pr_info())) == verb.EXIT_OK

    def test_a_record_for_the_previous_head_does_not(self, tmp_path):
        repo = self._repo(tmp_path, [self._ENTRY])
        emit_record(
            gate="push.verify:unit", head_sha=OTHER_FULL_SHA, tool="loadout-push", exit=0,
            started=T0, finished=T1,
        )
        argv = base_args(**{"--repo-path": repo.path})
        assert _run(argv, make_opener(pr_info=repo.pr_info())) == verb.EXIT_GATE_RESULT_BLOCKED

    def test_a_failing_record_refuses(self, tmp_path, capsys):
        repo = self._repo(tmp_path, [self._ENTRY])
        emit_record(
            gate="push.verify:unit", head_sha=repo.head_sha, tool="loadout-push", exit=2,
            started=T0, finished=T1,
        )
        argv = base_args(**{"--repo-path": repo.path})
        assert _run(argv, make_opener(pr_info=repo.pr_info())) == verb.EXIT_GATE_RESULT_BLOCKED
        assert "did not pass" in capsys.readouterr().err

    def test_a_malformed_push_verify_declaration_refuses(self, tmp_path, capsys):
        repo = self._repo(tmp_path, "not-a-list")
        argv = base_args(**{"--repo-path": repo.path})
        assert _run(argv, make_opener(pr_info=repo.pr_info())) == verb.EXIT_GATE_RESULT_BLOCKED
        assert "push.verify config FAILED to load" in capsys.readouterr().err


class TestStoreFailure:
    def test_a_store_inside_the_repository_under_review_refuses(self, tmp_path, monkeypatch, capsys):
        repo = init_gate_repo(tmp_path / "repo", tracked_gate={})
        monkeypatch.setenv(
            "CLAGENTIC_LOADOUT_VERDICT_RECORDS_DIR", str(repo.path / "records")
        )
        argv = base_args(**{"--repo-path": repo.path})
        assert _run(argv, make_opener(pr_info=repo.pr_info())) == verb.EXIT_GATE_RESULT_BLOCKED
        assert "the pull request under review can write" in capsys.readouterr().err

    def test_an_unreadable_record_file_is_warned_about_and_never_counted(self, tmp_path, capsys):
        _reviewer_record()
        directory = records_root() / FULL_SHA
        (directory / "20260101T000000Z-deadbeef.json").write_text("{", encoding="utf-8")
        assert (
            _run(base_args(**_REVIEWER_FLAG), _opener(comments=[_fence_comment()]))
            == verb.EXIT_OK
        )
        assert "verdict record ignored" in capsys.readouterr().err


class TestCallerShapes:
    """The shapes the requirement is reachable from: the process the merger runs
    as reads what other identities wrote, and both hosts use the one path."""

    def test_records_written_in_one_process_are_read_by_another_identity(self, tmp_path, monkeypatch):
        # The builder and the merger are different processes that share only the
        # records root the deployment names; nothing is passed between them.
        shared = tmp_path / "shared-records"
        monkeypatch.setenv("CLAGENTIC_LOADOUT_VERDICT_RECORDS_DIR", str(shared))
        repo = init_gate_repo(tmp_path / "repo", tracked_gate={})
        _write_repo_config(repo.path, push_verify=[{"name": "unit", "argv": [_PY, "-c", "pass"]}])
        emit_record(
            gate="push.verify:unit", head_sha=repo.head_sha, tool="loadout-push", exit=0,
            started=T0, finished=T1, root=shared,
        )
        argv = base_args(**{"--repo-path": repo.path})
        assert _run(argv, make_opener(pr_info=repo.pr_info())) == verb.EXIT_OK
        assert json.loads((next((shared / repo.head_sha).glob("*.json"))).read_text())

    @pytest.mark.parametrize("platform,make", [("forgejo", forgejo_opener), ("github", github_opener)])
    def test_both_hosts_use_the_one_requirement(self, platform, make, capsys):
        # The records are keyed by commit, not by host; the platform changes only
        # how the head and the fence are fetched.
        pr_info = {"head": {"sha": FULL_SHA}, "title": "feat: x"}
        argv = base_args(**{"--platform": platform, **_REVIEWER_FLAG})
        ok = _run(argv, make(pr_info=pr_info, comments=[_fence_comment()]))
        assert ok == verb.EXIT_OK
        assert GATE_CI in {r.gate for r in records_at_head(records_root(), FULL_SHA).records}

        stale = _run(
            argv, make(pr_info=pr_info, comments=[_fence_comment(head=OTHER_FULL_SHA)])
        )
        assert stale == verb.EXIT_GATE_RESULT_BLOCKED
        assert "not-measured" in capsys.readouterr().err
