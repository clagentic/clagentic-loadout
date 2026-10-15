"""The merge gate applies per-PR rulings from the ruling registry.

Every case runs the real verb, on both platforms, merging and under
--report-only, against a real git repository whose main tip is the PR base."""

from __future__ import annotations

import json
import os

import pytest

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.gate_evaluation import GateEvaluation, evaluate_at_gate
from clagentic_loadout.merge.gate_config import EvaluatorGate
from clagentic_loadout.merge.verdict import read_reviewer_verdict
from clagentic_loadout.review import ruling_provider as rp
from clagentic_loadout.review.ruling_provider import RulingScope
from tests._ruling_support import (
    FLOOR,
    FP,
    NON_FLOOR,
    OTHER_FP,
    REPO,
    ruled_finding,
    ruling,
    write_registry,
)
from tests._support.merge_verb import AllowingAuthorityProvider, RecordingTokenProvider, base_args
from tests.test_merge_computed_verdict import HEAD, LOGIN, NAME, _fence, _repo, _tip
from tests.test_merge_verb_platform_dispatch import _forgejo_opener, _github_opener

_PLATFORMS = {"forgejo": _forgejo_opener, "github": _github_opener}
_MODES = [pytest.param([], id="merge"), pytest.param(["--report-only"], id="report-only")]


def _finding(fact=NON_FLOOR, *, fingerprint=FP, **extra):
    """A finding as the evaluated_findings of a fence carries it."""
    item = {k: v for k, v in ruled_finding(fact, fingerprint=fingerprint, **extra).items()
            if k in ("file", "line", "rule_id", "message", "facts", "fingerprint")}
    if fingerprint is None:
        del item["fingerprint"]
    return item


def _comment(comment_id, body):
    return {
        "id": comment_id,
        "user": {"login": LOGIN},
        "body": body,
        "created_at": f"2026-01-01T00:00:{comment_id:02d}Z",
    }


class _Run:
    def __init__(self, code, out, err):
        self.code, self.out, self.err = code, out, err

    @property
    def text(self):
        return self.out + self.err


def _gate(platform, comments, repo, capsys, mode=()):
    info = {
        "head": {"sha": HEAD},
        "title": "feat: x",
        "base": {"ref": "main", "sha": _tip(repo), "repo": {"private": True, "visibility": "private"}},
    }
    opener = _PLATFORMS[platform](pr_info=info, comments=comments)
    argv = base_args(
        **{
            "--platform": platform,
            "--required-reviewer": f"{NAME}:{LOGIN}",
            "--repo-path": str(repo),
        }
    )
    capsys.readouterr()
    code = verb.main(
        [*argv, *mode],
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=opener,
    )
    captured = capsys.readouterr()
    return _Run(code, captured.out, captured.err)


@pytest.fixture
def repo(tmp_path):
    return _repo(tmp_path)


@pytest.fixture
def use_registry(tmp_path, monkeypatch):
    def configure(rulings):
        path = write_registry(tmp_path, rulings)
        monkeypatch.setenv(rp.RULINGS_FILE_ENV_VAR, str(path))
        return path

    return configure


def _blocked(*findings):
    return [_comment(1, _fence("BLOCKED", findings=list(findings)))]


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("platform", sorted(_PLATFORMS))
class TestRulingClearsAtTheGate:
    def test_a_ruling_at_this_pr_clears_a_kept_non_floor_blocking_finding(
        self, platform, mode, repo, use_registry, capsys
    ):
        use_registry([ruling(kind="false_positive")])

        run = _gate(platform, _blocked(_finding()), repo, capsys, mode)

        assert run.code == verb.EXIT_OK, run.err
        assert "ruling(s) recorded for this PR" in run.err
        for named in ("some-role", "some-caller", "design note 4", "src/a.py:10:R1"):
            assert named in run.err
        if not mode:
            result = json.loads(run.out.strip().splitlines()[-1])
            evaluation = result["verdict_evaluations"][0]
            assert evaluation["gate_outcome"] == "CLEAN" and evaluation["fence_outcome"] == "BLOCKED"
            assert "src/a.py:10:R1" in evaluation["applied_rulings"][0]
            assert evaluation["applied_dispositions"] == []
        else:
            assert "ruling applied:" in run.out

    def test_it_does_not_clear_another_pull_request(self, platform, mode, repo, use_registry, capsys):
        use_registry([ruling(pr=2)])

        run = _gate(platform, _blocked(_finding()), repo, capsys, mode)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "src/a.py:10:R1" in run.err

    def test_it_does_not_clear_another_repository(self, platform, mode, repo, use_registry, capsys):
        use_registry([ruling(repo="some-owner/other-repo")])

        assert _gate(platform, _blocked(_finding()), repo, capsys, mode).code == (
            verb.EXIT_GATE_RESULT_BLOCKED
        )

    def test_it_does_not_clear_another_fingerprint_and_names_the_ruling_it_did_not_apply(
        self, platform, mode, repo, use_registry, capsys
    ):
        use_registry([ruling(fingerprint=OTHER_FP)])

        run = _gate(platform, _blocked(_finding()), repo, capsys, mode)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "cleared nothing" in run.err and "matched no kept finding" in run.err

    def test_it_clears_only_the_finding_it_names(self, platform, mode, repo, use_registry, capsys):
        use_registry([ruling()])
        other = _finding(fingerprint=OTHER_FP, line=20, rule_id="R2")

        run = _gate(platform, _blocked(_finding(), other), repo, capsys, mode)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "src/a.py:20:R2" in run.err

    @pytest.mark.parametrize("kind", ["by_design", "false_positive", "accepted_risk", "mitigated"])
    def test_it_never_clears_a_security_floor_finding(
        self, platform, mode, kind, repo, use_registry, capsys
    ):
        use_registry([ruling(kind=kind)])

        run = _gate(platform, _blocked(_finding(FLOOR)), repo, capsys, mode)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "security-floor finding" in run.err
        assert "dispositions entry on the base branch" in run.err

    def test_a_fence_that_carries_no_fingerprint_cannot_be_ruled_on(
        self, platform, mode, repo, use_registry, capsys
    ):
        use_registry([ruling()])

        run = _gate(platform, _blocked(_finding(fingerprint=None)), repo, capsys, mode)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_an_expired_ruling_no_longer_clears_and_is_named(
        self, platform, mode, repo, use_registry, capsys
    ):
        import datetime as dt

        use_registry([ruling(kind="false_positive", at=dt.date(2026, 1, 1), expires=dt.date(2026, 2, 1))])

        run = _gate(platform, _blocked(_finding()), repo, capsys, mode)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_a_registry_the_pull_request_could_write_applies_nothing_and_says_so(
        self, platform, mode, repo, tmp_path, monkeypatch, capsys
    ):
        inside = write_registry(repo, [ruling(kind="false_positive")])
        monkeypatch.setenv(rp.RULINGS_FILE_ENV_VAR, str(inside))

        run = _gate(platform, _blocked(_finding()), repo, capsys, mode)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "not a trusted source" in run.err

    def test_a_world_writable_registry_applies_nothing(
        self, platform, mode, repo, use_registry, capsys
    ):
        path = use_registry([ruling(kind="false_positive")])
        os.chmod(path, 0o666)

        run = _gate(platform, _blocked(_finding()), repo, capsys, mode)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "writable by every user" in run.err


@pytest.mark.parametrize("platform", sorted(_PLATFORMS))
class TestWithoutARegistry:
    def test_a_blocked_fence_refuses_exactly_as_it_always_did(self, platform, repo, capsys, monkeypatch):
        monkeypatch.delenv(rp.RULINGS_FILE_ENV_VAR, raising=False)

        run = _gate(platform, _blocked(_finding()), repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "cleared nothing" not in run.err and "ruling registry" not in run.err

    def test_the_evaluation_record_has_no_ruling_keys(self, platform, repo, capsys, monkeypatch):
        monkeypatch.delenv(rp.RULINGS_FILE_ENV_VAR, raising=False)
        comments = [_comment(1, _fence("CLEAN", findings=[]))]

        run = _gate(platform, comments, repo, capsys)

        assert run.code == verb.EXIT_OK
        assert json.loads(run.out.strip().splitlines()[-1])["verdict_evaluations"] == [
            {
                "reviewer": NAME, "evaluated": True, "fence_outcome": "CLEAN",
                "gate_outcome": "CLEAN", "applied_dispositions": [], "degraded_allowed": False,
                "degraded_reasons": [],
            }
        ]

    def test_an_empty_registry_gives_the_same_record(self, platform, repo, use_registry, capsys):
        use_registry([])
        comments = [_comment(1, _fence("CLEAN", findings=[]))]

        run = _gate(platform, comments, repo, capsys)

        assert run.code == verb.EXIT_OK
        assert set(json.loads(run.out.strip().splitlines()[-1])["verdict_evaluations"][0]) == {
            "reviewer", "evaluated", "fence_outcome", "gate_outcome", "applied_dispositions",
            "degraded_allowed", "degraded_reasons",
        }


class TestCleanFenceAndRulingLifetime:
    def test_a_clean_fence_stays_clean_while_the_ruling_stands(self, repo, use_registry, capsys):
        use_registry([ruling(kind="false_positive")])
        comments = [_comment(1, _fence("CLEAN", findings=[_finding()]))]

        run = _gate("forgejo", comments, repo, capsys)

        assert run.code == verb.EXIT_OK, run.err

    def test_a_clean_fence_is_refused_once_the_ruling_that_cleared_it_is_gone(
        self, repo, use_registry, capsys
    ):
        use_registry([])
        comments = [_comment(1, _fence("CLEAN", findings=[_finding()]))]

        run = _gate("forgejo", comments, repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "records CLEAN" in run.err


class TestEveryFenceOfARole:
    """The gate evaluates each fence of a role on its own, with the registry
    read for that fence, so a ruling clears the finding in every fence."""

    def _evaluate(self, comments, repo, today=None):
        verdict = read_reviewer_verdict(
            comments, expected_login=LOGIN, current_head_sha=HEAD, pr_number=1,
            owner="some-owner", repo="some-repo", expected_reviewer_name=NAME,
        )
        from clagentic_loadout.review.policy_reader import GitPolicyReader

        return evaluate_at_gate(
            verdict, NAME, current_head_sha=HEAD, live_base_sha=_tip(repo),
            policy_reader=GitPolicyReader(repo), visibility="private",
            evaluator=EvaluatorGate(), ruling_scope=RulingScope(REPO, 1),
        )

    def test_a_ruling_clears_the_finding_in_each_of_several_fences(self, repo, use_registry):
        use_registry([ruling(kind="false_positive")])
        first = [_comment(1, _fence("BLOCKED", findings=[_finding()]))]
        second = [_comment(2, _fence("BLOCKED", findings=[_finding(), _finding(line=11, message="again")]))]

        results = [self._evaluate(first, repo), self._evaluate(second, repo)]

        assert all(isinstance(r, GateEvaluation) and r.gate_outcome == "CLEAN" for r in results)
        assert all(r.applied_rulings for r in results)

    def test_the_scope_is_part_of_the_question(self, repo, use_registry):
        from clagentic_loadout.merge.errors import VerdictBlockingError

        use_registry([ruling(kind="false_positive")])
        comments = [_comment(1, _fence("BLOCKED", findings=[_finding()]))]
        verdict = read_reviewer_verdict(
            comments, expected_login=LOGIN, current_head_sha=HEAD, pr_number=1,
            owner="some-owner", repo="some-repo", expected_reviewer_name=NAME,
        )
        from clagentic_loadout.review.policy_reader import GitPolicyReader

        with pytest.raises(VerdictBlockingError):
            evaluate_at_gate(
                verdict, NAME, current_head_sha=HEAD, live_base_sha=_tip(repo),
                policy_reader=GitPolicyReader(repo), visibility="private",
                evaluator=EvaluatorGate(), ruling_scope=None,
            )

