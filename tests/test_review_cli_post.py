"""Behavioural tests for `loadout-review post` and the run -> post hand-off:
the comment body is built entirely by the tool from structured findings, the
fence head equals the API head, and the landed comment is read back."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.review.post_record import REASON_UNVERIFIED
from clagentic_loadout.transport import provider_config
from clagentic_loadout.verdict_record import verb as record_verb
from clagentic_loadout.verdict_record.review_record import review_records_root
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
    # The stub states no facts, so the finding resolves to the worst case: it
    # blocks, and the verdict says which inputs are missing.
    assert "- a.py:1 [R1] stub finding" in posted
    assert "verdict: DEGRADED" in posted
    assert "```review-result" in posted
    assert f'"head_sha": "{HEAD_SHA}"' in posted
    assert '"reviewer": "reviewer"' in posted
    # The staged body pair was consumed by the post. The staging directory
    # must exist, or the emptiness check below would pass without proving
    # anything; listing the whole directory (rather than globbing one name
    # pattern) also catches a leftover under any other file name.
    staging = tmp_path / "tmp" / "clagentic-loadout"
    assert staging.is_dir()
    assert env.opener_state["posted_body"] is not None
    assert [entry.name for entry in staging.iterdir()] == []


def test_posted_body_has_no_caller_prose(env, tmp_path, capsys):
    findings = _write_findings(
        tmp_path / "f.json",
        [{**_NIT, "summary": "CALLER-PROSE-IN-FINDING"}],
        body="CALLER-PROSE-IN-DOCUMENT",
        notes="CALLER-PROSE-IN-NOTES",
    )

    code, _ = env.post("--findings", str(findings), "--status", "blocking", capsys=capsys)

    assert code == 0
    posted = env.opener_state["posted_body"]
    assert posted.splitlines()[0] == "REVIEWER — blocking (1 finding(s))"
    assert "stub finding" in posted
    assert "CALLER-PROSE" not in posted


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
    findings = _write_findings(tmp_path / "f.json", [], policy_base_sha=env.base_sha)
    monkeypatch.setattr(review_cli.review_post_verb, "main", lambda argv, **kwargs: 9)

    code, payload = env.post("--findings", str(findings), "--status", "clean", capsys=capsys)

    assert code == review_cli.EXIT_POST_FAILED
    assert payload == {
        "result": "post_failed",
        "review_post_exit_code": 9,
        "record": {"written": False, "reason": REASON_UNVERIFIED},
    }


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
    assert env.opener_state["posted_body"] is None


def test_non_positive_line_is_refused(env, tmp_path, capsys):
    findings = _write_findings(tmp_path / "f.json", [{**_NIT, "line": 0}])

    code, _ = env.post("--findings", str(findings), "--status", "blocking", capsys=capsys)

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert env.opener_state["posted_body"] is None


def test_a_head_sha_that_conflicts_with_the_findings_file_is_refused(env, tmp_path, capsys):
    findings = _write_findings(tmp_path / "f.json", [_NIT])

    code, _ = env.post(
        "--findings", str(findings), "--status", "blocking", "--head-sha", "c" * 40,
        capsys=capsys,
    )

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert env.opener_state["posted_body"] is None


def test_a_head_sha_that_matches_the_findings_file_is_accepted(env, tmp_path, capsys):
    findings = _write_findings(tmp_path / "f.json", [_NIT])

    code, _ = env.post(
        "--findings", str(findings), "--status", "blocking", "--head-sha", HEAD_SHA,
        capsys=capsys,
    )

    assert code == 0
    assert env.opener_state["posted_body"] is not None


def test_a_padded_repo_argument_still_matches_the_findings_file(env, tmp_path, capsys):
    findings = _write_findings(tmp_path / "f.json", [_NIT])

    code, _ = env.post(
        "--findings", str(findings), "--status", "blocking", "--repo", " some-owner/some-repo ",
        capsys=capsys,
    )

    assert code == 0
    assert env.opener_state["posted_body"] is not None


@pytest.mark.parametrize(
    "inner",
    [
        {"verified_id": 1, "verdict_block_verified": False},
        {"verified_id": 1},
        {"verified_id": None, "verdict_block_verified": True},
        {"verdict_block_verified": True},
        {"verified_id": 1, "verdict_block_verified": "yes"},
    ],
)
def test_an_unverified_landing_is_post_failed_not_posted(
    env, tmp_path, capsys, monkeypatch, inner
):
    findings = _write_findings(tmp_path / "f.json", [], policy_base_sha=env.base_sha)

    def fake_main(argv, **kwargs):
        print(json.dumps(inner))
        return 0

    monkeypatch.setattr(review_cli.review_post_verb, "main", fake_main)

    code, payload = env.post("--findings", str(findings), "--status", "clean", capsys=capsys)

    assert code == review_cli.EXIT_POST_FAILED
    assert payload["result"] == "post_failed"
    # No verified comment, so there is no id to key a record by.
    assert payload["record"] == {"written": False, "reason": REASON_UNVERIFIED}
    assert not list(review_records_root().rglob("*.json"))


def test_unparseable_review_post_output_is_post_failed_not_posted(
    env, tmp_path, capsys, monkeypatch
):
    findings = _write_findings(tmp_path / "f.json", [], policy_base_sha=env.base_sha)

    def fake_main(argv, **kwargs):
        print("this is not json")
        return 0

    monkeypatch.setattr(review_cli.review_post_verb, "main", fake_main)

    code, payload = env.post("--findings", str(findings), "--status", "clean", capsys=capsys)

    assert code == review_cli.EXIT_POST_FAILED
    assert payload["result"] == "post_failed"


def test_failed_review_post_keeps_its_stdout_for_diagnosis(env, tmp_path, capsys, monkeypatch):
    findings = _write_findings(tmp_path / "f.json", [], policy_base_sha=env.base_sha)

    def fake_main(argv, **kwargs):
        print("inner-failure-detail")
        return 9

    monkeypatch.setattr(review_cli.review_post_verb, "main", fake_main)

    code, out, err = env.invoke(
        "post", "--findings", str(findings), "--status", "clean", capsys=capsys
    )

    assert code == review_cli.EXIT_POST_FAILED
    assert "inner-failure-detail" in err
    assert "inner-failure-detail" not in out


def test_post_passes_the_verdict_route_flags_to_review_post(env, tmp_path, capsys, monkeypatch):
    findings = _write_findings(tmp_path / "f.json", [], policy_base_sha=env.base_sha)
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


def test_a_head_that_moves_between_the_first_read_and_the_post_is_refused(
    env, tmp_path, capsys, monkeypatch
):
    import dataclasses

    findings = _write_findings(tmp_path / "f.json", [_NIT])
    real_acquire = review_cli._acquire
    reads: list[int] = []

    def moving_acquire(*args, **kwargs):
        acquired = real_acquire(*args, **kwargs)
        reads.append(1)
        # The first read sees the head the findings were made for; a push
        # lands before the second.
        return acquired if len(reads) == 1 else dataclasses.replace(acquired, head_sha="c" * 40)

    monkeypatch.setattr(review_cli, "_acquire", moving_acquire)

    code, _ = env.post("--findings", str(findings), "--status", "blocking", capsys=capsys)

    assert code == review_cli.EXIT_STALE_HEAD
    assert len(reads) == 2
    assert env.opener_state["posted_body"] is None


def _acquire_moving_after(real_acquire, reads: list, *, stable_reads: int):
    """An _acquire that reports the original head for the first *stable_reads*
    reads and a moved head afterwards."""
    import dataclasses

    def moving_acquire(*args, **kwargs):
        acquired = real_acquire(*args, **kwargs)
        reads.append(1)
        if len(reads) <= stable_reads:
            return acquired
        return dataclasses.replace(acquired, head_sha="c" * 40)

    return moving_acquire


def test_a_landed_verdict_whose_head_has_moved_is_reported_not_called_posted(
    env, tmp_path, capsys, monkeypatch
):
    findings = _write_findings(tmp_path / "f.json", [_NIT])
    reads: list[int] = []
    monkeypatch.setattr(
        review_cli, "_acquire", _acquire_moving_after(review_cli._acquire, reads, stable_reads=2)
    )

    code, payload = env.post("--findings", str(findings), "--status", "blocking", capsys=capsys)

    # The comment cannot be taken back, so it stays; the caller is told it no
    # longer covers the PR.
    assert code == review_cli.EXIT_STALE_HEAD
    assert env.opener_state["posted_body"] is not None
    assert payload["result"] == "posted_head_moved"
    assert payload["verified_id"] == 5
    assert payload["head_sha"] == HEAD_SHA
    assert payload["current_head_sha"] == "c" * 40
    assert payload["head_recheck"] == "moved"
    assert len(reads) == 3


def test_a_landed_verdict_on_an_unmoved_head_is_confirmed_current(env, tmp_path, capsys):
    findings = _write_findings(tmp_path / "f.json", [_NIT])

    code, payload = env.post("--findings", str(findings), "--status", "blocking", capsys=capsys)

    assert code == 0
    assert payload["result"] == "posted"
    assert payload["head_recheck"] == "current"


def test_a_failed_closing_head_read_never_reports_success(
    env, tmp_path, capsys, monkeypatch
):
    findings = _write_findings(tmp_path / "f.json", [_NIT])
    real_acquire = review_cli._acquire
    reads: list[int] = []

    def acquire_failing_last(*args, **kwargs):
        reads.append(1)
        if len(reads) > 2:
            raise review_cli.ReviewCliError("host unreachable", review_cli.EXIT_ACQUIRE_FAILED)
        return real_acquire(*args, **kwargs)

    monkeypatch.setattr(review_cli, "_acquire", acquire_failing_last)

    code, out, err = env.invoke(
        "post", "--findings", str(findings), "--status", "blocking", capsys=capsys
    )

    assert code == review_cli.EXIT_POST_FAILED
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["result"] == "posted_head_unconfirmed"
    assert payload["head_recheck"] == "unavailable"
    assert "could not be re-checked" in err


def test_a_head_that_moves_before_the_post_is_refused_on_forgejo_too(
    env, tmp_path, capsys, monkeypatch
):
    from clagentic_loadout.acquire.contract import AcquiredPr
    from tests._review_cli_support import BASE_SHA, identity_provider

    findings = _write_findings(tmp_path / "f.json", [_NIT])
    heads = iter([HEAD_SHA, "c" * 40])

    def moving_acquire(args, **kwargs):
        return AcquiredPr(
            owner="some-owner", repo="some-repo", pr_number=42,
            base_sha=BASE_SHA, head_sha=next(heads), diff_text="",
        )

    monkeypatch.setattr(review_cli, "_acquire", moving_acquire)
    monkeypatch.setattr(
        review_cli.review_post_verb, "main", lambda argv, **kwargs: pytest.fail("posted")
    )

    code = review_cli.main(
        ["post", "--caller", "reviewer", "--repo", "some-owner/some-repo", "--pr", "42",
         "--platform", "forgejo", "--findings", str(findings), "--status", "blocking"],
        token_provider=env.token_provider,
        identity_provider=identity_provider(),
    )

    assert code == review_cli.EXIT_STALE_HEAD


def test_a_review_post_path_that_exits_directly_still_reports_post_failed(
    env, tmp_path, capsys, monkeypatch
):
    findings = _write_findings(tmp_path / "f.json", [], policy_base_sha=env.base_sha)

    def exiting_main(argv, **kwargs):
        print("inner-exit-detail")
        raise SystemExit(2)

    monkeypatch.setattr(review_cli.review_post_verb, "main", exiting_main)

    code, out, err = env.invoke(
        "post", "--findings", str(findings), "--status", "clean", capsys=capsys
    )

    assert code == review_cli.EXIT_POST_FAILED
    assert json.loads(out.strip().splitlines()[-1]) == {
        "result": "post_failed",
        "review_post_exit_code": 2,
        "record": {"written": False, "reason": REASON_UNVERIFIED},
    }
    assert "inner-exit-detail" in err


def test_a_findings_file_that_is_not_utf8_exits_findings_invalid(env, tmp_path, capsys):
    findings = tmp_path / "f.json"
    findings.write_bytes(b'{"findings": "\xff\xfe"}')

    code, _ = env.post("--findings", str(findings), "--status", "clean", capsys=capsys)

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert env.opener_state["posted_body"] is None


def test_a_hung_origin_probe_is_bounded_and_means_no_remote(tmp_path, monkeypatch):
    import subprocess

    seen: dict = {}

    def hung(argv, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(review_cli.subprocess, "run", hung)

    assert review_cli._origin_url(tmp_path) == ""
    assert seen["timeout"] == review_cli._GIT_PROBE_TIMEOUT_SECONDS


def test_a_fresh_post_reports_comment_created(env, tmp_path, capsys):
    findings = _write_findings(tmp_path / "f.json", [_NIT])

    code, payload = env.post("--findings", str(findings), "--status", "blocking", capsys=capsys)

    assert code == 0
    assert payload["result"] == "posted"
    assert payload["comment"] == "created"
    assert "reused_from_created_at" not in payload


def test_a_repeated_post_reports_comment_reused_with_the_same_id(env, tmp_path, capsys):
    findings = _write_findings(tmp_path / "f.json", [_NIT])
    args = ("--findings", str(findings), "--status", "blocking")

    _, first = env.post(*args, capsys=capsys)
    code, second = env.post(*args, capsys=capsys)

    assert code == 0
    assert second["result"] == "posted"
    assert second["comment"] == "reused"
    assert second["verified_id"] == first["verified_id"]
    assert second["reused_from_created_at"] == "2099-01-01T00:00:10Z"


def test_a_reused_comment_on_a_moved_head_keeps_both_signals(env, tmp_path, capsys, monkeypatch):
    findings = _write_findings(tmp_path / "f.json", [_NIT])
    args = ("--findings", str(findings), "--status", "blocking")
    env.post(*args, capsys=capsys)
    reads: list[int] = []
    monkeypatch.setattr(
        review_cli, "_acquire", _acquire_moving_after(review_cli._acquire, reads, stable_reads=2)
    )

    code, payload = env.post(*args, capsys=capsys)

    assert code == review_cli.EXIT_STALE_HEAD
    assert payload["result"] == "posted_head_moved"
    assert payload["comment"] == "reused"
    assert payload["reused_from_created_at"] == "2099-01-01T00:00:10Z"
    # The comment landed, so its record is kept whether or not the head held.
    assert payload["record"]["written"] is True
    assert record_verb.main(["review", str(payload["verified_id"])]) == 0


def _read_record(comment_id, capsys) -> dict:
    capsys.readouterr()
    assert record_verb.main(["review", str(comment_id)]) == record_verb.EXIT_OK
    return json.loads(capsys.readouterr().out)


def test_a_verified_post_keeps_a_record_the_read_verb_returns_by_comment_id(env, capsys):
    env.configure()
    _, run_payload = env.run(capsys=capsys)

    code, payload = env.post("--findings", run_payload["findings_file"], capsys=capsys)

    assert code == 0
    assert payload["record"]["written"] is True
    assert payload["record"]["comment_id"] == str(payload["verified_id"])
    record = _read_record(payload["verified_id"], capsys)
    assert {k: record[k] for k in ("repo", "pr", "head_sha", "role", "spec_hash")} == {
        "repo": "some-owner/some-repo", "pr": 42, "head_sha": HEAD_SHA, "role": "reviewer",
        "spec_hash": None,
    }
    assert record["policy_base_sha"] == env.base_sha
    assert record["engines"] == [{"engine": "carrier", "chunks": 1}]
    assert [(f["file"], f["line"]) for f in record["findings"]["evaluated"]] == [("a.py", 1)]
    assert [entry["rule_id"] for entry in record["findings"]["open"]] == ["R1"]
    assert record["findings"]["advisory"] == []
    assert record["dropped"] == []
    # The chunk is the one the run cut: its files and the lines of its one hunk.
    [chunk] = record["chunks"]
    assert (chunk["index"], chunk["files"]) == (1, ["a.py"])
    assert [(h["file"], h["first"], h["last"]) for h in chunk["hunks"]] == [("a.py", 1, 4)]


def test_a_record_keeps_the_dropped_candidates_with_their_reasons(env, tmp_path, capsys):
    findings = _write_findings(tmp_path / "f.json", [_NIT])
    dropped = [{"file": "b.py", "line": 7, "rule_id": "R2", "message": "m", "reason": "held by caller"}]
    path = tmp_path / "dropped.json"
    path.write_text(json.dumps(dropped), encoding="utf-8")

    _, payload = env.post(
        "--findings", str(findings), "--status", "blocking", "--dropped", str(path), capsys=capsys
    )

    assert _read_record(payload["verified_id"], capsys)["dropped"] == dropped


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        pytest.param(
            [{"index": 1, "files": ["a.py"], "engine": "carrier"}],
            [{"index": 1, "files": ["a.py"], "hunks": None}],
            id="earlier-release-without-spans",
        ),
        pytest.param(
            [{"index": 1, "files": ["a.py"], "hunks": [{"file": "a.py", "first": "x"}]}],
            [{"index": 1, "files": ["a.py"], "hunks": None}],
            id="malformed-spans",
        ),
        pytest.param(None, None, id="no-chunk-record"),
    ],
)
def test_a_findings_file_without_spans_still_posts_and_records_them_as_absent(
    env, tmp_path, capsys, chunks, expected
):
    overrides = {} if chunks is None else {"chunks": chunks}
    findings = _write_findings(tmp_path / "f.json", [_NIT], policy_base_sha=env.base_sha, **overrides)

    code, payload = env.post("--findings", str(findings), "--status", "blocking", capsys=capsys)

    assert code == 0
    assert payload["result"] == "posted"
    assert _read_record(payload["verified_id"], capsys)["chunks"] == expected


def test_a_record_that_cannot_be_written_is_reported_and_the_post_stands(
    env, tmp_path, capsys, monkeypatch
):
    # A file where the root should be: no directory can be made under it.
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv("CLAGENTIC_LOADOUT_REVIEW_RECORDS_DIR", str(blocker))
    findings = _write_findings(tmp_path / "f.json", [_NIT])

    code, out, err = env.invoke(
        "post", "--findings", str(findings), "--status", "blocking", capsys=capsys
    )

    payload = json.loads(out.strip().splitlines()[-1])
    assert code == 0
    assert payload["result"] == "posted"
    assert payload["record"]["written"] is False
    assert str(blocker) in payload["record"]["reason"]
    assert "no review record was written" in err
    assert env.opener_state["posted_body"] is not None
