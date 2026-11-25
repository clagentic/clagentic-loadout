"""repo_test.junit — counts and failing test ids from a JUnit XML report.

A test id is ``<classname>::<name>`` (just ``<name>`` when the case has no class
name). A case with a ``failure`` or ``error`` child is failing; one with a
``skipped`` child is skipped; every other case passed. The ids are what two runs
are compared by, so the format is part of the report contract. The failing ids
are a multiset: one entry per failed case, so two failed cases that share an id
appear twice and the entry count equals the failed count. Ids are never
rewritten, so no synthetic id can equal a real test's id, and a comparison
counts entries.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

MAX_JUNIT_BYTES = 256 * 1024 * 1024

# Recorded in result.json. A run without it predates per-case entries and holds
# one entry per distinct name, however many cases failed under that name.
ID_SCHEME = 2


class JunitError(Exception):
    """The report is missing, too large, or not JUnit XML."""


@dataclass(frozen=True)
class JunitSummary:
    collected: int
    passed: int
    failed: int
    skipped: int
    failing_ids: tuple[str, ...]


def _test_id(case: ET.Element) -> str:
    name = case.get("name") or ""
    classname = case.get("classname") or ""
    return f"{classname}::{name}" if classname else name


@dataclass(frozen=True)
class FailedCase:
    """One failed case, as the report holds it: the id, whether its first
    ``failure``/``error`` child was a failure or an error, that child's
    ``message`` attribute, and its text. Nothing here is redacted or bounded."""

    test_id: str
    kind: str
    message: str
    text: str


def _load_root(path: Path) -> ET.Element:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise JunitError(f"no JUnit report at the path the run was given: {exc}") from exc
    if size > MAX_JUNIT_BYTES:
        raise JunitError(f"the JUnit report is {size} bytes, over the {MAX_JUNIT_BYTES} byte limit")
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise JunitError(f"the JUnit report is not readable XML: {exc}") from exc
    if root.tag not in ("testsuites", "testsuite"):
        raise JunitError(f"the JUnit report root is <{root.tag}>, not <testsuites> or <testsuite>")
    return root


OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED = "skipped"


def _outcome(case: ET.Element) -> str:
    tags = {child.tag for child in case}
    if tags & {"failure", "error"}:
        return OUTCOME_FAILED
    if "skipped" in tags:
        return OUTCOME_SKIPPED
    return OUTCOME_PASSED


def iter_cases(path: Path) -> Iterator[tuple[str, str]]:
    """Every case of the report as ``(id, outcome)``, in report order, one entry
    per ``testcase`` element. The outcome rule lives here, so every view of a
    report agrees on it. The report is loaded when iteration starts, so a
    ``JunitError`` surfaces from the first ``next``."""
    for case in _load_root(path).iter("testcase"):
        yield _test_id(case), _outcome(case)


def parse_junit(path: Path) -> JunitSummary:
    failing: list[str] = []
    passed = skipped = 0
    for test_id, outcome in iter_cases(path):
        if outcome == OUTCOME_FAILED:
            failing.append(test_id)
        elif outcome == OUTCOME_SKIPPED:
            skipped += 1
        else:
            passed += 1
    return JunitSummary(
        collected=passed + len(failing) + skipped,
        passed=passed,
        failed=len(failing),
        skipped=skipped,
        failing_ids=tuple(sorted(failing)),
    )


def parse_outcomes(path: Path) -> dict[str, tuple[str, ...]]:
    """Every case's outcome keyed by its id: one entry per case, in report order,
    each ``passed``, ``failed`` or ``skipped`` by the same rule as ``parse_junit``.
    An id absent from the report is absent from the result."""
    outcomes: dict[str, list[str]] = {}
    for test_id, outcome in iter_cases(path):
        outcomes.setdefault(test_id, []).append(outcome)
    return {test_id: tuple(found) for test_id, found in outcomes.items()}


def parse_collected_ids(path: Path) -> list[str]:
    """The id of every case in the report, passed, failed and skipped, one entry
    per case in report order (two cases that share an id appear twice)."""
    return [test_id for test_id, _ in iter_cases(path)]


def parse_failed_cases(path: Path) -> list[FailedCase]:
    """One entry per failed case, in report order, so cases sharing an id each
    keep their own entry (the same multiset rule as ``JunitSummary.failing_ids``)."""
    cases: list[FailedCase] = []
    for case in _load_root(path).iter("testcase"):
        detail = next((child for child in case if child.tag in ("failure", "error")), None)
        if detail is not None:
            cases.append(
                FailedCase(
                    test_id=_test_id(case),
                    kind=detail.tag,
                    message=detail.get("message") or "",
                    text="".join(detail.itertext()),
                )
            )
    return cases


__all__ = [
    "ID_SCHEME",
    "MAX_JUNIT_BYTES",
    "OUTCOME_FAILED",
    "OUTCOME_PASSED",
    "OUTCOME_SKIPPED",
    "FailedCase",
    "JunitError",
    "JunitSummary",
    "iter_cases",
    "parse_collected_ids",
    "parse_failed_cases",
    "parse_junit",
    "parse_outcomes",
]
