"""`loadout-review run` writes pr.json (the PR title/body at the reviewed head)
beside findings.json, on every run, without changing findings.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clagentic_loadout.acquire.contract import AcquiredPr
from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.review.pr_record import BODY_MAX_BYTES, cap_body
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import BASE_SHA, HEAD_SHA, Env, make_diff

SINCE_SHA = "c" * 40
_PR = {"title": "feat: a change", "body": "## Compliance record\nhello"}


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    environment = Env(tmp_path)
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.delenv(review_cli.RUN_ROOT_ENV_VAR, raising=False)
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return environment


def _record(payload: dict) -> dict:
    return json.loads((Path(payload["run_dir"]) / "pr.json").read_text(encoding="utf-8"))


def test_github_run_records_the_fetched_title_body_and_head(env, capsys):
    env.configure()

    code, payload = env.run(capsys=capsys, pr_fields=_PR)

    assert code == 0
    assert _record(payload) == {
        "schema": "loadout.review-pr/1",
        "platform": "github",
        "repo": "some-owner/some-repo",
        "pr_number": 42,
        "head_sha": HEAD_SHA,
        "title": _PR["title"],
        "body": _PR["body"],
        "body_truncated": False,
    }
    findings = json.loads(Path(payload["findings_file"]).read_text(encoding="utf-8"))
    assert _record(payload)["head_sha"] == findings["head_sha"]


def test_forgejo_run_records_platform_title_body_and_head(env, capsys, monkeypatch):
    env.configure()

    class _Backend:
        def fetch_pr_content(self, *, owner, repo, pr_number, include_file_contents=False):
            return AcquiredPr(
                owner=owner, repo=repo, pr_number=pr_number, base_sha=BASE_SHA,
                head_sha=HEAD_SHA, diff_text=env.diff, title="fix: x", body="forgejo body",
            )

    monkeypatch.setattr(review_cli, "_build_acquire_backend", lambda *a, **k: _Backend())

    code, payload = env.run("--platform", "forgejo", capsys=capsys)

    assert code == 0
    record = _record(payload)
    assert (record["platform"], record["title"], record["body"], record["head_sha"]) == (
        "forgejo", "fix: x", "forgejo body", HEAD_SHA,
    )


@pytest.mark.parametrize("fields", [{}, {"body": None}, {"title": None, "body": None}])
def test_an_absent_or_null_body_is_the_empty_string(env, capsys, fields):
    env.configure()

    _, payload = env.run(capsys=capsys, pr_fields=fields)

    record = _record(payload)
    assert record["body"] == ""
    assert record["title"] == ""
    assert record["body_truncated"] is False


def test_an_oversized_body_is_capped_on_a_utf8_boundary_and_flagged(env, capsys):
    env.configure()
    # 3-byte characters: the 64 KiB cut falls inside one, which must be dropped.
    body = "€" * (BODY_MAX_BYTES // 3 + 10)

    _, payload = env.run(capsys=capsys, pr_fields={"title": "t", "body": body})

    record = _record(payload)
    assert record["body_truncated"] is True
    assert len(record["body"].encode("utf-8")) <= BODY_MAX_BYTES
    assert len(record["body"].encode("utf-8")) > BODY_MAX_BYTES - 3
    assert body.startswith(record["body"])


def test_cap_body_leaves_a_body_at_the_limit_whole():
    body = "a" * BODY_MAX_BYTES
    assert cap_body(body) == (body, False)
    assert cap_body(body + "a") == (body, True)


def test_a_resumed_run_rewrites_pr_json_at_the_current_head(env, capsys):
    env.configure(carrier_mode="hang", timeout_seconds=3, max_attempts=3)
    first_code, first = env.run(capsys=capsys, pr_fields={"title": "old", "body": "old body"})
    assert first_code == 10
    assert _record(first)["title"] == "old"

    from tests._review_cli_support import set_mode

    set_mode(env.stubs, "carrier", "array")
    second_code, second = env.run(capsys=capsys, pr_fields={"title": "new", "body": "new body"})

    assert second_code == 0
    assert (_record(second)["title"], _record(second)["body"]) == ("new", "new body")


def test_a_stale_pr_json_from_an_older_head_in_out_is_replaced(env, capsys, tmp_path):
    env.configure()
    out = tmp_path / "chosen"
    out.mkdir()
    (out / "pr.json").write_text(
        json.dumps({"schema": "loadout.review-pr/1", "head_sha": "d" * 40, "body": "stale"}),
        encoding="utf-8",
    )

    code, payload = env.run("--out", str(out), capsys=capsys, pr_fields=_PR)

    assert code == 0
    record = _record(payload)
    assert (record["head_sha"], record["body"]) == (HEAD_SHA, _PR["body"])


def test_a_delta_run_rewrites_pr_json_at_the_current_head(env, capsys, tmp_path):
    env.configure()
    prior = tmp_path / "prior.json"
    prior.write_text(
        json.dumps(
            {"owner": "some-owner", "repo": "some-repo", "pr_number": 42,
             "head_sha": SINCE_SHA, "findings": []}
        ),
        encoding="utf-8",
    )
    out = tmp_path / "delta-out"
    out.mkdir()
    (out / "pr.json").write_text(json.dumps({"head_sha": "d" * 40}), encoding="utf-8")

    code, payload = env.run(
        "--prior-findings", str(prior), "--out", str(out), capsys=capsys, pr_fields=_PR,
        compare={"status": "ahead", "diff": make_diff({"c.py": 2})},
    )

    assert code == 0
    assert payload["mode"] == "delta"
    record = _record(payload)
    assert (record["head_sha"], record["title"], record["body"]) == (
        HEAD_SHA, _PR["title"], _PR["body"],
    )


def _normalised_findings(payload: dict) -> str:
    document = json.loads(Path(payload["findings_file"]).read_text(encoding="utf-8"))
    for chunk in document["chunks"]:
        chunk["nonce"] = "<nonce>"
    return json.dumps(document, indent=2, sort_keys=True)


def test_findings_json_does_not_depend_on_pr_text_and_carries_no_pr_fields(
    env, capsys, tmp_path, monkeypatch
):
    env.configure()
    _, with_text = env.run(capsys=capsys, pr_fields=_PR)
    with_text_findings = _normalised_findings(with_text)

    (tmp_path / "second").mkdir()
    other = Env(tmp_path / "second")
    monkeypatch.setenv("STUB_DIR", str(other.stubs))
    other.configure()
    _, without = other.run(capsys=capsys)

    assert _normalised_findings(without) == with_text_findings
    assert set(json.loads(with_text_findings)) == {
        "schema", "owner", "repo", "pr_number", "head_sha", "base_sha", "mode", "since_head",
        "carried_count", "chunk_count", "chunks", "findings",
    }
