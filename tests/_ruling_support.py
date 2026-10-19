"""Shared builders for the ruling registry tests: rulings, registry files and
findings carrying a fingerprint. Names are synthetic throughout."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import yaml

from clagentic_loadout.review.rulings import Ruling, build_ruling, rulings_document
from tests._review_evaluate_support import facts, finding

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


def evaluated_finding(fact=NON_FLOOR, *, fingerprint=FP, **extra) -> dict:
    """A finding as the evaluated_findings of a fence carries it."""
    item = {k: v for k, v in ruled_finding(fact, fingerprint=fingerprint, **extra).items()
            if k in ("file", "line", "rule_id", "message", "facts", "fingerprint")}
    if fingerprint is None:
        del item["fingerprint"]
    return item
