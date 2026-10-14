"""A candidate is either a finding or dropped, never both: `loadout-review post`
and `review-post --verdict-findings` honour the drop, removing the candidate from
the findings before anything is evaluated, with one stderr notice each."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.review.dropped_overlap import drop_overlapping
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import HEAD_SHA, Env
from tests._support.review_verb import (
    RefusingTokenProvider,
    run_main,
)

HEAD = "a" * 40

NOTICE = (
    "loadout-review: notice: candidate a.py:3:R1 is in both --findings and --dropped; "
    "the drop is honoured"
)


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
    """(exit code, result object, stderr) of one post."""
    code, out, err = env.invoke(
        "post",
        "--findings", str(_write(tmp_path, "findings.json", findings)),
        "--dropped", str(_write(tmp_path, "dropped.json", dropped)),
        "--head-sha", HEAD_SHA, "--base-sha", env.base_sha, "--status", status,
        capsys=capsys,
    )
    out = out.strip()
    return code, json.loads(out.splitlines()[-1]) if out else {}, err


def test_overlap_by_id_drops_the_finding_and_keeps_it_listed(env, tmp_path, capsys):
    code, payload, err = _post(env, tmp_path, capsys, [_finding()], [_dropped()])

    assert code == 0
    assert payload["finding_count"] == 0
    assert payload["blocking_count"] == 0
    assert payload["open_finding_ids"] == []
    assert payload["dropped_count"] == 1
    assert "the caller validates it" in env.opener_state["posted_body"]
    assert NOTICE in err


def test_overlap_in_a_different_rule_or_line_is_not_an_overlap(env, tmp_path, capsys):
    code, payload, err = _post(
        env, tmp_path, capsys,
        [_finding(), _finding(rule_id="R2")], [_dropped(line=4), _dropped(rule_id="R3")],
    )

    assert code == 0
    assert payload["finding_count"] == 2
    assert "notice: candidate" not in err


def test_partial_overlap_removes_only_the_overlapping_candidate(env, tmp_path, capsys):
    code, payload, err = _post(
        env, tmp_path, capsys, [_finding(), _finding(line=8)], [_dropped()]
    )

    assert code == 0
    assert payload["finding_count"] == 1
    assert payload["blocking_count"] == 1
    assert err.count("notice: candidate") == 1
    assert "a.py:8:R1" not in err


def test_no_overlap_posts_every_finding_with_no_notice(env, tmp_path, capsys):
    code, payload, err = _post(env, tmp_path, capsys, [_finding()], [_dropped(line=9)])

    assert code == 0
    assert payload["finding_count"] == 1
    assert payload["dropped_count"] == 1
    assert "notice: candidate" not in err


def test_a_dropped_candidate_the_caller_ruled_resolved_posts_without_a_notice(
    env, tmp_path, capsys
):
    resolved = _write(tmp_path, "resolved.json", [{"id": "a.py:3:R1", "reason": "fixed"}])
    code, _, err = env.invoke(
        "post",
        "--findings", str(_write(tmp_path, "findings.json", [_finding()])),
        "--dropped", str(_write(tmp_path, "dropped.json", [_dropped()])),
        "--resolved-findings", str(resolved),
        "--head-sha", HEAD_SHA, "--base-sha", env.base_sha, "--status", "blocking",
        capsys=capsys,
    )

    assert code == 0
    assert "notice: candidate" not in err


def test_drop_overlapping_returns_every_finding_when_nothing_overlaps():
    findings = [_finding()]

    assert drop_overlapping(findings, [_dropped(line=9)]) == findings
    assert drop_overlapping(findings, []) == findings


def test_drop_overlapping_keeps_order_and_ignores_entries_that_are_not_objects(capsys):
    findings = [_finding(line=5), "x", _finding(), _finding(line=6)]

    kept = drop_overlapping(findings, [None, _dropped(), _dropped(line=5)])

    assert kept == ["x", _finding(line=6)]
    assert capsys.readouterr().err.count("notice: candidate") == 2


_ARGV = [
    "--caller", "reviewer", "--platform", "github", "--verdict-findings",
    "--verdict-head-sha", HEAD, "some-owner/some-repo", "42",
]


def test_review_post_removes_a_dropped_candidate_from_the_findings(monkeypatch, capsys):
    stdin = json.dumps(
        {
            "review_status": "blocking",
            "findings": [{"file": "a.py", "line": 3, "rule_id": "R1", "message": "m"}],
            "dropped": [_dropped()],
        }
    ).encode()
    # The overlap no longer stops the post as a usage error: it proceeds to the
    # token provider, whose refusal stands in for the host call.
    with pytest.raises(AssertionError, match="token provider must not be called"):
        run_main(
            _ARGV, stdin_bytes=stdin, token_provider=RefusingTokenProvider(), opener=None,
            monkeypatch=monkeypatch,
        )

    assert "review-post: notice: candidate a.py:3:R1 is in both" in capsys.readouterr().err
