"""Rulings wired into the evaluation context, the fence, the offline evaluate
verb and the verb that writes them."""

from __future__ import annotations

import argparse
import json

import pytest

from clagentic_loadout.merge.fence_state import normalize_findings_state, state_from_fence
from clagentic_loadout.merge.verdict import build_verdict_block, parse_verdict_block
from clagentic_loadout.review import ruling_provider as rp
from clagentic_loadout.review import ruling_verb
from clagentic_loadout.review.evaluate_verb import (
    EXIT_BLOCKED,
    EXIT_OK,
    EXIT_USAGE,
    add_evaluate_parser,
    run_evaluate,
)
from clagentic_loadout.review.evaluation import (
    EvaluationContext,
    context_from_config,
    evaluate_findings,
    evaluated_findings_for_fence,
)
from clagentic_loadout.transport.attestation import SOURCE_CONFIGURED, Identity
from tests._review_evaluate_support import HEAD, commit_files, make_repo
from tests._ruling_support import FLOOR, FP, NON_FLOOR, REPO, TODAY, ruled_finding, ruling, write_registry


@pytest.fixture
def base(tmp_path):
    repo = make_repo(tmp_path)
    return repo, commit_files(repo, {"README.md": "base\n"})


def _context(base, provider, scope=rp.RulingScope(REPO, 1)):
    repo, sha = base
    return EvaluationContext(
        repo_path=repo, policy_base_sha=sha, visibility="private",
        ruling_provider=provider, ruling_scope=scope,
    )


def _provider(tmp_path, *rulings):
    return rp.FileRulingProvider(write_registry(tmp_path, list(rulings)))


class TestEvaluationContext:
    def test_a_ruling_clears_a_kept_finding_through_the_context(self, base, tmp_path):
        provider = _provider(tmp_path, ruling(kind="false_positive"))

        verdict = evaluate_findings([ruled_finding()], HEAD, _context(base, provider), today=TODAY)

        assert verdict.outcome == "CLEAN"
        assert verdict.findings[0].disposition["source"] == "ruling"

    def test_without_a_scope_nothing_is_applied(self, base, tmp_path):
        provider = _provider(tmp_path, ruling(kind="false_positive"))

        verdict = evaluate_findings(
            [ruled_finding()], HEAD, _context(base, provider, scope=None), today=TODAY
        )

        assert verdict.outcome == "BLOCKED"

    def test_another_pull_request_in_the_scope_applies_nothing(self, base, tmp_path):
        provider = _provider(tmp_path, ruling(kind="false_positive"))

        verdict = evaluate_findings(
            [ruled_finding()], HEAD, _context(base, provider, rp.RulingScope(REPO, 2)), today=TODAY
        )

        assert verdict.outcome == "BLOCKED"

    def test_an_untrusted_registry_makes_the_verdict_degraded_and_says_why(self, base, tmp_path):
        provider = _provider(tmp_path, ruling(kind="false_positive"))
        scope = rp.RulingScope(REPO, 1, untrusted_roots=(tmp_path,))

        verdict = evaluate_findings([ruled_finding()], HEAD, _context(base, provider, scope), today=TODAY)

        assert verdict.outcome == "DEGRADED"
        assert any("not a trusted source" in reason for reason in verdict.degraded)

    def test_no_registry_configured_changes_nothing(self, base, tmp_path):
        with_none = evaluate_findings([ruled_finding(FLOOR)], HEAD, _context(base, None), today=TODAY)
        plain = evaluate_findings(
            [ruled_finding(FLOOR)], HEAD,
            EvaluationContext(repo_path=base[0], policy_base_sha=base[1], visibility="private"),
            today=TODAY,
        )

        assert with_none.as_dict() == plain.as_dict()

    def test_context_from_config_resolves_the_configured_registry(self, tmp_path, monkeypatch):
        path = write_registry(tmp_path, [ruling()])
        monkeypatch.setenv(rp.RULINGS_FILE_ENV_VAR, str(path))

        assert context_from_config(tmp_path / "no-config").ruling_provider.path == path

    def test_context_from_config_has_no_provider_without_a_path(self, tmp_path, monkeypatch):
        monkeypatch.delenv(rp.RULINGS_FILE_ENV_VAR, raising=False)

        assert context_from_config(tmp_path / "no-config").ruling_provider is None


class TestFenceFingerprint:
    def test_the_fingerprint_is_not_carried_by_default(self):
        assert "fingerprint" not in evaluated_findings_for_fence([ruled_finding()])[0]

    def test_it_is_carried_when_a_registry_is_configured(self):
        assert evaluated_findings_for_fence([ruled_finding()], with_fingerprint=True)[0]["fingerprint"] == FP

    def test_a_malformed_fingerprint_is_never_carried(self):
        carried = evaluated_findings_for_fence([ruled_finding(fingerprint="not-one")], with_fingerprint=True)

        assert "fingerprint" not in carried[0]

    def test_a_carried_fingerprint_survives_the_fence_round_trip(self):
        entry = evaluated_findings_for_fence([ruled_finding()], with_fingerprint=True)
        state = {"verdict_outcome": "BLOCKED", "verdict_reasons": ["r"], "evaluator_version": "1",
                 "evaluated_findings": entry}

        parsed = parse_verdict_block(build_verdict_block("some-reviewer", "blocking", HEAD, 5, None, state))

        assert state_from_fence(parsed).evaluated_findings[0]["fingerprint"] == FP

    @pytest.mark.parametrize("bad", ["x", "fp2:123", 7, ""])
    def test_a_malformed_fingerprint_in_a_fence_is_refused(self, bad):
        raw = {"verdict_outcome": "BLOCKED", "verdict_reasons": ["r"], "evaluator_version": "1",
               "evaluated_findings": [{"file": "a.py", "line": 1, "rule_id": "R1", "message": "m",
                                       "fingerprint": bad}]}

        with pytest.raises(ValueError, match="fingerprint"):
            normalize_findings_state(raw, head_sha=HEAD, review_status="blocking")


class TestEvaluateVerb:
    def _args(self, base, tmp_path, *extra):
        parser = argparse.ArgumentParser()
        add_evaluate_parser(parser.add_subparsers(dest="command"))
        findings = tmp_path / "f.json"
        findings.write_text(json.dumps({"head_sha": HEAD, "findings": [ruled_finding()]}), encoding="utf-8")
        repo, sha = base
        return parser.parse_args(
            ["evaluate", "--findings", str(findings), "--base", sha, "--head", HEAD,
             "--visibility", "private", "--repo-path", str(repo), "--format", "text", *extra]
        )

    def test_with_repo_and_pr_the_registry_ruling_applies(self, base, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv(rp.RULINGS_FILE_ENV_VAR, str(write_registry(tmp_path, [ruling(kind="false_positive")])))

        code = run_evaluate(self._args(base, tmp_path, "--repo", REPO, "--pr", "1"), config_root=tmp_path / "c")

        assert code == EXIT_OK
        assert "ruling" in capsys.readouterr().out

    def test_without_them_no_ruling_applies(self, base, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv(rp.RULINGS_FILE_ENV_VAR, str(write_registry(tmp_path, [ruling(kind="false_positive")])))

        assert run_evaluate(self._args(base, tmp_path), config_root=tmp_path / "c") == EXIT_BLOCKED

    def test_one_without_the_other_is_a_usage_error(self, base, tmp_path, capsys):
        code = run_evaluate(self._args(base, tmp_path, "--pr", "1"), config_root=tmp_path / "c")

        assert code == EXIT_USAGE
        assert "together" in capsys.readouterr().err


class TestRecordedByTheVerbThenApplied:
    """A ruling written by the verb, as a role the deployment authorizes, is the
    one the evaluator then applies: at its pull request and fingerprint only."""

    def test_the_round_trip(self, base, tmp_path, monkeypatch, capsys):
        registry = tmp_path / "registry"
        registry.mkdir()
        registry.chmod(0o755)
        monkeypatch.setenv(rp.RULINGS_FILE_ENV_VAR, str(registry / "rulings.yaml"))
        findings = tmp_path / "findings.json"
        findings.write_text(
            json.dumps({"owner": "some-owner", "repo": "some-repo", "pr_number": 1,
                        "head_sha": HEAD, "findings": [ruled_finding(NON_FLOOR)]}),
            encoding="utf-8",
        )
        identity = Identity("merger", SOURCE_CONFIGURED)

        code = ruling_verb.main(
            ["--role", "merger", "record", "--repo", REPO, "--pr", "1", "--findings", str(findings),
             "--fingerprint", FP, "--rule-id", "R1", "--path", "src/a.py", "--kind", "false_positive",
             "--rationale", "reviewed", "--requested-by", "some-caller", "--source-ref", "note 1"],
            identity_provider=lambda: identity, today=TODAY,
        )
        provider = rp.resolve_ruling_provider(tmp_path / "c")

        assert code == 0
        cleared = evaluate_findings([ruled_finding()], HEAD, _context(base, provider), today=TODAY)
        elsewhere = evaluate_findings(
            [ruled_finding()], HEAD, _context(base, provider, rp.RulingScope(REPO, 2)), today=TODAY
        )
        assert cleared.outcome == "CLEAN" and elsewhere.outcome == "BLOCKED"
        assert cleared.findings[0].disposition["by"] == "merger"
        assert cleared.findings[0].disposition["requested_by"] == "some-caller"
