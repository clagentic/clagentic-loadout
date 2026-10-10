"""A prior open finding whose file the delta touches is re-judged by the
reviewer, not carried verbatim; a file the delta leaves alone still carries;
and a caller can rule findings resolved. Every prior open finding is accounted
for exactly once."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clagentic_loadout.merge.fence_state import normalize_findings_state
from clagentic_loadout.merge.verdict import build_findings_verdict_body, parse_verdict_block
from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.review.delta import DeltaContext, account_for_prior
from clagentic_loadout.review.resolutions import (
    ResolutionsError,
    parse_resolutions,
    resolved_entry,
    split_resolved,
)
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import HEAD_SHA, Env, make_diff, prompts

SINCE_SHA = "c" * 40
_FP = "fp2:" + "a" * 16


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    environment = Env(tmp_path)
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.delenv(review_cli.RUN_ROOT_ENV_VAR, raising=False)
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return environment


def _finding(file="a.py", line=3, rule="R1", severity="blocking", message="bad", **extra):
    return {"file": file, "line": line, "rule_id": rule, "severity": severity,
            "message": message, **extra}


def _write(path: Path, data) -> str:
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def _prior(tmp_path: Path, findings: list) -> str:
    return _write(tmp_path / "prior.json", {
        "owner": "some-owner", "repo": "some-repo", "pr_number": 42,
        "head_sha": SINCE_SHA, "findings": findings,
    })


def _edit_at(name: str, old_start: int, new_start: int) -> dict:
    """A delta that changes `name` around one line, nowhere near the prior finding."""
    diff = "\n".join([
        f"diff --git a/{name} b/{name}", "index 1111111..2222222 100644",
        f"--- a/{name}", f"+++ b/{name}",
        f"@@ -{old_start},3 +{new_start},4 @@", " context", "+    def _helper(self): ...", " more", " end",
    ]) + "\n"
    return {"status": "ahead", "diff": diff}


def _document(payload: dict) -> dict:
    return json.loads(Path(payload["findings_file"]).read_text(encoding="utf-8"))


# Real shapes: the fix lands at a line other than the finding's own.
_FIX_SHAPES = pytest.mark.parametrize(
    ("file", "finding_line", "fix_line", "shifted_line"),
    [
        ("engram_sync_state.py", 86, 319, 86),
        ("tests/test_notifier.py", 140, 22, 141),
        ("notifier.py", 198, 61, 199),
    ],
    ids=["new-helper-after", "setup-before", "fixed-in-channel"],
)


@_FIX_SHAPES
def test_a_fix_at_a_different_line_is_resolved_only_when_the_reviewer_names_the_finding(
    env, tmp_path, capsys, file, finding_line, fix_line, shifted_line
):
    env.configure(carrier_mode="resolve_listed")
    prior = _prior(tmp_path, [_finding(file=file, line=finding_line)])

    code, payload = env.run(
        "--prior-findings", prior, capsys=capsys, compare=_edit_at(file, fix_line, fix_line)
    )

    assert code == 0
    document = _document(payload)
    assert document["findings"] == [] and document["carried_count"] == 0
    assert [(e["file"], e["line"], e["by"]) for e in document["resolved"]] == [
        (file, finding_line, "reviewer")
    ]
    sent = prompts(env.stubs, "carrier")[0]
    assert f"- {file}:{finding_line} [R1] (blocking) bad (prior location: line {finding_line})" in sent
    assert f"\n  id: {file}:{finding_line}:R1\n" in sent

    posted_code, _ = env.post("--findings", payload["findings_file"], "--status", "clean", capsys=capsys)
    assert posted_code == 0
    body = env.opener_state["posted_body"]
    assert f"Resolved (1):\n- {file}:{finding_line} [R1] bad" in body
    assert "clean (0 finding(s), 1 resolved)" in body
    assert parse_verdict_block(body)["resolved"][0]["by"] == "reviewer"


@_FIX_SHAPES
def test_a_silent_reply_leaves_the_finding_open_at_its_re_anchored_line(
    env, tmp_path, capsys, file, finding_line, fix_line, shifted_line
):
    env.configure(carrier_mode="empty")
    prior = _prior(tmp_path, [_finding(file=file, line=finding_line)])

    _, payload = env.run(
        "--prior-findings", prior, capsys=capsys, compare=_edit_at(file, fix_line, fix_line)
    )

    document = _document(payload)
    assert document["resolved"] == [] and document["resolved_count"] == 0
    assert [(f["file"], f["line"], f["prior_line"]) for f in document["findings"]] == [
        (file, shifted_line, finding_line)
    ]
    assert (document["prior_open_count"], document["kept_count"]) == (1, 1)


def test_a_malformed_resolved_list_resolves_nothing(env, tmp_path, capsys):
    env.configure(carrier_mode="resolve_malformed")
    prior = _prior(tmp_path, [_finding(line=3)])

    _, payload = env.run("--prior-findings", prior, capsys=capsys, compare=_edit_at("a.py", 20, 20))

    document = _document(payload)
    assert document["resolved"] == [] and len(document["findings"]) == 1
    assert document["prior_open_count"] == 1 and document["kept_count"] == 1


def test_a_failed_chunk_resolves_nothing(env, tmp_path, capsys):
    env.configure(carrier_mode="exit1")
    prior = _prior(tmp_path, [_finding(line=3)])

    code, payload = env.run("--prior-findings", prior, capsys=capsys, compare=_edit_at("a.py", 20, 20))

    assert code != 0
    assert "resolved" not in payload and "findings_file" not in payload


def test_a_finding_the_reviewer_reports_again_is_kept_and_re_anchored(env, tmp_path, capsys):
    env.configure(carrier_mode="array")  # the stub reports a.py:1 R1 for the first file
    prior = _prior(tmp_path, [_finding(line=3)])

    _, payload = env.run("--prior-findings", prior, capsys=capsys, compare=_edit_at("a.py", 1, 1))

    document = _document(payload)
    assert (document["prior_open_count"], document["kept_count"], document["resolved_count"]) == (1, 1, 0)
    assert document["carried_count"] == 0 and document["resolved"] == []
    assert [(f["line"], f["prior_line"]) for f in document["findings"]] == [(1, 3)]


def test_a_finding_on_an_untouched_file_is_carried_with_its_line(env, tmp_path, capsys):
    env.configure(carrier_mode="empty")
    prior = _prior(tmp_path, [_finding(file="a.py", line=3)])

    _, payload = env.run(
        "--prior-findings", prior, capsys=capsys,
        compare={"status": "ahead", "diff": make_diff({"c.py": 2})},
    )

    document = _document(payload)
    assert [(f["file"], f["line"], f["chunk"]) for f in document["findings"]] == [("a.py", 3, 0)]
    assert (document["prior_open_count"], document["carried_count"]) == (1, 1)
    assert document["resolved"] == []


def test_every_prior_finding_is_accounted_for_exactly_once(env, tmp_path, capsys):
    env.configure(carrier_mode="resolve_listed")
    prior = _prior(tmp_path, [
        _finding(file="a.py", line=3),
        _finding(file="b.py", line=4),
        _finding(file="c.py", line=5),
    ])
    resolved = _write(tmp_path / "resolved.json", ["c.py:5:R1"])
    diff = make_diff({"a.py": 2})

    _, payload = env.run(
        "--prior-findings", prior, "--resolved-findings", resolved,
        capsys=capsys, compare={"status": "ahead", "diff": diff},
    )

    document = _document(payload)
    assert document["prior_open_count"] == 3
    assert document["carried_count"] == 1          # b.py: file untouched
    assert document["kept_count"] == 0
    assert document["resolved_count"] == 2         # a.py by the reviewer, c.py by the caller
    assert {(e["file"], e["by"]) for e in document["resolved"]} == {
        ("a.py", "reviewer"), ("c.py", "caller")
    }
    assert (document["carried_count"] + document["kept_count"] + document["resolved_count"]
            == document["prior_open_count"])


def test_a_caller_resolution_hides_the_finding_from_the_reviewer_and_the_post(env, tmp_path, capsys):
    env.configure(carrier_mode="empty")
    prior = _prior(tmp_path, [_finding(file="b.py", line=4, **{"fingerprint": _FP})])
    ruling = _write(tmp_path / "resolved.json", [{"fingerprint": _FP, "reason": "refuted by the owner"}])

    _, payload = env.run(
        "--prior-findings", prior, "--resolved-findings", ruling, capsys=capsys,
        compare={"status": "ahead", "diff": make_diff({"c.py": 2})},
    )

    document = _document(payload)
    assert document["findings"] == [] and document["carried_count"] == 0
    assert document["resolved"] == [{
        "file": "b.py", "line": 4, "rule_id": "R1", "message": "bad", "by": "caller",
        "reason": "refuted by the owner", "fingerprint": _FP,
    }]
    assert "b.py:4" not in prompts(env.stubs, "carrier")[0]
    assert payload["unknown_resolved_findings"] == []

    code, _ = env.post("--findings", payload["findings_file"], "--status", "clean", capsys=capsys)
    assert code == 0
    body = env.opener_state["posted_body"]
    assert "Resolved by caller (1):\n- b.py:4 [R1] bad (reason: refuted by the owner)" in body
    assert parse_verdict_block(body)["resolved"][0]["reason"] == "refuted by the owner"


def test_an_unknown_resolution_is_warned_about_and_ignored(env, tmp_path, capsys):
    env.configure(carrier_mode="empty")
    prior = _prior(tmp_path, [_finding(file="b.py", line=4)])
    ruling = _write(tmp_path / "resolved.json", ["nowhere.py:1:R9", "b.py:4:R1"])

    code, out, err = env.invoke(
        "run", "--prior-findings", prior, "--resolved-findings", ruling, capsys=capsys,
        compare={"status": "ahead", "diff": make_diff({"c.py": 2})},
    )

    assert code == 0
    assert "resolved-findings entry 'nowhere.py:1:R9' matches no prior finding; ignored" in err
    assert "b.py:4:R1' matches" not in err
    assert json.loads(out.strip().splitlines()[-1])["unknown_resolved_findings"] == ["nowhere.py:1:R9"]


def test_post_applies_caller_resolutions_to_the_findings_it_posts(env, tmp_path, capsys):
    env.configure(carrier_mode="empty")
    findings = _write(tmp_path / "findings.json", [_finding(), _finding(file="x.py", line=9, rule="R2")])
    head = HEAD_SHA
    ruling = _write(tmp_path / "resolved.json", [
        {"id": "a.py:3:R1", "reason": "ruled by the lead"}, "ghost.py:1:R1",
    ])

    code, payload = env.post(
        "--findings", findings, "--head-sha", head, "--status", "blocking",
        "--resolved-findings", ruling, capsys=capsys,
    )

    assert code == 0
    assert payload["finding_count"] == 1
    body = env.opener_state["posted_body"]
    assert "- x.py:9 [R2]" in body and "- a.py:3 [R1] (blocking)" not in body
    assert "(reason: ruled by the lead)" in body


def test_post_clean_is_allowed_when_the_caller_resolved_the_only_blocker(env, tmp_path, capsys):
    env.configure(carrier_mode="empty")
    findings = _write(tmp_path / "findings.json", [_finding()])
    ruling = _write(tmp_path / "resolved.json", ["a.py:3:R1"])

    code, _ = env.post(
        "--findings", findings, "--head-sha", HEAD_SHA, "--status", "clean",
        "--resolved-findings", ruling, capsys=capsys,
    )

    assert code == 0


@pytest.mark.parametrize(
    "document",
    [{"id": "x"}, [{"id": "x", "fingerprint": _FP}], [{"nope": 1}], [{"id": "x", "extra": 1}],
     [{"fingerprint": "not-a-fingerprint"}], [{"id": "x", "reason": "two\nlines"}], [7]],
    ids=["not-a-list", "both-keys", "no-key", "unknown-field", "bad-fingerprint", "multiline", "bad-entry"],
)
def test_a_malformed_resolved_findings_file_is_refused(env, tmp_path, capsys, document):
    env.configure(carrier_mode="empty")
    ruling = _write(tmp_path / "resolved.json", document)

    code, _ = env.run("--resolved-findings", ruling, capsys=capsys)

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert env.token_provider.resolved_for == []


def test_resolutions_match_by_id_or_fingerprint(tmp_path):
    finding = _finding(**{"fingerprint": _FP})
    rulings = parse_resolutions([
        "a.py:3:R1", {"fingerprint": _FP}, {"id": _FP}, {"id": "a.py:4:R1"}, {"fingerprint": "fp2:" + "b" * 16},
    ])

    assert [r.matches(finding) for r in rulings] == [True, True, True, False, False]
    remaining, ruled, unknown = split_resolved([finding, _finding(line=9)], rulings)
    assert remaining == [_finding(line=9)] and len(ruled) == 1
    assert unknown == ["a.py:4:R1", "fp2:" + "b" * 16]
    with pytest.raises(ResolutionsError):
        parse_resolutions("nope")


@pytest.mark.parametrize(
    ("entry", "label"),
    [({"fingerprint": "fp2:zz"}, "entry 1.fingerprint"), ({"id": ""}, "entry 1.id")],
)
def test_a_malformed_ref_is_reported_under_the_key_it_was_read_from(entry, label):
    with pytest.raises(ResolutionsError) as raised:
        parse_resolutions([entry])

    assert label in str(raised.value)


def test_account_for_prior_pairs_by_file_and_rule_with_the_closest_line():
    prior = (_finding(line=10), _finding(line=50), _finding(file="z.py", line=1))
    context = DeltaContext("1" * 40, prior)

    accounting = account_for_prior(context, {"a.py"}, [_finding(line=48, severity="nit")])

    assert [f["file"] for f in accounting.carried] == ["z.py"]
    assert [(p["line"], r["line"]) for p, r in accounting.kept] == [(50, 48)]
    assert accounting.resolved == [] and [f["line"] for f in accounting.left_open] == [10]
    assert accounting.left_open[0]["prior_line"] == 10 and accounting.kept_count == 2


def test_a_report_with_the_same_message_as_a_kept_prior_finding_is_not_absorbed():
    context = DeltaContext("1" * 40, (_finding(line=10),))
    reports = [_finding(line=12), _finding(line=40)]

    accounting = account_for_prior(context, {"a.py"}, reports)

    assert [(p["line"], r["line"]) for p, r in accounting.kept] == [(10, 12)]
    assert not hasattr(accounting, "restatements")
    assert accounting.kept_count == 1 and accounting.left_open == []


def test_a_named_prior_finding_is_resolved_and_not_paired_with_an_unrelated_report():
    prior = (_finding(line=10), _finding(line=50))
    context = DeltaContext("1" * 40, prior)

    accounting = account_for_prior(
        context, {"a.py"}, [_finding(line=12, message="different")], resolved_positions={0}
    )

    assert [(e["line"], e["by"]) for e in accounting.resolved] == [(10, "reviewer")]
    assert [(p["line"], r["line"]) for p, r in accounting.kept] == [(50, 12)]
    assert accounting.left_open == []


def test_a_kept_prior_finding_and_one_left_open_both_stay_in_the_findings(env, tmp_path, capsys):
    # The stub reports a.py:1 R1 "stub finding", which pairs with the closer
    # prior finding; the other prior finding is not named by anyone and stays open.
    env.configure(carrier_mode="array")
    prior = _prior(tmp_path, [_finding(line=3, message="stub finding"), _finding(line=9, message="stub finding")])

    _, payload = env.run("--prior-findings", prior, capsys=capsys, compare=_edit_at("a.py", 1, 1))

    document = _document(payload)
    assert (document["prior_open_count"], document["kept_count"]) == (2, 2)
    assert sorted(f["prior_line"] for f in document["findings"]) == [3, 9]
    assert len(document["findings"]) == 2


def test_the_fence_state_validates_resolved_entries():
    ok = {"file": "a.py", "line": 3, "rule_id": "R1", "message": "m", "by": "caller", "reason": "r"}
    assert normalize_findings_state({"resolved": [ok]}, head_sha=HEAD_SHA, review_status="clean") == {
        "resolved": [ok]
    }
    for bad in (
        {**ok, "by": "someone"},
        {k: v for k, v in ok.items() if k != "reason"},
        {**ok, "extra": 1},
        {**ok, "line": 0},
    ):
        with pytest.raises(ValueError):
            normalize_findings_state({"resolved": [bad]}, head_sha=HEAD_SHA, review_status="clean")


def test_the_body_and_fence_list_resolved_findings_without_raising_the_fence_version():
    entries = [
        resolved_entry(_finding(), "reviewer"),
        resolved_entry(_finding(file="b.py", line=4), "caller", "refuted"),
    ]

    body = build_findings_verdict_body(
        "reviewer", "clean", HEAD_SHA, 42, [], findings_state={"resolved": entries}
    )

    assert "REVIEWER — clean (0 finding(s), 2 resolved)" in body
    assert "Resolved (1):\n- a.py:3 [R1] bad" in body
    assert "Resolved by caller (1):\n- b.py:4 [R1] bad (reason: refuted)" in body
    fence = parse_verdict_block(body)
    assert fence["resolved"] == entries and "fence_schema_version" not in fence
