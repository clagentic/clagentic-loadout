"""repo_test.junit — counts and failing test ids from a JUnit XML report.

A test id is ``<classname>::<name>`` (just ``<name>`` when the case has no class
name). A case with a ``failure`` or ``error`` child is failing; one with a
``skipped`` child is skipped; every other case passed. The ids are what two runs
are compared by, so the format is part of the report contract.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

_MAX_JUNIT_BYTES = 256 * 1024 * 1024


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


def parse_junit(path: Path) -> JunitSummary:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise JunitError(f"no JUnit report at the path the run was given: {exc}") from exc
    if size > _MAX_JUNIT_BYTES:
        raise JunitError(f"the JUnit report is {size} bytes, over the {_MAX_JUNIT_BYTES} byte limit")
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise JunitError(f"the JUnit report is not readable XML: {exc}") from exc
    if root.tag not in ("testsuites", "testsuite"):
        raise JunitError(f"the JUnit report root is <{root.tag}>, not <testsuites> or <testsuite>")
    failing: list[str] = []
    passed = failed = skipped = 0
    for case in root.iter("testcase"):
        tags = {child.tag for child in case}
        if tags & {"failure", "error"}:
            failed += 1
            failing.append(_test_id(case))
        elif "skipped" in tags:
            skipped += 1
        else:
            passed += 1
    return JunitSummary(
        collected=passed + failed + skipped,
        passed=passed,
        failed=failed,
        skipped=skipped,
        failing_ids=tuple(sorted(set(failing))),
    )


__all__ = ["JunitError", "JunitSummary", "parse_junit"]
