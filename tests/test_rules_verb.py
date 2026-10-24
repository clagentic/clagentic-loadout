"""`loadout-rules`: the group rules that apply to a repository, with sources."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.cli import main as umbrella_main
from clagentic_loadout.rules import groups as gr
from clagentic_loadout.rules import verb as rv
from tests._support.roles_groups import (
    profile_repo,
    write_groups,
    write_user_config,
)


def _run(capsys, *argv, config_root=None):
    capsys.readouterr()
    code = rv.main(list(argv), config_root=config_root)
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    return code, (json.loads(lines[-1]) if lines else None), captured.err


def _rules(result):
    return {rule["id"]: rule for group in result["groups"] for rule in group["rules"]}


def test_each_group_rule_prints_with_its_group_and_its_file_source(tmp_path, capsys):
    groups = write_groups(tmp_path)
    repo = profile_repo(tmp_path)

    code, result, _ = _run(
        capsys, "--repo", "some-repo", "--repo-path", str(repo), "--groups-file", str(groups)
    )

    assert code == rv.EXIT_OK and result["result"] == "rules"
    rules = _rules(result)
    assert set(rules) == {"shipped-1", "group-1", "group-2"}
    for rule in rules.values():
        assert rule["file"] == str(groups) and rule["group"]
    assert rules["group-1"]["group"] == "some-group"
    assert rules["group-1"]["source"] == "doc-b"
    assert result["groups_file"] == str(groups) and result["groups_file_from"] == gr.FROM_FLAG


def test_a_group_with_no_rules_prints_its_note(tmp_path, capsys):
    groups = write_groups(tmp_path)

    _, result, _ = _run(
        capsys, "--repo", "some-repo", "--repo-path", str(tmp_path), "--groups-file", str(groups)
    )

    noted = next(g for g in result["groups"] if g["id"] == "noted")
    assert noted["rules"] == [] and noted["group_note"] == "Rules are pending."


def test_jurisdiction_globs_match_bare_names_and_an_owner_is_dropped(tmp_path, capsys):
    groups = write_groups(tmp_path)

    _, bare, _ = _run(capsys, "--repo", "some-thing", "--repo-path", str(tmp_path), "--groups-file", str(groups))
    _, owned, _ = _run(
        capsys, "--repo", "an-owner/some-thing", "--repo-path", str(tmp_path), "--groups-file", str(groups)
    )
    _, none, _ = _run(capsys, "--repo", "elsewhere", "--repo-path", str(tmp_path), "--groups-file", str(groups))

    assert bare["repo"] == owned["repo"] == "some-thing"
    assert [g["id"] for g in bare["groups"]] == ["some-group"] == [g["id"] for g in owned["groups"]]
    assert none["groups"] == []


def test_a_class_selected_group_resolves_against_the_profile_class(tmp_path, capsys):
    groups = write_groups(tmp_path)
    repo = profile_repo(tmp_path)

    _, result, _ = _run(
        capsys, "--repo", "some-repo", "--repo-path", str(repo), "--groups-file", str(groups)
    )

    assert result["profile_class"] == "released-tool"
    shipped = next(g for g in result["groups"] if g["id"] == "shipped-tools")
    assert shipped["selected_by"] == "jurisdiction_class"
    assert result["notes"] == []


def test_with_no_profile_the_class_selected_line_prints_explicitly(tmp_path, capsys):
    groups = write_groups(tmp_path)
    repo = profile_repo(tmp_path, None)

    code, result, _ = _run(
        capsys, "--repo", "some-repo", "--repo-path", str(repo), "--groups-file", str(groups)
    )

    assert code == rv.EXIT_OK and result["profile_class"] is None
    assert any(rv.NO_PROFILE_NOTE in note for note in result["notes"])
    assert "shipped-1" not in _rules(result)
    assert "group-1" in _rules(result)


def test_a_tree_that_is_not_the_named_repository_is_not_read_for_its_class(tmp_path, capsys):
    groups = write_groups(tmp_path)
    repo = profile_repo(tmp_path, name="another-repo")

    _, result, _ = _run(
        capsys, "--repo", "some-repo", "--repo-path", str(repo), "--groups-file", str(groups)
    )

    assert result["profile_class"] is None
    assert any(rv.NO_PROFILE_NOTE in note and "not the repository" in note for note in result["notes"])


def test_a_class_the_profile_schema_does_not_admit_is_printed_with_a_note_and_exit_zero(
    tmp_path, capsys
):
    groups = write_groups(tmp_path)

    code, result, _ = _run(
        capsys, "--repo", "future-repo", "--repo-path", str(tmp_path), "--groups-file", str(groups)
    )

    assert code == rv.EXIT_OK
    (assigned,) = result["assigned_classes"]
    assert assigned["class"] == "future-class" and assigned["file"] == str(groups)
    assert assigned["admitted_by_profile_schema"] is False
    assert assigned["note"] == "the profile schema does not admit the class 'future-class'"
    (future_group,) = assigned["class_selected_groups"]
    assert future_group["id"] == "future" and future_group["applied"] is False
    assert future_group["rules"][0]["statement"] == "A future rule."
    assert future_group["rules"][0]["file"] == str(groups)
    assert result["groups"] == []


def test_without_a_groups_file_the_result_is_empty_explicit_and_exit_zero(tmp_path, capsys):
    code, result, _ = _run(
        capsys, "--repo", "some-repo", "--repo-path", str(tmp_path), config_root=tmp_path / "cfg"
    )

    assert code == rv.EXIT_OK
    assert result["result"] == "empty" and result["groups"] == []
    assert "no groups file is configured" in result["reason"]
    assert result["groups_file"] is None and result["groups_file_from"] == gr.FROM_NONE


def test_a_configured_path_with_no_file_is_empty_and_names_the_path(tmp_path, capsys):
    missing = tmp_path / "missing.yaml"

    code, result, _ = _run(
        capsys, "--repo", "some-repo", "--repo-path", str(tmp_path), "--groups-file", str(missing)
    )

    assert code == rv.EXIT_OK and result["result"] == "empty"
    assert str(missing) in result["reason"]


def test_the_location_comes_from_the_flag_then_the_environment_then_the_user_config(
    tmp_path, monkeypatch
):
    root = write_user_config(tmp_path / "cfg", "rules:\n  groups_file: from-config.yaml\n")

    assert gr.resolve_groups_location(None, {}, config_root=root) == gr.GroupsLocation(
        "from-config.yaml", gr.FROM_CONFIG
    )
    assert gr.resolve_groups_location(
        None, {gr.GROUPS_FILE_ENV_VAR: "from-env.yaml"}, config_root=root
    ) == gr.GroupsLocation("from-env.yaml", gr.FROM_ENV)
    assert gr.resolve_groups_location(
        "from-flag.yaml", {gr.GROUPS_FILE_ENV_VAR: "from-env.yaml"}, config_root=root
    ) == gr.GroupsLocation("from-flag.yaml", gr.FROM_FLAG)
    assert gr.resolve_groups_location(None, {}, config_root=tmp_path / "none").path is None


def test_the_environment_variable_is_read_by_the_verb(tmp_path, capsys, monkeypatch):
    groups = write_groups(tmp_path)
    monkeypatch.setenv(gr.GROUPS_FILE_ENV_VAR, str(groups))

    _, result, _ = _run(capsys, "--repo", "other-repo", "--repo-path", str(tmp_path))

    assert result["groups_file_from"] == gr.FROM_ENV and "group-1" in _rules(result)


@pytest.mark.parametrize(
    "text, needle",
    [
        ("schema_version: 2\ngroups: []\n", "schema_version"),
        ("schema_version: 1\ngroups: {}\n", "groups must be a list"),
        ("schema_version: 1\ngroups:\n  - {id: a, jurisdiction: [x], jurisdiction_class: y}\n", "exactly one"),
        ("schema_version: 1\ngroups:\n  - {id: a, jurisdiction: x}\n", "list of non-empty strings"),
        ("schema_version: 1\ngroups:\n  - {id: a, jurisdiction: [x], rules: [{id: r}]}\n", "statement"),
        ("- not\n- a mapping\n", "top level"),
        ("key: [unclosed\n", "cannot be read as YAML"),
    ],
)
def test_a_malformed_groups_file_exits_80_naming_the_resolved_path(tmp_path, capsys, text, needle):
    groups = write_groups(tmp_path, text)

    code, result, err = _run(
        capsys, "--repo", "some-repo", "--repo-path", str(tmp_path), "--groups-file", str(groups)
    )

    assert code == rv.EXIT_GROUPS_FILE == 80 and result is None
    assert str(groups) in err and needle in err


def test_an_invalid_profile_exits_81_naming_the_path(tmp_path, capsys):
    groups = write_groups(tmp_path)
    repo = profile_repo(tmp_path, "class: internal\nowner: x\n")

    code, _, err = _run(
        capsys, "--repo", "some-repo", "--repo-path", str(repo), "--groups-file", str(groups)
    )

    assert code == rv.EXIT_PROFILE_INVALID == 81
    assert "owner" in err and str(repo) in err


def test_the_verb_is_reachable_through_the_umbrella(tmp_path, capsys):
    groups = write_groups(tmp_path)

    code = umbrella_main(
        ["rules", "--repo", "other-repo", "--repo-path", str(tmp_path), "--groups-file", str(groups)]
    )

    assert code == 0 and json.loads(capsys.readouterr().out)["result"] == "rules"


def test_a_missing_repo_argument_is_a_usage_error(capsys):
    code, _, err = _run(capsys)

    assert code == rv.EXIT_USAGE and "--repo" in err
