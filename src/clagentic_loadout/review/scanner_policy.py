"""review.scanner_policy — what category of defect each scanner looks for.

The category of a scanner's hits decides whether an in-range hit is in the
security floor, which a by-design disposition cannot clear. It is therefore
policy, read from the repo profile at the pull request's base revision (like the
docs allowlist), and never from the scanner input file or the findings file,
which the reviewer writes. The profile declares it under ``review.scanners``::

    review:
      scanners:
        gitleaks: {category: secrets_exposure}
        style-lint: {category: other}

A scanner the policy does not name has no category, and the evaluator counts a
finding with no category at its worst case: in the floor. The default fails
closed; naming a scanner is how a repository takes a non-security scanner out.
"""

from __future__ import annotations

import re
from typing import Any

from clagentic_loadout.review.facts import CATEGORIES

KEY_SCANNERS = "scanners"
KEY_CATEGORY = "category"

#: The one definition of a valid scanner name, shared with the scanner input.
#: No '.', so the first '.' of a ``scanner.rule`` finding id always ends the scanner
#: name and two (scanner, rule) pairs can never share an id.
SCANNER_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_:-]{0,63}\Z")


class ScannerPolicyError(ValueError):
    """``review.scanners`` is not in the documented shape; the message names the key path."""


def parse_scanner_categories(block: Any, where: str = "review.scanners") -> tuple[tuple[str, str], ...]:
    """``(scanner, category)`` pairs from a profile's ``review.scanners`` value;
    empty when absent. Strict, so a half-understood entry is never half-applied."""
    if block is None:
        return ()
    if not isinstance(block, dict):
        raise ScannerPolicyError(f"{where} must be a mapping of scanner name to a category")
    out: list[tuple[str, str]] = []
    for name, entry in block.items():
        if not isinstance(name, str) or not SCANNER_NAME_RE.match(name):
            raise ScannerPolicyError(f"{where} has an invalid scanner name {name!r}")
        here = f"{where}.{name}"
        if not isinstance(entry, dict):
            raise ScannerPolicyError(f"{here} must be a mapping with a {KEY_CATEGORY}")
        unknown = sorted(str(k) for k in entry if k != KEY_CATEGORY)
        if unknown:
            raise ScannerPolicyError(
                "unknown or misplaced key: " + ", ".join(f"{here}.{key}" for key in unknown)
            )
        category = entry.get(KEY_CATEGORY)
        if category not in CATEGORIES:
            raise ScannerPolicyError(
                f"{here}.{KEY_CATEGORY} must be one of {', '.join(CATEGORIES)}, got {category!r}"
            )
        out.append((name, category))
    return tuple(out)


__all__ = ["KEY_CATEGORY", "KEY_SCANNERS", "SCANNER_NAME_RE", "ScannerPolicyError", "parse_scanner_categories"]
