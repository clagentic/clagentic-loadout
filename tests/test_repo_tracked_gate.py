"""This repo's own tracked merge-gate declaration must load cleanly under the
same parser loadout-merge applies to it at the PR base commit; a file that
fell back would silently stop gating."""

from __future__ import annotations

from pathlib import Path

from clagentic_loadout.merge.gate_config import parse_tracked_gate_text
from clagentic_loadout.merge.pre_checks_config import pre_checks_from_section
from clagentic_loadout.repo_config import TRACKED_GATE_RELATIVE_PATH

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_repos_tracked_gate_declares_reviewer_roles_and_loads_cleanly():
    gate_file = _REPO_ROOT / TRACKED_GATE_RELATIVE_PATH
    roles, scanners, merge_section = parse_tracked_gate_text(
        gate_file.read_text(encoding="utf-8"), source=str(gate_file)
    )
    assert roles, "an empty list would declare an explicit no-reviewer-gate opt-out"
    assert scanners == {}
    assert pre_checks_from_section(merge_section) == []
