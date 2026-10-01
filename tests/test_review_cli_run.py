"""Behavioural tests for `loadout-review run`: the whole acquire -> chunk ->
carrier -> validate -> merge path against a fake host API and stub engines,
with a synthetic role name and no real deployment identity."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.review.findings_contract import FORMAT_REPROMPT, OUTPUT_CONTRACT
from tests._review_cli_support import (
    HEAD_SHA,
    Env,
    github_opener,
    identity_provider,
    make_diff,
    prompts,
    set_mode,
    write_profile_config,
)


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    environment = Env(tmp_path)
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    return environment


def _chunk_records(payload: dict) -> dict:
    return json.loads(Path(payload["findings_file"]).read_text(encoding="utf-8"))


def test_complete_run_reviews_the_api_diff_and_binds_the_api_head(env, capsys):
    env.configure()
    code, payload = env.run(capsys=capsys)

    assert code == 0
    assert payload["result"] == "complete"
    findings = _chunk_records(payload)
    assert findings["head_sha"] == HEAD_SHA
    assert findings["findings"][0]["file"] == "a.py"
    sent = prompts(env.stubs, "carrier")
    assert len(sent) == 1
    assert "+line 1 of a.py" in sent[0]
    assert sent[0].rstrip().endswith(OUTPUT_CONTRACT.rstrip())
    assert [stage["stage"] for stage in payload["stages"]][:2] == ["acquired", "chunked"]
    assert payload["stages"][-1]["stage"] == "merged"


def test_stale_local_checkout_does_not_change_the_reviewed_diff(env, capsys, monkeypatch):
    subprocess.run(["git", "init", "-q", str(env.repo)], check=True)
    (env.repo / "unrelated.txt").write_text("local-only content\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(env.repo), "add", "unrelated.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(env.repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "-q", "-m", "local"],
        check=True,
    )
    monkeypatch.chdir(env.repo)
    env.configure()

    code, payload = env.run(capsys=capsys)

    assert code == 0
    sent = prompts(env.stubs, "carrier")[0]
    assert "+line 1 of a.py" in sent
    assert "local-only content" not in sent
    assert not (env.repo / ".git" / "worktrees").exists()
    assert all(method == "GET" for method, _ in env.opener_state["requests"])


def test_nothing_is_written_outside_the_run_dir(env, capsys):
    env.configure()
    before_repo = sorted(p.name for p in env.repo.iterdir())
    before_cfg = sorted(p.name for p in env.cfg.iterdir())

    code, payload = env.run(capsys=capsys)

    assert code == 0
    assert sorted(p.name for p in env.repo.iterdir()) == before_repo
    assert sorted(p.name for p in env.cfg.iterdir()) == before_cfg
    run_dir = Path(payload["run_dir"])
    assert run_dir == env.runs / "some-owner__some-repo" / "pr-42" / HEAD_SHA[:12]
    assert run_dir.is_dir()


def test_out_wins_over_the_default_run_dir(env, capsys, tmp_path):
    env.configure()
    out = tmp_path / "chosen"

    code, payload = env.run("--out", str(out), capsys=capsys)

    assert code == 0
    assert Path(payload["run_dir"]) == out
    assert (out / "findings.json").is_file()
    assert not env.runs.exists()


def test_prose_once_then_array_completes_with_a_format_reprompt(env, capsys):
    env.configure(carrier_mode="prose_then_array")

    code, payload = env.run(capsys=capsys)

    assert code == 0
    sent = prompts(env.stubs, "carrier")
    assert len(sent) == 2
    assert sent[1].endswith(FORMAT_REPROMPT)
    assert _chunk_records(payload)["findings"][0]["rule_id"] == "R1"


def test_prose_twice_blocks_with_the_reply_excerpt(env, capsys):
    env.configure(carrier_mode="prose")

    code, payload = env.run(capsys=capsys)

    assert code == 20
    assert payload["result"] == "blocked"
    assert payload["stage"] == "chunk-1"
    assert payload["reason"] == "FALLBACK_OUTPUT_INVALID"
    assert "seems fine" in payload["reply_excerpt"]


def test_timeout_once_then_success_completes(env, capsys):
    env.configure(carrier_mode="timeout_then_array", timeout_seconds=1)

    code, payload = env.run(capsys=capsys)

    assert code == 0
    assert len(prompts(env.stubs, "carrier")) == 2


def test_absent_carrier_runs_the_fallback_for_every_chunk(env, capsys):
    env.diff = make_diff({"a.py": 6, "b.py": 6, "c.py": 6})
    env.configure(carrier_mode="exit127", fallback_mode="array", chunk_lines=14)

    code, payload = env.run(capsys=capsys)

    assert code == 0
    document = _chunk_records(payload)
    assert document["chunk_count"] == 3
    assert [chunk["engine"] for chunk in document["chunks"]] == ["fallback"] * 3
    assert len(prompts(env.stubs, "fallback")) == 3
    assert len(prompts(env.stubs, "carrier")) == 3


def test_missing_executable_without_a_fallback_is_model_unavailable(env, capsys):
    write_profile_config(env.cfg, carrier=["/nonexistent/engine-binary"])

    code, payload = env.run(capsys=capsys)

    assert code == 20
    assert payload["reason"] == "MODEL_UNAVAILABLE"
    assert payload["stage"] == "chunk-1"


def test_stall_is_persisted_and_resumes_before_blocking(env, capsys):
    env.configure(carrier_mode="hang", timeout_seconds=0.5, max_attempts=2)

    first_code, first = env.run(capsys=capsys)
    second_code, second = env.run(capsys=capsys)

    assert first_code == 10
    assert first["result"] == "resume"
    assert first["pending_chunks"] == [1]
    assert second_code == 20
    assert second["reason"] == "CHUNK_TIMEOUT"
    assert "retries exhausted" in second["detail"]
    assert "stall-diagnostic-on-stderr" in second["stderr_excerpt"]
    assert "partial-reply-before-stall" in second["reply_excerpt"]


def test_resume_retries_only_the_stalled_chunk(env, capsys):
    env.diff = make_diff({"a.py": 6, "STALL_ME.py": 6, "c.py": 6})
    env.configure(carrier_mode="stall_marker", timeout_seconds=0.5, chunk_lines=14)

    first_code, first = env.run(capsys=capsys)
    second_code, second = env.run(capsys=capsys)

    assert first_code == 10
    assert first["pending_chunks"] == [2]
    assert second_code == 0
    sent = prompts(env.stubs, "carrier")
    assert sum("+line 1 of a.py" in p for p in sent) == 1
    assert sum("+line 1 of c.py" in p for p in sent) == 1
    assert sum("STALL_ME" in p for p in sent) == 3
    resumed = [s for s in second["stages"] if s.get("resumed") == "yes"]
    assert {s["stage"] for s in resumed} == {"chunk-1", "chunk-3"}


def test_changed_rulebook_does_not_reuse_cached_chunks(env, capsys, tmp_path):
    rulebook = tmp_path / "rulebook.md"
    rulebook.write_text("rule one", encoding="utf-8")
    env.configure(rulebook=str(rulebook))
    env.run(capsys=capsys)
    rulebook.write_text("rule two", encoding="utf-8")

    code, _ = env.run(capsys=capsys)

    assert code == 0
    sent = prompts(env.stubs, "carrier")
    assert len(sent) == 2
    assert "rule one" in sent[0] and "rule two" in sent[1]


def test_empty_diff_is_blocked_not_clean(env, capsys):
    env.diff = ""
    env.configure()

    code, payload = env.run(capsys=capsys)

    assert code == 20
    assert payload["reason"] == "DIFF_EMPTY"
    assert prompts(env.stubs, "carrier") == []


def test_unreadable_head_sha_from_the_api_is_blocked_at_acquired(env, capsys):
    env.configure()
    capsys.readouterr()
    code = review_cli.main(
        ["run", "--caller", "reviewer", "--repo", "some-owner/some-repo", "--pr", "42",
         "--platform", "github", "--repo-path", str(env.repo)],
        token_provider=env.token_provider,
        opener=github_opener(diff=env.diff, head_sha="abc123"),
        identity_provider=identity_provider(),
        config_root=env.cfg,
        run_root=env.runs,
    )
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert code == 20
    assert payload["stage"] == "acquired"
    assert payload["reason"] == "ACQUIRE_INVALID"


def test_missing_profile_exits_profile_invalid_before_any_mint(env, capsys):
    env.cfg.mkdir()

    code, _ = env.run(capsys=capsys)

    assert code == review_cli.EXIT_PROFILE_INVALID
    assert env.token_provider.resolved_for == []


def test_mismatched_attested_identity_is_refused_before_any_mint(env, capsys):
    env.configure()
    code = review_cli.main(
        ["run", "--caller", "reviewer", "--repo", "some-owner/some-repo", "--pr", "42",
         "--platform", "github"],
        token_provider=env.token_provider,
        opener=github_opener(diff=env.diff),
        identity_provider=identity_provider("someone-else"),
        config_root=env.cfg,
        run_root=env.runs,
    )

    assert code == review_cli.EXIT_CALLER_INVOKER_MISMATCH
    assert env.token_provider.resolved_for == []


def test_stage_status_changes_are_machine_readable_on_stderr(env, capsys):
    env.configure()
    env.run(capsys=capsys)
    # env.run drains capsys; re-run to read stderr from a fresh invocation.
    set_mode(env.stubs, "carrier", "array")
    review_cli.main(
        ["run", "--caller", "reviewer", "--repo", "some-owner/some-repo", "--pr", "42",
         "--platform", "github", "--repo-path", str(env.repo)],
        token_provider=env.token_provider,
        opener=github_opener(diff=env.diff),
        identity_provider=identity_provider(),
        config_root=env.cfg,
        run_root=env.runs,
    )
    err = capsys.readouterr().err
    assert "loadout-review: stage=acquired status=ok" in err
    assert "loadout-review: stage=chunked status=ok chunk_count=1" in err
    assert "loadout-review: stage=chunk-1 status=ok resumed=yes" in err
    assert "loadout-review: stage=merged status=ok" in err
