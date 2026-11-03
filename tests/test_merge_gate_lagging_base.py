"""The merge gate reads policy at the base branch's tip now, not at the base
commit the host's pull request record kept at the PR's last head push.

Every case runs the real verb on both platforms against a real git repository
whose main tip is AHEAD of the PR record's base.sha, the shape a host leaves
behind when main moves after the last push."""

from __future__ import annotations

import pytest
import yaml

from clagentic_loadout.merge import verb
from clagentic_loadout.review.policy_source import DISPOSITIONS_PATH
from tests._gate_repo import commit_files, git
from tests._ruling_host import PLATFORMS
from tests._ruling_support import (
    HEAD,
    LOGIN,
    NAME,
    pin_gate_clock,
)
from tests._ruling_support import evaluated_finding as _finding
from tests._ruling_support import fence as _fence
from tests._ruling_support import gate_repo as _repo
from tests._support.merge_verb import AllowingAuthorityProvider, RecordingTokenProvider, base_args

_ENTRY = {
    "path": "src/**",
    "rule_id": "R1",
    "kind": "false_positive",
    "rationale": "ruled by hand",
    "by": "a-maintainer",
    "at": "2026-01-01",
    "expires": "2999-01-01",
}
_WITH_DISPOSITION = yaml.safe_dump({"dispositions": [_ENTRY]})
_WITHOUT_DISPOSITION = yaml.safe_dump({"dispositions": []})
_MODES = [pytest.param([], id="merge"), pytest.param(["--report-only"], id="report-only")]


@pytest.fixture(autouse=True)
def gate_clock(monkeypatch):
    pin_gate_clock(monkeypatch)


def _sha(repo, ref="main"):
    return git(repo, "rev-parse", ref)


def _gate(platform, repo, recorded_base, capsys, mode=(), *, drop_base_ref=False, **host):
    """Run the gate on a recorded BLOCKED fence whose single finding a
    disposition covers, with the PR record's base.sha at *recorded_base*."""
    info = {
        "head": {"sha": HEAD},
        "title": "feat: x",
        "base": {"ref": "main", "sha": recorded_base, "repo": {"private": True, "visibility": "private"}},
    }
    if drop_base_ref:
        info["base"].pop("ref")
    comments = [
        {
            "id": 1,
            "user": {"login": LOGIN},
            "body": _fence("BLOCKED", findings=[_finding()]),
            "created_at": "2026-01-01T00:00:01Z",
        }
    ]
    opener = PLATFORMS[platform](pr_info=info, comments=comments, no_base_ref=drop_base_ref, **host)
    argv = base_args(
        **{"--platform": platform, "--required-reviewer": f"{NAME}:{LOGIN}", "--repo-path": str(repo)}
    )
    capsys.readouterr()
    code = verb.main(
        [*argv, *mode],
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=opener,
    )
    return code, capsys.readouterr().err


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("platform", sorted(PLATFORMS))
class TestPolicyIsReadAtTheBaseTip:
    def test_a_disposition_merged_after_the_last_push_clears_the_recorded_block(
        self, platform, mode, tmp_path, capsys
    ):
        repo = _repo(tmp_path)
        lagging = _sha(repo)
        commit_files(repo, {DISPOSITIONS_PATH: _WITH_DISPOSITION}, "chore: add disposition")
        tip = _sha(repo)
        assert tip != lagging

        code, err = _gate(platform, repo, lagging, capsys, mode, branch_tip=tip)

        assert code == verb.EXIT_OK, err

    def test_a_disposition_removed_after_the_last_push_no_longer_applies(
        self, platform, mode, tmp_path, capsys
    ):
        repo = _repo(tmp_path, dispositions=_WITH_DISPOSITION)
        lagging = _sha(repo)
        commit_files(repo, {DISPOSITIONS_PATH: _WITHOUT_DISPOSITION}, "chore: remove disposition")
        tip = _sha(repo)

        code, err = _gate(platform, repo, lagging, capsys, mode, branch_tip=tip)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED, err
        assert "src/a.py:10:R1" in err

    def test_the_same_lagging_record_with_the_disposition_still_on_the_tip_passes(
        self, platform, mode, tmp_path, capsys
    ):
        """Control for the removal case: the disposition's presence at the tip,
        not the lag, is what decides."""
        repo = _repo(tmp_path, dispositions=_WITH_DISPOSITION)
        lagging = _sha(repo)
        commit_files(repo, {"README.md": "unrelated\n"}, "docs: unrelated")

        code, err = _gate(platform, repo, lagging, capsys, mode, branch_tip=_sha(repo))

        assert code == verb.EXIT_OK, err


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("platform", sorted(PLATFORMS))
class TestAnUnreadableTipRefuses:
    @pytest.mark.parametrize("status", [404, 500])
    def test_a_failed_branch_read_refuses_and_never_uses_the_recorded_base(
        self, platform, mode, status, tmp_path, capsys
    ):
        # The recorded base would clear the block; reaching a pass would mean a fallback.
        repo = _repo(tmp_path, dispositions=_WITH_DISPOSITION)

        code, err = _gate(platform, repo, _sha(repo), capsys, mode, branch_tip_http=status)

        assert code == verb.EXIT_GATE_FACT_UNAVAILABLE, err
        assert "base branch tip" in err

    def test_a_malformed_tip_refuses(self, platform, mode, tmp_path, capsys):
        repo = _repo(tmp_path, dispositions=_WITH_DISPOSITION)

        code, err = _gate(platform, repo, _sha(repo), capsys, mode, branch_tip="not-a-sha")

        assert code == verb.EXIT_GATE_FACT_UNAVAILABLE, err

    def test_a_record_without_a_base_branch_refuses(self, platform, mode, tmp_path, capsys):
        repo = _repo(tmp_path, dispositions=_WITH_DISPOSITION)

        code, err = _gate(platform, repo, _sha(repo), capsys, mode, drop_base_ref=True)

        assert code == verb.EXIT_GATE_FACT_UNAVAILABLE, err
        assert "no base branch" in err
