"""`loadout-merge` names each dropped candidate a reviewer's fence marks unproven, in
its output and its merge-completion attestation, and the gate outcome is the same
whether the drop is unproven, proven, or from a fence written before the keys
existed (which is not reported)."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.unproven_drops import unproven_drop_lines
from clagentic_loadout.merge.verdict import build_verdict_block
from tests.test_merge_verb_attestation import (
    _AllowingAuthorityProvider,
    _FULL_SHA,
    _RecordingTokenProvider,
    _base_args,
    _forgejo_opener,
    _github_opener,
)

OTHER = "c" * 40
_DROP = {"file": "b.py", "line": 7, "rule_id": "R2", "message": "maybe a race", "reason": "held"}
FENCES = {
    "unproven": {**_DROP, "read_at": OTHER, "proven": False},
    "unproven-no-read-at": {**_DROP, "read_at": None, "proven": False},
    "proven": {**_DROP, "read_at": _FULL_SHA, "proven": True},
    "pre-change": dict(_DROP),
}
OPENERS = {"forgejo": _forgejo_opener, "github": _github_opener}


def _merge(platform: str, drop: dict, capsys):
    block = build_verdict_block(
        "some-reviewer", "clean", _FULL_SHA, 1, findings_state={"dropped": [drop]}
    )
    opener, posted = OPENERS[platform](
        pr_info={"head": {"sha": _FULL_SHA}, "title": "feat: ship it"},
        comments=[{"id": 1, "user": {"login": "reviewer-login"}, "body": block}],
        ci_state="success",
    )
    argv = _base_args(
        platform,
        **{"--expected-head-sha": _FULL_SHA, "--required-reviewer": "some-reviewer:reviewer-login"},
    )
    capsys.readouterr()
    code = verb.main(
        argv,
        token_provider=_RecordingTokenProvider(),
        authority_provider=_AllowingAuthorityProvider(),
        opener=opener,
    )
    captured = capsys.readouterr()
    result = json.loads(captured.out.strip().splitlines()[-1])
    return code, result, captured.err, posted[0]["body"]


@pytest.mark.parametrize("platform", ["forgejo", "github"])
def test_an_unproven_drop_is_named_in_the_output_and_the_attestation_and_gates_nothing(
    platform, capsys
):
    outcomes = {name: _merge(platform, drop, capsys) for name, drop in FENCES.items()}

    # The gate outcome is the same for every fence.
    assert {code for code, *_ in outcomes.values()} == {verb.EXIT_OK}
    assert len({json.dumps(result["verdict_evaluations"]) for _, result, *_ in outcomes.values()}) == 1
    for name in ("unproven", "unproven-no-read-at"):
        _, result, err, attestation = outcomes[name]
        read = f"read at {OTHER}" if name == "unproven" else "no read_at recorded"
        line = f"some-reviewer: dropped b.py:7:R2 is unproven ({read})"
        assert line in err and result["unproven_drops"] == [line]
        assert "Unproven drops" in attestation and line in attestation
    # A proven drop and a fence from before the keys existed are not reported.
    for name in ("proven", "pre-change"):
        _, result, err, attestation = outcomes[name]
        assert "unproven" not in err and "unproven_drops" not in result
        assert "Unproven drops" not in attestation


@pytest.mark.parametrize(
    "dropped,lines",
    [
        ([{"file": "a.py", "line": 1, "rule_id": "R1"}], 0),
        ([{"file": "a.py", "line": 1, "rule_id": "R1", "proven": True, "read_at": "x"}], 0),
        ([{"file": "a.py", "line": 1, "rule_id": "R1", "proven": False}], 1),
        (
            [
                {"file": "a.py", "line": 1, "rule_id": "R1", "proven": False, "read_at": "x"},
                {"file": "b.py", "line": 2, "rule_id": "R2", "proven": False},
            ],
            2,
        ),
    ],
    ids=["no-key", "proven", "unproven", "two-unproven"],
)
def test_only_an_entry_whose_proven_key_is_false_is_reported(dropped, lines):
    assert len(unproven_drop_lines("reviewer", dropped)) == lines
