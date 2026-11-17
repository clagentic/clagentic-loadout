"""`loadout-profile brief --role ROLE --step STEP`: the rules section, narrowed by
role and step, and `loadout-rules` reporting the same record keys. Role and step
names here are invented."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.review import profile_verb as pv
from clagentic_loadout.rules import groups as gr
from clagentic_loadout.rules import verb as rv
from tests._support.roles_groups import profile_repo, write_groups, write_user_config

RULE_GROUPS = """\
schema_version: 1
groups:
  - id: gen
    jurisdiction: ["some-*"]
    rules:
      - {id: all-1, statement: "  Applies to all.\\nSecond line.  ", source: doc-a}
      - {id: r-alpha, statement: Alpha rule., roles: [role-alpha], kind: constraint, said_at: 2026-11-01, relayed: true}
      - {id: r-alpha-s1, statement: Alpha at step one., roles: [role-alpha], steps: [step-one], kind: judgment}
      - {id: r-beta, statement: Beta rule., roles: [role-beta]}
      - {id: r-s2, statement: Step two rule., steps: [step-two]}
      - {id: r-dispatch, statement: Dispatch rule., steps: [dispatch]}
      - {id: r-po, statement: Orchestrator rule., roles: [project-orchestrator]}
      - {id: odd-key, statement: Has an odd key., colour: red}
      - {id: odd-kind, statement: Has an odd kind., kind: maybe}
"""

USER_CONFIG = (
    "role_classes:\n  role-alpha: build\n  role-beta: scope\n  role-gate: gate\n"
    "rules:\n  steps: [step-one, step-two]\n"
)

ALWAYS = {"all-1", "odd-key", "odd-kind"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.delenv(gr.GROUPS_FILE_ENV_VAR, raising=False)
    return (
        profile_repo(tmp_path),
        write_groups(tmp_path, RULE_GROUPS),
        write_user_config(tmp_path / "cfg", USER_CONFIG),
    )


def _json(capsys, main, argv, **kwargs):
    capsys.readouterr()
    code = main(argv, **kwargs)
    out = capsys.readouterr().out.strip().splitlines()
    return code, (json.loads(out[-1]) if out else None)


def _brief(capsys, world, role, step=None):
    repo, groups, cfg = world
    argv = ["brief", "--role", role, "--repo-path", str(repo), "--groups-file", str(groups)]
    if step is not None:
        argv += ["--step", step]
    return _json(capsys, pv.main, argv, config_root=cfg)


SELECTION = [
    ("role-alpha", "step-one", ALWAYS | {"r-alpha", "r-alpha-s1"}),
    ("role-alpha", "step-two", ALWAYS | {"r-alpha", "r-s2"}),
    ("role-alpha", None, ALWAYS | {"r-alpha", "r-alpha-s1", "r-s2", "r-dispatch"}),
    ("role-beta", "step-one", ALWAYS | {"r-beta"}),
    ("role-gate", "step-two", ALWAYS | {"r-s2"}),
    ("lead", None, ALWAYS | {"r-po", "r-s2", "r-dispatch"}),
    ("planner", None, ALWAYS | {"r-s2", "r-dispatch"}),
    ("role-zed", None, ALWAYS | {"r-s2", "r-dispatch"}),
    ("role-alpha", "step-zed", ALWAYS | {"r-alpha"}),
    ("role-beta", "dispatch", ALWAYS | {"r-beta", "r-dispatch"}),
    ("role-beta", "hand-off", ALWAYS | {"r-beta"}),
]


@pytest.mark.parametrize(("role", "step", "expected"), SELECTION)
def test_the_rules_section_lists_only_records_for_the_role_and_step(capsys, world, role, step, expected):
    code, result = _brief(capsys, world, role, step)

    assert code == 0
    assert {item["id"] for item in result["rules"]["items"]} == expected


DEFAULT_ONLY = ["file", "direct", "scope", "dispatch", "review", "merge", "hand-off"]
MALFORMED = "only the built-in steps are declared"
STEP_SOURCES = [
    # (user config, step asked, declared, source of the asked step, note fragment, extra declared step)
    ("", "dispatch", True, "default", None, None),
    ("rules:\n  steps: [step-one]\n", "step-one", True, "deployment", None, "step-one"),
    ("rules:\n  steps: [step-one, merge]\n", "merge", True, "default", None, "step-one"),
    ("rules:\n  steps: [step-one]\n", "step-zed", False, None, "step 'step-zed' is not declared", "step-one"),
    ("rules:\n  steps: [step-one, 3]\n", "hand-off", True, "default", MALFORMED, None),
    ("rules:\n  steps: nope\n", "step-one", False, None, MALFORMED, None),
]


@pytest.mark.parametrize(("config", "step", "declared", "source", "note", "extra"), STEP_SOURCES)
def test_the_brief_declares_the_built_in_steps_and_names_each_steps_source(
    capsys, world, config, step, declared, source, note, extra
):
    repo, groups, _ = world
    cfg = write_user_config(repo.parent / "cfg-steps", config)

    code, result = _json(
        capsys, pv.main,
        ["brief", "--role", "planner", "--repo-path", str(repo), "--groups-file", str(groups),
         "--step", step],
        config_root=cfg,
    )

    section = result["rules"]
    assert code == 0
    assert section["applies_to"]["step_declared"] is declared
    assert section["applies_to"]["step_source"] == source
    assert [d["step"] for d in section["declared_steps"]] == DEFAULT_ONLY + ([extra] if extra else [])
    assert {d["source"] for d in section["declared_steps"][: len(DEFAULT_ONLY)]} == {"default"}
    assert (note is None) or any(note in n for n in section["notes"])
    assert ("r-dispatch" in {i["id"] for i in section["items"]}) is (step == "dispatch")
    assert ALWAYS <= {i["id"] for i in section["items"]}


def test_an_undeclared_role_and_step_are_reported_and_the_profile_facts_remain(capsys, world):
    code, result = _brief(capsys, world, "role-zed", "step-zed")

    assert code == 0
    applies = result["rules"]["applies_to"]
    assert applies["role_declared"] is False and applies["step_declared"] is False
    assert any("role 'role-zed' is not declared" in n for n in result["rules"]["notes"])
    assert any("step 'step-zed' is not declared" in n for n in result["rules"]["notes"])
    assert result["shaped_for"] == "context" and result["role_class"] is None
    assert result["class"] == "released-tool" and "class_rules" in result and "paths" in result


def test_a_listed_rule_carries_its_words_source_kind_group_file_and_keys(capsys, world):
    _, groups, _ = world
    _, result = _brief(capsys, world, "role-alpha", "step-one")

    items = {item["id"]: item for item in result["rules"]["items"]}
    assert items["all-1"] == {
        "id": "all-1",
        "statement": "Applies to all.\nSecond line.",
        "source": "doc-a",
        "kind": None,
        "group": "gen",
        "file": str(groups),
        "not_understood": [],
    }
    alpha = items["r-alpha"]
    assert (alpha["kind"], alpha["said_at"], alpha["relayed"], alpha["roles"]) == (
        "constraint",
        "2026-11-01",
        True,
        ["role-alpha"],
    )


@pytest.mark.parametrize(
    ("rule_id", "named"),
    [("odd-key", "key 'colour'"), ("odd-kind", "kind 'maybe'")],
)
def test_an_unknown_key_or_kind_is_named_by_the_brief_and_by_loadout_rules(capsys, world, rule_id, named):
    repo, groups, cfg = world
    _, brief = _brief(capsys, world, "role-alpha")
    code, raw = _json(
        capsys, rv.main, ["--repo", "some-repo", "--repo-path", str(repo), "--groups-file", str(groups)],
        config_root=cfg,
    )

    in_brief = next(i for i in brief["rules"]["items"] if i["id"] == rule_id)
    in_rules = next(r for g in raw["groups"] for r in g["rules"] if r["id"] == rule_id)
    assert code == 0
    assert any(named in line for line in in_brief["not_understood"])
    assert in_rules["not_understood"] == in_brief["not_understood"]
    assert in_rules["statement"] == in_brief["statement"]


@pytest.mark.parametrize("role", ["planner", "builder", "coordinator", "researcher", "sysadmin", "reviewer"])
def test_every_role_class_carries_the_rules_section(capsys, world, role):
    code, result = _brief(capsys, world, role)

    assert code == 0 and ALWAYS <= {i["id"] for i in result["rules"]["items"]}
    if role == "reviewer":
        assert not {"class", "class_rules", "paths", "posture"} & set(result)
        assert result["gate_read"].startswith("unchanged")
    else:
        assert {"class", "class_rules"} <= set(result)


@pytest.mark.parametrize("role", ["planner", "builder", "reviewer"])
def test_no_groups_file_configured_is_said_and_exits_zero(capsys, tmp_path, monkeypatch, role):
    monkeypatch.delenv(gr.GROUPS_FILE_ENV_VAR, raising=False)
    repo = profile_repo(tmp_path)

    code, result = _json(
        capsys, pv.main,
        ["brief", "--role", role, "--step", "step-one", "--repo-path", str(repo)],
        config_root=tmp_path / "cfg",
    )

    assert code == 0 and result["rules"]["items"] == []
    assert "no groups file is configured" in result["rules"]["note"]
