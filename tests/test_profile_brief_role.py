"""`loadout-profile brief --role`: the profile shaped for the role reading it."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.review import profile_verb as pv
from clagentic_loadout.review.profile_role_brief import (
    GATE_READ_NOTE,
    POSTURE_KEYS,
    without_posture,
)
from tests._support.roles_groups import (
    profile_repo,
    write_groups,
    write_user_config,
)

POSTURE_IN_TEXT = ("exposure", "data_class", "visibility", "merge_control", "confirmed_at")


def _run(capsys, *argv, config_root=None):
    capsys.readouterr()
    code = pv.main(list(argv), config_root=config_root)
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    return code, (json.loads(lines[-1]) if lines else None), captured.err


def _keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys(inner)


def test_a_builder_gets_class_rules_deny_tool_packages_and_the_test_block(tmp_path, capsys):
    repo = profile_repo(tmp_path)

    code, result, _ = _run(capsys, "brief", "--role", "builder", "--repo-path", str(repo))

    assert code == pv.EXIT_OK and result["result"] == "brief"
    assert result["shaped_for"] == "envelope" and result["role_class"] == "build"
    assert result["class"] == "released-tool"
    assert set(result["class_rules"]) == {"released-tool", "internal"}
    assert result["deny"] == {"names": ["invented-name"], "hosts": ["host.example.test"], "users": []}
    assert result["tool_packages"] == ["tool_one"]
    assert result["tests"] == {"full": ["python3", "-m", "pytest", "--junitxml={junit}"]}
    assert result["paths"] == [{"glob": ".state/**", "class": "internal"}]


def test_no_brief_carries_a_posture_key(tmp_path, capsys):
    repo = profile_repo(tmp_path)
    groups = write_groups(tmp_path)

    for role in ("builder", "planner", "reviewer", "troubleshooter", "coordinator"):
        _, result, _ = _run(
            capsys, "brief", "--role", role, "--repo-path", str(repo), "--groups-file", str(groups)
        )
        found = set(_keys(result))
        assert not found & POSTURE_KEYS, (role, found & POSTURE_KEYS)
        text = json.dumps(result)
        assert not any(f'"{key}"' in text for key in POSTURE_IN_TEXT), role


def test_posture_keys_are_removed_at_any_depth_with_their_source_companions():
    doc = {
        "a": [{"exposure": "x", "exposure_source": "y", "kept": 1}],
        "merge_control": "z",
        "nested": {"visibility": "v", "data": "d", "fine": [{"confirmed_at": "t", "ok": 2}]},
    }

    assert without_posture(doc) == {"a": [{"kept": 1}], "nested": {"fine": [{"ok": 2}]}}


def test_a_planner_gets_class_and_group_rules_as_acceptance_criteria_candidates(tmp_path, capsys):
    repo = profile_repo(tmp_path)
    groups = write_groups(tmp_path)

    code, result, _ = _run(
        capsys, "brief", "--role", "planner", "--repo-path", str(repo), "--groups-file", str(groups)
    )

    assert code == pv.EXIT_OK and result["shaped_for"] == "acceptance-criteria"
    candidates = result["acceptance_criteria_candidates"]
    class_sources = {c["source"] for c in candidates if c["source"].startswith("class:")}
    assert class_sources == {"class:released-tool", "class:internal"}
    group_candidates = {c["rule"]: c for c in candidates if c["source"].startswith("group:")}
    assert set(group_candidates) == {"shipped-1", "group-1", "group-2"}
    assert group_candidates["group-1"]["source"] == "group:some-group"
    assert group_candidates["group-1"]["file"] == str(groups)
    assert group_candidates["group-1"]["statement"] == "Keep a changelog."
    assert any(c["source"] == "profile:test" for c in candidates)


def test_a_planner_is_told_how_many_denied_values_there_are_never_the_values(tmp_path, capsys):
    repo = profile_repo(tmp_path)

    _, result, _ = _run(capsys, "brief", "--role", "planner", "--repo-path", str(repo))

    assert "deny" not in result
    assert "invented-name" not in json.dumps(result) and "host.example.test" not in json.dumps(result)
    deny = next(c for c in result["acceptance_criteria_candidates"] if c["source"] == "profile:deny")
    assert "1 names" in deny["statement"] and "1 hosts" in deny["statement"]


def test_a_planner_with_no_groups_file_says_why_there_are_no_group_rules(tmp_path, capsys):
    repo = profile_repo(tmp_path)

    _, result, _ = _run(
        capsys, "brief", "--role", "planner", "--repo-path", str(repo), config_root=tmp_path / "cfg"
    )

    assert "no groups file is configured" in result["group_rules_note"]
    assert not any(c["source"].startswith("group:") for c in result["acceptance_criteria_candidates"])


def test_group_rules_follow_the_repo_name_when_given(tmp_path, capsys):
    repo = profile_repo(tmp_path)
    groups = write_groups(tmp_path)

    _, result, _ = _run(
        capsys,
        "brief", "--role", "planner", "--repo-path", str(repo),
        "--repo", "other-repo", "--groups-file", str(groups),
    )

    assert {c["rule"] for c in result["acceptance_criteria_candidates"] if "rule" in c} == {
        "shipped-1",
        "group-1",
        "group-2",
    }


@pytest.mark.parametrize("role", ["reviewer", "security-reviewer", "release-manager", "security", "merger"])
def test_a_gate_role_is_told_its_read_is_unchanged_and_is_shown_nothing_else(tmp_path, capsys, role):
    repo = profile_repo(tmp_path)

    code, result, _ = _run(capsys, "brief", "--role", role, "--repo-path", str(repo))

    assert code == pv.EXIT_OK
    assert result["shaped_for"] == "gate" and result["role_class"] == "gate"
    assert result["gate_read"] == GATE_READ_NOTE
    assert not {"class", "class_rules", "deny", "tool_packages", "tests"} & set(result)


def test_a_gate_role_does_not_need_a_profile(tmp_path, capsys):
    repo = profile_repo(tmp_path, None)

    code, result, _ = _run(capsys, "brief", "--role", "reviewer", "--repo-path", str(repo))

    assert code == pv.EXIT_OK and result["shaped_for"] == "gate"


def test_other_classes_get_the_class_and_its_rules_for_context(tmp_path, capsys):
    repo = profile_repo(tmp_path)

    _, result, _ = _run(capsys, "brief", "--role", "researcher", "--repo-path", str(repo))

    assert result["shaped_for"] == "context" and result["role_class"] == "inform"
    assert set(result) >= {"class", "class_rules", "paths"}
    assert not {"deny", "tool_packages", "tests"} & set(result)


def test_a_legacy_word_is_shaped_as_its_final_role(tmp_path, capsys):
    repo = profile_repo(tmp_path)

    _, result, _ = _run(capsys, "brief", "--role", "lead", "--repo-path", str(repo))

    assert result["role"] == "project-orchestrator" and result["role_word"] == "lead"
    assert result["role_source"] == "alias" and result["shaped_for"] == "acceptance-criteria"


def test_a_deployment_roles_file_decides_the_shape_by_class_never_by_name(tmp_path, capsys):
    repo = profile_repo(tmp_path)
    root = write_user_config(
        tmp_path / "cfg", "role_classes:\n  zz-custom-role: build\n  builder: inform\n"
    )

    _, custom, _ = _run(capsys, "brief", "--role", "zz-custom-role", "--repo-path", str(repo), config_root=root)
    _, builder, _ = _run(capsys, "brief", "--role", "builder", "--repo-path", str(repo), config_root=root)

    assert custom["shaped_for"] == "envelope" and custom["role_source"] == "deployment"
    assert builder["shaped_for"] == "context" and builder["role_class"] == "inform"
    assert custom["deny"] == {"names": ["invented-name"], "hosts": ["host.example.test"], "users": []}


def test_an_unknown_role_is_a_usage_error_naming_it(tmp_path, capsys):
    repo = profile_repo(tmp_path)

    code, result, err = _run(capsys, "brief", "--role", "no-such-role", "--repo-path", str(repo))

    assert code == pv.EXIT_USAGE and result is None
    assert "no-such-role" in err and "builder" in err


def test_a_missing_profile_exits_60_for_an_author_role(tmp_path, capsys):
    repo = profile_repo(tmp_path, None)

    code, result, err = _run(capsys, "brief", "--role", "builder", "--repo-path", str(repo))

    assert code == pv.EXIT_PROFILE_MISSING == 60
    assert result["result"] == "missing" and "loadout-profile init" in err


def test_an_invalid_profile_exits_61_naming_the_problem(tmp_path, capsys):
    repo = profile_repo(tmp_path, "class: internal\nowner: x\n")

    code, result, err = _run(capsys, "brief", "--role", "builder", "--repo-path", str(repo))

    assert code == pv.EXIT_PROFILE_INVALID == 61
    assert result["result"] == "invalid" and "owner" in result["problem"] and "owner" in err


def test_a_malformed_groups_file_exits_62_naming_the_path(tmp_path, capsys):
    repo = profile_repo(tmp_path)
    groups = write_groups(tmp_path, "schema_version: 9\n")

    code, _, err = _run(
        capsys, "brief", "--role", "planner", "--repo-path", str(repo), "--groups-file", str(groups)
    )

    assert code == pv.EXIT_IO_FAILED == 62 and str(groups) in err


def test_brief_reads_the_revision_when_given_a_ref(tmp_path, capsys):
    repo = profile_repo(tmp_path)
    (repo / ".clagentic" / "loadout" / "profile.yaml").write_text("class: internal\n", encoding="utf-8")

    _, working, _ = _run(capsys, "brief", "--role", "builder", "--repo-path", str(repo))
    _, committed, _ = _run(capsys, "brief", "--role", "builder", "--repo-path", str(repo), "--ref", "HEAD")

    assert (working["class"], committed["class"]) == ("internal", "released-tool")
    assert committed["at"] == "HEAD"
