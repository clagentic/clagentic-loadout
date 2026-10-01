"""Behavioural tests for `loadout-review post` and the run -> post hand-off:
the comment body is built entirely by the tool from structured findings, the
fence head equals the API head, and the landed comment is read back."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import HEAD_SHA, Env


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    environment = Env(tmp_path)
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    # Keep the review-post role check off any real user config.
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return environment


def _write_findings(path: Path, findings: list, **overrides) -> Path:
    document = {
        "owner": "some-owner",
        "repo": "some-repo",
        "pr_number": 42,
        "head_sha": HEAD_SHA,
        "findings": findings,
    }
    document.update(overrides)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


_NIT = {"file": "a.py", "line": 3, "rule_id": "R1", "severity": "nit", "message": "stub finding"}
_BLOCKING = {"file": "a.py", "line": 4, "rule_id": "R2", "severity": "blocking", "message": "bad"}


def test_run_then_post_is_two_commands_and_lands_a_verified_fence(env, tmp_path, capsys):
    env.configure()
    run_code, run_payload = env.run(capsys=capsys)
    assert run_code == 0

    code, payload = env.post(
        "--findings", run_payload["findings_file"], "--status", "blocking", capsys=capsys
    )

    assert code == 0
    assert payload["result"] == "posted"
    assert payload["verified_by_login"] == "reviewer"
    assert payload["verdict_block_verified"] is True
    posted = env.opener_state["posted_body"]
    assert "a.py:1 [R1] (nit) stub finding" in posted
    assert "```review-result" in posted
    assert f'"head_sha": "{HEAD_SHA}"' in posted
    assert '"reviewer": "reviewer"' in posted
    # The staged body pair was consumed by the post.
    assert list((tmp_path / "tmp" / "clagentic-loadout").glob("body.reviewer*")) == []


def test_posted_body_has_no_caller_prose(env, tmp_path, capsys):
    findings = _write_findings(tmp_path / "f.json", [_NIT])

    code, _ = env.post("--findings", str(findings), "--status", "blocking", capsys=capsys)

    assert code == 0
    posted = env.opener_state["posted_body"]
    assert posted.splitlines()[0] == "REVIEWER — blocking (1 finding(s))"


def test_stale_head_is_refused_and_nothing_is_posted(env, tmp_path, capsys):
    findings = _write_findings(tmp_path / "f.json", [_NIT], head_sha="c" * 40)

    code, _ = env.post("--findings", str(findings), "--status", "blocking", capsys=capsys)

    assert code == review_cli.EXIT_STALE_HEAD
    assert env.opener_state["posted_body"] is None

def test_clean_status_with_a_blocking_finding_is_refused(env, tmp_path, capsys):
    findings = _write_findings(tmp_path / "f.json", [_BLOCKING])

    code, _ = env.post("--findings", str(findings), "--status", "clean", capsys=capsys)

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert env.opener_state["posted_body"] is None


def test_findings_for_another_pr_are_refused(env, tmp_path, capsys):
    findings = _write_findings(tmp_path / "f.json", [], pr_number=7)

    code, _ = env.post("--findings", str(findings), "--status", "clean", capsys=capsys)

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert env.opener_state["posted_body"] is None


def test_bare_findings_array_needs_a_head_sha(env, tmp_path, capsys):
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps([_NIT]), encoding="utf-8")

    refused, _ = env.post("--findings", str(bare), "--status", "blocking", capsys=capsys)
    accepted, payload = env.post(
        "--findings", str(bare), "--status", "blocking", "--head-sha", HEAD_SHA, capsys=capsys
    )

    assert refused == review_cli.EXIT_FINDINGS_INVALID
    assert accepted == 0
    assert payload["head_sha"] == HEAD_SHA


def test_a_failing_review_post_path_is_reported_as_post_failed(env, tmp_path, capsys, monkeypatch):
    findings = _write_findings(tmp_path / "f.json", [])
    monkeypatch.setattr(review_cli.review_post_verb, "main", lambda argv, **kwargs: 9)

    code, payload = env.post("--findings", str(findings), "--status", "clean", capsys=capsys)

    assert code == review_cli.EXIT_POST_FAILED
    assert payload == {"result": "post_failed", "review_post_exit_code": 9}


@pytest.mark.parametrize("severity", ["Blocking", " blocking", "BLOCKING "])
def test_blocking_severity_is_normalized_before_the_contradiction_check(
    env, tmp_path, capsys, severity
):
    findings = _write_findings(tmp_path / "f.json", [{**_BLOCKING, "severity": severity}])

    code, _ = env.post("--findings", str(findings), "--status", "clean", capsys=capsys)

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert env.opener_state["posted_body"] is None


@pytest.mark.parametrize("bad", ["urgent", 3, ""])
def test_unknown_severity_is_refused(env, tmp_path, capsys, bad):
    findings = _write_findings(tmp_path / "f.json", [{**_NIT, "severity": bad}])

    code, _ = env.post("--findings", str(findings), "--status", "blocking", capsys=capsys)

    assert code == review_cli.EXIT_FINDINGS_INVALID


def test_non_positive_line_is_refused(env, tmp_path, capsys):
    findings = _write_findings(tmp_path / "f.json", [{**_NIT, "line": 0}])

    code, _ = env.post("--findings", str(findings), "--status", "blocking", capsys=capsys)

    assert code == review_cli.EXIT_FINDINGS_INVALID


def test_unparseable_review_post_output_is_post_failed_not_posted(
    env, tmp_path, capsys, monkeypatch
):
    findings = _write_findings(tmp_path / "f.json", [])

    def fake_main(argv, **kwargs):
        print("this is not json")
        return 0

    monkeypatch.setattr(review_cli.review_post_verb, "main", fake_main)

    code, payload = env.post("--findings", str(findings), "--status", "clean", capsys=capsys)

    assert code == review_cli.EXIT_POST_FAILED
    assert payload["result"] == "post_failed"


def test_failed_review_post_keeps_its_stdout_for_diagnosis(env, tmp_path, capsys, monkeypatch):
    findings = _write_findings(tmp_path / "f.json", [])

    def fake_main(argv, **kwargs):
        print("inner-failure-detail")
        return 9

    monkeypatch.setattr(review_cli.review_post_verb, "main", fake_main)
    capsys.readouterr()
    code = env._invoke("post", ["--findings", str(findings), "--status", "clean"])
    captured = capsys.readouterr()

    assert code == review_cli.EXIT_POST_FAILED
    assert "inner-failure-detail" in captured.err
    assert "inner-failure-detail" not in captured.out


def test_post_passes_the_verdict_route_flags_to_review_post(env, tmp_path, capsys, monkeypatch):
    findings = _write_findings(tmp_path / "f.json", [])
    seen: dict = {}

    def fake_main(argv, **kwargs):
        seen["argv"] = argv
        print(json.dumps({"verified_id": 1, "verdict_block_verified": True}))
        return 0

    monkeypatch.setattr(review_cli.review_post_verb, "main", fake_main)

    code, _ = env.post("--findings", str(findings), "--status", "clean", capsys=capsys)

    assert code == 0
    argv = seen["argv"]
    assert "--verdict-findings" in argv
    assert argv[argv.index("--verdict-head-sha") + 1] == HEAD_SHA
    assert "--body-env" in argv
