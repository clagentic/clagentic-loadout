"""Shared builders for the ruling registry tests: rulings, registry files,
findings carrying a fingerprint, and the gate repository and verdict fence the
merge-gate tests read. Names are synthetic throughout."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import yaml

from clagentic_loadout.merge.verdict import build_verdict_block
from clagentic_loadout.review.evaluate import EVALUATOR_VERSION
from clagentic_loadout.review.evaluation import finding_state_id
from clagentic_loadout.review.policy_source import DISPOSITIONS_PATH
from clagentic_loadout.review.rulings import Ruling, build_ruling, rulings_document
from tests._gate_repo import commit_files, git, init_gate_repo
from tests._review_evaluate_support import facts, finding

HEAD = "b" * 40
NAME = "some-reviewer"
REPO = "some-owner/some-repo"
FP = "fp2:0123456789abcdef"
OTHER_FP = "fp2:fedcba9876543210"
TODAY = dt.date(2026, 10, 1)

#: Blocks on the worst-case stakes every unstated path resolves to, and is not a
#: security-floor category: a ruling may clear it.
NON_FLOOR = facts(confidentiality="full", integrity="full", availability="full")
#: A security-floor category: no ruling may clear it.
FLOOR = facts(category="injection", confidentiality="full", integrity="full", availability="full")


def ruling(**overrides) -> Ruling:
    fields = {
        "repo": REPO,
        "pr": 1,
        "fingerprint": FP,
        "rule_id": "R1",
        "path": "src/a.py",
        "kind": "accepted_risk",
        "rationale": "accepted for this change",
        "by": "some-role",
        "requested_by": "some-caller",
        "source_ref": "design note 4",
        "at": TODAY,
    }
    fields.update(overrides)
    return build_ruling(**fields)


def registry_dir(root: Path) -> Path:
    directory = root / "registry"
    directory.mkdir(exist_ok=True)
    os.chmod(directory, 0o755)
    return directory


def write_registry(root: Path, rulings: list[Ruling]) -> Path:
    """A trusted registry file under *root*: owner-writable, not world-writable."""
    path = registry_dir(root) / "rulings.yaml"
    path.write_text(yaml.safe_dump(rulings_document(rulings), sort_keys=False), encoding="utf-8")
    os.chmod(path, 0o644)
    return path


def ruled_finding(fact=NON_FLOOR, *, fingerprint=FP, **extra) -> dict:
    return finding(fact=fact, fingerprint=fingerprint, **extra)


def gate_repo(tmp_path: Path, *, dispositions: str | None = None, **merge_gate) -> Path:
    """A repo whose main tip carries the tracked gate (and dispositions)."""
    path = tmp_path / "repo"
    init_gate_repo(path, tracked_gate={"required_reviewer_roles": [], **merge_gate})
    if dispositions is not None:
        commit_files(path, {DISPOSITIONS_PATH: dispositions}, "chore: add dispositions")
    return path


def tip(repo: Path) -> str:
    return git(repo, "rev-parse", "main")


#: Default for fence(opened=...): derive the open entries from the findings.
DERIVE = object()


def open_entries(findings):
    """findings_open as the post path writes it: one entry per distinct id."""
    entries = {}
    for item in findings:
        state_id = finding_state_id(item["file"], item["line"], item["rule_id"])
        entries.setdefault(state_id, {"id": state_id, "rule_id": item["rule_id"], "head": HEAD})
    return list(entries.values())


def fence(
    outcome, *, findings=(), status=None, head=HEAD, reasons=None, base=None, keep_findings=True,
    opened=DERIVE, **more,
):
    """A fence of *outcome*. A BLOCKED fence records every finding as blocking
    unless *opened* names the entries (None leaves findings_open out)."""
    state = {
        "verdict_outcome": outcome,
        "verdict_reasons": reasons if reasons is not None else ([] if outcome == "CLEAN" else ["recorded"]),
        "evaluator_version": EVALUATOR_VERSION,
        **more,
    }
    if opened is DERIVE:
        opened = open_entries(findings) if outcome == "BLOCKED" else None
    if opened is not None:
        state["findings_open"] = opened
    if keep_findings:
        state["evaluated_findings"] = list(findings)
    if base:
        state["policy_base_sha"] = base
    status = status or ("clean" if outcome == "CLEAN" else "blocking")
    return build_verdict_block(NAME, status, head, 1, findings_state=state)


def evaluated_finding(fact=NON_FLOOR, *, fingerprint=FP, **extra) -> dict:
    """A finding as the evaluated_findings of a fence carries it."""
    item = {k: v for k, v in ruled_finding(fact, fingerprint=fingerprint, **extra).items()
            if k in ("file", "line", "rule_id", "message", "facts", "fingerprint")}
    if fingerprint is None:
        del item["fingerprint"]
    return item
