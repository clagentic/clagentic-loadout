"""loadout-doctor: the role_classes section, the groups file, and the advisory
for a profile class the groups file disagrees with."""

from __future__ import annotations

from clagentic_loadout.doctor import cli as doctor_cli
from clagentic_loadout.doctor.roles_groups_check import check_roles_and_groups
from clagentic_loadout.review.policy_source import PROFILE_PATH
from tests._support.roles_groups import (
    GROUPS_TEXT,
    profile_repo,
    write_groups,
    write_user_config,
)


def test_nothing_configured_is_ok_with_an_advisory(tmp_path):
    result = check_roles_and_groups(None, config_root=tmp_path / "cfg")

    assert result.ok and "no groups file configured" in result.summary


def test_a_configured_groups_file_that_does_not_exist_is_an_advisory_naming_it(tmp_path):
    missing = tmp_path / "missing.yaml"
    root = write_user_config(tmp_path / "cfg", f"rules:\n  groups_file: {missing}\n")

    result = check_roles_and_groups(None, config_root=root)

    assert result.ok and str(missing) in result.summary
    assert result.resolved["groups_file_from"] == "config"


def test_a_valid_roles_section_and_groups_file_pass(tmp_path):
    groups = write_groups(tmp_path)
    root = write_user_config(
        tmp_path / "cfg", f"role_classes:\n  custom-role: build\nrules:\n  groups_file: {groups}\n"
    )

    result = check_roles_and_groups(None, config_root=root)

    assert result.ok and result.resolved["role_classes"] == ["custom-role"]
    assert result.resolved["groups_file"] == str(groups)


def test_an_unknown_class_in_role_classes_fails_naming_the_value(tmp_path):
    root = write_user_config(tmp_path / "cfg", "role_classes:\n  custom-role: wizard\n")

    result = check_roles_and_groups(None, config_root=root)

    assert not result.ok and "wizard" in result.summary


def test_a_malformed_groups_file_fails_naming_its_path(tmp_path):
    groups = write_groups(tmp_path, "schema_version: 7\n")
    root = write_user_config(tmp_path / "cfg", f"rules:\n  groups_file: {groups}\n")

    result = check_roles_and_groups(None, config_root=root)

    assert not result.ok and str(groups) in result.summary


def test_a_profile_class_the_groups_file_disagrees_with_is_advisory_only(tmp_path):
    groups = write_groups(tmp_path)
    root = write_user_config(tmp_path / "cfg", f"rules:\n  groups_file: {groups}\n")
    repo = profile_repo(tmp_path, "class: internal\n")

    result = check_roles_and_groups(repo, config_root=root)

    assert result.ok
    assert "profile class 'internal'" in result.summary and "assigns 'released-tool'" in result.summary
    assert "advisory" in result.summary


def test_an_agreeing_profile_class_raises_no_advisory(tmp_path):
    groups = write_groups(tmp_path)
    root = write_user_config(tmp_path / "cfg", f"rules:\n  groups_file: {groups}\n")
    repo = profile_repo(tmp_path)

    result = check_roles_and_groups(repo, config_root=root)

    assert result.ok and result.resolved["advisories"] == []


def test_a_class_the_profile_schema_does_not_admit_is_an_advisory_not_a_failure(tmp_path):
    groups = write_groups(tmp_path)
    root = write_user_config(tmp_path / "cfg", f"rules:\n  groups_file: {groups}\n")
    repo = profile_repo(tmp_path, "class: internal\n", name="future-repo")

    result = check_roles_and_groups(repo, config_root=root)

    assert result.ok and "does not admit" in result.summary


def test_a_repo_with_no_profile_raises_no_disagreement(tmp_path):
    groups = write_groups(tmp_path, GROUPS_TEXT)
    root = write_user_config(tmp_path / "cfg", f"rules:\n  groups_file: {groups}\n")
    repo = profile_repo(tmp_path, None)

    result = check_roles_and_groups(repo, config_root=root)

    assert result.ok and PROFILE_PATH not in result.summary


def test_the_doctor_verb_reports_the_check(tmp_path, capsys):
    rc = doctor_cli.main(["--config-root", str(tmp_path / "cfg")])

    assert rc == doctor_cli.EXIT_OK
    assert "roles_and_groups" in capsys.readouterr().out
