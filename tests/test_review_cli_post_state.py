"""`loadout-review post --state-file`: structured findings state reaches the
tool-built fence, and a bad state file fails before anything is posted."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.merge.verdict import parse_verdict_block
from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import HEAD_SHA, Env


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    environment = Env(tmp_path)
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return environment


def _findings(tmp_path):
    path = tmp_path / "f.json"
    path.write_text(
        json.dumps(
            {
                "owner": "some-owner",
                "repo": "some-repo",
                "pr_number": 42,
                "head_sha": HEAD_SHA,
                "findings": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _state_file(tmp_path, document):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_state_file_lands_inside_the_fence(env, tmp_path, capsys):
    state = {
        "cleared_claims": [{"id": "F1", "head": HEAD_SHA, "evidence": "guard added"}],
        "scanners_run": [{"scanner": "alpha", "status": "not_invoked", "reason": "judgment-only pass"}],
    }
    code, payload = env.post(
        "--findings", str(_findings(tmp_path)), "--status", "clean",
        "--state-file", str(_state_file(tmp_path, state)), capsys=capsys,
    )
    assert code == 0
    assert payload["verdict_block_verified"] is True
    fence = parse_verdict_block(env.opener_state["posted_body"])
    assert fence["cleared_claims"] == state["cleared_claims"]
    assert fence["scanners_run"] == state["scanners_run"]
    assert fence["fence_schema_version"] == 2
    assert "guard added" not in env.opener_state["posted_body"].split("```review-result")[0]


@pytest.mark.parametrize(
    "document",
    [
        {"findings_open": [{"id": "F1", "rule_id": "R1", "head": HEAD_SHA}]},
        {"cleared_claims": [{"id": "F1", "head": "c" * 40, "evidence": "x"}]},
        {"bogus": 1},
        ["not", "an", "object"],
    ],
    ids=["clean-with-open", "claim-at-other-head", "unknown-field", "not-an-object"],
)
def test_a_bad_state_file_is_refused_and_nothing_is_posted(env, tmp_path, capsys, document):
    code, _ = env.post(
        "--findings", str(_findings(tmp_path)), "--status", "clean",
        "--state-file", str(_state_file(tmp_path, document)), capsys=capsys,
    )
    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert env.opener_state["posted_body"] is None


def test_an_unreadable_state_file_is_refused(env, tmp_path, capsys):
    code, _ = env.post(
        "--findings", str(_findings(tmp_path)), "--status", "clean",
        "--state-file", str(tmp_path / "missing.json"), capsys=capsys,
    )
    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert env.opener_state["posted_body"] is None
