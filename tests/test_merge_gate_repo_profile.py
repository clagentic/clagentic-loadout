"""The merge gate does not refuse on the repo profile.

A repository with no profile, or an invalid one, gets a finding from the review
that scores `none`; a rule hit under a valid profile is held at `low`. Each case
runs the real merge verb against a real repository, with the findings the review
itself would state (not hand-written ones), and the gate must reach the same
CLEAN outcome the fence records and proceed."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.merge import verb
from clagentic_loadout.review import profile_rules as pr
from clagentic_loadout.review.chunking import plan_chunks
from clagentic_loadout.review.policy_source import PROFILE_PATH, read_policy
from clagentic_loadout.review.profile_settings import ProfileSettings
from tests._gate_repo import commit_files
from tests.test_merge_computed_verdict import _comment, _fence, _merge, _repo, _tip

HOST = "host-one.example.test"
_PROFILE_WITH_DENY = f"class: released-tool\ndeny:\n  hosts: [{HOST}]\n"
_DIFF = (
    "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,1 +1,2 @@\n"
    f" x = 1\n+url = 'https://{HOST}/api'\n"
)


def _review_findings(repo) -> list[dict]:
    """What `loadout-review run` states for _DIFF against *repo*'s tip."""
    return pr.profile_findings(
        read_policy(repo, _tip(repo)), plan_chunks(_DIFF), ProfileSettings(require=True)
    )


@pytest.mark.parametrize(
    "profile_text, expected_rule",
    [
        (None, pr.RULE_MISSING),
        ("class: not-a-class\n", pr.RULE_INVALID),
        ("class: [unclosed\n", pr.RULE_INVALID),
        (_PROFILE_WITH_DENY, pr.RULE_DENIED),
    ],
    ids=["missing", "invalid-value", "invalid-yaml", "rule-hit"],
)
def test_a_clean_fence_carrying_a_profile_finding_proceeds_to_merge(
    tmp_path, capsys, profile_text, expected_rule
):
    repo = _repo(tmp_path)
    if profile_text is not None:
        commit_files(repo, {PROFILE_PATH: profile_text}, "chore: add profile")
    findings = _review_findings(repo)
    assert [f["rule_id"] for f in findings] == [expected_rule]

    run = _merge([_comment(1, _fence("CLEAN", findings=findings))], repo, capsys)

    assert run.code == verb.EXIT_OK, run.err
    result = json.loads(run.out.strip().splitlines()[-1])
    (evaluation,) = result["verdict_evaluations"]
    assert evaluation["fence_outcome"] == evaluation["gate_outcome"] == "CLEAN"


def test_a_profile_that_goes_missing_after_the_review_does_not_change_the_gates_outcome(
    tmp_path, capsys
):
    """The review saw a rule hit under a profile; by merge time the base has no
    profile. The finding is advisory either way, and the gate computes the same
    outcome from the fence's findings."""
    repo = _repo(tmp_path)
    commit_files(repo, {PROFILE_PATH: _PROFILE_WITH_DENY}, "chore: add profile")
    findings = _review_findings(repo)
    commit_files(repo, {PROFILE_PATH: "class: not-a-class\n"}, "chore: break profile")

    run = _merge([_comment(1, _fence("CLEAN", findings=findings))], repo, capsys)

    assert run.code == verb.EXIT_OK, run.err
