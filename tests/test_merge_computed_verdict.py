"""The merge gate's own computation of a reviewer's verdict.

Every case runs the real verb with an injected host opener and a real git
repository, because the gate reads policy (dispositions) from the PR's live base
commit. Fences are built with the product's own fence builder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.verdict import build_verdict_block
from clagentic_loadout.review.evaluate import EVALUATOR_VERSION
from clagentic_loadout.review.policy_source import DISPOSITIONS_PATH
from clagentic_loadout.transport import provider_config
from tests._gate_repo import commit_files, git, init_gate_repo
from tests._review_evaluate_support import facts
from tests._support.merge_verb import (
    AllowingAuthorityProvider,
    RecordingTokenProvider,
    base_args,
    make_opener,
)

HEAD = "b" * 40
OTHER_HEAD = "c" * 40
LOGIN = "reviewer-login"
NAME = "some-reviewer"
PATH = "src/a.py"

#: Critical on the worst-case stakes every unstated path resolves to.
BLOCKING_FACTS = facts(confidentiality="full", integrity="full", availability="full")
#: A security-floor category: by_design can never clear it.
FLOOR_FACTS = facts(category="injection", confidentiality="full", integrity="full", availability="full")
#: Scores none: advisory, never blocks.
HARMLESS_FACTS = facts()


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "no-user-config")


def _finding(fact=BLOCKING_FACTS, *, line=10, rule="R1", **extra):
    entry = {"file": PATH, "line": line, "rule_id": rule, "message": "something is wrong", **extra}
    if fact is not None:
        entry["facts"] = fact
    return entry


def _disposition_file(*, kind="false_positive", expires="2999-01-01", rule="R1", path="src/**"):
    entry = {
        "path": path, "rule_id": rule, "kind": kind, "rationale": "ruled by hand",
        "by": "a-maintainer", "at": "2026-01-01", "expires": expires,
    }
    return yaml.safe_dump({"dispositions": [entry]})


def _repo(tmp_path: Path, *, dispositions: str | None = None, **merge_gate) -> Path:
    """A repo whose main tip carries the tracked gate (and dispositions)."""
    path = tmp_path / "repo"
    init_gate_repo(path, tracked_gate={"required_reviewer_roles": [], **merge_gate})
    if dispositions is not None:
        commit_files(path, {DISPOSITIONS_PATH: dispositions}, "chore: add dispositions")
    return path


def _tip(repo: Path) -> str:
    return git(repo, "rev-parse", "main")


def _fence(outcome, *, findings=(), status=None, head=HEAD, reasons=None, base=None, keep_findings=True, **more):
    state = {
        "verdict_outcome": outcome,
        "verdict_reasons": reasons if reasons is not None else ([] if outcome == "CLEAN" else ["recorded"]),
        "evaluator_version": EVALUATOR_VERSION,
        **more,
    }
    if keep_findings:
        state["evaluated_findings"] = list(findings)
    if base:
        state["policy_base_sha"] = base
    status = status or ("clean" if outcome == "CLEAN" else "blocking")
    return build_verdict_block(NAME, status, head, 1, findings_state=state)


def _comment(comment_id, body):
    return {"id": comment_id, "user": {"login": LOGIN}, "body": body}


class _Run:
    def __init__(self, code, err, out, posted):
        self.code, self.err, self.out, self.posted = code, err, out, posted


def _merge(comments, repo: Path | None, capsys, *, head=HEAD, extra=None, pr_extra=None):
    posted: list[str] = []
    info = {"head": {"sha": head}, "title": "feat: x"}
    if repo is not None:
        info["base"] = {"ref": "main", "sha": _tip(repo), "repo": {"private": True}}
    info.update(pr_extra or {})
    inner = make_opener(pr_info=info, comments=comments)

    def opener(req, timeout=15):
        if req.get_method() == "POST" and "/comments" in req.full_url:
            posted.append(json.loads(req.data.decode("utf-8"))["body"])
        return inner(req, timeout)

    argv = base_args(
        **{"--required-reviewer": f"{NAME}:{LOGIN}", **({"--repo-path": str(repo)} if repo else {})}
    )
    capsys.readouterr()
    code = verb.main(
        argv + (extra or []),
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=opener,
    )
    captured = capsys.readouterr()
    return _Run(code, captured.err, captured.out, posted)


class TestBlocked:
    def test_a_blocked_fence_refuses_naming_each_finding_and_its_clearing_stanza(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        findings = [_finding(), _finding(line=20, rule="R2")]
        run = _merge([_comment(1, _fence("BLOCKED", findings=findings, base=_tip(repo)))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "src/a.py:10:R1" in run.err and "src/a.py:20:R2" in run.err
        assert run.err.count("dispositions:") == 2
        assert '- path: "src/a.py"' in run.err or 'path: "src/a.py"' in run.err
        assert 'rule_id: "R2"' in run.err
        assert DISPOSITIONS_PATH in run.err

    def test_a_security_floor_finding_says_by_design_cannot_clear_it(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, _fence("BLOCKED", findings=[_finding(FLOOR_FACTS)]))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "security floor forbids" in run.err

    def test_a_blocked_fence_then_a_clean_fence_at_the_same_head_merges(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        comments = [
            _comment(1, _fence("BLOCKED", findings=[_finding()])),
            _comment(2, _fence("CLEAN", findings=[])),
        ]
        run = _merge(comments, repo, capsys)

        assert run.code == verb.EXIT_OK

    def test_a_clean_fence_whose_poster_asked_for_blocking_still_refuses(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, _fence("CLEAN", status="blocking", findings=[]))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "BLOCKED merge" in run.err


class TestClean:
    def test_clean_at_the_merge_head_that_the_gate_reproduces_proceeds(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        findings = [_finding(HARMLESS_FACTS)]
        run = _merge([_comment(1, _fence("CLEAN", findings=findings))], repo, capsys)

        assert run.code == verb.EXIT_OK
        result = json.loads(run.out.strip().splitlines()[-1])
        assert result["verdict_evaluations"] == [
            {
                "reviewer": NAME, "evaluated": True, "fence_outcome": "CLEAN",
                "gate_outcome": "CLEAN", "applied_dispositions": [], "degraded_allowed": False,
                "degraded_reasons": [],
            }
        ]

    def test_the_recorded_policy_base_is_never_read(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        elsewhere = "d" * 40
        run = _merge([_comment(1, _fence("CLEAN", findings=[], base=elsewhere))], repo, capsys)

        assert run.code == verb.EXIT_OK

    def test_a_clean_fence_the_gate_cannot_read_policy_for_refuses_naming_repo_path(
        self, tmp_path, capsys
    ):
        run = _merge([_comment(1, _fence("CLEAN", findings=[]))], None, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "--repo-path" in run.err
        assert "records CLEAN" in run.err

    def test_a_fence_that_does_not_carry_its_findings_cannot_be_checked(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, _fence("CLEAN", keep_findings=False))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "evaluated_findings" in run.err


class TestStale:
    def test_an_outcome_of_stale_refuses_naming_both_heads(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, _fence("STALE", findings=[]))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "STALE" in run.err and HEAD in run.err

    def test_a_fence_for_another_head_refuses_naming_the_heads(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, _fence("CLEAN", findings=[], head=OTHER_HEAD))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert OTHER_HEAD in run.err and HEAD in run.err


class TestDegraded:
    def _degraded(self):
        # No facts on a blocking finding: the gate computes DEGRADED as well.
        return _fence("DEGRADED", findings=[_finding(None)], reasons=["facts absent on src/a.py:10:R1"])

    def test_degraded_refuses_by_default_naming_the_reasons(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, self._degraded())], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "DEGRADED" in run.err
        assert "facts absent" in run.err
        assert "allow_degraded" in run.err

    def test_degraded_for_a_role_that_is_not_allowed_refuses_when_another_is(self, tmp_path, capsys):
        repo = _repo(tmp_path, evaluator={"allow_degraded": {"another-role": True, NAME: False}})
        run = _merge([_comment(1, self._degraded())], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_degraded_proceeds_when_allowed_for_the_role_and_the_attestation_records_it(
        self, tmp_path, capsys
    ):
        repo = _repo(tmp_path, evaluator={"allow_degraded": {NAME: True}})
        run = _merge([_comment(1, self._degraded())], repo, capsys)

        assert run.code == verb.EXIT_OK
        assert "allow_degraded" in run.err
        assert run.posted, "the merge-completion attestation was not posted"
        assert "DEGRADED allowed by merge.evaluator.allow_degraded" in run.posted[-1]
        result = json.loads(run.out.strip().splitlines()[-1])
        assert result["verdict_evaluations"][0]["degraded_allowed"] is True

    def test_allow_degraded_never_lets_a_blocked_fence_through(self, tmp_path, capsys):
        repo = _repo(tmp_path, evaluator={"allow_degraded": {NAME: True}})
        run = _merge([_comment(1, _fence("BLOCKED", findings=[_finding()]))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_a_malformed_evaluator_declaration_is_strict(self, tmp_path, capsys):
        repo = _repo(tmp_path, evaluator={"allow_degraded": {NAME: "yes"}})
        degraded = _merge([_comment(1, self._degraded())], repo, capsys)
        legacy = _merge([_comment(1, build_verdict_block(NAME, "clean", HEAD, 1))], repo, capsys)

        assert degraded.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "merge.evaluator is malformed" in degraded.err
        assert legacy.code == verb.EXIT_GATE_RESULT_BLOCKED


class TestLiveBasePolicy:
    def test_a_floor_finding_whose_disposition_expired_before_merge_refuses_naming_it(
        self, tmp_path, capsys
    ):
        expired = _disposition_file(kind="accepted_risk", expires="2026-01-02")
        repo = _repo(tmp_path, dispositions=expired)
        run = _merge(
            [_comment(1, _fence("CLEAN", findings=[_finding(FLOOR_FACTS)]))], repo, capsys
        )

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "expired" in run.err
        assert "src/** R1" in run.err
        assert "records CLEAN" in run.err

    def test_a_disposition_merged_after_the_review_demotes_the_block_and_is_named(
        self, tmp_path, capsys
    ):
        repo = _repo(tmp_path)
        reviewed_at = _tip(repo)
        commit_files(repo, {DISPOSITIONS_PATH: _disposition_file()}, "chore: rule on it")
        fence = _fence("BLOCKED", findings=[_finding()], base=reviewed_at)
        run = _merge([_comment(1, fence)], repo, capsys)

        assert run.code == verb.EXIT_OK
        assert "false_positive" in run.err
        assert "src/** R1" in run.err
        assert _tip(repo) in run.err
        assert "false_positive" in run.posted[-1]

    def test_a_disposition_that_narrows_by_fingerprint_never_demotes_a_recorded_block(
        self, tmp_path, capsys
    ):
        entry = yaml.safe_load(_disposition_file())
        entry["dispositions"][0]["fingerprint"] = "fp2:" + "a" * 16
        repo = _repo(tmp_path, dispositions=yaml.safe_dump(entry))
        run = _merge([_comment(1, _fence("BLOCKED", findings=[_finding()]))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "fingerprint" in run.err

    def test_a_stricter_policy_merged_after_the_review_refuses_naming_the_policy_change(
        self, tmp_path, capsys
    ):
        repo = _repo(tmp_path, dispositions=_disposition_file())
        reviewed_at = _tip(repo)
        # The review was posted when the base ruled the finding a false positive;
        # the ruling has since been removed from the base.
        commit_files(repo, {DISPOSITIONS_PATH: yaml.safe_dump({"dispositions": []})}, "chore: revoke")
        fence = _fence("CLEAN", findings=[_finding()], base=reviewed_at)
        run = _merge([_comment(1, fence)], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "policy base moved" in run.err
        assert reviewed_at in run.err and _tip(repo) in run.err
        assert "src/a.py:10:R1" in run.err

    def test_a_recorded_block_without_a_disposition_to_explain_it_is_not_cleared(
        self, tmp_path, capsys
    ):
        repo = _repo(tmp_path)
        # The fence says BLOCKED but its finding is harmless: the gate's own
        # computation is CLEAN with nothing applied, and a block stands.
        fence = _fence("BLOCKED", findings=[_finding(HARMLESS_FACTS)])
        run = _merge([_comment(1, fence)], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "no disposition explains the difference" in run.err


class TestNotEvaluated:
    def _legacy(self):
        return build_verdict_block(NAME, "clean", HEAD, 1)

    def test_a_pre_v3_fence_applies_its_asserted_status_with_a_notice(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, self._legacy())], repo, capsys)

        assert run.code == verb.EXIT_OK
        assert "was not evaluated" in run.err
        assert "asserted by its poster" in run.err or "status its poster asserted" in run.err

    def test_a_pre_v3_blocking_fence_still_refuses_by_its_status(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, build_verdict_block(NAME, "blocking", HEAD, 1))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_require_evaluated_refuses_a_pre_v3_fence_naming_it(self, tmp_path, capsys):
        repo = _repo(tmp_path, evaluator={"require_evaluated": True})
        run = _merge([_comment(7, self._legacy())], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "comment #7" in run.err
        assert "require_evaluated" in run.err

    def test_require_evaluated_does_not_disturb_an_evaluated_fence(self, tmp_path, capsys):
        repo = _repo(tmp_path, evaluator={"require_evaluated": True})
        run = _merge([_comment(1, _fence("CLEAN", findings=[]))], repo, capsys)

        assert run.code == verb.EXIT_OK


class TestReportOnly:
    def test_prints_the_computed_verdict_and_merges_nothing(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, _fence("CLEAN", findings=[]))], repo, capsys, extra=["--report-only"])

        assert run.code == verb.EXIT_OK
        assert f"verdict {NAME}: fence CLEAN, gate CLEAN" in run.out
        assert "nothing was merged" in run.out
        assert not run.posted

    def test_a_blocked_fence_is_refused_the_same_way(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge(
            [_comment(1, _fence("BLOCKED", findings=[_finding()]))], repo, capsys, extra=["--report-only"]
        )

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "src/a.py:10:R1" in run.err
        assert not run.posted
