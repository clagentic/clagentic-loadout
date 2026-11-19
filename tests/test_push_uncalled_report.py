"""At push time the public symbols a branch adds that nothing calls or tests are
printed. Reporting only: nothing here can fail a push or change its output."""

from __future__ import annotations

import io

import pytest

from clagentic_loadout.push import uncalled_report
from clagentic_loadout.push import verb as push_verb
from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReadError
from tests._gate_repo import git
from tests._review_evaluate_support import commit_files, make_repo

BASE = {"lib/a.py": "def keep():\n    pass\n", "lib/server-auth.js": "function check() {}\nmodule.exports = { check };\n"}

PY_FRESH = "def keep():\n    pass\n\n\ndef fresh():\n    pass\n"
UNLOCK_HEAD = (
    "function check() {}\n\nfunction unlockUser(username) {}\n\nmodule.exports = { check, unlockUser };\n"
)

#: (head files, names reported). Each row is one push whose base is BASE.
ROWS = {
    "python-def-and-class": (
        {"lib/a.py": PY_FRESH + "\n\nclass Fresh:\n    pass\n"}, ["lib/a.py::Fresh", "lib/a.py::fresh"]
    ),
    "esm-function": ({"lib/b.js": "export function newThing() {}\n"}, ["lib/b.js::newThing"]),
    "esm-default-named-function": ({"lib/b.js": "export default function main() {}\n"}, ["lib/b.js::main"]),
    "esm-clause-with-rename": (
        {"lib/b.js": "function alpha() {}\nfunction beta() {}\nexport { alpha, beta as gamma };\n"},
        ["lib/b.js::alpha", "lib/b.js::gamma"],
    ),
    "esm-const-and-class": (
        {"lib/b.js": "export const limit = 1;\nexport class Widget {}\n"}, ["lib/b.js::Widget", "lib/b.js::limit"]
    ),
    "commonjs-assignments": (
        {"lib/b.js": "exports.one = function () {};\nmodule.exports.two = () => 2;\n"},
        ["lib/b.js::one", "lib/b.js::two"],
    ),
    "commonjs-object-keys": (
        {"lib/b.js": "function inner() {}\nmodule.exports = { outer: inner, plain: 1 };\n"},
        ["lib/b.js::outer", "lib/b.js::plain"],
    ),
    # The replay of a console pull request: a function added to a module and to its
    # module.exports object, referenced nowhere else in the tree.
    "added-to-an-existing-exports-object": (
        {"lib/server-auth.js": UNLOCK_HEAD}, ["lib/server-auth.js::unlockUser"]
    ),
    # beta's only other mention is the value of alpha's export entry: that token is not
    # beta's own export, so it is a reference to beta and only alpha is reported.
    "another-symbols-export-entry-is-a-reference": (
        {"lib/b.js": "function alpha() {}\nfunction beta() {}\nmodule.exports = { alpha: beta, beta };\n"},
        ["lib/b.js::alpha"],
    ),
    "string-in-dunder-all-is-no-caller": (
        {"lib/c.py": '__all__ = ["listed"]\n\n\ndef listed():\n    pass\n'}, ["lib/c.py::listed"]
    ),
    # Not reported: called from source, referenced from a test, private, already present
    # at the base, re-exported, or in a test file.
    "called-from-source": (
        {"lib/c.py": "def used():\n    pass\n", "lib/d.py": "from lib.c import used\n\nused()\n"}, []
    ),
    "called-from-javascript": (
        {"lib/b.js": "export function used() {}\n", "lib/e.js": "import { used } from './b';\nused();\n"}, []
    ),
    "referenced-only-from-a-test": (
        {"lib/c.py": "def tested():\n    pass\n", "tests/test_c.py": "from lib.c import tested\n"}, []
    ),
    "javascript-test-reference": (
        {"lib/b.js": "export function tested() {}\n", "test/b.test.js": "import { tested } from '../lib/b';\n"}, []
    ),
    "private-name": ({"lib/c.py": "def _hidden():\n    pass\n"}, []),
    "unchanged-symbol": ({"lib/a.py": "def keep():\n    pass\n\n\n# edit\n"}, []),
    "re-export-is-not-an-addition": ({"lib/b.js": "export { check } from './server-auth';\n"}, []),
    "test-file-adds-a-public-helper": ({"tests/test_c.py": "def helper():\n    pass\n"}, []),
    "other-language": ({"lib/c.go": "func Exported() {}\n"}, []),
    "unparsable-source-is-skipped": ({"lib/c.py": "def (:\n", "lib/b.js": "const s = 'open;\n"}, []),
}


@pytest.fixture
def repo(tmp_path):
    path = make_repo(tmp_path)
    return path, commit_files(path, BASE)


#: The branch's pushed head: one commit on top of BASE.
_push_head = commit_files


@pytest.mark.parametrize("name", list(ROWS))
def test_a_public_symbol_added_with_no_caller_and_no_test_is_named(repo, name):
    path, base = repo
    files, reported = ROWS[name]
    head = _push_head(path, files)

    lines = uncalled_report.notice_lines(path, [base])

    assert lines == [
        f"push: new public symbol with no caller and no test at {head}: {symbol}" for symbol in reported
    ]


def test_the_list_is_capped_and_the_rest_counted(repo, monkeypatch):
    path, base = repo
    monkeypatch.setattr(uncalled_report, "REPORT_CAP", 2)
    head = _push_head(path, {"lib/c.py": "def one():\n    pass\n\n\ndef two():\n    pass\n\n\ndef three():\n    pass\n"})

    lines = uncalled_report.notice_lines(path, [base])

    assert len(lines) == 3 and lines[-1] == (
        f"push: and 1 more new public symbols with no caller and no test at {head}"
    )


def test_a_base_that_does_not_resolve_prints_nothing(repo):
    path, _base = repo
    _push_head(path, {"lib/c.py": "def fresh():\n    pass\n"})

    assert uncalled_report.notice_lines(path, ["no-such-ref"]) == []


def test_the_first_base_ref_that_resolves_is_used(repo):
    path, base = repo
    _push_head(path, {"lib/c.py": "def fresh():\n    pass\n"})

    assert len(uncalled_report.notice_lines(path, ["no-such-ref", base])) == 1


@pytest.mark.parametrize("method", ["changed_paths", "list_paths"], ids=["changed-paths-unreadable", "head-tree-unreadable"])
def test_a_tree_that_cannot_be_read_is_one_not_computed_line_never_an_error(repo, monkeypatch, method):
    path, base = repo
    _push_head(path, {"lib/c.py": "def fresh():\n    pass\n"})

    def unreadable(self, *args):
        raise PolicyReadError("cannot read the repository")

    monkeypatch.setattr(GitPolicyReader, method, unreadable)

    lines = uncalled_report.notice_lines(path, [base])

    assert lines == ["push: new public symbols not computed: cannot read the repository"]


def test_print_notice_writes_each_line_to_the_stream(repo):
    path, base = repo
    _push_head(path, {"lib/c.py": "def one():\n    pass\n\n\ndef two():\n    pass\n"})
    stream = io.StringIO()

    uncalled_report.print_notice(path, [base], stream)

    assert stream.getvalue().count("\n") == 2


def test_the_push_verification_step_prints_the_report_and_its_outcome_is_unchanged(repo, capsys):
    path, base = repo
    git(path, "remote", "add", "origin", "https://git-host.example.test/some-owner/some-repo.git")
    head = _push_head(path, {"lib/c.py": "def fresh():\n    pass\n"})

    with_base = push_verb._run_verification_outcome(path, body="body", skip=False, base_refs=[base])
    reported = capsys.readouterr().err
    without_base = push_verb._run_verification_outcome(path, body="body", skip=False)

    assert f"push: new public symbol with no caller and no test at {head}: lib/c.py::fresh" in reported
    assert with_base == without_base == ("body", push_verb.VERIFICATION_NONE, None)
    assert "no caller and no test" not in capsys.readouterr().err
