"""merge.scanner_coverage: the one required-scanner accessor, the one satisfaction
predicate, the coverage record, and the agreement between the scanner status and
name definitions in Python and in the two published schemas.

Every name below is synthetic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import clagentic_loadout
from clagentic_loadout.merge.fence_state import SCANNER_STATUSES
from clagentic_loadout.merge.scanner_coverage import (
    NOT_RUN_FAILING,
    NOT_RUN_REPORTED,
    SATISFIED,
    classify_scanner,
    coverage_header_line,
    required_scanners_for,
    scanner_coverage,
)
from clagentic_loadout.review.scanner_policy import SCANNER_NAME_RE

SCHEMAS = Path(clagentic_loadout.__file__).resolve().parent / "schemas"


class _Gate:
    def __init__(self, by_role):
        self._by_role = by_role

    def scanners_for(self, reviewer_name):
        return self._by_role.get(reviewer_name, ())


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"scanner": "s", "status": "ran"}, SATISFIED),
        ({"scanner": "s", "status": "not_applicable", "reason": "no code"}, SATISFIED),
        ({"scanner": "s", "status": "not_applicable"}, NOT_RUN_FAILING),
        ({"scanner": "s", "status": "not_applicable", "reason": "  "}, NOT_RUN_FAILING),
        ({"scanner": "s", "status": "not_invoked", "reason": "judgment only"}, NOT_RUN_REPORTED),
        ({"scanner": "s", "status": "not_invoked"}, NOT_RUN_REPORTED),
        ({"scanner": "s", "status": "failed", "reason": "timed out"}, NOT_RUN_FAILING),
        ({"scanner": "s", "status": "timeout"}, NOT_RUN_FAILING),
        ({"scanner": "s", "status": None}, NOT_RUN_FAILING),
        (None, NOT_RUN_FAILING),
    ],
)
def test_the_predicate_has_three_outcomes(entry, expected):
    assert classify_scanner(entry) == expected


def test_the_required_set_is_the_gate_entry_for_the_role_then_the_user_level_each_once():
    gate = _Gate({"role-a": ("alpha", "beta"), "role-b": ("gamma",)})
    assert required_scanners_for("role-a", gate, ["beta", "delta"]) == ("alpha", "beta", "delta")
    assert required_scanners_for("role-c", gate, ["delta"]) == ("delta",)
    assert required_scanners_for("role-a", None, ["delta"]) == ("delta",)
    assert required_scanners_for(None, gate, ()) == ()


@pytest.mark.parametrize(
    ("scanners_run", "required", "not_run", "complete"),
    [
        # AC1: nothing required, one scanner failed: reported, not required.
        (
            [{"scanner": "a", "status": "failed", "reason": "boom"}],
            (),
            [{"scanner": "a", "status": "failed", "reason": "boom", "required": False}],
            False,
        ),
        # AC3: a required scanner that was not invoked is listed as required.
        (
            [{"scanner": "a", "status": "not_invoked", "reason": "judgment only"}],
            ("a",),
            [{"scanner": "a", "status": "not_invoked", "reason": "judgment only", "required": True}],
            False,
        ),
        # AC5: everything ran, or was not applicable with a reason.
        (
            [
                {"scanner": "a", "status": "ran"},
                {"scanner": "b", "status": "not_applicable", "reason": "no manifest"},
            ],
            ("a", "b"),
            [],
            True,
        ),
        # A required scanner with no report at all is listed as missing.
        (
            [{"scanner": "a", "status": "ran"}],
            ("a", "b"),
            [{"scanner": "b", "status": "missing", "reason": "no result was reported", "required": True}],
            False,
        ),
        (None, (), [], True),
    ],
)
def test_the_coverage_record(scanners_run, required, not_run, complete):
    coverage = scanner_coverage(scanners_run, required)
    assert coverage == {"complete": complete, "not_run": not_run}


def test_the_header_line_names_each_scanner_that_did_not_run_and_is_absent_when_all_did():
    coverage = scanner_coverage(
        [{"scanner": "a", "status": "failed", "reason": "boom"}, {"scanner": "b", "status": "ran"}],
        ("a",),
    )
    assert coverage_header_line(coverage) == "scanners not run: a (failed, required)"
    assert coverage_header_line(scanner_coverage([{"scanner": "b", "status": "ran"}])) is None


def _schema(name):
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def test_the_published_schemas_agree_with_the_python_status_and_name_definitions():
    scanner_input = _schema("scanner-input.schema.json")["properties"]["scanners"]["items"]["properties"]
    review_result = _schema("review-result.schema.json")["properties"]["scanners_run"]["items"]["properties"]

    assert tuple(scanner_input["status"]["enum"]) == SCANNER_STATUSES
    assert tuple(review_result["status"]["enum"]) == SCANNER_STATUSES
    # The schema anchors with ^ and $, the Python pattern with \A and \Z.
    assert scanner_input["scanner"]["pattern"] == (
        SCANNER_NAME_RE.pattern.replace("\\A", "^").replace("\\Z", "$")
    )
