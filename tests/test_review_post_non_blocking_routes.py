"""The two routes by which a reviewer's non-blocking note posts clean, and the
labels that are not one.

`loadout-review post` recomputes the verdict from each finding's facts. It reads
no severity label, no rule_id and no marker as a reason to clear a finding,
because the findings file is the reviewer's to write and a relabelled real
blocker would then clear itself. A producer that assesses a candidate as not a
defect of the change under review has two routes the evaluator does honour:

* state complete low-risk facts, so the finding scores none and does not block;
* list the candidate in ``--dropped`` with its reason, so the drop wins.

Each captured shape below is pinned in its producer-fixed form (posts clean) and
in the form a producer emitted before using a route (refused by ``--status
clean``), so the contract cannot drift in either direction.
"""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import HEAD_SHA, Env
from tests._review_evaluate_support import facts

OUTSIDE_FILE = "scripts/crew_runtime/gate_report.py"
OUTSIDE_RULE = "review.outside-range.1"
DOWNGRADED_FILE = "src/routes/choice.py"
DOWNGRADED_RULE = "path-choice.5"


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


def _outside_range(**extra):
    """A scanner hit on an unchanged context line that the judge demoted."""
    return {
        "file": OUTSIDE_FILE, "line": 40, "rule_id": OUTSIDE_RULE, "severity": "nit",
        "message": "use-defused-xml-parse on an unchanged import line, outside the diff range",
        **extra,
    }


def _downgraded(**extra):
    """A finding in a floor category that the judge demoted to a nit."""
    return {
        "file": DOWNGRADED_FILE, "line": 12, "rule_id": DOWNGRADED_RULE, "severity": "nit",
        "message": "the chosen path is not checked against the caller's role",
        "facts": facts(
            category="authn_authz", precondition="can_open_pr", confidentiality="partial"
        ),
        **extra,
    }


def _low_risk_facts():
    return facts(category="other", reachable="no")


def _dropped_entry(finding, reason="judged not a failure sequence"):
    return {
        "file": finding["file"], "line": finding["line"], "rule_id": finding["rule_id"],
        "message": finding["message"], "reason": reason,
    }


def _post(env, tmp_path, capsys, findings, dropped=None, status="clean"):
    """(exit code, stdout, stderr) of one post asking for *status*."""
    extra = ["--dropped", str(_write(tmp_path, "dropped.json", dropped))] if dropped else []
    return env.invoke(
        "post",
        "--findings", str(_write(tmp_path, "findings.json", findings)),
        "--head-sha", HEAD_SHA, "--base-sha", env.base_sha, "--status", status,
        *extra,
        capsys=capsys,
    )


def _result(out):
    return json.loads(out.strip().splitlines()[-1])


def test_an_outside_range_hit_with_no_facts_is_degraded_and_clean_is_refused(
    env, tmp_path, capsys
):
    code, _, err = _post(env, tmp_path, capsys, [_outside_range()])

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert "contradicts the computed verdict DEGRADED" in err
    assert f"{OUTSIDE_FILE}:40:{OUTSIDE_RULE}" in err
    assert "--dropped" in err and "state its facts" in err
    assert env.opener_state["posted_body"] is None


def test_an_outside_range_hit_that_states_complete_low_risk_facts_posts_clean(
    env, tmp_path, capsys
):
    hit = _outside_range(facts=_low_risk_facts())

    code, out, err = _post(env, tmp_path, capsys, [hit])

    assert code == 0, err
    assert _result(out)["verdict_outcome"] == "CLEAN"
    assert _result(out)["blocking_count"] == 0
    body = env.opener_state["posted_body"]
    assert "Advisory findings (1):" in body
    assert f"{OUTSIDE_FILE}:40 [{OUTSIDE_RULE}]" in body


def test_an_outside_range_hit_listed_as_dropped_posts_clean_and_is_listed(env, tmp_path, capsys):
    hit = _outside_range()

    code, out, err = _post(env, tmp_path, capsys, [hit], [_dropped_entry(hit, "outside the range")])

    assert code == 0, err
    assert _result(out)["verdict_outcome"] == "CLEAN"
    assert _result(out)["dropped_count"] == 1
    assert "outside the range" in env.opener_state["posted_body"]


def test_a_judge_downgraded_floor_finding_keeps_its_floor_facts_and_blocks(
    env, tmp_path, capsys
):
    code, _, err = _post(env, tmp_path, capsys, [_downgraded()])

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert "contradicts the computed verdict BLOCKED" in err
    assert f"{DOWNGRADED_FILE}:12:{DOWNGRADED_RULE} blocks" in err
    assert env.opener_state["posted_body"] is None


def test_a_judge_downgrade_with_no_failure_sequence_listed_as_dropped_posts_clean(
    env, tmp_path, capsys
):
    finding = _downgraded()

    code, out, err = _post(
        env, tmp_path, capsys, [finding], [_dropped_entry(finding, "no failure sequence")]
    )

    assert code == 0, err
    assert _result(out)["verdict_outcome"] == "CLEAN"
    assert _result(out)["finding_count"] == 0
    assert "no failure sequence" in env.opener_state["posted_body"]


@pytest.mark.parametrize(
    "relabel",
    [
        {"severity": "nit"},
        {"rule_id": OUTSIDE_RULE},
        {"rule_id": OUTSIDE_RULE, "severity": "nit"},
    ],
    ids=["nit", "outside-range-rule", "both"],
)
def test_relabelling_an_in_range_floor_blocker_does_not_clear_it(env, tmp_path, capsys, relabel):
    blocker = _downgraded(**relabel)

    code, _, err = _post(env, tmp_path, capsys, [blocker])

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert "contradicts the computed verdict BLOCKED" in err
    assert env.opener_state["posted_body"] is None


@pytest.mark.parametrize("severity", ["nit", "blocking"])
def test_an_in_range_finding_with_absent_facts_still_degrades_whatever_its_rule_id(
    env, tmp_path, capsys, severity
):
    finding = {
        "file": "a.py", "line": 3, "rule_id": OUTSIDE_RULE, "severity": severity,
        "message": "relabelled as outside the range",
    }

    code, _, err = _post(env, tmp_path, capsys, [finding])

    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert "contradicts the computed verdict DEGRADED" in err
    assert "no facts were stated" in err or "facts absent" in err
