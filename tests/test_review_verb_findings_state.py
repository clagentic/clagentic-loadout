"""review-post: structured findings state is accepted as JSON fields and
rendered by the tool inside the fence, never as body text."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.merge.verdict import build_verdict_block, parse_verdict_block
from clagentic_loadout.review import verb
from clagentic_loadout.transport import provider_config
from tests.test_review_verb import (
    _github_verdict_opener,
    _RecordingTokenProvider,
    _RefusingTokenProvider,
    _run_main,
)

HEAD = "a" * 40
OLDER = "b" * 40
OPEN = {"findings_open": [{"id": "F1", "rule_id": "R1", "head": HEAD}]}
CLEARED = {"cleared_claims": [{"id": "F1", "head": HEAD, "evidence": "guard added"}]}
SCANNERS = {"scanners_run": [{"scanner": "alpha", "status": "ran"}]}


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "no-user-config")


def _findings_stdin(status, state, findings=()):
    return json.dumps({"review_status": status, "findings": list(findings), **state}).encode()


_ARGV_FINDINGS = [
    "--caller", "reviewer", "--platform", "github", "--verdict-findings",
    "--verdict-head-sha", HEAD, "some-owner/some-repo", "42",
]
_ARGV_STATUS = [
    "--caller", "reviewer", "--platform", "github", "--verdict-review-status", "clean",
    "--verdict-head-sha", HEAD, "some-owner/some-repo", "42",
]


def _posted_fence(captured):
    return parse_verdict_block(captured["posted_body"])


class TestStateIsRenderedInsideTheToolBuiltFence:
    def test_findings_route(self, monkeypatch, capsys):
        captured: dict = {}
        state = {**OPEN, **SCANNERS, "supersedes": 9}
        code = _run_main(
            _ARGV_FINDINGS,
            stdin_bytes=_findings_stdin("blocking", state),
            token_provider=_RecordingTokenProvider(),
            opener=_github_verdict_opener(capture_into=captured),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        fence = _posted_fence(captured)
        assert fence["fence_schema_version"] == 2
        assert fence["findings_open"] == OPEN["findings_open"]
        assert fence["scanners_run"] == SCANNERS["scanners_run"]
        assert fence["supersedes"] == 9
        assert json.loads(capsys.readouterr().out)["verdict_block_verified"] is True

    def test_review_status_route(self, monkeypatch):
        captured: dict = {}
        stdin = json.dumps({"body": "No issues.", "review_status": "clean", **CLEARED}).encode()
        code = _run_main(
            _ARGV_STATUS,
            stdin_bytes=stdin,
            token_provider=_RecordingTokenProvider(),
            opener=_github_verdict_opener(capture_into=captured),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert _posted_fence(captured)["cleared_claims"] == CLEARED["cleared_claims"]
        assert "guard added" not in captured["posted_body"].split("```review-result")[0]

    def test_no_state_leaves_the_fence_unversioned(self, monkeypatch):
        captured: dict = {}
        code = _run_main(
            _ARGV_FINDINGS,
            stdin_bytes=_findings_stdin("clean", {}),
            token_provider=_RecordingTokenProvider(),
            opener=_github_verdict_opener(capture_into=captured),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert "fence_schema_version" not in _posted_fence(captured)


class TestStateCannotArriveAnyOtherWay:
    def test_a_hand_authored_fence_carrying_state_is_refused_like_any_hand_authored_fence(
        self, monkeypatch
    ):
        hand_authored = build_verdict_block("reviewer", "clean", HEAD, 42, findings_state=CLEARED)
        stdin = json.dumps({"body": f"Looks fine.\n{hand_authored}", "review_status": "clean"}).encode()
        code = _run_main(
            _ARGV_STATUS,
            stdin_bytes=stdin,
            token_provider=_RefusingTokenProvider(),
            opener=None,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERDICT_BLOCK_USAGE

    def test_state_without_a_verdict_route_is_refused(self, monkeypatch):
        stdin = json.dumps({"body": "plain comment", **OPEN}).encode()
        code = _run_main(
            ["--caller", "reviewer", "--platform", "github", "some-owner/some-repo", "42"],
            stdin_bytes=stdin,
            token_provider=_RefusingTokenProvider(),
            opener=None,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERDICT_BLOCK_USAGE

    @pytest.mark.parametrize(
        "status,state",
        [
            ("clean", OPEN),
            ("blocking", {"cleared_claims": [{"id": "F1", "head": OLDER, "evidence": "x"}]}),
            ("blocking", {"findings_open": [{"id": "F1", "rule_id": "R1"}]}),
            ("blocking", {"scanners_run": None}),
        ],
        ids=["clean-with-open", "claim-at-other-head", "missing-field", "explicit-null"],
    )
    def test_malformed_or_contradictory_state_is_refused_before_any_post(
        self, monkeypatch, status, state
    ):
        code = _run_main(
            _ARGV_FINDINGS,
            stdin_bytes=_findings_stdin(status, state),
            token_provider=_RefusingTokenProvider(),
            opener=None,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERDICT_BLOCK_USAGE


def test_a_fence_that_lands_without_its_state_fails_the_readback(monkeypatch, capsys):
    # The transport readback only accepts a landed body that contains the posted
    # one, so a fence cannot be replaced in place; a trailing stateless fence
    # (which the last-fence-wins parse reads) is the one reachable shape. The
    # state round-trip check runs before the single-fence backstop, and the
    # assertions below pin that it, not the backstop, is what refused.
    def drop_state(posted):
        return posted + build_verdict_block("reviewer", "blocking", HEAD, 42)

    code = _run_main(
        _ARGV_FINDINGS,
        stdin_bytes=_findings_stdin("blocking", OPEN),
        token_provider=_RecordingTokenProvider(),
        opener=_github_verdict_opener(landed_body=drop_state),
        monkeypatch=monkeypatch,
    )
    assert code == verb.EXIT_VERDICT_BLOCK_MISMATCH
    err = capsys.readouterr().err
    assert "findings_open: expected" in err
    assert "fence_schema_version: expected" in err
