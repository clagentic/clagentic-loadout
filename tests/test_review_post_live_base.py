"""`loadout-review post` reads policy at the PR's live base, taken from its own host
fetch, whatever the findings file or --base-sha say."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.review.policy_source import DISPOSITIONS_PATH
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import HEAD_SHA, Env

STALE_BASE = "c" * 40
_MILD = {
    "category": "correctness", "reachable": "yes", "attacker_precondition": "none",
    "impact": {"confidentiality": "none", "integrity": "none", "availability": "none"},
    "durability": "durable",
}
_SERIOUS = {
    "category": "path_traversal", "reachable": "yes", "attacker_precondition": "none",
    "impact": {"confidentiality": "full", "integrity": "none", "availability": "none"},
    "durability": "durable",
}
_DISPOSITION = """\
dispositions:
  - path: "a.py"
    rule_id: R1
    kind: false_positive
    rationale: "checked by hand"
    by: a-maintainer
    at: 2026-09-01
"""


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    environment = Env(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(scratch)
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.delenv(review_cli.RUN_ROOT_ENV_VAR, raising=False)
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return environment


def _bare_array(tmp_path: Path, facts: dict) -> str:
    path = tmp_path / "judged.json"
    path.write_text(
        json.dumps([{"file": "a.py", "line": 1, "rule_id": "R1", "message": "m", "facts": facts}]),
        encoding="utf-8",
    )
    return str(path)


def _object_form(tmp_path: Path, env, capsys, recorded_base: str | None) -> str:
    env.configure(carrier_mode="facts_mild")
    _, payload = env.run(capsys=capsys, local_repo=False, base_files={"README.md": "x\n"})
    document = json.loads(Path(payload["findings_file"]).read_text(encoding="utf-8"))
    document["policy_base_sha"] = recorded_base
    path = tmp_path / "recorded.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def _post(env, capsys, findings: str, *extra: str, base_files=None, **kwargs):
    files = {"README.md": "x\n"} if base_files is None else base_files
    return env.invoke(
        "post", "--findings", findings, "--status", "clean", *extra,
        capsys=capsys, local_repo=False, base_files=files, **kwargs,
    )


def test_a_bare_array_with_only_advisory_findings_posts_clean_without_base_sha(
    env, capsys, tmp_path
):
    findings = _bare_array(tmp_path, _MILD)

    code, out, err = _post(env, capsys, findings, "--head-sha", HEAD_SHA)

    assert code == 0, err
    assert json.loads(out.strip().splitlines()[-1])["verdict_outcome"] == "CLEAN"
    assert env.base_sha in env.opener_state["posted_body"]
    assert "notice" not in err


def test_a_recorded_base_equal_to_the_live_base_gives_no_notice(env, capsys, tmp_path):
    findings = _object_form(tmp_path, env, capsys, env.base_sha)

    code, _, err = _post(env, capsys, findings)

    assert code == 0, err
    assert "notice" not in err


def test_a_recorded_base_that_differs_is_not_used_and_a_notice_names_both(env, capsys, tmp_path):
    findings = _object_form(tmp_path, env, capsys, STALE_BASE)

    code, _, err = _post(env, capsys, findings)

    assert code == 0, err
    assert STALE_BASE in err and env.base_sha in err and "notice" in err
    assert env.base_sha in env.opener_state["posted_body"]
    assert STALE_BASE not in env.opener_state["posted_body"]
    requests = [url for _, url in env.opener_state["requests"]]
    assert not any(STALE_BASE in url for url in requests)


def test_a_base_sha_equal_to_the_live_base_gives_no_notice(env, capsys, tmp_path):
    findings = _bare_array(tmp_path, _MILD)

    code, _, err = _post(env, capsys, findings, "--head-sha", HEAD_SHA, "--base-sha", env.base_sha)

    assert code == 0, err
    assert "notice" not in err


def test_a_base_sha_that_differs_is_not_used_and_a_notice_names_both(env, capsys, tmp_path):
    findings = _bare_array(tmp_path, _MILD)

    code, _, err = _post(env, capsys, findings, "--head-sha", HEAD_SHA, "--base-sha", STALE_BASE)

    assert code == 0, err
    assert "--base-sha" in err and STALE_BASE in err and env.base_sha in err
    assert env.base_sha in env.opener_state["posted_body"]
    assert STALE_BASE not in env.opener_state["posted_body"]


def test_a_malformed_base_sha_is_refused(env, capsys, tmp_path):
    findings = _bare_array(tmp_path, _MILD)

    code, _, err = _post(env, capsys, findings, "--head-sha", HEAD_SHA, "--base-sha", "main")

    assert code == review_cli.EXIT_USAGE == 1
    assert "--base-sha" in err
    assert env.opener_state["posted_body"] is None


def test_a_policy_fetch_failure_at_the_live_base_is_degraded_with_no_fallback(
    env, capsys, tmp_path
):
    findings = _bare_array(tmp_path, _MILD)

    code, _, err = _post(
        env, capsys, findings, "--head-sha", HEAD_SHA, "--base-sha", env.base_sha,
        base_files={DISPOSITIONS_PATH: _DISPOSITION}, contents_http=500,
    )

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert "DEGRADED" in err and "HTTP 500" in err
    assert env.opener_state["posted_body"] is None


def test_a_disposition_at_the_live_base_demotes_a_bare_array_finding(env, capsys, tmp_path):
    findings = _bare_array(tmp_path, _SERIOUS)

    code, _, err = _post(
        env, capsys, findings, "--head-sha", HEAD_SHA,
        base_files={"README.md": "x\n", DISPOSITIONS_PATH: _DISPOSITION},
    )

    assert code == 0, err
    assert "false_positive" in env.opener_state["posted_body"]
