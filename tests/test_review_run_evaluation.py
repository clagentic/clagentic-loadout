"""`loadout-review run` and `post` with the evaluator: the reviewer states facts,
severity and blocking are computed, and the posted verdict is the computed one.
Every run here is a real subprocess stub engine against a fake host and a real
local repository holding the base revision."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clagentic_loadout.merge.verdict import parse_verdict_block
from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.review.policy_source import DISPOSITIONS_PATH
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import HEAD_SHA, Env, make_diff, write_profile_config, write_stub
from tests._review_evaluate_support import commit_files

SINCE_SHA = "c" * 40

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
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.delenv(review_cli.RUN_ROOT_ENV_VAR, raising=False)
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return environment


def _document(payload: dict) -> dict:
    return json.loads(Path(payload["findings_file"]).read_text(encoding="utf-8"))


def _classification(document: dict) -> list[tuple]:
    return [
        (f["file"], f["line"], f["rule_id"], f["blocking"], f["computed_severity"])
        for f in document["findings"]
    ]


def _fence(env) -> dict:
    return parse_verdict_block(env.opener_state["posted_body"])


def test_a_reply_with_facts_and_no_severity_gets_a_computed_severity_and_outcome(env, capsys):
    env.configure(carrier_mode="facts_serious")

    code, payload = env.run(capsys=capsys)

    assert code == 0
    document = _document(payload)
    (finding,) = document["findings"]
    assert finding["computed_severity"] == "high"
    assert finding["blocking"] is True
    assert finding["facts_state"] == "complete"
    assert document["verdict_outcome"] == payload["verdict_outcome"] == "BLOCKED"
    assert document["verdict_reasons"] == ["a.py:1:R1 blocks (computed severity high)"]
    assert document["evaluator_version"]
    assert document["policy_base_sha"] == env.base_sha
    assert payload["blocking_count"] == 1


def test_a_finding_that_states_mild_facts_is_advisory_and_the_outcome_is_clean(env, capsys):
    env.configure(carrier_mode="facts_mild")

    _, payload = env.run(capsys=capsys)

    document = _document(payload)
    (finding,) = document["findings"]
    assert (finding["computed_severity"], finding["blocking"]) == ("none", False)
    assert document["verdict_outcome"] == "CLEAN"
    assert document["verdict_reasons"] == []


def test_a_severity_in_the_reply_is_ignored_with_a_notice_and_the_computed_one_is_used(
    env, capsys
):
    env.configure(carrier_mode="facts_serious_with_severity")

    code, out, err = env.invoke("run", capsys=capsys)

    assert code == 0
    assert "severity that is ignored" in err
    document = _document(json.loads(out.strip().splitlines()[-1]))
    (finding,) = document["findings"]
    # The reply said "nit"; the facts say high.
    assert finding["computed_severity"] == "high"
    assert finding["severity"] == "blocking"
    assert document["verdict_outcome"] == "BLOCKED"


def test_a_finding_missing_its_facts_is_worst_case_and_the_outcome_is_degraded(env, capsys):
    env.configure(carrier_mode="array")

    _, payload = env.run(capsys=capsys)

    document = _document(payload)
    (finding,) = document["findings"]
    assert finding["blocking"] is True
    assert finding["facts_state"] == "absent"
    assert document["verdict_outcome"] == "DEGRADED"
    assert any("a.py:1:R1" in reason and "absent" in reason for reason in document["verdict_reasons"])


def test_an_unstated_repository_visibility_is_worst_case_and_the_outcome_says_so(env, capsys):
    env.configure(carrier_mode="facts_mild")

    _, payload = env.run(capsys=capsys, repo_record=None)

    document = _document(payload)
    assert document["repository_visibility"] == "public"
    assert document["verdict_outcome"] == "DEGRADED"
    assert any("visibility" in reason for reason in document["verdict_reasons"])


def test_a_public_repository_marks_every_path_exposed(env, capsys):
    env.configure(carrier_mode="facts_mild")

    _, payload = env.run(capsys=capsys, repo_record={"private": False})

    assert _document(payload)["repository_visibility"] == "public"


def test_a_base_revision_that_cannot_be_read_is_degraded(env, capsys):
    env.configure(carrier_mode="facts_mild")

    _, payload = env.run(capsys=capsys, base_sha="e" * 40)

    document = _document(payload)
    assert document["verdict_outcome"] == "DEGRADED"
    assert any("cannot read" in reason for reason in document["verdict_reasons"])


def test_an_identical_diff_reviewed_twice_gets_an_identical_classification(
    env, tmp_path, capsys
):
    env.configure(carrier_mode="facts_serious")

    _, first = env.run("--out", str(tmp_path / "one"), capsys=capsys)
    _, second = env.run("--out", str(tmp_path / "two"), capsys=capsys)

    assert _classification(_document(first)) == _classification(_document(second))
    assert _document(first)["verdict_outcome"] == _document(second)["verdict_outcome"]


def test_a_disposition_at_the_base_makes_the_finding_advisory_and_it_is_named_when_posted(
    env, capsys
):
    base = commit_files(env.repo, {DISPOSITIONS_PATH: _DISPOSITION})
    env.configure(carrier_mode="facts_serious")

    _, payload = env.run(capsys=capsys, base_sha=base)
    document = _document(payload)
    (finding,) = document["findings"]
    assert finding["blocking"] is False
    assert finding["disposition"]["kind"] == "false_positive"
    assert document["verdict_outcome"] == "CLEAN"

    code, posted = env.post(
        "--findings", payload["findings_file"], "--status", "clean", capsys=capsys, base_sha=base
    )

    assert code == 0, posted
    body = env.opener_state["posted_body"]
    assert "Advisory findings (1):" in body
    assert "Blocking findings" not in body
    assert "cleared by false_positive disposition" in body
    fence = _fence(env)
    assert fence["verdict_outcome"] == "CLEAN"
    assert fence["review_status"] == "clean"
    # The finding was demoted, so nothing is held open on its account.
    assert "findings_open" not in fence


def test_a_blocked_post_lists_the_blocking_finding_with_the_stanza_that_would_clear_it(
    env, capsys
):
    env.configure(carrier_mode="facts_serious")
    _, payload = env.run(capsys=capsys)

    code, posted = env.post("--findings", payload["findings_file"], "--status", "blocking", capsys=capsys)

    assert code == 0, posted
    assert posted["verdict_outcome"] == "BLOCKED"
    body = env.opener_state["posted_body"]
    assert "verdict: BLOCKED" in body
    assert "Blocking findings (1):" in body
    assert "- a.py:1 [R1] stub finding" in body
    assert "computed severity: high; blocking" in body
    assert f"add this entry to {DISPOSITIONS_PATH} on the base branch" in body
    assert '- path: "a.py"' in body
    assert "kind: accepted_risk" in body
    fence = _fence(env)
    assert fence["verdict_outcome"] == "BLOCKED"
    assert fence["review_status"] == "blocking"
    assert fence["policy_base_sha"] == env.base_sha
    assert fence["fence_schema_version"] == 3


def test_status_clean_is_refused_when_the_computed_outcome_is_blocked(env, capsys):
    env.configure(carrier_mode="facts_serious")
    _, payload = env.run(capsys=capsys)

    code, _, err = env.invoke(
        "post", "--findings", payload["findings_file"], "--status", "clean", capsys=capsys
    )

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert "--status clean" in err and "BLOCKED" in err
    assert env.opener_state["posted_body"] is None


def test_status_clean_is_refused_when_the_computed_outcome_is_degraded(env, capsys):
    env.configure(carrier_mode="array")
    _, payload = env.run(capsys=capsys)

    code, _, err = env.invoke(
        "post", "--findings", payload["findings_file"], "--status", "clean", capsys=capsys
    )

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert "--status clean" in err and "DEGRADED" in err
    assert env.opener_state["posted_body"] is None


def test_a_file_that_asserts_a_clean_outcome_is_not_believed_at_post(env, tmp_path, capsys):
    env.configure(carrier_mode="facts_serious")
    _, payload = env.run(capsys=capsys)
    document = _document(payload)
    document.update(verdict_outcome="CLEAN", verdict_reasons=[])
    for finding in document["findings"]:
        finding.update(blocking=False, computed_severity="none", severity="nit")
    edited = tmp_path / "edited.json"
    edited.write_text(json.dumps(document), encoding="utf-8")

    code, _, err = env.invoke(
        "post", "--findings", str(edited), "--status", "clean", capsys=capsys
    )

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert "BLOCKED" in err


def test_a_stricter_status_than_the_computed_one_is_honoured(env, capsys):
    env.configure(carrier_mode="facts_mild")
    _, payload = env.run(capsys=capsys)

    code, posted = env.post(
        "--findings", payload["findings_file"], "--status", "blocking", capsys=capsys
    )

    assert code == 0
    assert posted["status"] == "blocking"
    assert posted["verdict_outcome"] == "CLEAN"
    assert _fence(env)["review_status"] == "blocking"


def test_status_may_be_omitted_and_the_computed_one_is_posted(env, capsys):
    env.configure(carrier_mode="facts_mild")
    _, payload = env.run(capsys=capsys)

    code, posted = env.post("--findings", payload["findings_file"], capsys=capsys)

    assert code == 0
    assert posted["status"] == "clean"
    assert _fence(env)["review_status"] == "clean"


def test_a_bare_array_without_a_base_is_degraded_not_clean(env, tmp_path, capsys):
    bare = tmp_path / "bare.json"
    bare.write_text("[]", encoding="utf-8")

    refused, _, err = env.invoke(
        "post", "--findings", str(bare), "--head-sha", HEAD_SHA, "--status", "clean",
        capsys=capsys,
    )

    assert refused == review_cli.EXIT_FINDINGS_INVALID
    assert "DEGRADED" in err
    assert "policy base revision is not known" in err


def test_a_base_sha_that_conflicts_with_the_recorded_one_is_refused(env, capsys):
    env.configure(carrier_mode="facts_mild")
    _, payload = env.run(capsys=capsys)

    code, _, _ = env.invoke(
        "post", "--findings", payload["findings_file"], "--base-sha", "d" * 40,
        "--status", "blocking", capsys=capsys,
    )

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert env.opener_state["posted_body"] is None


def test_the_standalone_evaluate_verb_reaches_the_outcome_the_post_recorded(env, capsys):
    env.configure(carrier_mode="facts_serious")
    _, payload = env.run(capsys=capsys)
    code, posted = env.post(
        "--findings", payload["findings_file"], "--status", "blocking", capsys=capsys
    )
    assert code == 0, posted
    capsys.readouterr()

    exit_code = review_cli.main(
        [
            "evaluate", "--findings", payload["findings_file"], "--base", env.base_sha,
            "--head", HEAD_SHA, "--visibility", "private", "--repo-path", str(env.repo),
        ],
        config_root=env.cfg,
    )

    standalone = json.loads(capsys.readouterr().out)
    assert exit_code == 40
    assert standalone["outcome"] == _fence(env)["verdict_outcome"] == "BLOCKED"


@pytest.mark.parametrize("role", ["reviewer", "security"])
@pytest.mark.parametrize("delta", [False, True], ids=["full", "delta"])
def test_every_role_and_run_shape_posts_the_computed_outcome_and_the_evidence_line(
    env, tmp_path, capsys, role, delta
):
    write_profile_config(
        env.cfg, role=role, carrier=write_stub(env.stubs, "carrier", "facts_serious")
    )
    extra = ["--caller", role]
    kwargs: dict = {"identity": role}
    if delta:
        prior = tmp_path / "prior.json"
        prior.write_text(
            json.dumps(
                {
                    "owner": "some-owner", "repo": "some-repo", "pr_number": 42,
                    "head_sha": SINCE_SHA, "findings": [],
                }
            ),
            encoding="utf-8",
        )
        extra += ["--prior-findings", str(prior)]
        kwargs["compare"] = {"status": "ahead", "diff": make_diff({"a.py": 4})}

    code, payload = env.run(*extra, capsys=capsys, **kwargs)
    assert code == 0, payload
    assert payload["mode"] == ("delta" if delta else "full")
    code, posted = env.post(
        "--findings", payload["findings_file"], "--status", "blocking", "--caller", role,
        capsys=capsys, identity=role,
    )

    assert code == 0, posted
    body = env.opener_state["posted_body"]
    lines = body.splitlines()
    assert lines[0] == f"{role.upper()} — blocking (1 finding(s))"
    assert lines[1] == "verdict: BLOCKED"
    assert any(line.startswith("range: ") for line in lines)
    assert "engine: carrier" in lines
    fence = _fence(env)
    assert fence["reviewer"] == role
    assert fence["verdict_outcome"] == "BLOCKED"
    assert fence["fence_schema_version"] == 3


def test_a_kept_prior_finding_is_judged_again_under_the_current_base_and_never_dropped(
    env, tmp_path, capsys
):
    base = commit_files(env.repo, {DISPOSITIONS_PATH: _DISPOSITION})
    env.configure(carrier_mode="empty")
    prior_facts = {
        "category": "path_traversal",
        "reachable": "yes",
        "attacker_precondition": "none",
        "impact": {"confidentiality": "full", "integrity": "none", "availability": "none"},
        "durability": "durable",
    }
    prior = tmp_path / "prior.json"
    prior.write_text(
        json.dumps(
            {
                "owner": "some-owner",
                "repo": "some-repo",
                "pr_number": 42,
                "head_sha": SINCE_SHA,
                "findings": [
                    {
                        "file": "a.py", "line": 3, "rule_id": "R1", "severity": "blocking",
                        "message": "bad", "facts": prior_facts,
                        # A standing computed under an older base is not carried over.
                        "blocking": True, "computed_severity": "critical",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    _, payload = env.run(
        "--prior-findings", str(prior), capsys=capsys, base_sha=base,
        compare={"status": "ahead", "diff": make_diff({"c.py": 2})},
    )

    document = _document(payload)
    assert document["mode"] == "delta"
    # The delta's own base is the last reviewed head; policy comes from the PR's.
    assert document["base_sha"] == SINCE_SHA
    assert document["policy_base_sha"] == base
    (kept,) = document["findings"]
    assert (kept["file"], kept["line"]) == ("a.py", 3)
    assert kept["blocking"] is False
    assert kept["computed_severity"] == "high"
    assert kept["disposition"]["kind"] == "false_positive"
    assert document["carried_count"] == 1
