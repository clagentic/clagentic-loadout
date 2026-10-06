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
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import (
    HEAD_SHA,
    Env,
    make_diff,
    prompts,
    set_mode,
    write_profile_config,
)


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    environment = Env(tmp_path)
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.delenv(review_cli.RUN_ROOT_ENV_VAR, raising=False)
    # Keep any config lookup off the real user's config.
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
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


def _snapshot(root: Path, *, skip: tuple[Path, ...]) -> list[str]:
    """Every path under *root* (recursively) except under the *skip* trees."""
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if not any(path == s or s in path.parents for s in skip)
    )


def test_nothing_is_written_outside_the_run_dir(env, capsys, tmp_path):
    env.configure()
    # The stub engines log their own prompts under stubs/; that is the test
    # fixture writing, not the verb.
    skip = (env.runs, env.stubs)
    before = _snapshot(tmp_path, skip=skip)

    code, payload = env.run(capsys=capsys)

    assert code == 0
    assert _snapshot(tmp_path, skip=skip) == before
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
    assert payload["reason"] == "OUTPUT_INVALID"
    assert "seems fine" in payload["reply_excerpt"]


def test_timeout_once_then_success_completes(env, capsys):
    # The margin is for the second call, which does not stall: it must not time
    # out on a loaded runner.
    env.configure(carrier_mode="timeout_then_array", timeout_seconds=3)

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


def test_carrier_going_absent_on_the_format_reprompt_still_uses_the_fallback(env, capsys):
    env.configure(carrier_mode="prose_then_exit127", fallback_mode="array")

    code, payload = env.run(capsys=capsys)

    assert code == 0
    document = _chunk_records(payload)
    assert [chunk["engine"] for chunk in document["chunks"]] == ["fallback"]
    assert len(prompts(env.stubs, "carrier")) == 2
    assert len(prompts(env.stubs, "fallback")) == 1


def test_carrier_going_absent_on_the_format_reprompt_without_a_fallback_is_unavailable(
    env, capsys
):
    env.configure(carrier_mode="prose_then_exit127")

    code, payload = env.run(capsys=capsys)

    assert code == 20
    assert payload["reason"] == "MODEL_UNAVAILABLE"


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
    # Non-stalling chunks share this timeout, so it carries a margin for a
    # loaded runner.
    env.configure(carrier_mode="stall_marker", timeout_seconds=3, chunk_lines=14)

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
    first_code, _ = env.run(capsys=capsys)
    assert first_code == 0
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

    code, payload = env.run(capsys=capsys, head_sha="abc123")

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
    code = env.main("run", identity="someone-else")

    assert code == review_cli.EXIT_CALLER_INVOKER_MISMATCH
    assert env.token_provider.resolved_for == []


def test_stage_status_changes_are_machine_readable_on_stderr(env, capsys):
    env.configure()

    _, _, fresh_err = env.invoke("run", capsys=capsys)
    _, _, resumed_err = env.invoke("run", capsys=capsys)

    assert "loadout-review: stage=acquired status=ok" in fresh_err
    assert "loadout-review: stage=chunked status=ok chunk_count=1" in fresh_err
    assert "loadout-review: stage=chunk-1 status=ok nonce=" in fresh_err
    assert "resumed=yes" not in fresh_err
    assert "loadout-review: stage=merged status=ok" in fresh_err
    assert "loadout-review: stage=chunk-1 status=ok resumed=yes" in resumed_err


def test_a_terminal_failure_is_not_sticky_and_is_retried_on_reinvoke(env, capsys):
    env.configure(carrier_mode="prose")

    first_code, first = env.run(capsys=capsys)
    sent_after_first = len(prompts(env.stubs, "carrier"))
    second_code, second = env.run(capsys=capsys)

    assert (first_code, second_code) == (20, 20)
    assert second["reason"] == "OUTPUT_INVALID"
    assert len(prompts(env.stubs, "carrier")) > sent_after_first
    assert "state_dir" not in second
    assert not list(Path(first["run_dir"]).glob("state-*/result-*.json"))


def test_model_unavailable_then_engine_appears_completes_on_reinvoke(env, capsys):
    env.configure(carrier_mode="exit127")

    first_code, first = env.run(capsys=capsys)
    set_mode(env.stubs, "carrier", "array")
    second_code, second = env.run(capsys=capsys)

    assert first_code == 20
    assert first["reason"] == "MODEL_UNAVAILABLE"
    assert second_code == 0
    assert second["result"] == "complete"


def test_exit_20_then_reinvoke_gives_a_fresh_budget_and_reuses_ok_chunks(env, capsys):
    env.diff = make_diff({"a.py": 6, "STALL_ME.py": 6})
    env.configure(carrier_mode="stall_marker", timeout_seconds=3, chunk_lines=14, max_attempts=1)

    first_code, first = env.run(capsys=capsys)
    second_code, second = env.run(capsys=capsys)

    assert first_code == 20
    assert first["reason"] == "CHUNK_TIMEOUT"
    assert second_code == 0
    sent = prompts(env.stubs, "carrier")
    assert sum("+line 1 of a.py" in p for p in sent) == 1
    resumed = [s for s in second["stages"] if s.get("resumed") == "yes"]
    assert {s["stage"] for s in resumed} == {"chunk-1"}


def test_both_engines_absent_reports_both_diagnostics(env, capsys):
    write_profile_config(
        env.cfg,
        carrier=["/nonexistent/carrier-binary"],
        fallback=["/nonexistent/fallback-binary"],
    )

    code, payload = env.run(capsys=capsys)

    assert code == 20
    assert payload["reason"] == "MODEL_UNAVAILABLE"
    assert "carrier-binary" in payload["detail"]
    assert "fallback-binary" in payload["detail"]


def test_a_corrupt_persisted_attempt_count_does_not_crash_the_run(env, capsys):
    env.configure(carrier_mode="hang", timeout_seconds=0.5, max_attempts=3)
    first_code, first = env.run(capsys=capsys)
    assert first_code == 10
    record_path = next(Path(first["run_dir"]).glob("state-*/result-0001.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["attempts"] = "many"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    code, _ = env.run(capsys=capsys)

    # An unreadable count is treated as zero prior attempts: the budget
    # restarts (1 of 3 used) rather than counting as exhausted.
    assert code == 10
    rewritten = json.loads(record_path.read_text(encoding="utf-8"))
    assert rewritten["attempts"] == 1


def test_chunked_is_reported_once_when_the_state_dir_cannot_be_created(tmp_path):
    from clagentic_loadout.acquire.contract import AcquiredPr
    from clagentic_loadout.review.profile_config import ReviewProfile
    from clagentic_loadout.review.run_pipeline import run_review

    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the run directory should be", encoding="utf-8")
    acquired = AcquiredPr(
        owner="some-owner", repo="some-repo", pr_number=42,
        base_sha="a" * 40, head_sha="b" * 40, diff_text=make_diff({"a.py": 3}),
    )
    profile = ReviewProfile(
        name="reviewer", carrier=("unused",), fallback=None, rulebook_text="",
        chunk_lines=600, timeout_seconds=1.0, fallback_timeout_seconds=1.0,
        max_attempts=1, parallel=1,
    )

    outcome = run_review(acquired, profile, blocker, emit=lambda *a, **k: None)

    assert outcome.exit_code == 20
    chunked = [s["status"] for s in outcome.stages if s["stage"] == "chunked"]
    assert chunked == ["failed"]


def test_a_usage_limit_trips_the_breaker_for_the_rest_of_the_run_and_is_reported(tmp_path):
    import subprocess

    from clagentic_loadout.acquire.contract import AcquiredPr
    from clagentic_loadout.review.profile_config import ReviewProfile
    from clagentic_loadout.review.run_pipeline import run_review

    array = json.dumps(
        [{"file": "a.py", "line": 1, "rule_id": "R1", "severity": "nit", "message": "m"}]
    )
    calls: list[str] = []

    def runner(argv, *, input, capture_output, timeout, cwd):
        calls.append(argv[0])
        if argv[0] == "carrier-engine":
            stderr = b"prompt echo\n" * 2000 + b"ERROR: You've hit your usage limit.\n"
            return subprocess.CompletedProcess(argv, 1, b"", stderr)
        return subprocess.CompletedProcess(argv, 0, array.encode(), b"")

    acquired = AcquiredPr(
        owner="some-owner", repo="some-repo", pr_number=42, base_sha="a" * 40,
        head_sha="b" * 40, diff_text=make_diff({"a.py": 6, "b.py": 6, "c.py": 6}),
    )
    profile = ReviewProfile(
        name="reviewer", carrier=("carrier-engine",), fallback=("fallback-engine",),
        rulebook_text="", chunk_lines=8, timeout_seconds=5.0, fallback_timeout_seconds=5.0,
        max_attempts=3, parallel=1,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    outcome = run_review(acquired, profile, run_dir, emit=lambda *a, **k: None, runner=runner)

    assert outcome.exit_code == 0
    assert calls.count("carrier-engine") == 1
    assert calls.count("fallback-engine") == outcome.payload["chunk_count"] >= 2
    assert outcome.payload["carrier_unavailable_reason"] == "usage_limit"
    assert outcome.payload["engines"] == {"fallback": outcome.payload["chunk_count"]}
    chunk_stages = [s for s in outcome.stages if s["stage"].startswith("chunk-")]
    assert all(s["status"] == "fallback" for s in chunk_stages)
    assert all(s["note"] == "carrier unavailable: usage_limit" for s in chunk_stages)


@pytest.mark.parametrize("bad_sha", ["../..", "abc123", "B" * 40])
def test_default_run_dir_refuses_a_head_sha_that_is_not_40_lowercase_hex(tmp_path, bad_sha):
    from clagentic_loadout.review.run_pipeline import default_run_dir

    with pytest.raises(ValueError, match="40 lowercase hex"):
        default_run_dir(tmp_path, "some-owner", "some-repo", 42, bad_sha)


def test_a_traversal_head_sha_from_the_api_creates_nothing_under_the_run_root(env, capsys):
    env.configure()

    code, payload = env.run(capsys=capsys, head_sha="../../escape")

    assert code == 20
    assert payload["reason"] == "ACQUIRE_INVALID"
    assert not env.runs.exists()
    assert not (env.runs.parent / "escape").exists()


def test_a_failed_state_write_leaves_no_temp_file_behind(tmp_path):
    from clagentic_loadout.review.run_pipeline import _write_json

    target = tmp_path / "result.json"
    target.mkdir()  # os.replace onto a directory fails after the temp file exists

    with pytest.raises(OSError):
        _write_json(target, {"k": "v"})

    assert [p.name for p in tmp_path.iterdir()] == ["result.json"]


def test_a_cached_record_without_an_index_does_not_crash_the_merge(env, capsys):
    env.configure()
    first_code, first = env.run(capsys=capsys)
    assert first_code == 0
    record_path = next(Path(first["run_dir"]).glob("state-*/result-0001.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    del record["index"]
    record_path.write_text(json.dumps(record), encoding="utf-8")

    code, payload = env.run(capsys=capsys)

    assert code == 0
    assert {f["chunk"] for f in _chunk_records(payload)["findings"]} == {1}


def test_a_diff_without_file_headers_is_reviewed_not_reported_clean(env, capsys):
    env.diff = "--- a/plain.txt\n+++ b/plain.txt\n@@ -1 +1 @@\n-old\n+new\n"
    env.configure()

    code, payload = env.run(capsys=capsys)

    assert code == 0
    sent = prompts(env.stubs, "carrier")
    assert len(sent) == 1
    assert "+new" in sent[0]
    assert payload["finding_count"] == 1


def test_public_git_host_base_resolver_follows_a_patch_of_the_private_name(monkeypatch):
    from clagentic_loadout.transport import git_host_api

    monkeypatch.setattr(
        git_host_api, "_resolve_git_host_base", lambda explicit, **kwargs: "http://patched.example"
    )

    assert git_host_api.resolve_git_host_base(None) == "http://patched.example"


def test_usage_error_exits_usage_not_the_token_failure_code(env, capsys):
    code = review_cli.main(["run", "--caller", "reviewer", "--pr", "notanumber"])

    assert code == review_cli.EXIT_USAGE
    assert code != review_cli.EXIT_TOKEN_FETCH_FAILED


def test_an_explicit_out_dir_is_bound_to_the_head_and_never_resumes_an_older_head(env, capsys, tmp_path):
    env.configure()
    out = tmp_path / "chosen"
    first_code, _ = env.run("--out", str(out), capsys=capsys)
    assert first_code == 0
    assert (out / "findings.json").is_file()
    set_mode(env.stubs, "carrier", "exit1")

    code, _ = env.run("--out", str(out), capsys=capsys, head_sha="c" * 40)

    # The old head's finished chunk is not reused (the carrier ran again and
    # failed) and its merged findings no longer sit in the directory.
    assert code != 0
    assert not (out / "findings.json").exists()
    assert len(prompts(env.stubs, "carrier")) > 1
    binding = json.loads((out / "run-binding.json").read_text(encoding="utf-8"))
    assert binding["head_sha"] == "c" * 40


def test_the_same_head_keeps_its_findings_and_cached_chunks_in_an_explicit_out_dir(env, capsys, tmp_path):
    env.configure()
    out = tmp_path / "chosen"
    env.run("--out", str(out), capsys=capsys)

    code, _ = env.run("--out", str(out), capsys=capsys)

    assert code == 0
    assert len(prompts(env.stubs, "carrier")) == 1


def test_a_local_setup_failure_is_not_retried_and_blocks_at_once(env, capsys, tmp_path):
    script = tmp_path / "engine"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o644)
    write_profile_config(env.cfg, carrier=[str(script)], max_attempts=3)

    code, payload = env.run(capsys=capsys)

    # Resume (exit 10) would send the caller around a loop that cannot change.
    assert code == 20
    assert payload["reason"] == "CARRIER_FAILED"
    assert "cannot run" in payload["detail"]


def test_findings_order_is_the_same_for_a_resumed_run_and_a_fresh_run(env, capsys, tmp_path):
    env.diff = make_diff({"a.py": 6, "STALL_ME.py": 6, "c.py": 6})
    env.configure(carrier_mode="stall_marker", timeout_seconds=3, chunk_lines=14)
    env.run("--out", str(tmp_path / "resumed"), capsys=capsys)
    resumed_code, resumed = env.run("--out", str(tmp_path / "resumed"), capsys=capsys)
    set_mode(env.stubs, "carrier", "array")
    fresh_code, fresh = env.run("--out", str(tmp_path / "fresh"), capsys=capsys)

    assert (resumed_code, fresh_code) == (0, 0)
    order = lambda payload: [  # noqa: E731
        (f["chunk"], f["file"]) for f in _chunk_records(payload)["findings"]
    ]
    assert order(resumed) == order(fresh) == [(1, "a.py"), (2, "STALL_ME.py"), (3, "c.py")]
