"""`loadout-review post` derives `cleared_claims` for the earlier open findings
the verdict being posted positively resolves, and leaves every other one open
for the merge gate to refuse."""

from __future__ import annotations

import json

import pytest
import yaml

from clagentic_loadout.merge import verdict as gate
from clagentic_loadout.merge.errors import VerdictPriorFindingsOpenError
from clagentic_loadout.merge.verdict import parse_verdict_block
from clagentic_loadout.review.evaluation import finding_state_id
from clagentic_loadout.review.policy_source import DISPOSITIONS_PATH
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import HEAD_SHA, Env
from tests._review_evaluate_support import commit_files, facts

NEW_HEAD = "c" * 40
SERIOUS = facts(confidentiality="full", integrity="full", availability="full")


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    environment = Env(tmp_path)
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return environment


def _finding(line, rule):
    return {"file": "a.py", "line": line, "rule_id": rule, "message": f"{rule} is wrong", "facts": SERIOUS}


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _comment(cid, body):
    return {
        "id": cid, "user": {"login": "reviewer"}, "body": body,
        "created_at": f"2099-01-01T00:00:0{cid}Z", "html_url": "http://c",
    }


def _first_round(env, tmp_path, capsys, findings):
    """A real BLOCKED post at HEAD_SHA; returns the comment it landed as."""
    env.opener_state["posted_body"] = None
    path = _write(
        tmp_path / "round1.json",
        {"owner": "some-owner", "repo": "some-repo", "pr_number": 42, "head_sha": HEAD_SHA,
         "findings": findings},
    )
    code, _ = env.post("--findings", str(path), capsys=capsys)
    assert code == 0
    return _comment(1, env.opener_state["posted_body"])


def _run_dir(tmp_path, resolved, *, head=NEW_HEAD, bound=NEW_HEAD):
    directory = tmp_path / "run"
    _write(directory / "run-binding.json", {
        "repo": "some-owner/some-repo", "pr_number": 42, "head_sha": bound, "since_head": HEAD_SHA,
    })
    _write(directory / "findings.json", {
        "head_sha": head, "findings": [], **({"resolved": resolved} if resolved is not None else {}),
    })
    return directory


def _resolved(line, rule):
    return {"file": "a.py", "line": line, "rule_id": rule, "message": f"{rule} is wrong", "by": "reviewer"}


def _second_round(env, tmp_path, capsys, prior, *extra, findings=()):
    env.opener_state["posted_body"] = None
    path = _write(tmp_path / "judged.json", list(findings))
    code, out, err = env.invoke(
        "post", "--findings", str(path), "--head-sha", NEW_HEAD, *extra,
        capsys=capsys, head_sha=NEW_HEAD, comments=[prior], compare={"status": "diverged"},
    )
    assert code == 0, err
    payload = json.loads(out.strip().splitlines()[-1])
    payload["stderr"] = err
    return payload, parse_verdict_block(env.opener_state["posted_body"])


def _gate(prior, env):
    landed = _comment(5, env.opener_state["posted_body"])
    return gate.read_reviewer_verdict(
        [prior, landed], expected_login="reviewer", current_head_sha=NEW_HEAD, pr_number=42,
        owner="some-owner", repo="some-repo", expected_reviewer_name="reviewer",
    )


def _ids(*pairs):
    return [finding_state_id("a.py", line, rule) for line, rule in pairs]


def test_a_bare_array_post_clears_what_the_run_directory_resolved(env, tmp_path, capsys):
    prior = _first_round(env, tmp_path, capsys, [_finding(3, "R1"), _finding(4, "R2")])
    run_dir = _run_dir(tmp_path, [_resolved(3, "R1"), _resolved(4, "R2")])

    payload, fence = _second_round(env, tmp_path, capsys, prior, "--run-dir", str(run_dir))

    a, b = _ids((3, "R1"), (4, "R2"))
    assert payload["cleared_finding_ids"] == [a, b]
    assert payload["uncleared_finding_ids"] == []
    assert [c["id"] for c in fence["cleared_claims"]] == [a, b]
    assert {c["head"] for c in fence["cleared_claims"]} == {NEW_HEAD}
    assert fence["cleared_claims"][0]["evidence"] == f"not raised at {NEW_HEAD[:12]} after re-review"
    verdict = _gate(prior, env)
    gate.assert_prior_findings_resolved(verdict, "reviewer")
    assert [f["id"] for f, _ in verdict.cleared_prior_findings] == [a, b]


def test_an_open_finding_the_run_did_not_resolve_stays_open_and_the_gate_names_it(
    env, tmp_path, capsys
):
    prior = _first_round(env, tmp_path, capsys, [_finding(3, "R1"), _finding(4, "R2")])
    run_dir = _run_dir(tmp_path, [_resolved(3, "R1")])

    payload, fence = _second_round(env, tmp_path, capsys, prior, "--run-dir", str(run_dir))

    a, b = _ids((3, "R1"), (4, "R2"))
    assert [c["id"] for c in fence["cleared_claims"]] == [a]
    assert payload["uncleared_finding_ids"] == [b]
    with pytest.raises(VerdictPriorFindingsOpenError, match=b):
        gate.assert_prior_findings_resolved(_gate(prior, env), "reviewer")


def test_silence_alone_clears_nothing_and_the_missing_source_is_named(env, tmp_path, capsys):
    prior = _first_round(env, tmp_path, capsys, [_finding(3, "R1")])

    payload, fence = _second_round(env, tmp_path, capsys, prior)

    assert "cleared_claims" not in fence
    assert payload["uncleared_finding_ids"] == _ids((3, "R1"))
    assert "the reviewer-side resolved list is missing" in payload["stderr"]
    assert "no run directory was found" in payload["stderr"]


def test_a_run_directory_for_another_head_clears_nothing_from_any_reviewer_source(
    env, tmp_path, capsys
):
    prior = _first_round(env, tmp_path, capsys, [_finding(3, "R1")])
    run_dir = _run_dir(tmp_path, [_resolved(3, "R1")], bound="d" * 40)

    payload, fence = _second_round(env, tmp_path, capsys, prior, "--run-dir", str(run_dir))

    assert "cleared_claims" not in fence
    assert "is bound to head" in payload["stderr"]


def test_an_object_findings_document_resolved_list_is_read_too(env, tmp_path, capsys):
    prior = _first_round(env, tmp_path, capsys, [_finding(3, "R1")])
    env.opener_state["posted_body"] = None
    path = _write(tmp_path / "doc.json", {
        "owner": "some-owner", "repo": "some-repo", "pr_number": 42, "head_sha": NEW_HEAD,
        "findings": [], "resolved": [_resolved(3, "R1")],
    })

    code, payload = env.post(
        "--findings", str(path), capsys=capsys, head_sha=NEW_HEAD, comments=[prior]
    )

    assert code == 0
    assert payload["cleared_finding_ids"] == _ids((3, "R1"))


def test_a_base_disposition_that_demotes_a_finding_clears_it(env, tmp_path, capsys):
    prior = _first_round(env, tmp_path, capsys, [_finding(3, "R1")])
    entry = {
        "path": "a.py", "rule_id": "R1", "kind": "by_design", "rationale": "ruled",
        "by": "a-maintainer", "at": "2026-01-01", "expires": "2999-01-01",
    }
    env.base_sha = commit_files(env.repo, {DISPOSITIONS_PATH: yaml.safe_dump({"dispositions": [entry]})})

    payload, fence = _second_round(
        env, tmp_path, capsys, prior, "--base-sha", env.base_sha, findings=[_finding(3, "R1")]
    )

    assert payload["cleared_finding_ids"] == _ids((3, "R1"))
    assert fence["cleared_claims"][0]["evidence"].startswith("demoted by base disposition by_design")


def test_a_caller_ruling_clears_a_finding_the_review_no_longer_reports(env, tmp_path, capsys):
    prior = _first_round(env, tmp_path, capsys, [_finding(3, "R1")])
    rulings = _write(tmp_path / "rulings.json", [{"id": "a.py:3:R1", "reason": "fixed in review"}])

    payload, fence = _second_round(
        env, tmp_path, capsys, prior, "--resolved-findings", str(rulings)
    )

    assert fence["cleared_claims"][0]["evidence"] == "fixed in review"
    assert fence["resolved"][0]["by"] == "caller"
    assert "matches no prior finding" not in payload["stderr"]


def test_a_reviewer_drop_that_maps_to_one_prior_finding_clears_it(env, tmp_path, capsys):
    prior = _first_round(env, tmp_path, capsys, [_finding(3, "R1")])
    dropped = _write(tmp_path / "dropped.json", [{
        "file": "a.py", "line": 3, "rule_id": "R1", "message": "R1 is wrong",
        "reason": "ruled by design",
    }])

    payload, fence = _second_round(env, tmp_path, capsys, prior, "--dropped", str(dropped))

    assert fence["cleared_claims"][0]["evidence"] == (
        f"dropped by reviewer at {NEW_HEAD[:12]}: ruled by design"
    )


def test_a_reviewer_drop_that_maps_to_no_prior_finding_clears_nothing(env, tmp_path, capsys):
    prior = _first_round(env, tmp_path, capsys, [_finding(3, "R1")])
    dropped = _write(tmp_path / "dropped.json", [{
        "file": "a.py", "line": 3, "rule_id": "OTHER", "message": "x", "reason": "by design",
    }])

    payload, fence = _second_round(env, tmp_path, capsys, prior, "--dropped", str(dropped))

    assert "cleared_claims" not in fence
    with pytest.raises(VerdictPriorFindingsOpenError, match=_ids((3, "R1"))[0]):
        gate.assert_prior_findings_resolved(_gate(prior, env), "reviewer")


def test_a_drop_beside_two_priors_of_one_file_and_rule_clears_neither(env, tmp_path, capsys):
    prior = _first_round(env, tmp_path, capsys, [_finding(3, "R1"), _finding(8, "R1")])
    dropped = _write(tmp_path / "dropped.json", [{
        "file": "a.py", "line": 5, "rule_id": "R1", "message": "x", "reason": "by design",
    }])

    payload, fence = _second_round(env, tmp_path, capsys, prior, "--dropped", str(dropped))

    assert "cleared_claims" not in fence
    assert sorted(payload["uncleared_finding_ids"]) == sorted(_ids((3, "R1"), (8, "R1")))


def test_a_first_review_with_no_earlier_fence_posts_as_before(env, tmp_path, capsys):
    env.opener_state["posted_body"] = None
    path = _write(tmp_path / "first.json", {
        "owner": "some-owner", "repo": "some-repo", "pr_number": 42, "head_sha": HEAD_SHA,
        "findings": [_finding(3, "R1")],
    })

    code, payload = env.post("--findings", str(path), capsys=capsys)

    assert code == 0
    assert payload["cleared_finding_ids"] == []
    assert "cleared_claims" not in parse_verdict_block(env.opener_state["posted_body"])
