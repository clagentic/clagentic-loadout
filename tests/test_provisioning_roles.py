"""test_provisioning_roles.py — ROLE -> verb-set config coverage (lr-4e04).

Covers: the default/reference mapping, repo-local override (replace, not
merge), unknown-role/unknown-verb resolved-values errors, and malformed
config shapes. Uses SYNTHETIC role/verb-set names throughout — the
conformance gate (CLAUDE.md rule 6a) requires this module to work with
invented role names and no lore present; nothing here depends on the seed
role names being the only possible taxonomy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clagentic_loadout.provisioning.roles import (
    DEFAULT_ROLE_VERBS,
    InvalidRoleConfigError,
    KNOWN_VERBS,
    load_role_verbs,
    resolve_role_verbs,
)


def _write_config(repo_root: Path, yaml_text: str) -> None:
    loadout_dir = repo_root / ".clagentic" / "loadout"
    loadout_dir.mkdir(parents=True, exist_ok=True)
    (loadout_dir / "config.yaml").write_text(yaml_text, encoding="utf-8")


def _write_legacy_config(repo_root: Path, yaml_text: str) -> None:
    loadout_dir = repo_root / ".loadout"
    loadout_dir.mkdir(parents=True, exist_ok=True)
    (loadout_dir / "config.yaml").write_text(yaml_text, encoding="utf-8")


def test_default_mapping_covers_seed_roles() -> None:
    for role in ("builder", "reviewer", "merger", "lead"):
        assert role in DEFAULT_ROLE_VERBS
        assert DEFAULT_ROLE_VERBS[role], f"{role} must declare at least one verb"
        for verb in DEFAULT_ROLE_VERBS[role]:
            assert verb in KNOWN_VERBS


def test_no_repo_root_returns_default_mapping() -> None:
    assert load_role_verbs(None) == dict(DEFAULT_ROLE_VERBS)


def test_no_config_file_returns_default_mapping(tmp_path: Path) -> None:
    assert load_role_verbs(tmp_path) == dict(DEFAULT_ROLE_VERBS)


def test_repo_config_with_synthetic_roles_replaces_default_entirely(tmp_path: Path) -> None:
    """CLAUDE.md rule 6a conformance: synthetic role names, no lore, no
    dependency on the seed role taxonomy being present at all."""
    _write_config(
        tmp_path,
        """
roles:
  zorbnaut:
    - push
  flibbertigibbet:
    - git-host-api
    - merge
""",
    )
    resolved = load_role_verbs(tmp_path)
    assert resolved == {
        "zorbnaut": ("push",),
        "flibbertigibbet": ("git-host-api", "merge"),
    }
    # The seed roles are GONE, not merged in -- a repo's own declaration is
    # authoritative, never silently supplemented by the built-in default.
    assert "builder" not in resolved


def test_resolve_role_verbs_success(tmp_path: Path) -> None:
    _write_config(tmp_path, "roles:\n  widget-runner:\n    - poll-wait\n")
    assert resolve_role_verbs("widget-runner", repo_root=tmp_path) == ("poll-wait",)


def test_resolve_unknown_role_reports_resolved_values(tmp_path: Path) -> None:
    _write_config(tmp_path, "roles:\n  known-role:\n    - push\n")
    with pytest.raises(InvalidRoleConfigError) as exc_info:
        resolve_role_verbs("nonexistent-role", repo_root=tmp_path)
    msg = str(exc_info.value)
    assert "nonexistent-role" in msg
    assert "known-role" in msg
    assert str(tmp_path / ".clagentic" / "loadout" / "config.yaml") in msg


def test_unknown_verb_in_config_raises_with_resolved_values(tmp_path: Path) -> None:
    _write_config(tmp_path, "roles:\n  some-role:\n    - not-a-real-verb\n")
    with pytest.raises(InvalidRoleConfigError) as exc_info:
        load_role_verbs(tmp_path)
    msg = str(exc_info.value)
    assert "not-a-real-verb" in msg
    assert "some-role" in msg
    for verb in KNOWN_VERBS:
        assert verb in msg  # known-good set is reported, not a stale guess


def test_empty_roles_section_raises(tmp_path: Path) -> None:
    _write_config(tmp_path, "roles: {}\n")
    with pytest.raises(InvalidRoleConfigError):
        load_role_verbs(tmp_path)


def test_role_with_empty_verb_list_raises(tmp_path: Path) -> None:
    _write_config(tmp_path, "roles:\n  empty-role:\n    []\n")
    with pytest.raises(InvalidRoleConfigError):
        load_role_verbs(tmp_path)


def test_role_with_non_list_verbs_raises(tmp_path: Path) -> None:
    _write_config(tmp_path, "roles:\n  bad-role: push\n")
    with pytest.raises(InvalidRoleConfigError):
        load_role_verbs(tmp_path)


def test_malformed_yaml_raises(tmp_path: Path) -> None:
    _write_config(tmp_path, "roles: [this is not, a mapping\n")
    with pytest.raises(InvalidRoleConfigError):
        load_role_verbs(tmp_path)


def test_non_mapping_top_level_document_raises(tmp_path: Path) -> None:
    _write_config(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(InvalidRoleConfigError):
        load_role_verbs(tmp_path)


def test_invalid_role_name_token_raises(tmp_path: Path) -> None:
    _write_config(tmp_path, "roles:\n  \"bad role with spaces\":\n    - push\n")
    with pytest.raises(InvalidRoleConfigError):
        load_role_verbs(tmp_path)


def test_legacy_path_is_read_when_new_path_absent(tmp_path: Path, capsys) -> None:
    """Transitional back-compat (lr-446c35): a repo that has not yet
    migrated off .loadout/config.yaml is still read, with a one-line
    deprecation warning to stderr. Removed after the fleet migration
    (lr-a645aa)."""
    _write_legacy_config(tmp_path, "roles:\n  legacy-role:\n    - push\n")

    resolved = load_role_verbs(tmp_path)

    assert resolved == {"legacy-role": ("push",)}
    stderr = capsys.readouterr().err
    assert "deprecated" in stderr
    assert stderr.count("\n") == 1


def test_new_path_wins_when_both_present(tmp_path: Path, capsys) -> None:
    _write_legacy_config(tmp_path, "roles:\n  legacy-role:\n    - push\n")
    _write_config(tmp_path, "roles:\n  new-role:\n    - merge\n")

    resolved = load_role_verbs(tmp_path)

    assert resolved == {"new-role": ("merge",)}
    assert capsys.readouterr().err == ""
