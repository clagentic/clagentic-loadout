"""test_provisioning_settings_path.py — parameterized settings-file location
and empty-HOME fail-fast (lr-4e04).

Mirrors scripts/install.sh's lr-e8cc discipline (see test_install_script.py
for the shell-side equivalent) on the Python side of the provisioning
contract: --settings-file flag > CLAGENTIC_LOADOUT_SETTINGS_FILE env var >
HOME-derived default, with a refusal (never a silent root-relative path)
when HOME is empty/unset and nothing compensates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clagentic_loadout.provisioning.settings_path import (
    DEFAULT_SETTINGS_FILE_RELATIVE,
    SETTINGS_FILE_ENV_VAR,
    SettingsPathError,
    resolve_settings_path,
)


def test_explicit_flag_wins_over_everything() -> None:
    resolved = resolve_settings_path(
        "/explicit/path.json",
        env={SETTINGS_FILE_ENV_VAR: "/env/path.json", "HOME": "/home/someone"},
    )
    assert resolved == Path("/explicit/path.json")


def test_env_var_wins_over_home_default() -> None:
    resolved = resolve_settings_path(None, env={SETTINGS_FILE_ENV_VAR: "/env/path.json", "HOME": "/home/someone"})
    assert resolved == Path("/env/path.json")


def test_home_derived_default_when_nothing_else_given() -> None:
    resolved = resolve_settings_path(None, env={"HOME": "/home/someone"})
    assert resolved == Path("/home/someone") / DEFAULT_SETTINGS_FILE_RELATIVE


def test_empty_home_with_no_override_raises() -> None:
    with pytest.raises(SettingsPathError) as exc_info:
        resolve_settings_path(None, env={"HOME": ""})
    msg = str(exc_info.value)
    assert "HOME" in msg
    assert SETTINGS_FILE_ENV_VAR in msg


def test_unset_home_with_no_override_raises() -> None:
    with pytest.raises(SettingsPathError):
        resolve_settings_path(None, env={})


def test_empty_home_compensated_by_explicit_flag_does_not_raise() -> None:
    resolved = resolve_settings_path("/explicit/path.json", env={"HOME": ""})
    assert resolved == Path("/explicit/path.json")


def test_empty_home_compensated_by_env_var_does_not_raise() -> None:
    resolved = resolve_settings_path(None, env={SETTINGS_FILE_ENV_VAR: "/env/path.json", "HOME": ""})
    assert resolved == Path("/env/path.json")
