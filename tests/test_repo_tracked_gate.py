"""This repo's own tracked merge-gate declaration must load cleanly under the
same rules loadout-merge applies to it at the PR base commit; a file that
fell back would silently stop gating."""

from __future__ import annotations

from pathlib import Path

from clagentic_loadout.merge.repo_gate_runtime import gate_from_tracked_text
from clagentic_loadout.repo_config import TRACKED_GATE_RELATIVE_PATH

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_repos_tracked_gate_declares_reviewer_roles_and_loads_cleanly():
    gate_file = _REPO_ROOT / TRACKED_GATE_RELATIVE_PATH
    gate = gate_from_tracked_text(gate_file.read_text(encoding="utf-8"), source=str(gate_file))
    assert gate.warnings == ()
    assert gate.pre_checks_error == ""
    assert gate.reviewer_roles, "an empty list would declare an explicit no-reviewer-gate opt-out"
    assert gate.required_scanners == {}
    assert gate.pre_checks == ()
