"""The merge gate and scanner findings: the gate reads the posted fence at the head,
recomputes the verdict from the findings it carries, and so blocks on an in-range
scanner finding that was not dropped and ignores outside-range context."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge import verb
from clagentic_loadout.review.scanner_findings import finding_of
from clagentic_loadout.review.scanner_input import ScannerHit
from clagentic_loadout.transport import provider_config
from tests._ruling_support import fence as _fence
from tests._ruling_support import gate_repo as _repo
from tests._ruling_support import tip as _tip
from tests._ruling_support import comment as _comment
from tests._ruling_support import merge as _merge

HIT = ScannerHit("sast-one", "src/a.py", 3, "use-safe-parse", "unsafe parse call")
CONTEXT = {
    "scanner": "sast-one", "file": "src/a.py", "line": 1, "rule_id": "use-safe-parse",
    "message": "unsafe parse call",
}


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "no-user-config")


def _evaluated(item: dict) -> dict:
    return {k: item[k] for k in ("file", "line", "rule_id", "message", "facts")}


def test_an_in_range_scanner_finding_that_was_not_dropped_blocks_at_the_gate(tmp_path, capsys):
    repo = _repo(tmp_path)
    findings = [_evaluated(finding_of(HIT, {"sast-one": "other"}))]

    run = _merge([_comment(1, _fence("BLOCKED", findings=findings, base=_tip(repo)))], repo, capsys)

    assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
    assert "src/a.py:3:sast-one.use-safe-parse" in run.err


def test_a_clean_fence_that_still_carries_the_in_range_finding_is_not_trusted(tmp_path, capsys):
    repo = _repo(tmp_path)
    findings = [_evaluated(finding_of(HIT, {"sast-one": "other"}))]

    run = _merge([_comment(1, _fence("CLEAN", findings=findings, base=_tip(repo)))], repo, capsys)

    assert run.code == verb.EXIT_GATE_RESULT_BLOCKED


def test_outside_range_context_in_the_fence_does_not_block(tmp_path, capsys):
    repo = _repo(tmp_path)

    run = _merge(
        [_comment(1, _fence("CLEAN", findings=[], base=_tip(repo), outside_range=[CONTEXT]))],
        repo, capsys,
    )

    assert run.code == verb.EXIT_OK, run.err
