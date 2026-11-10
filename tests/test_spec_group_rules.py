"""`loadout-spec new` fills the group rules through the one shared resolution
`loadout-profile brief --role` uses, and never refuses on a groups problem."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.review import profile_verb as pv
from clagentic_loadout.rules.applying import format_rule_item
from clagentic_loadout.rules.groups import GROUPS_FILE_ENV_VAR
from clagentic_loadout.spec import model
from clagentic_loadout.spec import verb as spec_verb
from tests._gate_repo import git
from tests._support.roles_groups import profile_repo, write_groups

GROUPS = """\
schema_version: 1
groups:
  - id: x-group
    jurisdiction: ["x-*"]
    rules:
      - {id: rule-one, statement: Do the first thing., source: doc-one}
      - {id: rule-two, statement: Do the second thing.}
  - id: elsewhere
    jurisdiction: ["y-*"]
    rules:
      - {id: rule-three, statement: Never listed.}
"""


@pytest.fixture(autouse=True)
def _no_ambient_groups(monkeypatch):
    monkeypatch.delenv(GROUPS_FILE_ENV_VAR, raising=False)


def _new(capsys, tmp_path, *argv, role="any-role"):
    capsys.readouterr()
    code = spec_verb.main(["new", "--role", role, *argv], config_root=tmp_path / "cfg")
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if code == 0 else None), captured.err


def test_a_matching_group_fills_items_with_source_and_no_note(tmp_path, capsys):
    repo = profile_repo(tmp_path)
    groups = write_groups(tmp_path, GROUPS)

    code, draft, _ = _new(
        capsys, tmp_path, "--repo-path", str(repo), "--repo", "x-tool", "--groups-file", str(groups)
    )

    assert code == 0
    assert draft["group_rules"] == {
        "source": f"{groups} (flag)",
        "note": None,
        "items": [
            "x-group/rule-one: Do the first thing. [source: doc-one]",
            "x-group/rule-two: Do the second thing.",
        ],
    }


def test_group_rules_fill_for_every_role(tmp_path, capsys):
    repo = profile_repo(tmp_path)
    groups = write_groups(tmp_path, GROUPS)

    for role in ("planner", "builder", "reviewer", "invented-role"):
        _, draft, _ = _new(
            capsys, tmp_path, "--repo-path", str(repo), "--repo", "x-tool",
            "--groups-file", str(groups), role=role,
        )
        assert len(draft["group_rules"]["items"]) == 2, role


def test_no_groups_file_gives_an_empty_section_with_a_note_and_success(tmp_path, capsys):
    repo = profile_repo(tmp_path)

    code, draft, _ = _new(capsys, tmp_path, "--repo-path", str(repo), "--repo", "x-tool")

    assert code == 0
    assert draft["group_rules"]["items"] == [] and draft["group_rules"]["source"] is None
    assert "no groups file is configured" in draft["group_rules"]["note"]


def test_a_malformed_groups_file_gives_an_empty_section_naming_the_file_and_success(tmp_path, capsys):
    repo = profile_repo(tmp_path)
    groups = write_groups(tmp_path, "schema_version: 9\n")

    code, draft, _ = _new(
        capsys, tmp_path, "--repo-path", str(repo), "--repo", "x-tool", "--groups-file", str(groups)
    )

    assert code == 0
    section = draft["group_rules"]
    assert section["items"] == [] and section["source"] is None
    assert str(groups) in section["note"] and "schema_version" in section["note"]


def test_a_repo_that_no_group_selects_says_so(tmp_path, capsys):
    repo = profile_repo(tmp_path)
    groups = write_groups(tmp_path, GROUPS)

    code, draft, _ = _new(
        capsys, tmp_path, "--repo-path", str(repo), "--repo", "z-tool", "--groups-file", str(groups)
    )

    assert code == 0
    assert draft["group_rules"]["items"] == []
    assert "applies to this repository" in draft["group_rules"]["note"]
    assert draft["group_rules"]["source"] == f"{groups} (flag)"


def test_an_unnameable_repository_leaves_the_section_empty_with_a_note(tmp_path, capsys):
    repo = profile_repo(tmp_path)
    # No origin and no --repo: nothing names the repository.
    git(repo, "remote", "remove", "origin")
    groups = write_groups(tmp_path, GROUPS)

    code, draft, _ = _new(capsys, tmp_path, "--repo-path", str(repo), "--groups-file", str(groups))

    assert code == 0
    assert draft["group_rules"]["items"] == []
    assert "no --repo and no readable origin" in draft["group_rules"]["note"]


def test_the_repo_name_defaults_to_the_origin(tmp_path, capsys):
    repo = profile_repo(tmp_path, name="x-from-origin")
    groups = write_groups(tmp_path, GROUPS)

    _, draft, _ = _new(capsys, tmp_path, "--repo-path", str(repo), "--groups-file", str(groups))

    assert len(draft["group_rules"]["items"]) == 2


def test_a_spec_carrying_group_rules_seals_and_verifies_on_show(tmp_path, capsys, monkeypatch):
    repo = profile_repo(tmp_path)
    groups = write_groups(tmp_path, GROUPS)
    monkeypatch.setenv("CLAGENTIC_LOADOUT_SPECS_DIR", str(tmp_path / "specs"))
    _, draft, _ = _new(
        capsys, tmp_path, "--repo-path", str(repo), "--repo", "x-tool", "--groups-file", str(groups)
    )
    draft["goal"] = "A goal."
    draft["acceptance_criteria"] = [{"id": "ac-1", "given": "g", "when": "w", "then": "t"}]
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(draft), encoding="utf-8")

    capsys.readouterr()
    code = spec_verb.main(
        ["seal", "--file", str(path), "--repo-path", str(repo), "--head", "a" * 40],
        config_root=tmp_path / "cfg",
    )
    sealed = json.loads(capsys.readouterr().out)
    assert code == 0
    code = spec_verb.main(["show", sealed["spec_hash"]], config_root=tmp_path / "cfg")
    shown = json.loads(capsys.readouterr().out)

    assert code == 0 and shown["group_rules"]["items"] == draft["group_rules"]["items"]
    assert model.is_spec_hash(sealed["spec_hash"])


def test_profile_brief_keeps_the_same_rules_and_the_62_exit(tmp_path, capsys):
    repo = profile_repo(tmp_path)
    groups = write_groups(tmp_path, GROUPS)

    capsys.readouterr()
    code = pv.main(
        ["brief", "--role", "planner", "--repo-path", str(repo), "--repo", "x-tool",
         "--groups-file", str(groups)]
    )
    brief = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0
    rules = [c for c in brief["acceptance_criteria_candidates"] if c["source"].startswith("group:")]
    assert [c["rule"] for c in rules] == ["rule-one", "rule-two"]

    bad = write_groups(tmp_path, "schema_version: 9\n", name="bad.yaml")
    assert pv.main(
        ["brief", "--role", "planner", "--repo-path", str(repo), "--groups-file", str(bad)]
    ) == pv.EXIT_IO_FAILED


def test_item_format_omits_the_source_suffix_when_there_is_none():
    base = {"group": "g", "id": "r", "statement": "S."}
    assert format_rule_item({**base, "source": None}) == "g/r: S."
    assert format_rule_item({**base, "source": "doc"}) == "g/r: S. [source: doc]"
