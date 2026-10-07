"""`loadout-review post`: reviewer evidence reaches the posted comment and the
fence end to end: a blocking finding's failure sequence, the dropped
candidates, the commit range, and the engine that answered each run."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.merge.verdict import parse_verdict_block
from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import BASE_SHA, HEAD_SHA, Env

SEQUENCE = "1. Caller passes a path with a NUL byte. 2. The open() call raises. 3. The worker dies and the queue stalls."


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


def _findings(tmp_path, findings):
    return _write(tmp_path, "findings.json", findings)


def _post(env, tmp_path, capsys, findings, *extra, status="blocking"):
    return env.post(
        "--findings", str(_findings(tmp_path, findings)), "--head-sha", HEAD_SHA,
        "--status", status, *extra, capsys=capsys,
    )


def _blocking(**extra):
    return {
        "file": "a.py", "line": 3, "rule_id": "R1", "severity": "blocking",
        "message": "unchecked path", **extra,
    }


def _prose(env) -> str:
    return env.opener_state["posted_body"].split("```review-result")[0]


def test_failure_sequence_is_posted_under_its_bullet_and_in_the_fence(env, tmp_path, capsys):
    code, payload = _post(env, tmp_path, capsys, [_blocking(failure_sequence=SEQUENCE)])

    assert code == 0
    assert payload["verdict_block_verified"] is True
    lines = _prose(env).splitlines()
    bullet = next(i for i, line in enumerate(lines) if line.startswith("- a.py:3 [R1]"))
    assert lines[bullet + 1] == f"  failure sequence: {SEQUENCE}"
    fence = parse_verdict_block(env.opener_state["posted_body"])
    assert fence["failure_sequences"] == [
        {"file": "a.py", "line": 3, "rule_id": "R1", "failure_sequence": SEQUENCE}
    ]
    # Evidence is not findings state: the fence keeps its version.
    assert "fence_schema_version" not in fence


def test_a_multi_line_failure_sequence_stays_under_its_bullet(env, tmp_path, capsys):
    sequence = "step one\nstep two\nstep three"
    code, _ = _post(env, tmp_path, capsys, [_blocking(failure_sequence=sequence)])

    assert code == 0
    assert "  failure sequence: step one\n    step two\n    step three\n" in _prose(env)
    fence = parse_verdict_block(env.opener_state["posted_body"])
    assert fence["failure_sequences"][0]["failure_sequence"] == sequence


def test_a_findings_file_without_the_field_posts_exactly_as_before(env, tmp_path, capsys):
    code, _ = _post(env, tmp_path, capsys, [_blocking(), {**_blocking(), "line": 4, "severity": "nit"}])

    assert code == 0
    body = env.opener_state["posted_body"]
    assert _prose(env) == (
        "REVIEWER — blocking (2 finding(s))\n"
        "- a.py:3 [R1] (blocking) unchecked path\n"
        "- a.py:4 [R1] (nit) unchecked path\n\n"
    )
    fence = parse_verdict_block(body)
    assert set(fence) == {"reviewer", "review_status", "head_sha", "pr_number"}


def test_an_empty_failure_sequence_is_treated_as_absent(env, tmp_path, capsys):
    code, _ = _post(env, tmp_path, capsys, [_blocking(failure_sequence="   ")])

    assert code == 0
    assert "failure sequence" not in env.opener_state["posted_body"]
    assert "failure_sequences" not in parse_verdict_block(env.opener_state["posted_body"])


def test_a_fence_shaped_failure_sequence_is_refused_before_anything_is_posted(env, tmp_path, capsys):
    code, _ = _post(env, tmp_path, capsys, [_blocking(failure_sequence="x ```review-result y")])

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert env.opener_state["posted_body"] is None


def _dropped(**extra):
    return {
        "file": "b.py", "line": 7, "rule_id": "R2", "message": "maybe a race",
        "reason": "the lock is held by the caller", **extra,
    }


def test_dropped_candidates_render_a_section_and_count_in_the_header(env, tmp_path, capsys):
    dropped = [_dropped(), _dropped(line=9, reason="dead code")]
    code, payload = _post(
        env, tmp_path, capsys, [], "--dropped", str(_write(tmp_path, "dropped.json", dropped)),
        status="clean",
    )

    assert code == 0
    assert payload["dropped_count"] == 2
    prose = _prose(env)
    assert prose.splitlines()[0] == "REVIEWER — clean (0 finding(s), 2 dropped)"
    assert "Dropped candidates (2):" in prose
    assert "- b.py:7 [R2] maybe a race (reason: the lock is held by the caller)" in prose
    assert "- b.py:9 [R2] maybe a race (reason: dead code)" in prose
    assert parse_verdict_block(env.opener_state["posted_body"])["dropped"] == dropped


def test_a_dropped_file_may_be_an_object_with_a_dropped_array(env, tmp_path, capsys):
    path = _write(tmp_path, "dropped.json", {"dropped": [_dropped()]})
    code, _ = _post(env, tmp_path, capsys, [], "--dropped", str(path), status="clean")

    assert code == 0
    assert "1 dropped" in _prose(env)


def test_an_empty_dropped_file_changes_nothing(env, tmp_path, capsys):
    path = _write(tmp_path, "dropped.json", [])
    code, payload = _post(env, tmp_path, capsys, [], "--dropped", str(path), status="clean")

    assert code == 0
    assert "dropped_count" not in payload
    assert _prose(env).splitlines()[0] == "REVIEWER — clean (0 finding(s))"


@pytest.mark.parametrize(
    "document",
    [
        "not json at all",
        {"unrelated": []},
        [_dropped(reason="")],
        [{k: v for k, v in _dropped().items() if k != "rule_id"}],
        [_dropped(line=0)],
        [_dropped(message="has ``` fence")],
    ],
    ids=["not-json", "wrong-shape", "empty-reason", "missing-rule", "bad-line", "fence-shaped"],
)
def test_a_bad_dropped_file_is_refused_before_anything_is_posted(env, tmp_path, capsys, document):
    path = tmp_path / "dropped.json"
    path.write_text(
        document if isinstance(document, str) else json.dumps(document), encoding="utf-8"
    )
    code, _ = _post(env, tmp_path, capsys, [], "--dropped", str(path), status="clean")

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert env.opener_state["posted_body"] is None


def test_an_unreadable_dropped_file_is_refused(env, tmp_path, capsys):
    code, _ = _post(
        env, tmp_path, capsys, [], "--dropped", str(tmp_path / "missing.json"), status="clean"
    )

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert env.opener_state["posted_body"] is None


def test_a_state_file_may_not_carry_the_derived_evidence_fields(env, tmp_path, capsys):
    state = _write(tmp_path, "state.json", {"dropped": [_dropped()]})
    code, _ = _post(env, tmp_path, capsys, [], "--state-file", str(state), status="clean")

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert env.opener_state["posted_body"] is None


def _run_and_post(env, capsys):
    code, run = env.run(capsys=capsys)
    assert code == 0, run
    code, payload = env.post("--findings", run["findings_file"], "--status", "clean", capsys=capsys)
    assert code == 0, payload
    return env.opener_state["posted_body"]


def test_a_carrier_run_states_its_range_and_engine(env, capsys):
    env.configure(carrier_mode="empty")

    body = _run_and_post(env, capsys)

    prose = body.split("```review-result")[0].splitlines()
    assert prose[1] == f"range: {BASE_SHA[:12]}..{HEAD_SHA[:12]}"
    assert prose[2] == "engine: carrier"
    fence = parse_verdict_block(body)
    assert fence["range"] == {"basis": "base..head", "base": BASE_SHA, "head": HEAD_SHA}
    assert fence["engines"] == [{"engine": "carrier", "chunks": 1}]


def test_a_fallback_run_shows_its_engine_model_and_reason_in_the_verdict(env, capsys):
    env.configure(
        carrier_mode="usage_limit", fallback_mode="empty", fallback_model="backup-model"
    )

    body = _run_and_post(env, capsys)

    assert "engine: fallback: backup-model, reason usage_limit" in body.split("```review-result")[0]
    fence = parse_verdict_block(body)
    assert fence["engines"] == [
        {"engine": "fallback", "model": "backup-model", "reason": "usage_limit", "chunks": 1}
    ]


def test_a_fallback_without_a_model_label_still_names_the_engine(env, capsys):
    env.configure(carrier_mode="usage_limit", fallback_mode="empty")

    body = _run_and_post(env, capsys)

    assert "engine: fallback, reason usage_limit" in body.split("```review-result")[0]


def test_a_bare_findings_array_has_no_run_evidence(env, tmp_path, capsys):
    code, _ = _post(env, tmp_path, capsys, [], status="clean")

    assert code == 0
    fence = parse_verdict_block(env.opener_state["posted_body"])
    assert "range" not in fence and "engines" not in fence
