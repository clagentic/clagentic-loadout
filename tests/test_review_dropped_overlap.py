"""A candidate is either a finding or dropped, never both: `loadout-review post`
and `review-post --verdict-findings` refuse the overlap before anything is posted."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.review import verb
from clagentic_loadout.review.dropped_overlap import overlapping_ids
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import HEAD_SHA, Env
from tests._support.review_verb import (
    RefusingTokenProvider,
    run_main,
)

HEAD = "a" * 40


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    environment = Env(tmp_path)
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return environment


def _write(tmp_path, name, document):
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _finding(**extra):
    return {
        "file": "a.py", "line": 3, "rule_id": "R1", "severity": "blocking",
        "message": "unchecked path", **extra,
    }


def _dropped(**extra):
    return {
        "file": "a.py", "line": 3, "rule_id": "R1", "message": "unchecked path",
        "reason": "the caller validates it", **extra,
    }


def _post(env, tmp_path, capsys, findings, dropped, status="blocking"):
    return env.post(
        "--findings", str(_write(tmp_path, "findings.json", findings)),
        "--dropped", str(_write(tmp_path, "dropped.json", dropped)),
        "--head-sha", HEAD_SHA, "--base-sha", env.base_sha, "--status", status,
        capsys=capsys,
    )


def test_a_candidate_in_both_lists_is_refused_with_its_id_and_nothing_is_posted(
    env, tmp_path, capsys
):
    code, _, err = env.invoke(
        "post",
        "--findings", str(_write(tmp_path, "findings.json", [_finding()])),
        "--dropped", str(_write(tmp_path, "dropped.json", [_dropped()])),
        "--head-sha", HEAD_SHA, "--base-sha", env.base_sha, "--status", "blocking",
        capsys=capsys,
    )

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert env.opener_state["posted_body"] is None
    assert "a.py:3:R1" in err


def test_distinct_candidates_still_post(env, tmp_path, capsys):
    code, payload = _post(env, tmp_path, capsys, [_finding()], [_dropped(line=9)])

    assert code == 0
    assert payload["dropped_count"] == 1


def test_a_dropped_candidate_the_caller_ruled_resolved_is_not_a_conflict(env, tmp_path, capsys):
    resolved = _write(tmp_path, "resolved.json", [{"id": "a.py:3:R1", "reason": "fixed"}])
    code, _ = env.post(
        "--findings", str(_write(tmp_path, "findings.json", [_finding()])),
        "--dropped", str(_write(tmp_path, "dropped.json", [_dropped()])),
        "--resolved-findings", str(resolved),
        "--head-sha", HEAD_SHA, "--base-sha", env.base_sha, "--status", "blocking",
        capsys=capsys,
    )

    assert code == 0


def test_overlapping_ids_reports_each_shared_id_once_in_finding_order():
    findings = [_finding(line=5), _finding(), _finding()]
    dropped = [_dropped(), _dropped(line=5), _dropped(line=8)]

    assert overlapping_ids(findings, dropped) == ["a.py:5:R1", "a.py:3:R1"]


def test_overlapping_ids_ignores_entries_that_are_not_objects():
    assert overlapping_ids(["x", _finding()], [None, _dropped()]) == ["a.py:3:R1"]


_ARGV = [
    "--caller", "reviewer", "--platform", "github", "--verdict-findings",
    "--verdict-head-sha", HEAD, "some-owner/some-repo", "42",
]


def test_review_post_refuses_findings_and_dropped_sharing_an_id(monkeypatch, capsys):
    stdin = json.dumps(
        {
            "review_status": "blocking",
            "findings": [{"file": "a.py", "line": 3, "rule_id": "R1", "message": "m"}],
            "dropped": [_dropped()],
        }
    ).encode()
    code = run_main(
        _ARGV, stdin_bytes=stdin, token_provider=RefusingTokenProvider(), opener=None,
        monkeypatch=monkeypatch,
    )

    assert code == verb.EXIT_VERDICT_BLOCK_USAGE
    assert "a.py:3:R1" in capsys.readouterr().err
