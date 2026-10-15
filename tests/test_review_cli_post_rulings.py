"""`loadout-review post` with a ruling registry: the ruling for this pull request
clears the finding it names, and the fence carries the fingerprint the merge gate
needs to match it. Without a registry nothing about the post changes."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.merge.verdict import parse_verdict_block
from clagentic_loadout.review import ruling_provider as rp
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import HEAD_SHA, Env
from tests._ruling_support import FLOOR, FP, NON_FLOOR, ruling, write_registry


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    environment = Env(tmp_path)
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    monkeypatch.delenv(rp.RULINGS_FILE_ENV_VAR, raising=False)
    return environment


def _finding(facts=NON_FLOOR):
    return {
        "file": "src/a.py", "line": 3, "rule_id": "R1", "severity": "blocking",
        "message": "unchecked path", "facts": facts, "fingerprint": FP,
    }


def _post(env, tmp_path, capsys, findings):
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(findings), encoding="utf-8")
    code, payload = env.post(
        "--findings", str(path), "--head-sha", HEAD_SHA, "--base-sha", env.base_sha, capsys=capsys
    )
    return code, payload, parse_verdict_block(env.opener_state["posted_body"])


def _register(tmp_path, monkeypatch, **overrides):
    import datetime as dt

    overrides.setdefault("pr", 42)
    overrides.setdefault("kind", "false_positive")
    overrides.setdefault("at", dt.date.today())
    path = write_registry(tmp_path, [ruling(**overrides)])
    monkeypatch.setenv(rp.RULINGS_FILE_ENV_VAR, str(path))


def test_a_ruling_for_this_pull_request_clears_the_finding_and_the_fence_carries_the_fingerprint(
    env, tmp_path, monkeypatch, capsys
):
    _register(tmp_path, monkeypatch)

    code, payload, fence = _post(env, tmp_path, capsys, [_finding()])

    assert code == 0
    assert payload["status"] == "clean" and payload["blocking_count"] == 0
    assert fence["verdict_outcome"] == "CLEAN"
    assert [f["fingerprint"] for f in fence["evaluated_findings"]] == [FP]
    assert "cleared by false_positive ruling" in env.opener_state["posted_body"]
    assert "findings_open" not in fence


def test_a_ruling_for_another_pull_request_clears_nothing(env, tmp_path, monkeypatch, capsys):
    _register(tmp_path, monkeypatch, pr=7)

    code, payload, fence = _post(env, tmp_path, capsys, [_finding()])

    assert code == 0
    assert payload["status"] == "blocking" and payload["blocking_count"] == 1
    assert fence["verdict_outcome"] == "BLOCKED"


def test_a_floor_finding_is_not_cleared_and_the_post_says_which_route_clears_it(
    env, tmp_path, monkeypatch, capsys
):
    _register(tmp_path, monkeypatch)

    code, payload, fence = _post(env, tmp_path, capsys, [_finding(FLOOR)])

    assert code == 0 and payload["status"] == "blocking"
    assert fence["verdict_outcome"] == "BLOCKED"
    assert "dispositions entry on the base branch" in env.opener_state["posted_body"]


def test_without_a_registry_the_fence_carries_no_fingerprint_and_nothing_is_cleared(
    env, tmp_path, capsys
):
    code, payload, fence = _post(env, tmp_path, capsys, [_finding()])

    assert code == 0 and payload["status"] == "blocking"
    assert fence["verdict_outcome"] == "BLOCKED"
    assert all("fingerprint" not in f for f in fence["evaluated_findings"])


def test_a_registry_inside_the_repo_path_is_not_used(env, tmp_path, monkeypatch, capsys):
    path = write_registry(env.repo, [ruling(pr=42, kind="false_positive")])
    monkeypatch.setenv(rp.RULINGS_FILE_ENV_VAR, str(path))

    code, payload, fence = _post(env, tmp_path, capsys, [_finding()])

    assert code == 0 and payload["status"] == "blocking"
    assert fence["verdict_outcome"] == "DEGRADED"
    assert any("not a trusted source" in reason for reason in fence["verdict_reasons"])
