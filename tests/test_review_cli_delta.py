"""Behavioural tests for `loadout-review run` in delta mode: review only the
changes since the caller's own last verdict, answer for its open findings, and
fall back to the full diff whenever a delta is not well defined."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import HEAD_SHA, Env, make_diff, prompts

SINCE_SHA = "c" * 40

_BLOCKING = {"file": "a.py", "line": 3, "rule_id": "R1", "severity": "blocking", "message": "bad"}
_PRAISE = {"file": "a.py", "line": 1, "rule_id": "R9", "severity": "praise", "message": "nice"}


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    environment = Env(tmp_path)
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.delenv(review_cli.RUN_ROOT_ENV_VAR, raising=False)
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return environment


def _prior(path: Path, findings: list, **overrides) -> str:
    document = {
        "owner": "some-owner",
        "repo": "some-repo",
        "pr_number": 42,
        "head_sha": SINCE_SHA,
        "findings": findings,
    }
    document.update(overrides)
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def _ahead(diff: str | None = None) -> dict:
    return {"status": "ahead", "diff": diff or make_diff({"c.py": 2})}


def _document(payload: dict) -> dict:
    return json.loads(Path(payload["findings_file"]).read_text(encoding="utf-8"))


def _stage(payload: dict, name: str) -> dict:
    return next(s for s in payload["stages"] if s["stage"] == name)


def test_a_fast_forward_reviews_only_the_delta_and_frames_the_open_findings(env, tmp_path, capsys):
    env.configure()
    prior = _prior(tmp_path / "prior.json", [_BLOCKING, _PRAISE])

    code, payload = env.run("--prior-findings", prior, capsys=capsys, compare=_ahead())

    assert code == 0
    sent = prompts(env.stubs, "carrier")
    assert len(sent) == 1
    assert "+line 1 of c.py" in sent[0]
    assert "+line 1 of a.py" not in sent[0]
    assert "## Incremental review" in sent[0]
    assert SINCE_SHA[:12] in sent[0]
    # a.py is not in the delta, so the chunk cannot judge its finding: it is
    # carried forward instead of listed.
    assert "- a.py:3 [R1] (blocking) bad" not in sent[0]
    assert "- none" in sent[0]
    assert "nice" not in sent[0]
    assert sent[0].rstrip().endswith("Reply [] when the chunk has no findings.")
    assert _stage(payload, "delta") == {
        "stage": "delta", "status": "ok", "mode": "delta", "since_head": SINCE_SHA,
    }
    assert [s["stage"] for s in payload["stages"]][:3] == ["acquired", "delta", "chunked"]


def test_the_findings_file_records_the_delta_and_keeps_open_findings_on_untouched_files(
    env, tmp_path, capsys
):
    env.configure()
    prior = _prior(tmp_path / "prior.json", [_BLOCKING])

    _, payload = env.run("--prior-findings", prior, capsys=capsys, compare=_ahead())

    document = _document(payload)
    assert (document["mode"], document["since_head"]) == ("delta", SINCE_SHA)
    assert document["base_sha"] == SINCE_SHA
    assert document["head_sha"] == HEAD_SHA
    assert document["carried_count"] == 1
    carried = [f for f in document["findings"] if f["chunk"] == 0]
    assert [(f["file"], f["rule_id"], f["severity"]) for f in carried] == [("a.py", "R1", "blocking")]
    assert any(f["file"] == "c.py" for f in document["findings"])
    assert payload["mode"] == "delta"


def test_a_finding_on_a_file_the_delta_touches_is_left_to_the_reviewer_not_carried(
    env, tmp_path, capsys
):
    env.configure(carrier_mode="empty")
    prior = _prior(tmp_path / "prior.json", [{**_BLOCKING, "line": 2}])
    compare = _ahead(make_diff({"a.py": 2}))

    _, payload = env.run("--prior-findings", prior, capsys=capsys, compare=compare)

    document = _document(payload)
    assert document["carried_count"] == 0
    assert document["findings"] == []


def test_a_finding_outside_the_hunks_of_a_touched_file_is_carried(env, tmp_path, capsys):
    env.configure(carrier_mode="empty")
    prior = _prior(tmp_path / "prior.json", [_BLOCKING])
    compare = _ahead(make_diff({"a.py": 2}))

    _, payload = env.run("--prior-findings", prior, capsys=capsys, compare=compare)

    document = _document(payload)
    assert document["carried_count"] == 1
    assert [(f["file"], f["line"]) for f in document["findings"]] == [("a.py", 3)]
    assert "- a.py:3 [R1]" not in prompts(env.stubs, "carrier")[0]


def test_a_delta_run_has_its_own_run_directory_beside_the_full_one(env, tmp_path, capsys):
    env.configure()
    prior = _prior(tmp_path / "prior.json", [])

    _, delta_payload = env.run("--prior-findings", prior, capsys=capsys, compare=_ahead())
    _, full_payload = env.run(capsys=capsys)

    assert Path(delta_payload["run_dir"]).name == f"{HEAD_SHA[:12]}-since-{SINCE_SHA[:12]}"
    assert Path(full_payload["run_dir"]).name == HEAD_SHA[:12]
    assert _document(full_payload)["mode"] == "full"


def test_a_delta_run_resumes_from_its_finished_chunks(env, tmp_path, capsys):
    env.configure()
    prior = _prior(tmp_path / "prior.json", [_BLOCKING])

    env.run("--prior-findings", prior, capsys=capsys, compare=_ahead())
    code, _ = env.run("--prior-findings", prior, capsys=capsys, compare=_ahead())

    assert code == 0
    assert len(prompts(env.stubs, "carrier")) == 1


@pytest.mark.parametrize(
    ("compare", "reason"),
    [
        ({"status": "diverged", "diff": ""}, "NON_FAST_FORWARD"),
        ({"status": "behind", "diff": ""}, "NON_FAST_FORWARD"),
        ({"status": "ahead", "diff": " \n"}, "DELTA_EMPTY"),
        ({"http_status": 404}, "RANGE_UNAVAILABLE"),
    ],
    ids=["diverged", "behind", "empty", "unreadable"],
)
def test_a_delta_that_is_not_well_defined_reviews_the_full_diff_and_names_why(
    env, tmp_path, capsys, compare, reason
):
    env.configure()
    prior = _prior(tmp_path / "prior.json", [_BLOCKING])

    code, out, err = env.invoke(
        "run", "--prior-findings", prior, capsys=capsys, compare=compare
    )

    payload = json.loads(out.strip().splitlines()[-1])
    assert code == 0
    sent = prompts(env.stubs, "carrier")
    assert "+line 1 of a.py" in sent[0]
    assert "Incremental review" not in sent[0]
    assert _stage(payload, "delta") == {
        "stage": "delta", "status": "fallback", "mode": "full", "reason": reason,
    }
    assert f"delta review not used ({reason})" in err
    assert _document(payload)["mode"] == "full"
    assert _document(payload)["carried_count"] == 0


def test_the_head_not_having_moved_reviews_the_full_diff_without_reading_a_range(
    env, tmp_path, capsys
):
    env.configure()
    prior = _prior(tmp_path / "prior.json", [], head_sha=HEAD_SHA)

    code, payload = env.run("--prior-findings", prior, capsys=capsys)

    assert code == 0
    assert _stage(payload, "delta")["reason"] == "SAME_HEAD"
    assert all("/compare/" not in url for _, url in env.opener_state["requests"])


def test_a_head_sha_alone_reviews_a_delta_with_no_open_findings_to_answer_for(env, capsys):
    env.configure()

    code, payload = env.run("--prior-head-sha", SINCE_SHA, capsys=capsys, compare=_ahead())

    assert code == 0
    assert "- none" in prompts(env.stubs, "carrier")[0]
    assert _stage(payload, "delta")["status"] == "ok"


def test_a_run_without_prior_flags_reports_no_delta_stage(env, capsys):
    env.configure()

    _, payload = env.run(capsys=capsys)

    assert all(s["stage"] != "delta" for s in payload["stages"])
    assert _document(payload)["mode"] == "full"


def test_a_bare_prior_array_needs_a_head_sha(env, tmp_path, capsys):
    env.configure()
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps([_BLOCKING]), encoding="utf-8")

    refused, _ = env.run("--prior-findings", str(bare), capsys=capsys)
    accepted, payload = env.run(
        "--prior-findings", str(bare), "--prior-head-sha", SINCE_SHA,
        capsys=capsys, compare=_ahead(),
    )

    assert refused == review_cli.EXIT_FINDINGS_INVALID
    assert accepted == 0
    assert _stage(payload, "delta")["status"] == "ok"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"pr_number": 7}, review_cli.EXIT_FINDINGS_INVALID),
        ({"repo": "another-repo"}, review_cli.EXIT_FINDINGS_INVALID),
        ({"head_sha": "short"}, review_cli.EXIT_FINDINGS_INVALID),
    ],
)
def test_prior_findings_for_another_pr_or_without_a_usable_head_are_refused_before_any_io(
    env, tmp_path, capsys, overrides, expected
):
    env.configure()
    prior = _prior(tmp_path / "prior.json", [_BLOCKING], **overrides)

    code, _ = env.run("--prior-findings", prior, capsys=capsys)

    assert code == expected
    assert env.token_provider.resolved_for == []


def test_a_prior_finding_without_a_severity_is_refused(env, tmp_path, capsys):
    env.configure()
    unrated = {k: v for k, v in _BLOCKING.items() if k != "severity"}
    prior = _prior(tmp_path / "prior.json", [unrated])

    code, _ = env.run("--prior-findings", prior, capsys=capsys)

    assert code == review_cli.EXIT_FINDINGS_INVALID


def test_a_malformed_prior_head_sha_is_a_usage_error(env, capsys):
    env.configure()

    code, _ = env.run("--prior-head-sha", "abc123", capsys=capsys)

    assert code == review_cli.EXIT_USAGE


def test_a_prior_head_that_conflicts_with_the_prior_file_is_refused(env, tmp_path, capsys):
    env.configure()
    prior = _prior(tmp_path / "prior.json", [])

    code, _ = env.run(
        "--prior-findings", prior, "--prior-head-sha", "d" * 40, capsys=capsys
    )

    assert code == review_cli.EXIT_FINDINGS_INVALID


def test_a_delta_findings_file_posts_with_its_carried_findings(env, tmp_path, capsys):
    env.configure(carrier_mode="empty")
    prior = _prior(tmp_path / "prior.json", [_BLOCKING])
    _, run_payload = env.run("--prior-findings", prior, capsys=capsys, compare=_ahead())

    code, payload = env.post(
        "--findings", run_payload["findings_file"], "--status", "blocking", capsys=capsys
    )

    assert code == 0
    assert payload["result"] == "posted"
    assert payload["finding_count"] == 1
    assert "a.py:3 [R1] (blocking) bad" in env.opener_state["posted_body"]
