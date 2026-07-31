"""test_provisioning_cli.py — loadout-provision-allowlist verb coverage
(lr-4e04).

Covers --help/--version/exit codes (CLAUDE.md rule 4's CLI hygiene
contract — also exercised via test_cli_conformance.py's parametrized
suite, which this verb is now registered in), the print-default vs.
--write opt-in split, and end-to-end role -> fragment -> settings-file
flow using synthetic role/repo names (CLAUDE.md rule 6a conformance gate).
"""

from __future__ import annotations

import json
from pathlib import Path

from clagentic_loadout.provisioning import cli as provisioning_cli
from clagentic_loadout.provisioning.settings_path import SETTINGS_FILE_ENV_VAR


def _write_config(repo_root: Path, yaml_text: str) -> None:
    loadout_dir = repo_root / ".clagentic" / "loadout"
    loadout_dir.mkdir(parents=True, exist_ok=True)
    (loadout_dir / "config.yaml").write_text(yaml_text, encoding="utf-8")


def test_missing_role_flag_is_usage_error(capsys) -> None:
    rc = provisioning_cli.main([])
    assert rc == provisioning_cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "--role" in err


def test_default_behavior_prints_fragment_for_builtin_role(capsys) -> None:
    rc = provisioning_cli.main(["--role", "builder"])
    assert rc == provisioning_cli.EXIT_OK
    out = capsys.readouterr().out
    fragment = json.loads(out)
    assert "Bash(loadout-push:*)" in fragment
    assert "Bash(loadout-push *)" in fragment


def test_unknown_role_with_no_repo_root_reports_resolved_values(capsys) -> None:
    rc = provisioning_cli.main(["--role", "not-a-real-role"])
    assert rc == provisioning_cli.EXIT_ROLE_CONFIG_INVALID
    err = capsys.readouterr().err
    assert "not-a-real-role" in err
    assert "builder" in err  # known-good roles reported, not a stale guess


def test_synthetic_role_from_repo_root_config(tmp_path: Path, capsys) -> None:
    """Conformance (rule 6a): a repo-local config declaring an entirely
    invented role name, resolved with no lore present and no dependency on
    the seed role taxonomy."""
    _write_config(tmp_path, "roles:\n  zorbnaut:\n    - git-host-api\n")
    rc = provisioning_cli.main(["--role", "zorbnaut", "--repo-root", str(tmp_path)])
    assert rc == provisioning_cli.EXIT_OK
    fragment = json.loads(capsys.readouterr().out)
    assert fragment == sorted(["Bash(loadout-git-host-api:*)", "Bash(loadout-git-host-api *)"])


def test_write_flag_merges_into_settings_file(tmp_path: Path, capsys) -> None:
    settings_file = tmp_path / "settings.json"
    rc = provisioning_cli.main(
        ["--role", "builder", "--write", "--settings-file", str(settings_file)]
    )
    assert rc == provisioning_cli.EXIT_OK
    doc = json.loads(settings_file.read_text(encoding="utf-8"))
    assert "Bash(loadout-push:*)" in doc["permissions"]["allow"]
    # Default (no --write) path never touched the filesystem for this
    # settings_file path at all -- prove the print-only call above made no
    # file.
    other_settings_file = tmp_path / "print-only-settings.json"
    provisioning_cli.main(["--role", "builder", "--settings-file", str(other_settings_file)])
    assert not other_settings_file.exists()


def test_write_flag_is_idempotent_across_two_roles(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    provisioning_cli.main(["--role", "builder", "--write", "--settings-file", str(settings_file)])
    provisioning_cli.main(["--role", "merger", "--write", "--settings-file", str(settings_file)])
    doc = json.loads(settings_file.read_text(encoding="utf-8"))
    allow = doc["permissions"]["allow"]
    assert "Bash(loadout-push:*)" in allow
    assert "Bash(loadout-merge:*)" in allow
    assert len(allow) == len(set(allow))


def test_settings_file_env_var_used_when_no_explicit_flag(tmp_path: Path, monkeypatch) -> None:
    settings_file = tmp_path / "env-settings.json"
    monkeypatch.setenv(SETTINGS_FILE_ENV_VAR, str(settings_file))
    rc = provisioning_cli.main(["--role", "builder", "--write"])
    assert rc == provisioning_cli.EXIT_OK
    assert settings_file.exists()


def test_empty_home_no_override_write_fails_closed(monkeypatch, capsys) -> None:
    monkeypatch.delenv(SETTINGS_FILE_ENV_VAR, raising=False)
    monkeypatch.setenv("HOME", "")
    rc = provisioning_cli.main(["--role", "builder", "--write"])
    assert rc == provisioning_cli.EXIT_SETTINGS_PATH_INVALID
    err = capsys.readouterr().err
    assert "HOME" in err
