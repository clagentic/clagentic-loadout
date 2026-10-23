"""repo_test.junit — counts and failing test ids from a JUnit XML report.

A test id is ``<classname>::<name>`` (just ``<name>`` when the case has no class
name). A case with a ``failure`` or ``error`` child is failing; one with a
``skipped`` child is skipped; every other case passed. The ids are what two runs
are compared by, so the format is part of the report contract. Failing cases that
share an id are told apart by a ``#dupN`` suffix, so there is exactly one id per
failed case.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

MAX_JUNIT_BYTES = 256 * 1024 * 1024

# Recorded in result.json. A run without it predates distinct ids and holds one
# id per distinct name, however many cases failed under that name.
ID_SCHEME = 2
DUPLICATE_MARK = "#dup"
_DUPLICATE_RE = re.compile(r"#dup\d+\Z")


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
    if size > MAX_JUNIT_BYTES:
        raise JunitError(f"the JUnit report is {size} bytes, over the {MAX_JUNIT_BYTES} byte limit")
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise JunitError(f"the JUnit report is not readable XML: {exc}") from exc
    if root.tag not in ("testsuites", "testsuite"):
        raise JunitError(f"the JUnit report root is <{root.tag}>, not <testsuites> or <testsuite>")
    failing: list[tuple[str, str]] = []
    passed = failed = skipped = 0
    for case in root.iter("testcase"):
        tags = {child.tag for child in case}
        if tags & {"failure", "error"}:
            failed += 1
            failing.append((_test_id(case), "failure" if "failure" in tags else "error"))
        elif "skipped" in tags:
            skipped += 1
        else:
            passed += 1
    return JunitSummary(
        collected=passed + failed + skipped,
        passed=passed,
        failed=failed,
        skipped=skipped,
        failing_ids=_distinct_ids(failing),
    )


def _distinct_ids(failing: list[tuple[str, str]]) -> tuple[str, ...]:
    """One id per failing case, so the id count equals the failed count.

    Several failing cases can share a ``classname::name``: pytest writes a second
    case when a test fails and then errors in teardown. Collapsing them would let
    a new failure hide behind an old one in a comparison. The first case of a
    group keeps the plain id (the form older runs recorded); the others get
    ``#dupN``. The group is ordered by kind, never by document position, so the
    same failures always get the same ids.
    """
    by_base: dict[str, list[str]] = {}
    for base, kind in failing:
        by_base.setdefault(base, []).append(kind)
    taken = set(by_base)
    ids: list[str] = []
    for base, kinds in by_base.items():
        ids.append(base)
        number = 1
        for _ in sorted(kinds)[1:]:
            number += 1
            candidate = f"{base}{DUPLICATE_MARK}{number}"
            while candidate in taken:
                number += 1
                candidate = f"{base}{DUPLICATE_MARK}{number}"
            taken.add(candidate)
            ids.append(candidate)
    return tuple(sorted(ids))


def collapse_duplicate_id(test_id: str) -> str:
    """The plain ``classname::name`` an id stands for, with any ``#dupN`` removed."""
    return _DUPLICATE_RE.sub("", test_id)


__all__ = [
    "DUPLICATE_MARK",
    "ID_SCHEME",
    "MAX_JUNIT_BYTES",
    "JunitError",
    "JunitSummary",
    "collapse_duplicate_id",
    "parse_junit",
]
