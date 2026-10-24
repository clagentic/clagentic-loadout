"""The role vocabulary: final roles by class, legacy words as aliases, and the
deployment's own overrides. Nothing here names an agent."""

from __future__ import annotations

from pathlib import Path

import pytest

from clagentic_loadout import role_vocabulary as rv
from clagentic_loadout.guard.role_allowlist import BashRole
from clagentic_loadout.provisioning.roles import DEFAULT_ROLE_VERBS
from tests._support.roles_groups import write_user_config

EXPECTED = {
    "coordinator": "coordinate",
    "project-orchestrator": "scope",
    "planner": "scope",
    "builder": "build",
    "reviewer": "gate",
    "security-reviewer": "gate",
    "release-manager": "gate",
    "troubleshooter": "inform",
    "observer": "inform",
    "researcher": "inform",
    "sysadmin": "host",
}

SRC = Path(rv.__file__).resolve().parent


def test_the_default_vocabulary_is_the_eleven_roles_in_six_classes():
    assert rv.ROLE_CLASSES == EXPECTED
    assert rv.CLASSES == ("coordinate", "scope", "build", "gate", "inform", "host")
    assert set(EXPECTED.values()) == set(rv.CLASSES)


def test_every_legacy_word_main_accepts_resolves_to_a_final_role():
    legacy_words = {role.value for role in BashRole} | set(DEFAULT_ROLE_VERBS)

    for word in legacy_words:
        resolution = rv.resolve_role(word)
        assert resolution.role in rv.ROLE_CLASSES, word
        assert resolution.role_class == rv.ROLE_CLASSES[resolution.role]


@pytest.mark.parametrize(
    "word, role, role_class",
    [
        ("security", "security-reviewer", "gate"),
        ("merger", "release-manager", "gate"),
        ("lead", "project-orchestrator", "scope"),
        ("planning_reader", "planner", "scope"),
        ("infra", "sysadmin", "host"),
    ],
)
def test_a_legacy_word_is_an_alias_of_its_final_role(word, role, role_class):
    resolution = rv.resolve_role(word)

    assert (resolution.role, resolution.role_class, resolution.source) == (
        role,
        role_class,
        rv.SOURCE_ALIAS,
    )
    assert resolution.word == word


def test_a_word_in_both_vocabularies_is_a_default_role_not_an_alias():
    assert rv.resolve_role("builder").source == rv.SOURCE_DEFAULT


def test_an_unknown_word_names_itself_and_the_known_set():
    with pytest.raises(rv.RoleVocabularyError) as info:
        rv.resolve_role("no-such-role")

    assert "no-such-role" in str(info.value) and "planner" in str(info.value)


def test_a_deployment_roles_file_gives_a_role_of_its_own_a_class(tmp_path):
    root = write_user_config(tmp_path / "cfg", "role_classes:\n  custom-role: build\n  builder: gate\n")

    overrides = rv.load_role_classes(root)

    assert rv.resolve_role("custom-role", overrides) == rv.RoleResolution(
        "custom-role", "custom-role", "build", rv.SOURCE_DEPLOYMENT
    )
    assert rv.resolve_role("builder", overrides).role_class == "gate"
    assert rv.resolve_role("planner", overrides).role_class == "scope"


def test_a_deployment_class_for_a_final_role_reaches_its_legacy_alias(tmp_path):
    overrides = rv.load_role_classes(
        write_user_config(tmp_path / "cfg", "role_classes:\n  release-manager: inform\n")
    )

    assert rv.resolve_role("merger", overrides).role_class == "inform"


def test_roles_in_a_class_merge_the_default_and_the_deployment(tmp_path):
    overrides = {"custom-role": "gate"}

    assert rv.roles_in_class("gate", overrides) == [
        "custom-role",
        "release-manager",
        "reviewer",
        "security-reviewer",
    ]


def test_no_file_or_no_section_means_no_overrides(tmp_path):
    assert rv.load_role_classes(tmp_path / "missing") == {}
    assert rv.load_role_classes(write_user_config(tmp_path / "cfg", "other: {}\n")) == {}


@pytest.mark.parametrize(
    "text, needle",
    [
        ("role_classes:\n  some-role: wizard\n", "wizard"),
        ("role_classes:\n  'bad word': build\n", "bad word"),
    ],
)
def test_a_malformed_role_classes_section_names_the_value(tmp_path, text, needle):
    with pytest.raises(rv.RoleVocabularyError) as info:
        rv.load_role_classes(write_user_config(tmp_path / "cfg", text))

    assert needle in str(info.value)


def test_no_name_from_a_deployment_registry_appears_in_loadout_code():
    """A deployment's registry names its agents; this package must name none. The
    registry here is synthetic, and the vocabulary is the only thing a feature
    keys on."""
    registry = {"zz-agent-one": "builder", "zz-agent-two": "planner", "zz-agent-three": "sysadmin"}
    sources = [path for path in SRC.rglob("*.py")]
    assert sources

    for path in sources:
        text = path.read_text(encoding="utf-8").lower()
        for agent in registry:
            assert agent not in text, f"{agent!r} appears in {path}"

    # And the shipped vocabulary keys only on roles and classes.
    assert not set(registry) & (set(rv.ROLE_CLASSES) | set(rv.LEGACY_ALIASES))
