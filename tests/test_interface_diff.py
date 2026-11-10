"""The declared interface: its parser and the diff between two revisions, through
the parser, the library and the `loadout-interface diff` verb."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.cli import main as umbrella_main
from clagentic_loadout.interface import verb as interface_verb
from clagentic_loadout.interface.diff import (
    CHANGE_ADDED,
    CHANGE_CHANGED,
    CHANGE_REMOVED,
    STATE_MISSING,
    STATE_PRESENT,
    diff_documents,
)
from clagentic_loadout.interface.model import InterfaceError, parse_interface_text
from tests._support.interface_fixtures import (
    ADDED_INTERFACE,
    BASE_INTERFACE,
    RENAMED_INTERFACE,
    interface_repo,
)


def _diff(base: str | None, head: str | None):
    return diff_documents(
        parse_interface_text(base, "base") if base is not None else None,
        parse_interface_text(head, "head") if head is not None else None,
    )


class TestParser:
    def test_a_valid_file_reads_into_sorted_names_and_codes(self):
        doc = parse_interface_text(BASE_INTERFACE, "x")

        assert [v.name for v in doc.verbs] == ["tool-one", "tool-two"]
        assert doc.verbs[0].exit_codes == (("failed", 5), ("ok", 0), ("usage", 1))
        assert doc.verbs[0].subcommands == ("run",)
        assert doc.schemas == ("thing.schema.json",)
        assert doc.env == ("TOOL_HOME",)

    @pytest.mark.parametrize(
        "text,fragment",
        [
            ("- not a mapping\n", "top level must be a mapping"),
            ("schema_version: 2\n", "schema_version must be 1"),
            ("schema_version: 1\nverbz: []\n", "unknown top-level key"),
            ("schema_version: 1\nverbs: [{name: a}, {name: a}]\n", "more than once"),
            ("schema_version: 1\nverbs: [{name: a, exit_codes: {ok: yes}}]\n", "must be an integer"),
            ("schema_version: 1\nverbs: [{name: a, colour: red}]\n", "unknown key"),
            ("schema_version: 1\nenv: [A, A]\n", "more than once"),
            ("schema_version: 1\nschemas: not-a-list\n", "list of non-empty strings"),
            ("schema_version: 1\nverbs: [unclosed\n", "cannot be read as YAML"),
        ],
    )
    def test_a_file_that_departs_from_the_shape_is_refused_naming_where(self, text, fragment):
        with pytest.raises(InterfaceError) as exc:
            parse_interface_text(text, "some/place.yaml")

        assert fragment in str(exc.value) and "some/place.yaml" in str(exc.value)


class TestDiff:
    def test_a_renamed_verb_is_breaking_and_names_the_old_verb(self):
        diff = _diff(BASE_INTERFACE, RENAMED_INTERFACE)

        assert diff.breaking
        removed = [c for c in diff.changes if c.change == CHANGE_REMOVED]
        assert [c.element for c in removed] == ["verb tool-one"]
        assert [c.element for c in diff.changes if c.change == CHANGE_ADDED] == ["verb tool-uno"]

    def test_only_additions_is_not_breaking(self):
        diff = _diff(BASE_INTERFACE, ADDED_INTERFACE)

        assert not diff.breaking
        assert {c.change for c in diff.changes} == {CHANGE_ADDED}
        assert {c.kind for c in diff.changes} == {"env", "exit_code"}

    def test_a_removed_or_renumbered_exit_code_is_breaking(self):
        removed = _diff(BASE_INTERFACE, BASE_INTERFACE.replace(", failed: 5", ""))
        renumbered = _diff(BASE_INTERFACE, BASE_INTERFACE.replace("failed: 5", "failed: 6"))

        assert removed.breaking and removed.changes[0].element == "verb tool-one exit failed"
        assert renumbered.breaking
        change = renumbered.changes[0]
        assert change.change == CHANGE_CHANGED and "from 5 to 6" in change.detail
        assert change.name == "tool-one"

    @pytest.mark.parametrize(
        "head,element",
        [
            (BASE_INTERFACE.replace("subcommands: [run]", "subcommands: []"), "verb tool-one subcommand run"),
            (BASE_INTERFACE.replace("schemas: [thing.schema.json]", "schemas: []"), "schema thing.schema.json"),
            (BASE_INTERFACE.replace("env: [TOOL_HOME]", "env: []"), "env TOOL_HOME"),
        ],
    )
    def test_removing_any_other_element_is_breaking(self, head, element):
        diff = _diff(BASE_INTERFACE, head)

        assert diff.breaking and [c.element for c in diff.breaking_changes()] == [element]

    def test_a_missing_file_at_either_side_is_reported_and_not_breaking(self):
        no_base = _diff(None, BASE_INTERFACE)
        no_head = _diff(BASE_INTERFACE, None)
        neither = _diff(None, None)

        assert (no_base.base_state, no_base.head_state) == (STATE_MISSING, STATE_PRESENT)
        assert (no_head.base_state, no_head.head_state) == (STATE_PRESENT, STATE_MISSING)
        assert not (no_base.breaking or no_head.breaking or neither.breaking)
        assert all(d.notes for d in (no_base, no_head, neither))
        assert no_base.changes == no_head.changes == ()


def _run(capsys, *argv):
    capsys.readouterr()
    code = interface_verb.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestDiffVerb:
    def test_a_renamed_verb_prints_breaking_and_names_it_and_exits_zero(self, tmp_path, capsys):
        repo, base, head = interface_repo(tmp_path, BASE_INTERFACE, RENAMED_INTERFACE)

        code, out, _ = _run(capsys, "diff", "--base", base, "--head", head, "--repo-path", str(repo))

        result = json.loads(out)
        assert code == 0
        assert result["breaking"] is True
        assert [c["element"] for c in result["removed"]] == ["verb tool-one"]
        assert [c["element"] for c in result["added"]] == ["verb tool-uno"]

    def test_additions_only_print_not_breaking(self, tmp_path, capsys):
        repo, base, head = interface_repo(tmp_path, BASE_INTERFACE, ADDED_INTERFACE)

        code, out, _ = _run(capsys, "diff", "--base", base, "--head", head, "--repo-path", str(repo))

        result = json.loads(out)
        assert code == 0 and result["breaking"] is False and result["removed"] == []

    def test_a_revision_without_the_file_is_reported_not_an_error(self, tmp_path, capsys):
        repo, base, head = interface_repo(tmp_path, None, BASE_INTERFACE)

        code, out, _ = _run(capsys, "diff", "--base", base, "--head", head, "--repo-path", str(repo))

        result = json.loads(out)
        assert code == 0
        assert (result["base"], result["head"]) == ("missing", "present")
        assert result["breaking"] is False and result["notes"]

    def test_an_unknown_revision_is_exit_90_naming_it(self, tmp_path, capsys):
        repo, _base, head = interface_repo(tmp_path, BASE_INTERFACE, BASE_INTERFACE)

        code, out, err = _run(
            capsys, "diff", "--base", "no-such-rev", "--head", head, "--repo-path", str(repo)
        )

        assert code == interface_verb.EXIT_UNREADABLE and out == ""
        assert "no-such-rev" in err

    def test_an_invalid_interface_file_is_exit_91_naming_the_revision(self, tmp_path, capsys):
        repo, base, head = interface_repo(tmp_path, BASE_INTERFACE, "schema_version: 9\n")

        code, _out, err = _run(capsys, "diff", "--base", base, "--head", head, "--repo-path", str(repo))

        assert code == interface_verb.EXIT_INTERFACE_INVALID
        assert "schema_version" in err and head[:12] in err

    def test_the_umbrella_reaches_the_verb(self, tmp_path, capsys):
        repo, base, head = interface_repo(tmp_path, BASE_INTERFACE, RENAMED_INTERFACE)

        capsys.readouterr()
        code = umbrella_main(
            ["interface", "diff", "--base", base, "--head", head, "--repo-path", str(repo)]
        )

        assert code == 0 and json.loads(capsys.readouterr().out)["breaking"] is True
