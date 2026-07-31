"""test_doctor_cli.py — loadout-doctor verb coverage (lr-e625).

Covers --help/--version/exit codes (CLAUDE.md rule 4's CLI hygiene contract
-- also exercised via test_cli_conformance.py's parametrized suite, which
this verb is now registered in) and the aggregate exit-code mapping from
per-check results.
"""

from __future__ import annotations

import yaml

from clagentic_loadout.doctor import cli as doctor_cli
from clagentic_loadout.transport import provider_config
from clagentic_loadout.transport.github_app_config import (
    CONFIG_KEY_SLUGS,
    CONFIG_SECTION_GITHUB_APP,
    USER_CONFIG_FILENAME as GITHUB_APP_CONFIG_FILENAME,
)


def _write_github_app_config(config_root, mapping: dict) -> None:
    config_root.mkdir(parents=True, exist_ok=True)
    (config_root / GITHUB_APP_CONFIG_FILENAME).write_text(yaml.safe_dump(mapping))


def _write_loadout_config(repo_root, yaml_text: str) -> None:
    loadout_dir = repo_root / ".loadout"
    loadout_dir.mkdir(parents=True, exist_ok=True)
    (loadout_dir / "config.yaml").write_text(yaml_text, encoding="utf-8")


def test_help_exits_ok(capsys):
    rc = doctor_cli.main(["--help"])
    assert rc == doctor_cli.EXIT_OK
    assert capsys.readouterr().out.strip()


def test_version_exits_ok_and_prints_package_version(capsys):
    from clagentic_loadout._version import get_version

    rc = doctor_cli.main(["--version"])
    assert rc == doctor_cli.EXIT_OK
    assert get_version() in capsys.readouterr().out


def test_default_run_with_no_config_passes(tmp_path, monkeypatch, capsys):
    """Conformance (rule 6a): no lore, a synthetic/empty config root, no
    network -- default static providers and no slugs map means every check
    passes."""
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    rc = doctor_cli.main(["--config-root", str(tmp_path / "user-config")])
    assert rc == doctor_cli.EXIT_OK
    out = capsys.readouterr().out
    assert "credentials:forgejo" in out
    assert "credentials:github" in out
    assert "github_app_slugs_coverage" in out


def test_missing_slug_coverage_fails_and_reports_reserved_exit_code(tmp_path, monkeypatch, capsys):
    config_root = tmp_path / "user-config"
    _write_github_app_config(
        config_root,
        {CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_SLUGS: {"builder": "app-builder"}}},
    )
    repo_root = tmp_path / "repo"
    _write_loadout_config(
        repo_root, "roles:\n  builder:\n    - push\n  reviewer:\n    - git-host-api\n"
    )
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", config_root)

    rc = doctor_cli.main(["--config-root", str(config_root), "--repo-root", str(repo_root)])
    assert rc == doctor_cli.EXIT_CHECKS_FAILED
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "reviewer" in out


def test_repo_root_opts_into_schema_check(tmp_path, monkeypatch, capsys):
    config_root = tmp_path / "user-config"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", config_root)
    repo_root = tmp_path / "repo"
    _write_loadout_config(repo_root, "roles:\n  builder:\n    - push\n")

    rc = doctor_cli.main(["--config-root", str(config_root), "--repo-root", str(repo_root)])
    assert rc == doctor_cli.EXIT_OK
    out = capsys.readouterr().out
    assert "repo_loadout_schema" in out


def test_no_repo_root_skips_schema_check(tmp_path, monkeypatch, capsys):
    config_root = tmp_path / "user-config"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", config_root)

    rc = doctor_cli.main(["--config-root", str(config_root)])
    assert rc == doctor_cli.EXIT_OK
    out = capsys.readouterr().out
    assert "repo_loadout_schema" not in out


def test_attestation_source_check_appears_in_default_run(tmp_path, monkeypatch, capsys):
    """check_attestation_source_configured (lr-8e1593) runs as part of the
    default check set, alongside the pre-existing five."""
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    rc = doctor_cli.main(["--config-root", str(tmp_path / "user-config")])
    assert rc == doctor_cli.EXIT_OK
    out = capsys.readouterr().out
    assert "attestation_source_configured" in out


def test_summary_line_reports_pass_count_on_stderr(tmp_path, monkeypatch, capsys):
    config_root = tmp_path / "user-config"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", config_root)

    rc = doctor_cli.main(["--config-root", str(config_root)])
    assert rc == doctor_cli.EXIT_OK
    err = capsys.readouterr().err
    assert "checks passed" in err
