"""test_review_verdict_required_roles.py — unit tests for
clagentic_loadout.review.verdict_required_roles (lr-9ba589 fold-in #1).

Coverage:
  - load_verdict_required_roles: absent key -> empty frozenset (unconfigured,
    non-breaking default); present list -> parsed frozenset; malformed value
    (non-list, list with a non-string/empty entry, explicit null) raises
    InvalidVerdictRequiredRolesConfigError -- never silently permissive.
  - check_verdict_required_role: no-op when the caller is not in the
    configured list, or when the list is empty/absent; no-op when either
    verdict flag is supplied; raises VerdictRequiredRoleRefusedError when the
    caller IS listed and neither flag is supplied.

Integration coverage (both body-ingestion routes, end-to-end through
review.verb.main, staged-pair preservation) lives in test_review_verb.py's
TestVerdictRequiredRoleRefusesFenceless -- this file is the primitive's own
unit-level contract, mirroring test_transport_read_host_guard.py's
relationship to transport.git_host_api's own integration coverage.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.transport import provider_config
from clagentic_loadout.review.verdict_required_roles import (
    CONFIG_KEY,
    CONFIG_SECTION,
    InvalidVerdictRequiredRolesConfigError,
    VerdictRequiredRoleRefusedError,
    check_verdict_required_role,
    load_verdict_required_roles,
)


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    """Same belt-and-suspenders isolation as
    test_transport_read_host_guard.py's own fixture: a real deployment
    config.yaml on the host running these tests must never leak a live
    verdict_required_roles list into a test asserting the unconfigured
    default."""
    isolated_root = tmp_path / "isolated-user-config-root"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", isolated_root)


def _write_config(config_root, *, raw_yaml_value: str) -> None:
    config_root.mkdir(parents=True, exist_ok=True)
    config_path = config_root / provider_config.USER_CONFIG_FILENAME
    config_path.write_text(
        f"{CONFIG_SECTION}:\n  {CONFIG_KEY}: {raw_yaml_value}\n", encoding="utf-8"
    )


class TestLoadVerdictRequiredRoles:
    def test_absent_key_is_empty_frozenset(self, tmp_path):
        config_root = tmp_path / "config-root"
        result = load_verdict_required_roles(config_root)
        assert result == frozenset()

    def test_absent_config_file_is_empty_frozenset(self, tmp_path):
        config_root = tmp_path / "does-not-exist"
        result = load_verdict_required_roles(config_root)
        assert result == frozenset()

    def test_present_list_is_parsed(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_config(config_root, raw_yaml_value='["reviewer", "security"]')
        result = load_verdict_required_roles(config_root)
        assert result == frozenset({"reviewer", "security"})

    def test_explicit_empty_list_is_empty_frozenset(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_config(config_root, raw_yaml_value="[]")
        result = load_verdict_required_roles(config_root)
        assert result == frozenset()

    def test_entries_are_stripped(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_config(config_root, raw_yaml_value='[" reviewer "]')
        result = load_verdict_required_roles(config_root)
        assert result == frozenset({"reviewer"})

    def test_non_list_value_raises_not_permissive(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_config(config_root, raw_yaml_value="42")
        with pytest.raises(InvalidVerdictRequiredRolesConfigError) as exc_info:
            load_verdict_required_roles(config_root)
        msg = str(exc_info.value)
        assert str(config_root / provider_config.USER_CONFIG_FILENAME) in msg
        assert CONFIG_SECTION in msg
        assert CONFIG_KEY in msg
        assert "int" in msg

    def test_explicit_null_raises_not_absent(self, tmp_path):
        """A PRESENT `verdict_required_roles: null` must never be treated
        the same as a genuinely absent key -- mirrors
        transport.read_host_guard's ABSENT-VS-PRESENT-NULL fix."""
        config_root = tmp_path / "config-root"
        _write_config(config_root, raw_yaml_value="null")
        with pytest.raises(InvalidVerdictRequiredRolesConfigError):
            load_verdict_required_roles(config_root)

    def test_list_with_non_string_entry_raises(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_config(config_root, raw_yaml_value='["reviewer", 5]')
        with pytest.raises(InvalidVerdictRequiredRolesConfigError) as exc_info:
            load_verdict_required_roles(config_root)
        assert "5" in str(exc_info.value)

    def test_list_with_empty_string_entry_raises(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_config(config_root, raw_yaml_value='["reviewer", ""]')
        with pytest.raises(InvalidVerdictRequiredRolesConfigError):
            load_verdict_required_roles(config_root)

    def test_mapping_value_raises(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_config(config_root, raw_yaml_value="{nested: true}")
        with pytest.raises(InvalidVerdictRequiredRolesConfigError) as exc_info:
            load_verdict_required_roles(config_root)
        assert "dict" in str(exc_info.value)


class TestCheckVerdictRequiredRole:
    def test_unconfigured_is_a_noop(self, tmp_path):
        config_root = tmp_path / "config-root"
        check_verdict_required_role(
            "reviewer",
            verdict_review_status=None,
            verdict_findings=False,
            config_root=config_root,
        )

    def test_caller_not_in_list_is_a_noop(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_config(config_root, raw_yaml_value='["security"]')
        check_verdict_required_role(
            "reviewer",
            verdict_review_status=None,
            verdict_findings=False,
            config_root=config_root,
        )

    def test_listed_caller_with_review_status_flag_is_a_noop(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_config(config_root, raw_yaml_value='["reviewer"]')
        check_verdict_required_role(
            "reviewer",
            verdict_review_status="clean",
            verdict_findings=False,
            config_root=config_root,
        )

    def test_listed_caller_with_findings_flag_is_a_noop(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_config(config_root, raw_yaml_value='["reviewer"]')
        check_verdict_required_role(
            "reviewer",
            verdict_review_status=None,
            verdict_findings=True,
            config_root=config_root,
        )

    def test_listed_caller_with_neither_flag_refuses(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_config(config_root, raw_yaml_value='["reviewer"]')
        with pytest.raises(VerdictRequiredRoleRefusedError) as exc_info:
            check_verdict_required_role(
                "reviewer",
                verdict_review_status=None,
                verdict_findings=False,
                config_root=config_root,
            )
        msg = str(exc_info.value)
        assert "reviewer" in msg
        assert "--verdict-review-status" in msg
        assert "--verdict-findings" in msg

    def test_malformed_config_propagates_not_swallowed(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_config(config_root, raw_yaml_value="42")
        with pytest.raises(InvalidVerdictRequiredRolesConfigError):
            check_verdict_required_role(
                "reviewer",
                verdict_review_status=None,
                verdict_findings=False,
                config_root=config_root,
            )
