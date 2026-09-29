"""test_push_host_guard.py — tests for clagentic_loadout.push.host_guard
(lr-0e39f9; WIDEN/NARROW + empty-set-collapse fix lr-57573e, mirroring PR
#33's read-guard design).

Coverage:
  - resolve_allowed_hosts precedence: explicit > env var > None (permissive)
    default -- mirrors test_transport_read_host_guard.py's own coverage
    shape for the sibling guard (lr-57573e: the permissive default is now
    None, not an empty frozenset -- see RETURN-TYPE, below).
  - check_host_allowed: permissive when no allowlist configured (None);
    DENY when allowed_hosts is an empty frozenset (a real, configured
    "restrict to nothing," not permissive -- the empty-set-collapse defect
    this task fixes); deny when configured and the derived api_base host
    does not match any configured entry; allow when it does -- both a bare
    "host[:port]" allowlist entry and a full "scheme://host[:port]" entry
    are accepted (via transport.host_match.host_matches).
  - No operator hostname baked in anywhere -- the allowed set is entirely
    caller/config supplied.
  - TestConfigWidenGuard (lr-57573e): the caller-widening fix -- explicit/
    env can never ADD a host absent from the operator-configured
    PUSH_HOST_CONFIG_SECTION allowlist; narrowing via intersection works;
    the config-only path (neither explicit nor env supplied) works; and
    config UNSET reproduces the pre-fix, caller-settable-only precedence
    byte-for-byte (back-compat).
  - TestConfigListForm (lr-57573e): a YAML list of strings is accepted
    alongside the comma-separated string; a malformed value (int, mapping,
    list containing a non-string) raises InvalidPushHostConfigError rather
    than silently degrading to permissive -- and no test in this class
    ever reaches a token mint (resolve_allowed_hosts/push_host_config_is_set
    raise BEFORE check_host_allowed is even called).
  - TestCorrectiveMessageMode (lr-57573e): the HostDeniedError message
    names the env var/flag when config is UNSET, and names the
    push_host_guard.allowed_hosts config key (never the env var/flag) when
    config IS SET.
  - TestAbsentVsPresentNullConfig (lr-57573e): an explicit
    `allowed_hosts: null` (or `allowed_hosts:` with no value) in the config
    file must NOT be treated as "unconfigured" -- both raise
    InvalidPushHostConfigError, mint-free, distinct from a genuinely ABSENT
    key (which stays permissive, config UNSET).
  - TestResolveHostCeiling (lr-57573e fold-in #1, F2): resolve_host_ceiling
    -- the single-read replacement for the resolve_allowed_hosts +
    push_host_config_is_set two-call shape push.verb previously used --
    agrees with the two separate calls across every mode, and its
    HostCeilingResolution result feeds check_host_allowed's two parameters
    directly.
  - TestSharedUserConfigRoot (lr-57573e fold-in #1, F1, HOLDEN-adjudicated):
    push.host_guard's config-file tier and provider_config's `credentials:`
    tier resolve from the SAME user-level config root by construction --
    proof that a redirected HOME/XDG_CONFIG_HOME cannot widen this ceiling
    without also redirecting (and therefore losing) the operator's real
    credential root, mirroring test_transport_read_host_guard.py::
    TestSharedUserConfigRoot exactly.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.push.errors import HostDeniedError, InvalidPushHostConfigError
from clagentic_loadout.push.host_guard import (
    ALLOWED_HOSTS_ENV_VAR,
    PUSH_HOST_CONFIG_KEY,
    PUSH_HOST_CONFIG_SECTION,
    check_host_allowed,
    push_host_config_is_set,
    resolve_allowed_hosts,
    resolve_host_ceiling,
)
from clagentic_loadout.transport import provider_config
from clagentic_loadout.transport.host_guard_resolve import HostCeilingResolution


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    """Belt-and-suspenders isolation, mirroring
    test_transport_read_host_guard.py's own `_isolate_user_config_root`
    fixture: resolve_allowed_hosts's config-file tier
    (PUSH_HOST_CONFIG_SECTION) reads through
    transport.provider_config.load_user_config_section, which falls back to
    provider_config.DEFAULT_USER_CONFIG_ROOT -- the REAL
    ~/.config/clagentic/loadout/ directory -- for any call that omits
    config_root. A real deployment config.yaml on the host running these
    tests must never leak a live allowlist into a test asserting the
    config-UNSET (back-compat) precedence."""
    isolated_root = tmp_path / "isolated-user-config-root"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", isolated_root)


def _write_config(config_root, *, section: str, key: str, value: str) -> None:
    config_root.mkdir(parents=True, exist_ok=True)
    config_path = config_root / provider_config.USER_CONFIG_FILENAME
    config_path.write_text(f"{section}:\n  {key}: \"{value}\"\n", encoding="utf-8")


def _write_raw_config(config_root, *, section: str, key: str, raw_yaml_value: str) -> None:
    config_root.mkdir(parents=True, exist_ok=True)
    config_path = config_root / provider_config.USER_CONFIG_FILENAME
    config_path.write_text(f"{section}:\n  {key}: {raw_yaml_value}\n", encoding="utf-8")


def test_config_section_is_not_shared_with_read_host_guard():
    from clagentic_loadout.transport.read_host_guard import (
        READ_HOST_CONFIG_SECTION,
    )

    assert PUSH_HOST_CONFIG_SECTION != READ_HOST_CONFIG_SECTION
    assert PUSH_HOST_CONFIG_SECTION == "push_host_guard"


class TestResolveAllowedHosts:
    def test_explicit_wins_over_env(self):
        result = resolve_allowed_hosts(
            frozenset({"https://explicit-host.example.com"}),
            env={ALLOWED_HOSTS_ENV_VAR: "https://env-host.example.com"},
        )
        assert result == frozenset({"https://explicit-host.example.com"})

    def test_explicit_empty_set_is_honored_not_reinterpreted(self):
        result = resolve_allowed_hosts(frozenset(), env={})
        assert result == frozenset()

    def test_env_var_parsed_comma_separated_trimmed(self):
        result = resolve_allowed_hosts(
            None,
            env={
                ALLOWED_HOSTS_ENV_VAR: (
                    " https://git-host-a.example.com, https://git-host-b.example.com "
                    ",https://git-host-c.example.com"
                )
            },
        )
        assert result == frozenset(
            {
                "https://git-host-a.example.com",
                "https://git-host-b.example.com",
                "https://git-host-c.example.com",
            }
        )

    def test_env_var_empty_entries_dropped(self):
        result = resolve_allowed_hosts(
            None,
            env={
                ALLOWED_HOSTS_ENV_VAR: (
                    "https://git-host-a.example.com,,https://git-host-b.example.com"
                )
            },
        )
        assert result == frozenset(
            {"https://git-host-a.example.com", "https://git-host-b.example.com"}
        )

    def test_no_explicit_no_env_is_none_permissive_default(self):
        # lr-57573e RETURN-TYPE fix: None (not an empty frozenset) is the
        # permissive-default sentinel -- collapsing this to frozenset()
        # would be indistinguishable from a configured-but-narrowed-to-
        # nothing DENY-everything outcome once the config tier is in play
        # (see TestConfigWidenGuard).
        result = resolve_allowed_hosts(None, env={})
        assert result is None

    def test_env_var_whitespace_only_treated_as_unset(self):
        result = resolve_allowed_hosts(None, env={ALLOWED_HOSTS_ENV_VAR: "   "})
        assert result is None


class TestCheckHostAllowed:
    def test_permissive_when_no_allowlist_configured(self):
        # None (nothing configured anywhere) -- proves there is no baked
        # allow-list of any particular operator hostname; permissiveness is
        # a property of allowed_hosts=None, not of any specific host value.
        check_host_allowed(
            "https://totally-synthetic-host.example.com", allowed_hosts=None
        )

    def test_denied_when_allowed_hosts_is_an_empty_frozenset(self):
        # lr-57573e EMPTY-SET-COLLAPSE fix: an EMPTY frozenset is NOT the
        # permissive case -- it means a restriction IS configured (e.g. an
        # operator's explicit "restrict to nothing", or a caller value
        # narrowed to zero overlap with the config ceiling) and must deny
        # unconditionally. Only None means "no restriction configured."
        with pytest.raises(HostDeniedError):
            check_host_allowed(
                "https://totally-synthetic-host.example.com", allowed_hosts=frozenset()
            )

    def test_denied_when_configured_and_host_absent(self):
        with pytest.raises(HostDeniedError) as exc_info:
            check_host_allowed(
                "https://attacker.example.net",
                allowed_hosts=frozenset({"https://git-host.example.com"}),
            )
        assert "attacker.example.net" in str(exc_info.value)

    def test_allowed_when_configured_and_host_present(self):
        check_host_allowed(
            "https://git-host.example.com",
            allowed_hosts=frozenset(
                {"https://git-host.example.com", "https://other-host.example.com"}
            ),
        )

    def test_bare_authority_allowlist_entry_matches_full_url_api_base(self):
        # An allowlist entry supplied as a bare host[:port] (no scheme) still
        # matches an api_base that IS a full URL -- transport.host_match.
        # host_matches handles both shapes on either side of the comparison.
        check_host_allowed(
            "http://git-host.example.com:3000",
            allowed_hosts=frozenset({"git-host.example.com:3000"}),
        )

    def test_same_host_different_port_is_denied(self):
        # A same-hostname-different-port pair must NEVER be treated as a
        # match -- a reverse-proxy misconfiguration or copy-paste typo is a
        # real, distinct host as far as where the bearer token gets sent.
        with pytest.raises(HostDeniedError):
            check_host_allowed(
                "http://git-host.example.com:9999",
                allowed_hosts=frozenset({"http://git-host.example.com:3000"}),
            )

    def test_case_insensitive_host_match(self):
        check_host_allowed(
            "http://GIT-HOST.EXAMPLE.COM:3000",
            allowed_hosts=frozenset({"http://git-host.example.com:3000"}),
        )

    def test_deny_message_never_reveals_a_token_or_credential(self):
        # Sanity: the guard's own message construction never touches a
        # secret value -- there is none in scope here, but this locks the
        # message shape to static labels + caller-supplied api_base/allowlist
        # only.
        with pytest.raises(HostDeniedError) as exc_info:
            check_host_allowed(
                "https://attacker.example.net",
                allowed_hosts=frozenset({"https://git-host.example.com"}),
            )
        msg = str(exc_info.value)
        assert "attacker.example.net" in msg
        assert ALLOWED_HOSTS_ENV_VAR in msg


class TestConfigWidenGuard:
    """lr-57573e: a caller who controls the live git remote (and therefore
    api_base) also controls --allowed-host / CLAGENTIC_LOADOUT_PUSH_ALLOWED_HOSTS
    in the SAME invocation, so those two caller-settable sources could never
    actually protect against that same caller -- only an operator-
    controlled, non-per-call source (the user-level config file) can WIDEN
    the effective allowlist. Mirrors
    test_transport_read_host_guard.py::TestConfigWidenGuard exactly, for
    push's own config tier."""

    def test_flag_cannot_add_a_host_absent_from_config(self, tmp_path):
        """The core fix: --allowed-host (explicit) naming a host NOT present
        in the configured allowlist must NOT appear in the resolved set --
        the config tier is a ceiling, not a co-equal source, once it is
        set."""
        config_root = tmp_path / "config-root"
        _write_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            value="https://forgejo.example.com:3000",
        )
        result = resolve_allowed_hosts(
            frozenset({"https://attacker.example.net"}),
            env={},
            config_root=config_root,
        )
        assert result == frozenset()
        # The caller's own attacker host is refused by check_host_allowed
        # against this empty resolved set -- a token provider consuming
        # this result never even gets a chance to mint against it.
        with pytest.raises(HostDeniedError):
            check_host_allowed("https://attacker.example.net", allowed_hosts=result)

    def test_env_cannot_add_a_host_absent_from_config(self, tmp_path):
        """Same fix, via the env var instead of the explicit flag."""
        config_root = tmp_path / "config-root"
        _write_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            value="https://forgejo.example.com:3000",
        )
        result = resolve_allowed_hosts(
            None,
            env={ALLOWED_HOSTS_ENV_VAR: "https://attacker.example.net"},
            config_root=config_root,
        )
        assert result == frozenset()

    def test_flag_narrows_configured_set(self, tmp_path):
        """A caller-supplied value that overlaps a SUBSET of the configured
        allowlist narrows to that subset -- narrowing is still permitted
        (only widening is blocked)."""
        config_root = tmp_path / "config-root"
        _write_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            value="https://forgejo.example.com:3000,https://other.example.com:3000",
        )
        result = resolve_allowed_hosts(
            frozenset({"https://forgejo.example.com:3000"}),
            env={},
            config_root=config_root,
        )
        assert result == frozenset({"https://forgejo.example.com:3000"})

    def test_narrowing_matches_via_host_match_normalization(self, tmp_path):
        """Narrowing uses the SAME normalized host:port comparison
        check_host_allowed itself uses (transport.host_match.host_matches),
        not a raw string-equality set intersection."""
        config_root = tmp_path / "config-root"
        _write_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            value="forgejo.example.com:3000",
        )
        result = resolve_allowed_hosts(
            frozenset({"https://forgejo.example.com:3000"}),
            env={},
            config_root=config_root,
        )
        assert result == frozenset({"forgejo.example.com:3000"})

    def test_config_only_path_no_caller_value_supplied(self, tmp_path):
        """Neither --allowed-host nor the env var supplied at all -- the
        full configured set is the effective allowlist."""
        config_root = tmp_path / "config-root"
        _write_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            value="https://forgejo.example.com:3000,https://other.example.com:3000",
        )
        result = resolve_allowed_hosts(None, env={}, config_root=config_root)
        assert result == frozenset(
            {"https://forgejo.example.com:3000", "https://other.example.com:3000"}
        )

    def test_config_unset_reproduces_pre_fix_precedence_explicit(self, tmp_path):
        """Back-compat: with NO PUSH_HOST_CONFIG_SECTION written at all,
        explicit still wins outright over env, byte-for-byte the pre-fix
        behavior -- an unconfigured deployment sees no change."""
        config_root = tmp_path / "config-root"
        result = resolve_allowed_hosts(
            frozenset({"https://explicit-host.example.com"}),
            env={ALLOWED_HOSTS_ENV_VAR: "https://env-host.example.com"},
            config_root=config_root,
        )
        assert result == frozenset({"https://explicit-host.example.com"})

    def test_config_unset_reproduces_pre_fix_precedence_env(self, tmp_path):
        """Back-compat: with NO PUSH_HOST_CONFIG_SECTION written at all, the
        env var still resolves on its own (no explicit), byte-for-byte the
        pre-fix behavior."""
        config_root = tmp_path / "config-root"
        result = resolve_allowed_hosts(
            None,
            env={ALLOWED_HOSTS_ENV_VAR: "https://env-host.example.com"},
            config_root=config_root,
        )
        assert result == frozenset({"https://env-host.example.com"})

    def test_config_unset_reproduces_pre_fix_permissive_default(self, tmp_path):
        """Back-compat: with NO PUSH_HOST_CONFIG_SECTION written and no
        caller value supplied either, the permissive None default is
        unchanged."""
        config_root = tmp_path / "config-root"
        result = resolve_allowed_hosts(None, env={}, config_root=config_root)
        assert result is None

    def test_config_set_to_empty_string_is_a_real_restrict_to_nothing_choice(
        self, tmp_path
    ):
        """An operator who explicitly configures an empty allowed_hosts
        value has made a real choice ('restrict to nothing') -- this is
        config SET (the key is present), not config UNSET, so a caller value
        can never widen past it either."""
        config_root = tmp_path / "config-root"
        _write_config(
            config_root, section=PUSH_HOST_CONFIG_SECTION, key=PUSH_HOST_CONFIG_KEY, value=""
        )
        result = resolve_allowed_hosts(
            frozenset({"https://attacker.example.net"}),
            env={},
            config_root=config_root,
        )
        assert result == frozenset()


class TestConfigListForm:
    """lr-57573e: a malformed or non-string PUSH_HOST_CONFIG_KEY value must
    never be silently treated as 'unconfigured' -- mirrors
    test_transport_read_host_guard.py::TestConfigListForm exactly."""

    def test_yaml_list_form_is_accepted(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            raw_yaml_value="[forgejo.example.com:3000, other.example.com:3000]",
        )
        result = resolve_allowed_hosts(None, env={}, config_root=config_root)
        assert result == frozenset(
            {"forgejo.example.com:3000", "other.example.com:3000"}
        )

    def test_yaml_list_form_is_enforced_not_permissive(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            raw_yaml_value="[forgejo.example.com:3000]",
        )
        allowed_hosts = resolve_allowed_hosts(None, env={}, config_root=config_root)
        with pytest.raises(HostDeniedError):
            check_host_allowed(
                "https://attacker.example.net", allowed_hosts=allowed_hosts
            )

    def test_string_form_still_accepted_back_compat(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            value="forgejo.example.com:3000,other.example.com:3000",
        )
        result = resolve_allowed_hosts(None, env={}, config_root=config_root)
        assert result == frozenset(
            {"forgejo.example.com:3000", "other.example.com:3000"}
        )

    def test_int_value_raises_not_permissive(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            raw_yaml_value="42",
        )
        with pytest.raises(InvalidPushHostConfigError) as exc_info:
            resolve_allowed_hosts(None, env={}, config_root=config_root)
        msg = str(exc_info.value)
        assert str(config_root / provider_config.USER_CONFIG_FILENAME) in msg
        assert PUSH_HOST_CONFIG_SECTION in msg
        assert PUSH_HOST_CONFIG_KEY in msg
        assert "int" in msg

    def test_int_value_never_mints_a_token_no_call_reaches_check_host_allowed(
        self, tmp_path
    ):
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            raw_yaml_value="42",
        )
        with pytest.raises(InvalidPushHostConfigError):
            resolve_allowed_hosts(None, env={}, config_root=config_root)

    def test_mapping_value_raises_not_permissive(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            raw_yaml_value="{nested: true}",
        )
        with pytest.raises(InvalidPushHostConfigError) as exc_info:
            resolve_allowed_hosts(None, env={}, config_root=config_root)
        msg = str(exc_info.value)
        assert "dict" in msg

    def test_list_with_an_int_entry_raises_not_permissive(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            raw_yaml_value="[forgejo.example.com:3000, 12345]",
        )
        with pytest.raises(InvalidPushHostConfigError) as exc_info:
            resolve_allowed_hosts(None, env={}, config_root=config_root)
        msg = str(exc_info.value)
        assert "12345" in msg

    def test_push_host_config_is_set_also_raises_on_malformed_value(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            raw_yaml_value="42",
        )
        with pytest.raises(InvalidPushHostConfigError):
            push_host_config_is_set(config_root)


class TestCorrectiveMessageMode:
    """lr-57573e: check_host_allowed's HostDeniedError message must point at
    --allowed-host/the env var when config is UNSET, and at the
    push_host_guard.allowed_hosts config key (never the env var/flag) when
    config IS SET -- mirrors
    test_transport_read_host_guard.py::TestCorrectiveMessageMode exactly."""

    def test_config_unset_message_names_env_var_and_flag(self):
        with pytest.raises(HostDeniedError) as exc_info:
            check_host_allowed(
                "https://attacker.example.net",
                allowed_hosts=frozenset({"https://git-host.example.com"}),
                config_is_set=False,
            )
        msg = str(exc_info.value)
        assert ALLOWED_HOSTS_ENV_VAR in msg
        assert "allowed-host" in msg

    def test_config_set_message_names_config_key_not_env_var_advice(self):
        with pytest.raises(HostDeniedError) as exc_info:
            check_host_allowed(
                "https://attacker.example.net",
                allowed_hosts=frozenset(),
                config_is_set=True,
            )
        msg = str(exc_info.value)
        assert PUSH_HOST_CONFIG_SECTION in msg
        assert PUSH_HOST_CONFIG_KEY in msg
        assert "user-level config file" in msg
        assert "can only NARROW" in msg

    def test_config_set_end_to_end_via_resolve_and_check(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            value="forgejo.example.com:3000",
        )
        allowed_hosts = resolve_allowed_hosts(
            frozenset({"https://attacker.example.net"}),
            env={},
            config_root=config_root,
        )
        is_set = push_host_config_is_set(config_root)
        assert is_set is True
        with pytest.raises(HostDeniedError) as exc_info:
            check_host_allowed(
                "https://attacker.example.net",
                allowed_hosts=allowed_hosts,
                config_is_set=is_set,
            )
        msg = str(exc_info.value)
        assert PUSH_HOST_CONFIG_SECTION in msg
        assert PUSH_HOST_CONFIG_KEY in msg

    def test_config_unset_end_to_end_via_resolve_and_check(self, tmp_path):
        config_root = tmp_path / "config-root"
        resolved = resolve_allowed_hosts(
            frozenset({"https://attacker.example.net"}),
            env={},
            config_root=config_root,
        )
        assert resolved == frozenset({"https://attacker.example.net"})
        is_set = push_host_config_is_set(config_root)
        assert is_set is False
        with pytest.raises(HostDeniedError) as exc_info:
            check_host_allowed(
                "https://attacker.example.net",
                allowed_hosts=frozenset({"https://git-host.example.com"}),
                config_is_set=is_set,
            )
        msg = str(exc_info.value)
        assert ALLOWED_HOSTS_ENV_VAR in msg


class TestAbsentVsPresentNullConfig:
    """lr-57573e: an explicit `allowed_hosts: null` (or `allowed_hosts:`
    with no value) must NOT be treated as 'unconfigured' -- mirrors
    test_transport_read_host_guard.py::TestAbsentVsPresentNullConfig
    exactly."""

    def test_explicit_null_value_raises_not_permissive(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            raw_yaml_value="null",
        )
        with pytest.raises(InvalidPushHostConfigError) as exc_info:
            resolve_allowed_hosts(None, env={}, config_root=config_root)
        msg = str(exc_info.value)
        assert str(config_root / provider_config.USER_CONFIG_FILENAME) in msg
        assert PUSH_HOST_CONFIG_SECTION in msg
        assert PUSH_HOST_CONFIG_KEY in msg
        assert "NoneType" in msg

    def test_key_present_with_no_value_raises_same_as_explicit_null(self, tmp_path):
        config_root = tmp_path / "config-root"
        config_root.mkdir(parents=True, exist_ok=True)
        config_path = config_root / provider_config.USER_CONFIG_FILENAME
        config_path.write_text(
            f"{PUSH_HOST_CONFIG_SECTION}:\n  {PUSH_HOST_CONFIG_KEY}:\n",
            encoding="utf-8",
        )
        with pytest.raises(InvalidPushHostConfigError) as exc_info:
            resolve_allowed_hosts(None, env={}, config_root=config_root)
        msg = str(exc_info.value)
        assert PUSH_HOST_CONFIG_KEY in msg

    def test_explicit_null_never_mints_a_token_no_call_reaches_check_host_allowed(
        self, tmp_path
    ):
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            raw_yaml_value="null",
        )
        with pytest.raises(InvalidPushHostConfigError):
            resolve_allowed_hosts(None, env={}, config_root=config_root)

    def test_push_host_config_is_set_also_raises_on_explicit_null(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            raw_yaml_value="null",
        )
        with pytest.raises(InvalidPushHostConfigError):
            push_host_config_is_set(config_root)

    def test_genuinely_absent_key_stays_permissive(self, tmp_path):
        config_root = tmp_path / "config-root"
        config_root.mkdir(parents=True, exist_ok=True)
        config_path = config_root / provider_config.USER_CONFIG_FILENAME
        config_path.write_text(f"{PUSH_HOST_CONFIG_SECTION}: {{}}\n", encoding="utf-8")
        result = resolve_allowed_hosts(None, env={}, config_root=config_root)
        assert result is None
        assert push_host_config_is_set(config_root) is False


class TestResolveHostCeiling:
    """lr-57573e fold-in #1, F2 (HOLDEN-adjudicated): push.verb previously
    called resolve_allowed_hosts and push_host_config_is_set SEPARATELY --
    two independent reads/parses of the same push_host_guard.allowed_hosts
    config key per invocation. resolve_host_ceiling is the single call that
    replaces both, returning a HostCeilingResolution(allowed_hosts,
    config_is_set) from ONE read. These tests prove the combined function
    agrees with the two separate calls it replaces, across every mode
    TestConfigWidenGuard/TestAbsentVsPresentNullConfig above already cover
    for the two-call shape."""

    def test_config_unset_matches_separate_calls(self, tmp_path):
        config_root = tmp_path / "config-root"
        result = resolve_host_ceiling(
            frozenset({"https://explicit-host.example.com"}),
            env={ALLOWED_HOSTS_ENV_VAR: "https://env-host.example.com"},
            config_root=config_root,
        )
        assert result == HostCeilingResolution(
            allowed_hosts=frozenset({"https://explicit-host.example.com"}),
            config_is_set=False,
        )

    def test_config_set_narrows_and_reports_is_set(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            value="https://forgejo.example.com:3000",
        )
        result = resolve_host_ceiling(
            frozenset({"https://attacker.example.net"}),
            env={},
            config_root=config_root,
        )
        assert result == HostCeilingResolution(
            allowed_hosts=frozenset(), config_is_set=True
        )

    def test_malformed_config_raises_before_either_field_is_produced(self, tmp_path):
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            raw_yaml_value="42",
        )
        with pytest.raises(InvalidPushHostConfigError):
            resolve_host_ceiling(None, env={}, config_root=config_root)

    def test_result_feeds_check_host_allowed_directly(self, tmp_path):
        """The combined result's two fields are exactly what
        check_host_allowed's allowed_hosts/config_is_set parameters expect
        -- no adaptation needed at the call site."""
        config_root = tmp_path / "config-root"
        _write_config(
            config_root,
            section=PUSH_HOST_CONFIG_SECTION,
            key=PUSH_HOST_CONFIG_KEY,
            value="https://forgejo.example.com:3000",
        )
        result = resolve_host_ceiling(
            frozenset({"https://attacker.example.net"}),
            env={},
            config_root=config_root,
        )
        with pytest.raises(HostDeniedError) as exc_info:
            check_host_allowed(
                "https://attacker.example.net",
                allowed_hosts=result.allowed_hosts,
                config_is_set=result.config_is_set,
            )
        msg = str(exc_info.value)
        assert PUSH_HOST_CONFIG_SECTION in msg
        assert "can only NARROW" in msg


class TestSharedUserConfigRoot:
    """lr-57573e fold-in #1, F1 (HOLDEN-adjudicated, OVERRULED on substance
    against widening HOME): push.host_guard's config-file tier and
    provider_config's `credentials:` tier must resolve from the SAME
    user-level config root -- otherwise a caller could pair a
    caller-controlled, narrower push_host_guard ceiling against the
    operator's real credential root, defeating the ceiling's own purpose (a
    redirected HOME/XDG_CONFIG_HOME would then widen/narrow the push
    allowlist without touching which credential actually gets minted). Both
    tiers read through the ONE shared loader,
    provider_config.load_user_config_section (mirrors
    test_transport_read_host_guard.py::TestSharedUserConfigRoot exactly, for
    push's own config tier) -- proven here by writing BOTH sections into ONE
    physical file under one root and confirming each tier's own public
    reader sees the value the OTHER tier's section carries when given the
    identical root, i.e. there is exactly one file/root in play, not two
    independently-resolved ones."""

    def test_push_host_guard_and_credentials_section_share_one_config_file(self, tmp_path):
        config_root = tmp_path / "config-root"
        config_root.mkdir(parents=True, exist_ok=True)
        config_path = config_root / provider_config.USER_CONFIG_FILENAME
        config_path.write_text(
            "credentials:\n"
            "  token_provider_forgejo: command\n"
            "  token_command_forgejo: \"echo real-operator-token\"\n"
            f"{PUSH_HOST_CONFIG_SECTION}:\n"
            f"  {PUSH_HOST_CONFIG_KEY}: \"forgejo.example.com:3000\"\n",
            encoding="utf-8",
        )

        # push.host_guard's own tier, resolved via THIS root.
        allowed = resolve_allowed_hosts(None, env={}, config_root=config_root)
        assert allowed == frozenset({"forgejo.example.com:3000"})

        # provider_config's `credentials:` tier, resolved via the SAME root
        # value passed the SAME way (config_root=...) -- if the two tiers
        # ever resolved from independently-computed roots, this would be
        # the seam that could diverge; it reads the value the fixture wrote
        # into the SAME physical file the allowlist read above came from.
        kind, command = provider_config.resolve_provider_kind_and_command(
            provider_config.PLATFORM_FORGEJO,
            env={},
            config_root=config_root,
        )
        assert kind == provider_config.PROVIDER_KIND_COMMAND
        assert command == "echo real-operator-token"

    def test_omitted_config_root_resolves_both_tiers_to_the_same_live_default(
        self, monkeypatch, tmp_path
    ):
        """Both loaders fall back to provider_config.DEFAULT_USER_CONFIG_ROOT
        when config_root is OMITTED -- load_user_config_section (the ONE
        shared loader both push.host_guard and provider_config's credentials
        tier call through) reads that constant live from provider_config's
        OWN module namespace at call time, so redirecting
        provider_config.DEFAULT_USER_CONFIG_ROOT (e.g. via a redirected
        HOME/XDG_CONFIG_HOME) redirects BOTH tiers in lock-step even with
        config_root never passed explicitly -- proven here by monkeypatching
        that one constant and confirming push.host_guard's own
        omitted-config_root call observes a config file written under the
        redirected root. This is the F1 finding's actual substance: a caller
        that redirects HOME to widen this ceiling redirects the credential
        root right along with it, so it can never widen the ceiling while
        leaving the operator's real credential resolution untouched."""
        isolated_root = tmp_path / "isolated-live-default"
        monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", isolated_root)
        isolated_root.mkdir(parents=True, exist_ok=True)
        config_path = isolated_root / provider_config.USER_CONFIG_FILENAME
        config_path.write_text(
            f"{PUSH_HOST_CONFIG_SECTION}:\n  {PUSH_HOST_CONFIG_KEY}: \"forgejo.example.com:3000\"\n",
            encoding="utf-8",
        )
        # config_root OMITTED here (unlike every other test in this file,
        # which pins it explicitly) -- this is the exact call shape that
        # depends on push.host_guard's loader and provider_config's loader
        # sharing one live default.
        result = resolve_allowed_hosts(None, env={})
        assert result == frozenset({"forgejo.example.com:3000"})
