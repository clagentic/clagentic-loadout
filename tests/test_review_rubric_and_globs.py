"""Path globbing, the rubric table, and the published document agreeing with the code."""

from __future__ import annotations

from pathlib import Path

import pytest

from clagentic_loadout.review import rubric
from clagentic_loadout.review import stakes as stk
from clagentic_loadout.review.path_glob import path_matches

DOC = Path(__file__).resolve().parent.parent / "docs" / "review-evaluator.md"


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/a/b.py", False),
        ("src/**", "src/a/b.py", True),
        ("src/**/b.py", "src/b.py", True),
        ("src/**/b.py", "src/x/y/b.py", True),
        ("**/Caddyfile", "Caddyfile", True),
        ("**/Caddyfile", "deploy/Caddyfile", True),
        ("**/Caddyfile", "deploy/Caddyfile.bak", False),
        ("a?c", "abc", True),
        ("a?c", "a/c", False),
        ("a.b", "axb", False),
        ("**/ingress/**", "k8s/ingress/app.yaml", True),
    ],
)
def test_glob_semantics(pattern, path, expected):
    assert path_matches(pattern, path) is expected


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("*", "a/b.py", True),
        ("/docs/", "docs/a.md", True),
        ("/docs/", "x/docs/a.md", False),
        ("docs/", "x/docs/a.md", True),
        ("*.js", "web/app.js", True),
        ("/src/*.py", "src/a.py", True),
        ("/src/*.py", "src/a/b.py", False),
        ("/src/*.py", "other/src/a.py", False),
    ],
)
def test_codeowners_pattern_semantics(pattern, path, expected):
    assert stk.codeowners_pattern_matches(pattern, path) is expected


def test_rubric_table_in_the_document_is_the_table_in_the_code():
    text = DOC.read_text(encoding="utf-8")
    rows = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and cells[0] in rubric.SEVERITY_TABLE and len(cells) == 5:
            rows[cells[0]] = tuple(cells[1:])
    assert rows == rubric.SEVERITY_TABLE


def test_document_states_the_default_threshold_and_exit_codes():
    text = DOC.read_text(encoding="utf-8")
    assert f"(default `{rubric.DEFAULT_BLOCK_AT}`)" in text
    from clagentic_loadout.review import evaluate_verb as verb

    for outcome, code in verb.OUTCOME_EXIT_CODES.items():
        assert f"| {outcome} | {code} |" in text


def test_document_lists_every_exposure_marker_glob():
    text = DOC.read_text(encoding="utf-8")
    for glob in stk.EXPOSURE_MARKER_GLOBS:
        assert f"`{glob}`" in text


def test_document_lists_every_category_and_floor_category():
    from clagentic_loadout.review import facts as fx

    text = DOC.read_text(encoding="utf-8")
    for category in fx.CATEGORIES:
        assert f"`{category}`" in text
    floor_sentence = text.split("The security-floor categories are", 1)[1].split(".", 1)[0]
    for category in fx.FLOOR_CATEGORIES:
        assert f"`{category}`" in floor_sentence


def test_severity_ordering_helpers():
    assert rubric.SEVERITIES[0] == "none" and rubric.SEVERITIES[-1] == "critical"
    assert rubric.severity_rank("high") > rubric.severity_rank("medium")
