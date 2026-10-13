"""`loadout-review post` derives the fence's `findings_open` from the computed
blocking set and carries the findings the verdict was computed from."""

from __future__ import annotations

import json

import pytest
import yaml

from clagentic_loadout.merge.verdict import parse_verdict_block
from clagentic_loadout.review.evaluation import finding_state_id
from clagentic_loadout.review.policy_source import DISPOSITIONS_PATH
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import HEAD_SHA, Env
from tests._review_evaluate_support import commit_files, facts


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    environment = Env(tmp_path)
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return environment


SERIOUS = facts(confidentiality="full", integrity="full", availability="full")


def _finding(line, rule, fact=SERIOUS):
    return {"file": "a.py", "line": line, "rule_id": rule, "message": f"{rule} is wrong", "facts": fact}


def _findings_file(tmp_path, findings):
    path = tmp_path / "findings.json"
    path.write_text(
        json.dumps(
            {
                "owner": "some-owner", "repo": "some-repo", "pr_number": 42,
                "head_sha": HEAD_SHA, "findings": findings,
            }
        ),
        encoding="utf-8",
    )
    return path


def _rule_on(env, rule):
    entry = {
        "path": "a.py", "rule_id": rule, "kind": "false_positive", "rationale": "ruled",
        "by": "a-maintainer", "at": "2026-01-01", "expires": "2999-01-01",
    }
    env.base_sha = commit_files(env.repo, {DISPOSITIONS_PATH: yaml.safe_dump({"dispositions": [entry]})})


def test_a_finding_demoted_by_a_base_disposition_is_not_listed_as_open(env, tmp_path, capsys):
    _rule_on(env, "R1")
    findings = _findings_file(tmp_path, [_finding(3, "R1"), _finding(4, "R2")])

    code, payload = env.post(
        "--findings", str(findings), "--base-sha", env.base_sha, capsys=capsys
    )

    assert code == 0
    fence = parse_verdict_block(env.opener_state["posted_body"])
    assert fence["verdict_outcome"] == "BLOCKED"
    assert fence["findings_open"] == [
        {"id": finding_state_id("a.py", 4, "R2"), "rule_id": "R2", "head": HEAD_SHA}
    ]
    assert payload["open_finding_ids"] == [finding_state_id("a.py", 4, "R2")]
    body = env.opener_state["posted_body"].split("```review-result")[0].splitlines()
    assert "Advisory findings (1):" in body


def test_a_clean_post_holds_nothing_open(env, tmp_path, capsys):
    _rule_on(env, "R1")
    findings = _findings_file(tmp_path, [_finding(3, "R1")])

    code, _ = env.post(
        "--findings", str(findings), "--status", "clean", "--base-sha", env.base_sha, capsys=capsys
    )

    assert code == 0
    fence = parse_verdict_block(env.opener_state["posted_body"])
    assert fence["verdict_outcome"] == "CLEAN"
    assert fence["review_status"] == "clean"
    assert "findings_open" not in fence


def test_findings_open_in_the_state_file_is_replaced_by_the_computed_set(env, tmp_path, capsys):
    findings = _findings_file(tmp_path, [_finding(4, "R2")])
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"findings_open": [{"id": "caller-typed", "rule_id": "R9", "head": HEAD_SHA}]}),
        encoding="utf-8",
    )

    code, _, err = env.invoke(
        "post", "--findings", str(findings), "--state-file", str(state), capsys=capsys
    )

    assert code == 0
    assert "findings_open in the state file is ignored" in err
    fence = parse_verdict_block(env.opener_state["posted_body"])
    assert [entry["id"] for entry in fence["findings_open"]] == [finding_state_id("a.py", 4, "R2")]


def test_the_fence_carries_the_findings_with_their_facts_and_nothing_else(env, tmp_path, capsys):
    item = {**_finding(4, "R2"), "summary": "CALLER-PROSE", "fingerprint": "fp2:" + "a" * 16}
    findings = _findings_file(tmp_path, [item])

    code, _ = env.post("--findings", str(findings), capsys=capsys)

    assert code == 0
    fence = parse_verdict_block(env.opener_state["posted_body"])
    assert fence["evaluated_findings"] == [
        {"file": "a.py", "line": 4, "rule_id": "R2", "message": "R2 is wrong", "facts": SERIOUS}
    ]


def test_a_fence_shaped_fact_is_refused_before_anything_is_posted(env, tmp_path, capsys):
    hostile = {**SERIOUS, "evidence": "x ```review-result y"}
    findings = _findings_file(tmp_path, [_finding(4, "R2", hostile)])

    code, _ = env.post("--findings", str(findings), capsys=capsys)

    assert code != 0
    assert env.opener_state["posted_body"] is None
