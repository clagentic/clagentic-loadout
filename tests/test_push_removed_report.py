"""At push time the test cases, public symbols and verbs a branch removes are printed.
Reporting only: nothing here can fail a push or change its outcome."""

from __future__ import annotations

import io

import pytest

from clagentic_loadout.push import removed_report
from clagentic_loadout.push import verb as push_verb
from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReadError
from tests._gate_repo import git
from tests._review_evaluate_support import commit_files, make_repo
from tests.test_spec_removals import commit_changes

BASE = {
    "lib/a.py": "def keep():\n    pass\n\n\ndef gone():\n    pass\n",
    "tests/test_a.py": "def test_kept():\n    pass\n\n\ndef test_dropped():\n    pass\n",
}

#: (change, lines printed after the head sha is known). Each row is one push whose base is BASE.
ROWS = {
    "a-removed-public-symbol": ({"lib/a.py": "def keep():\n    pass\n"}, ["lib/a.py::gone (symbol)"]),
    "a-removed-test-case": (
        {"tests/test_a.py": "def test_kept():\n    pass\n"}, ["tests/test_a.py::test_dropped (test)"]
    ),
    "a-deleted-file": (
        {"lib/a.py": None},
        ["lib/a.py::gone (symbol)", "lib/a.py::keep (symbol)"],
    ),
    "nothing-removed": ({"lib/c.py": "def fresh():\n    pass\n"}, []),
}


@pytest.fixture
def repo(tmp_path):
    path = make_repo(tmp_path)
    return path, commit_files(path, BASE)


@pytest.mark.parametrize("name", list(ROWS))
def test_what_a_branch_removes_is_named_and_nothing_else_is(repo, name):
    path, base = repo
    change, items = ROWS[name]
    head = commit_changes(path, change)

    lines = removed_report.notice_lines(path, [base])

    assert lines == [f"push: removed at {head}: {item}" for item in items]


def test_the_list_is_capped_and_the_rest_counted(repo, monkeypatch):
    path, base = repo
    monkeypatch.setattr(removed_report, "REPORT_CAP", 1)
    head = commit_changes(path, {"lib/a.py": None})

    lines = removed_report.notice_lines(path, [base])

    assert len(lines) == 2 and lines[-1] == f"push: and 1 more removed at {head}"


def test_a_base_that_does_not_resolve_prints_nothing(repo):
    path, _ = repo
    commit_changes(path, {"lib/a.py": None})

    assert removed_report.notice_lines(path, ["no-such-ref"]) == []


@pytest.mark.parametrize("method", ["changed_paths", "resolve_commit"], ids=["paths-unreadable", "head-unreadable"])
def test_a_tree_that_cannot_be_read_is_one_not_computed_line_never_an_error(repo, monkeypatch, method):
    path, base = repo
    commit_changes(path, {"lib/a.py": None})

    def unreadable(self, *args):
        raise PolicyReadError("cannot read the repository")

    monkeypatch.setattr(GitPolicyReader, method, unreadable)

    lines = removed_report.notice_lines(path, [base])

    assert lines == ["push: removed behaviour not computed: cannot read the repository"]


def test_print_notice_writes_each_line_to_the_stream(repo):
    path, base = repo
    commit_changes(path, {"lib/a.py": None})
    stream = io.StringIO()

    removed_report.print_notice(path, [base], stream)

    assert stream.getvalue().count("\n") == 2


def test_the_push_verification_step_prints_the_report_and_its_outcome_is_unchanged(repo, capsys):
    path, base = repo
    git(path, "remote", "add", "origin", "https://git-host.example.test/some-owner/some-repo.git")
    head = commit_changes(path, {"lib/a.py": "def keep():\n    pass\n"})

    with_base = push_verb._run_verification_outcome(path, body="body", skip=False, base_refs=[base])
    reported = capsys.readouterr().err
    without_base = push_verb._run_verification_outcome(path, body="body", skip=False)

    assert f"push: removed at {head}: lib/a.py::gone (symbol)" in reported
    assert with_base == without_base == ("body", push_verb.VERIFICATION_NONE, None)
    assert "push: removed at" not in capsys.readouterr().err
