"""test_transport_host_guard_resolve.py — tests for
clagentic_loadout.transport.host_guard_resolve (lr-57573e).

This module's own precedence/parsing/fail-closed-config contract is
exercised exhaustively, end to end, through its two current callers'
own test suites (tests/test_transport_read_host_guard.py,
tests/test_push_host_guard.py) -- this file locks the SHARED module's own
public surface directly, independent of either guard's own vocabulary, so a
future third caller (or a refactor of either existing caller) cannot
silently change the shared algorithm without a dedicated test noticing."""

from __future__ import annotations

import pytest

from clagentic_loadout.transport import host_guard_resolve, provider_config


class _FakeInvalidConfigError(Exception):
    def __init__(self, config_path, *, received, detail=None):
        message = f"{config_path}: bad value {received!r}"
        if detail:
            message += f" ({detail})"
        super().__init__(message)
        self.config_path = config_path
        self.received = received


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    isolated_root = tmp_path / "isolated-user-config-root"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", isolated_root)


def _write_config(config_root, *, section: str, key: str, value: str) -> None:
    config_root.mkdir(parents=True, exist_ok=True)
    config_path = config_root / provider_config.USER_CONFIG_FILENAME
    config_path.write_text(f"{section}:\n  {key}: \"{value}\"\n", encoding="utf-8")


class TestParseCommaSeparated:
    def test_empty_string_returns_empty_frozenset(self):
        assert host_guard_resolve.parse_comma_separated("") == frozenset()

    def test_whitespace_only_returns_empty_frozenset(self):
        assert host_guard_resolve.parse_comma_separated("   ") == frozenset()

    def test_trims_and_drops_empty_entries(self):
        result = host_guard_resolve.parse_comma_separated(" a.example.com , ,b.example.com ")
        assert result == frozenset({"a.example.com", "b.example.com"})


class TestResolveCeilingHosts:
    def test_config_unset_explicit_wins_over_env(self, tmp_path):
        result = host_guard_resolve.resolve_ceiling_hosts(
            frozenset({"explicit.example.com"}),
            env={"SOME_ENV": "env.example.com"},
            env_var="SOME_ENV",
            config_root=tmp_path / "root",
            config_section="a_guard",
            config_key="allowed_hosts",
            invalid_config_error=_FakeInvalidConfigError,
        )
        assert result == frozenset({"explicit.example.com"})

    def test_config_unset_no_caller_value_is_none(self, tmp_path):
        result = host_guard_resolve.resolve_ceiling_hosts(
            None,
            env={},
            env_var="SOME_ENV",
            config_root=tmp_path / "root",
            config_section="a_guard",
            config_key="allowed_hosts",
            invalid_config_error=_FakeInvalidConfigError,
        )
        assert result is None

    def test_config_set_narrows_caller_value(self, tmp_path):
        config_root = tmp_path / "root"
        _write_config(
            config_root, section="a_guard", key="allowed_hosts",
            value="good.example.com:3000,other.example.com:3000",
        )
        result = host_guard_resolve.resolve_ceiling_hosts(
            frozenset({"good.example.com:3000", "attacker.example.net"}),
            env={},
            env_var="SOME_ENV",
            config_root=config_root,
            config_section="a_guard",
            config_key="allowed_hosts",
            invalid_config_error=_FakeInvalidConfigError,
        )
        assert result == frozenset({"good.example.com:3000"})

    def test_config_set_no_caller_value_is_full_ceiling(self, tmp_path):
        config_root = tmp_path / "root"
        _write_config(
            config_root, section="a_guard", key="allowed_hosts", value="good.example.com:3000",
        )
        result = host_guard_resolve.resolve_ceiling_hosts(
            None,
            env={},
            env_var="SOME_ENV",
            config_root=config_root,
            config_section="a_guard",
            config_key="allowed_hosts",
            invalid_config_error=_FakeInvalidConfigError,
        )
        assert result == frozenset({"good.example.com:3000"})

    def test_malformed_config_raises_the_supplied_error_type(self, tmp_path):
        config_root = tmp_path / "root"
        config_root.mkdir(parents=True, exist_ok=True)
        (config_root / provider_config.USER_CONFIG_FILENAME).write_text(
            "a_guard:\n  allowed_hosts: 42\n", encoding="utf-8",
        )
        with pytest.raises(_FakeInvalidConfigError):
            host_guard_resolve.resolve_ceiling_hosts(
                None,
                env={},
                env_var="SOME_ENV",
                config_root=config_root,
                config_section="a_guard",
                config_key="allowed_hosts",
                invalid_config_error=_FakeInvalidConfigError,
            )


class TestConfigIsSet:
    def test_false_when_key_absent(self, tmp_path):
        config_root = tmp_path / "root"
        assert (
            host_guard_resolve.config_is_set(
                config_root=config_root,
                config_section="a_guard",
                config_key="allowed_hosts",
                invalid_config_error=_FakeInvalidConfigError,
            )
            is False
        )

    def test_true_when_key_present(self, tmp_path):
        config_root = tmp_path / "root"
        _write_config(config_root, section="a_guard", key="allowed_hosts", value="")
        assert (
            host_guard_resolve.config_is_set(
                config_root=config_root,
                config_section="a_guard",
                config_key="allowed_hosts",
                invalid_config_error=_FakeInvalidConfigError,
            )
            is True
        )
