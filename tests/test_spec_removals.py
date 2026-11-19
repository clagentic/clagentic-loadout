"""spec.removals: the test cases, public symbols and verbs a change removes, computed
from the base and head of a git repository or from a reader and a list of paths.
Reporting only; nothing here refuses anything."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReadError
from clagentic_loadout.spec import removals
from tests._review_evaluate_support import commit_files, make_repo, run_git

PY_TESTS = (
    "def test_one():\n    pass\n\n\ndef test_two():\n    pass\n\n\ndef helper():\n    pass\n\n\n"
    "class TestBox:\n    def test_inside(self):\n        pass\n\n    def helper(self):\n        pass\n\n\n"
    "class Box:\n    def test_not_a_case(self):\n        pass\n"
)
PY_LIB = "def keep():\n    pass\n\n\ndef gone():\n    pass\n\n\nclass Gone:\n    pass\n\n\ndef _private():\n    pass\n"
JS_LIB = "export function keep() {}\nexport function gone() {}\nexport const limit = 1;\n"
JS_TESTS = (
    "test('first', () => {});\nit('second', () => {});\ntest('twice', () => {});\ntest('twice', () => {});\n"
    "re.test(value);\ntest(`tmpl ${x}`, () => {});\n"
)
PACKAGE_BIN = '{"name": "pkg", "bin": {"one": "a.js", "two": "b.js"}}'
PACKAGE_STRING_BIN = '{"name": "pkg", "bin": "cli.js"}'
PACKAGE_NONE = '{"name": "pkg"}'
BASE = {
    "tests/test_a.py": PY_TESTS,
    "lib/a.py": PY_LIB,
    "lib/b.js": JS_LIB,
    "web/b.test.js": JS_TESTS,
    "package.json": PACKAGE_BIN,
    "sub/package.json": PACKAGE_STRING_BIN,
    "lib/x.go": "package x\n",
    "lib/ok.py": "def survivor():\n    pass\n",
}

#: change (path -> new text, None deletes the path) -> (removed (kind, item) pairs in
#: order, paths listed as not computed). Every row's base is BASE.
ROWS = {
    "python-test-function-and-method": (
        {"tests/test_a.py": "def helper():\n    pass\n\n\nclass TestBox:\n    def helper(self):\n        pass\n"},
        [
            ("test", "tests/test_a.py::TestBox::test_inside"),
            ("test", "tests/test_a.py::test_one"),
            ("test", "tests/test_a.py::test_two"),
        ],
        [],
    ),
    "python-test-file-deleted-outright": (
        {"tests/test_a.py": None},
        [
            ("test", "tests/test_a.py::TestBox::test_inside"),
            ("test", "tests/test_a.py::test_one"),
            ("test", "tests/test_a.py::test_two"),
        ],
        [],
    ),
    "python-public-symbols-not-private-names": (
        {"lib/a.py": "def keep():\n    pass\n"},
        [("symbol", "lib/a.py::Gone"), ("symbol", "lib/a.py::gone")],
        [],
    ),
    "javascript-export": (
        {"lib/b.js": "export function keep() {}\n"},
        [("symbol", "lib/b.js::gone"), ("symbol", "lib/b.js::limit")],
        [],
    ),
    # n at base, m < n at head: n - m items; a call on an object and a title that is
    # not a single string literal are not cases.
    "javascript-test-titles-count-multiplicity": (
        {"web/b.test.js": "test('first', () => {});\ntest('twice', () => {});\ntest('twice', () => {});\n"},
        [("test", "web/b.test.js::second")],
        [],
    ),
    "javascript-test-title-removed-once-of-two": (
        {"web/b.test.js": "test('first', () => {});\nit('second', () => {});\ntest('twice', () => {});\n"},
        [("test", "web/b.test.js::twice")],
        [],
    ),
    "package-json-bin-object-and-string": (
        {"package.json": PACKAGE_NONE, "sub/package.json": PACKAGE_NONE},
        [("verb", "one"), ("verb", "pkg"), ("verb", "two")],
        [],
    ),
    # Nothing is inferred from the new path holding the same content.
    "a-rename-is-the-deletion-of-the-old-path": (
        {"lib/a.py": None, "lib/renamed.py": PY_LIB},
        [("symbol", "lib/a.py::Gone"), ("symbol", "lib/a.py::gone"), ("symbol", "lib/a.py::keep")],
        [],
    ),
    "an-added-item-removes-nothing": ({"lib/c.py": "def fresh():\n    pass\n"}, [], []),
    # A source file in an uncovered language and a covered file that no longer parses
    # are said, add nothing, and the rest is still computed.
    "uncovered-language-and-unparseable-file": (
        {"lib/x.go": "package y\n", "lib/ok.py": "def (:\n", "lib/b.js": "export function keep() {}\n"},
        [("symbol", "lib/b.js::gone"), ("symbol", "lib/b.js::limit")],
        ["lib/ok.py", "lib/x.go"],
    ),
}


@pytest.fixture
def repo(tmp_path):
    path = make_repo(tmp_path)
    return path, commit_files(path, BASE)


def commit_changes(path: Path, changes: dict[str, str | None]) -> str:
    """Commit *changes* on top of HEAD: text writes the file, None deletes it."""
    for rel, text in changes.items():
        if text is None:
            run_git(path, "rm", "-q", "--", rel)
            continue
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        run_git(path, "add", "--", rel)
    run_git(path, "commit", "-q", "-m", "chore: fixture change")
    return run_git(path, "rev-parse", "HEAD")


def listed(report: dict) -> list[tuple[str, str]]:
    return [(entry["kind"], entry["item"]) for entry in report["items"]]


@pytest.mark.parametrize("name", list(ROWS))
def test_the_report_lists_what_the_change_removes(repo, name):
    path, base = repo
    changes, expected, skipped = ROWS[name]
    head = commit_changes(path, changes)

    report = removals.report_between(GitPolicyReader(path), base, head)

    assert report["status"] == "computed"
    assert (report["base_sha"], report["head_sha"]) == (base, head)
    assert sorted(listed(report)) == sorted(expected)
    assert sorted(entry["path"] for entry in report["not_computed"]) == sorted(skipped)
    assert all(entry["reason"] for entry in report["not_computed"])
    assert report["note"] == (f"{len(expected)} removed" if expected else "none")


def test_an_item_reports_its_path_and_language(repo):
    path, base = repo
    head = commit_changes(path, {"lib/a.py": "def keep():\n    pass\n", "package.json": PACKAGE_NONE})

    report = removals.report_between(GitPolicyReader(path), base, head)

    assert {(e["item"], e["path"], e["language"]) for e in report["items"]} == {
        ("lib/a.py::Gone", "lib/a.py", "python"),
        ("lib/a.py::gone", "lib/a.py", "python"),
        ("one", "package.json", "json"),
        ("two", "package.json", "json"),
    }


def test_a_pyproject_script_removed_is_a_verb(tmp_path):
    pytest.importorskip("tomllib")
    path = make_repo(tmp_path)
    base = commit_files(path, {"pyproject.toml": '[project]\nname = "x"\n\n[project.scripts]\nalpha = "x:a"\nbeta = "x:b"\n'})
    head = commit_changes(path, {"pyproject.toml": '[project]\nname = "x"\n\n[project.scripts]\nalpha = "x:a"\n'})

    report = removals.report_between(GitPolicyReader(path), base, head)

    assert listed(report) == [("verb", "beta")]


def test_an_invalid_package_json_at_the_head_is_not_computed_and_adds_no_item(repo):
    path, base = repo
    head = commit_changes(path, {"package.json": "{not json"})

    report = removals.report_between(GitPolicyReader(path), base, head)

    assert listed(report) == []
    assert [entry["path"] for entry in report["not_computed"]] == ["package.json"]


class _Reader:
    """A reader over fixed trees, as the host API serves one."""

    def __init__(self, trees, broken=()):
        self.trees, self.broken = trees, set(broken)

    def list_paths(self, rev):
        if rev not in self.trees:
            raise PolicyReadError(f"cannot read the file list at {rev}")
        return list(self.trees[rev])

    def read_text(self, rev, path):
        if (rev, path) in self.broken:
            raise PolicyReadError(f"cannot read {path} at {rev}")
        return self.trees[rev][path]


def test_a_reader_and_a_path_list_give_the_report_a_host_served_change_needs():
    reader = _Reader({"b": {"lib/a.py": PY_LIB}, "h": {"lib/a.py": "def keep():\n    pass\n"}})

    report = removals.report_for_paths(reader, "b", "h", ["lib/a.py", "README.md"])

    assert listed(report) == [("symbol", "lib/a.py::Gone"), ("symbol", "lib/a.py::gone")]
    assert (report["base_sha"], report["head_sha"]) == ("b", "h")


@pytest.mark.parametrize(
    "reader, expect",
    [
        (_Reader({"b": {"lib/a.py": PY_LIB}}), "cannot read the file list at h"),
        (_Reader({"h": {"lib/a.py": PY_LIB}}), "cannot read the file list at b"),
    ],
    ids=["head-tree-unreadable", "base-tree-unreadable"],
)
def test_a_tree_that_cannot_be_read_is_a_report_that_is_not_computed(reader, expect):
    report = removals.report_for_paths(reader, "b", "h", ["lib/a.py"])

    assert report["status"] == "not_computed"
    assert report["items"] == [] and report["note"] == expect


def test_a_file_unreadable_at_one_side_is_listed_and_adds_no_item():
    reader = _Reader(
        {"b": {"lib/a.py": PY_LIB, "lib/b.py": PY_LIB}, "h": {"lib/a.py": "", "lib/b.py": ""}},
        broken={("h", "lib/a.py")},
    )

    report = removals.report_for_paths(reader, "b", "h", ["lib/a.py", "lib/b.py"])

    assert report["status"] == "computed"
    assert {entry["item"].split("::")[0] for entry in report["items"]} == {"lib/b.py"}
    assert [entry["path"] for entry in report["not_computed"]] == ["lib/a.py"]


# --- the replay of a real change: a module and its tests deleted outright ------------

FIXTURES = Path(__file__).parent / "fixtures" / "removals_pr732"
MODULE = "scripts/crew_runtime/review_pr_record.py"
MODULE_TESTS = "tests/test_review_judge_pr_record.py"

# The test functions of the deleted file at the base of the replayed change. The file
# held sixteen; the task named seven for the part of it that drops a candidate by the
# PR record, and all sixteen are asserted here, which includes them.
DELETED_TESTS = [
    "test_a_body_with_a_record_drops_the_candidate_with_the_reason",
    "test_a_body_without_a_record_keeps_the_candidate",
    "test_a_heading_with_no_evidence_after_it_is_not_a_record",
    "test_a_head_mismatch_keeps_the_candidate_and_records_the_reason",
    "test_a_findings_file_with_no_head_keeps_the_candidate",
    "test_a_truncated_body_without_a_record_downgrades_to_a_nit",
    "test_a_truncated_body_that_still_holds_a_record_drops_the_candidate",
    "test_no_pr_json_keeps_the_candidate_and_says_so",
    "test_an_unparseable_or_wrong_schema_pr_json_keeps_the_candidate",
    "test_other_candidates_are_never_touched",
    "test_a_reviewer_drop_of_the_candidate_keeps_the_reviewers_reason",
    "test_the_real_pr_662_body_is_detected_as_a_record",
    "test_the_real_pr_662_record_drops_the_candidate_through_the_cli",
    "test_the_real_console_pr_440_record_saying_pending_real_run_is_detected",
    "test_pending_real_run_is_evidence_with_or_without_post_merge",
    "test_the_section_sign_form_and_case_are_detected",
]
DELETED_SYMBOLS = ["Resolution", "has_section_6_record", "load_record", "resolve"]


def test_the_replay_of_a_deleted_module_and_its_tests_lists_every_removed_case_and_symbol(tmp_path):
    # The two files are the base content of the replayed change, with the one
    # repository name that the anonymization guard rejects renamed (the count and
    # every other identifier are unchanged); the head deletes both.
    path = make_repo(tmp_path)
    base = commit_files(
        path,
        {
            MODULE: (FIXTURES / "review_pr_record.py.txt").read_text(encoding="utf-8"),
            MODULE_TESTS: (FIXTURES / "test_review_judge_pr_record.py.txt").read_text(encoding="utf-8"),
            "scripts/keep.py": "def kept():\n    pass\n",
        },
    )
    head = commit_changes(path, {MODULE: None, MODULE_TESTS: None})

    report = removals.report_between(GitPolicyReader(path), base, head)

    assert report["status"] == "computed" and report["not_computed"] == []
    assert sorted(listed(report)) == sorted(
        [("test", f"{MODULE_TESTS}::{name}") for name in DELETED_TESTS]
        + [("symbol", f"{MODULE}::{name}") for name in DELETED_SYMBOLS]
    )


def test_the_report_is_json_serialisable(repo):
    path, base = repo
    head = commit_changes(path, {"lib/a.py": "def keep():\n    pass\n"})

    json.dumps(removals.report_between(GitPolicyReader(path), base, head))
