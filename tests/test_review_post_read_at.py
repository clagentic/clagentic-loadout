"""`loadout-review post --dropped` records the revision each drop was read at and
whether that is the reviewed head: in the fence, in the comment and on stderr, and
never in the verdict or the exit code."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clagentic_loadout.merge.verdict import parse_verdict_block
from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import HEAD_SHA, Env, github_opener, identity_provider

OTHER = "c" * 40


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")


def _drop(line: int, **extra) -> dict:
    return {
        "file": "b.py", "line": line, "rule_id": "R2", "message": "maybe a race",
        "reason": "the lock is held by the caller", **extra,
    }


def _post(root: Path, capsys, dropped: list, *, repo: str = "some-owner/some-repo"):
    """One post of an empty clean review with *dropped*, in its own deployment;
    returns (exit code, result object, stderr, fence, comment prose)."""
    root.mkdir(parents=True, exist_ok=True)
    env = Env(root)
    findings = root / "findings.json"
    findings.write_text("[]", encoding="utf-8")
    drops = root / "dropped.json"
    drops.write_text(json.dumps(dropped), encoding="utf-8")
    capsys.readouterr()
    code = review_cli.main(
        [
            "post", "--caller", "reviewer", "--repo", repo, "--pr", "42", "--platform", "github",
            "--repo-path", str(env.repo), "--findings", str(findings), "--head-sha", HEAD_SHA,
            "--base-sha", env.base_sha, "--status", "clean", "--dropped", str(drops),
        ],
        token_provider=env.token_provider,
        opener=github_opener(diff=env.diff, state=env.opener_state, base_sha=env.base_sha),
        identity_provider=identity_provider("reviewer"),
        config_root=env.cfg,
        run_root=env.runs,
    )
    captured = capsys.readouterr()
    lines = [line for line in captured.out.strip().splitlines() if line.startswith("{")]
    posted = env.opener_state["posted_body"]
    return (
        code,
        json.loads(lines[-1]) if lines else {},
        captured.err,
        parse_verdict_block(posted) if posted else {},
        posted.split("```review-result")[0] if posted else "",
    )


def test_a_post_records_read_at_and_proven_and_marks_the_unproven_without_changing_the_verdict(
    tmp_path, capsys
):
    mixed = [_drop(7, read_at=HEAD_SHA), _drop(8, read_at=OTHER), _drop(9)]
    code, result, err, fence, prose = _post(tmp_path / "mixed", capsys, mixed)
    all_proven = [_drop(line, read_at=HEAD_SHA) for line in (7, 8, 9)]
    ref_code, ref_result, ref_err, ref_fence, _ = _post(tmp_path / "proven", capsys, all_proven)

    assert code == 0
    assert [(e["read_at"], e["proven"]) for e in fence["dropped"]] == [
        (HEAD_SHA, True), (OTHER, False), (None, False),
    ]
    assert fence["fence_schema_version"] == 10
    lines = {line.split(" ")[1]: line for line in prose.splitlines() if line.startswith("- b.py")}
    assert "[unproven" not in lines["b.py:7"]
    assert f"[unproven: read at {OTHER}]" in lines["b.py:8"]
    assert "[unproven: no read_at]" in lines["b.py:9"]
    assert "b.py:7:R2" not in err
    assert f"b.py:8:R2 is unproven (read at {OTHER}" in err
    assert "b.py:9:R2 is unproven (no read_at" in err
    # The post is the one it would be with every drop proven.
    assert ref_err.count("is unproven") == 0
    assert (ref_code, ref_result["status"], ref_result["verdict_outcome"]) == (
        code, result["status"], result["verdict_outcome"],
    )
    assert ref_fence["verdict_outcome"] == fence["verdict_outcome"]
    assert result["dropped_count"] == 3


@pytest.mark.parametrize(
    "given,recorded",
    [("not-a-sha", "not-a-sha"), (OTHER[:12], OTHER[:12]), (12345, "12345"), ("   ", None)],
    ids=["text", "short-sha", "non-string", "blank"],
)
def test_a_read_at_that_is_not_a_full_sha_is_recorded_as_given_and_unproven(
    tmp_path, capsys, given, recorded
):
    code, _, err, fence, _ = _post(tmp_path, capsys, [_drop(7, read_at=given)])

    assert code == 0
    assert fence["dropped"][0]["read_at"] == recorded and fence["dropped"][0]["proven"] is False
    assert "b.py:7:R2 is unproven" in err
    if recorded is not None:
        assert recorded in err


def test_a_proven_flag_typed_into_the_dropped_file_is_replaced_by_the_computed_one(tmp_path, capsys):
    code, _, _, fence, _ = _post(tmp_path, capsys, [_drop(7, read_at=OTHER, proven=True)])

    assert code == 0 and fence["dropped"][0]["proven"] is False


def test_a_post_for_a_repository_whose_name_starts_with_a_dot_records_the_drops(tmp_path, capsys):
    code, result, _, fence, _ = _post(
        tmp_path, capsys, [_drop(7, read_at=HEAD_SHA)], repo="some-owner/.github"
    )

    assert code == 0 and result["result"] == "posted"
    assert fence["dropped"][0]["proven"] is True
