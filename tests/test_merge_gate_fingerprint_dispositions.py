"""A standing disposition that names a fingerprint clears a recorded block only
when the block's fingerprint is known and equal.

Every case runs the real verb on both platforms against a real git repository
whose main tip is the PR base, with fences built by the product's own builder.
The block's fingerprint comes from the fence's findings_open (current fences)
or, for a fence posted before findings_open carried it, from its
evaluated_findings."""

from __future__ import annotations

import json

import pytest
import yaml

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.fence_state import FENCE_SCHEMA_VERSIONS, FINGERPRINT_FENCE_SCHEMA_VERSION
from clagentic_loadout.merge.verdict import parse_verdict_block
from clagentic_loadout.review.finding_identity import is_fingerprint
from clagentic_loadout.merge import fence_state
from tests._ruling_host import PLATFORMS
from tests._ruling_support import FP, HEAD, LOGIN, NAME, OTHER_FP, open_entries
from tests._ruling_support import evaluated_finding as _finding
from tests._ruling_support import fence as _fence
from tests._ruling_support import gate_repo as _repo
from tests._ruling_support import tip as _tip
from tests._support.merge_verb import AllowingAuthorityProvider, RecordingTokenProvider, base_args


def _disposition(fingerprint=None) -> str:
    entry = {
        "path": "src/a.py", "rule_id": "R1", "kind": "false_positive",
        "rationale": "ruled by hand", "by": "a-maintainer", "at": "2026-01-01",
        "expires": "2999-01-01",
    }
    if fingerprint is not None:
        entry["fingerprint"] = fingerprint
    return yaml.safe_dump({"dispositions": [entry]})


def _gate(platform, fence_body, repo, capsys):
    comments = [{"id": 1, "user": {"login": LOGIN}, "body": fence_body,
                 "created_at": "2026-01-01T00:00:01Z"}]
    info = {
        "head": {"sha": HEAD}, "title": "feat: x",
        "base": {"ref": "main", "sha": _tip(repo), "repo": {"private": True, "visibility": "private"}},
    }
    argv = base_args(
        **{"--platform": platform, "--required-reviewer": f"{NAME}:{LOGIN}", "--repo-path": str(repo)}
    )
    capsys.readouterr()
    code = verb.main(
        argv,
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=PLATFORMS[platform](pr_info=info, comments=comments),
    )
    return code, capsys.readouterr().err


def _new_fence(evaluated):
    """A current fence: findings_open states the fingerprint."""
    opened = [{**entry, "fingerprint": FP} for entry in open_entries([evaluated])]
    return _fence("BLOCKED", findings=[evaluated], opened=opened)


def _old_fence(evaluated):
    """A fence posted before findings_open carried a fingerprint."""
    return _fence("BLOCKED", findings=[evaluated])


@pytest.mark.parametrize("platform", sorted(PLATFORMS))
class TestNewFence:
    def test_an_entry_with_the_blocks_fingerprint_clears_it(self, platform, tmp_path, capsys):
        repo = _repo(tmp_path, dispositions=_disposition(FP))
        code, err = _gate(platform, _new_fence(_finding()), repo, capsys)
        assert code == verb.EXIT_OK, err

    def test_an_entry_with_another_fingerprint_does_not(self, platform, tmp_path, capsys):
        repo = _repo(tmp_path, dispositions=_disposition(OTHER_FP))
        code, err = _gate(platform, _new_fence(_finding()), repo, capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert f'fingerprint: "{FP}"' in err

    def test_the_fence_entry_decides_when_evaluated_findings_carry_none(
        self, platform, tmp_path, capsys
    ):
        evaluated = _finding(fingerprint=None)
        cleared = _repo(tmp_path / "a", dispositions=_disposition(FP))
        assert _gate(platform, _new_fence(evaluated), cleared, capsys)[0] == verb.EXIT_OK
        refused = _repo(tmp_path / "b", dispositions=_disposition(OTHER_FP))
        assert _gate(platform, _new_fence(evaluated), refused, capsys)[0] == (
            verb.EXIT_GATE_RESULT_BLOCKED
        )


@pytest.mark.parametrize("platform", sorted(PLATFORMS))
class TestOldFence:
    def test_the_fingerprint_resolves_through_evaluated_findings(self, platform, tmp_path, capsys):
        repo = _repo(tmp_path, dispositions=_disposition(FP))
        code, err = _gate(platform, _old_fence(_finding()), repo, capsys)
        assert code == verb.EXIT_OK, err

    def test_another_fingerprint_does_not_clear_it(self, platform, tmp_path, capsys):
        repo = _repo(tmp_path, dispositions=_disposition(OTHER_FP))
        code, _ = _gate(platform, _old_fence(_finding()), repo, capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_without_any_fingerprint_a_fingerprinted_entry_does_not_match(
        self, platform, tmp_path, capsys
    ):
        repo = _repo(tmp_path, dispositions=_disposition(FP))
        code, err = _gate(platform, _old_fence(_finding(fingerprint=None)), repo, capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "cannot be resolved" in err

    def test_without_any_fingerprint_a_path_and_rule_entry_matches(self, platform, tmp_path, capsys):
        repo = _repo(tmp_path, dispositions=_disposition())
        code, err = _gate(platform, _old_fence(_finding(fingerprint=None)), repo, capsys)
        assert code == verb.EXIT_OK, err


def test_the_fingerprint_shape_check_agrees_with_the_derivation_module():
    for value in (FP, OTHER_FP, "fp2:aaa", "fp1:0123456789abcdef", "fp2:0123456789ABCDEF", None, 5):
        assert fence_state.is_fingerprint(value) == is_fingerprint(value)


def test_a_fence_whose_open_entries_carry_a_fingerprint_is_stamped_version_six():
    parsed = parse_verdict_block(_new_fence(_finding()))
    assert parsed["fence_schema_version"] == FINGERPRINT_FENCE_SCHEMA_VERSION == 6
    assert 6 in FENCE_SCHEMA_VERSIONS
    assert parsed["findings_open"][0]["fingerprint"] == FP


def test_a_fence_without_a_fingerprint_keeps_its_version_and_is_still_read():
    parsed = parse_verdict_block(_old_fence(_finding()))
    assert parsed["fence_schema_version"] == 3
    assert "fingerprint" not in parsed["findings_open"][0]


def test_a_malformed_fingerprint_in_findings_open_is_refused():
    entry = {**open_entries([_finding()])[0], "fingerprint": "fp2:nope"}
    with pytest.raises(ValueError, match="fingerprint"):
        _fence("BLOCKED", findings=[_finding()], opened=[entry])
