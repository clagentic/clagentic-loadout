"""test_transport_read_host_guard.py — tests for
clagentic_loadout.transport.read_host_guard (lr-4ebce1, WIDEN/NARROW fix
lr-4ebce1 fold-in #1; FAIL-OPEN-ON-CONFIG + MISLEADING-MESSAGE fix lr-4ebce1
fold-in #2).

Coverage:
  - resolve_allowed_hosts precedence: explicit > env var > empty (permissive)
    default -- mirrors test_push_host_guard.py's own coverage shape for the
    sibling guard.
  - check_host_allowed: permissive when no allowlist configured; deny when
    configured and the resolved git-host base does not match any configured
    entry; allow when it does -- both a bare "host[:port]" allowlist entry
    and a full "scheme://host[:port]" entry are accepted (via
    transport.host_match.host_matches).
  - No operator hostname baked in anywhere -- the allowed set is entirely
    caller/config supplied.
  - The env var is a SEPARATE name from push.host_guard.ALLOWED_HOSTS_ENV_VAR
    (own allowlist, not shared -- see module docstring).
  - TestConfigWidenGuard (lr-4ebce1 fold-in #1): the caller-widening fix --
    explicit/env can never ADD a host absent from the operator-configured
    READ_HOST_CONFIG_SECTION allowlist (token provider never reached on a
    denied call at the git_host_api integration layer -- covered separately
    in test_transport_git_host_api.py); narrowing via intersection works;
    the config-only path (neither explicit nor env supplied) works; and
    config UNSET reproduces the pre-fix, caller-settable-only precedence
    byte-for-byte (back-compat).
  - TestConfigListForm (lr-4ebce1 fold-in #2, defect 1): a YAML list of
    strings is accepted alongside the comma-separated string; a malformed
    value (int, mapping, list containing a non-string) raises
    InvalidReadHostConfigError rather than silently degrading to
    permissive -- and no test in this class ever reaches a token mint
    (resolve_allowed_hosts/read_host_config_is_set raise BEFORE
    check_host_allowed is even called, mirroring the "provider never
    reached" invariant TestConfigWidenGuard already established for a
    denied call).
  - TestCorrectiveMessageMode (lr-4ebce1 fold-in #2, defect 2): the
    HostDeniedError message names the env var/flag when config is UNSET,
    and names the read_host_guard.allowed_hosts config key (never the env
    var/flag) when config IS SET.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.push.host_guard import ALLOWED_HOSTS_ENV_VAR as PUSH_ALLOWED_HOSTS_ENV_VAR
from clagentic_loadout.transport import provider_config
from clagentic_loadout.transport.read_host_guard import (
    ALLOWED_HOSTS_ENV_VAR,
    READ_HOST_CONFIG_KEY,
    READ_HOST_CONFIG_SECTION,
    HostDeniedError,
    InvalidReadHostConfigError,
    check_host_allowed,
    read_host_config_is_set,
    resolve_allowed_hosts,
)


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    """Belt-and-suspenders isolation, same pattern/rationale as
    test_transport_git_host_api.py's own `_isolate_user_config_root`
    fixture: resolve_allowed_hosts's new config-file tier
    (READ_HOST_CONFIG_SECTION) reads through
    transport.provider_config.load_user_config_section, which falls back to
    provider_config.DEFAULT_USER_CONFIG_ROOT -- the REAL
    ~/.config/clagentic/loadout/ directory -- for any call that omits
    config_root. A real deployment config.yaml on the host running these
    tests must never leak a live allowlist into a test asserting the
    config-UNSET (back-compat) precedence. Every test in
    TestConfigWidenGuard that cares about the config-file tier already pins
    config_root=tmp_path explicitly; this autouse fixture is a conformance
    backstop so a future test that forgets to pin it still resolves to an
    empty, per-test tmp directory rather than real host state."""
    isolated_root = tmp_path / "isolated-user-config-root"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", isolated_root)


def _write_config(config_root, *, section: str, key: str, value: str) -> None:
    """Write a minimal <config_root>/config.yaml with one section/key, using
    the plain YAML scalar shape every other config-file test fixture in this
    package writes (no PyYAML dependency needed here -- a flat one-section,
    one-key mapping has no shape PyYAML's safe_load would parse differently
    from simple block-mapping syntax)."""
    config_root.mkdir(parents=True, exist_ok=True)
    config_path = config_root / provider_config.USER_CONFIG_FILENAME
    config_path.write_text(f"{section}:\n  {key}: \"{value}\"\n", encoding="utf-8")


def _write_raw_config(config_root, *, section: str, key: str, raw_yaml_value: str) -> None:
    """Write <config_root>/config.yaml with one section/key whose VALUE is
    the raw YAML text *raw_yaml_value* (e.g. a flow-sequence list, or a bare
    scalar of a non-string type) -- unlike _write_config, this does not wrap
    the value in quotes, so the caller controls exactly what YAML shape
    PyYAML's safe_load parses it as (a list, an int, a mapping, ...)."""
    config_root.mkdir(parents=True, exist_ok=True)
    config_path = config_root / provider_config.USER_CONFIG_FILENAME
    config_path.write_text(f"{section}:\n  {key}: {raw_yaml_value}\n", encoding="utf-8")


def test_env_var_is_not_shared_with_push():
    assert ALLOWED_HOSTS_ENV_VAR != PUSH_ALLOWED_HOSTS_ENV_VAR
    assert ALLOWED_HOSTS_ENV_VAR == "CLAGENTIC_LOADOUT_READ_ALLOWED_HOSTS"


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
        # None (not an empty frozenset) is the permissive-default sentinel
        # -- see read_host_guard's own "RETURN-TYPE FIX": collapsing this to
        # frozenset() would be indistinguishable from a configured-but-
        # narrowed-to-nothing DENY-everything outcome once the config tier
        # is in play (see TestConfigWidenGuard).
        result = resolve_allowed_hosts(None, env={})
        assert result is None

    def test_env_var_whitespace_only_treated_as_unset(self):
        result = resolve_allowed_hosts(None, env={ALLOWED_HOSTS_ENV_VAR: "   "})
        assert result is None


class TestCheckHostAllowed:
    def test_permissive_when_no_allowlist_configured(self):
        # An arbitrary synthetic host -- proves there is no baked allow-list
        # of any particular operator hostname; permissiveness is a property
        # of allowed_hosts=None (nothing configured), not of any specific
        # host value.
        check_host_allowed(
            "https://totally-synthetic-host.example.com", allowed_hosts=None
        )

    def test_denied_when_allowed_hosts_is_an_empty_frozenset(self):
        # An EMPTY frozenset is NOT the permissive case -- it means a
        # restriction IS configured (e.g. an operator's explicit "restrict
        # to nothing", or a caller value narrowed to zero overlap with the
        # config ceiling) and must deny unconditionally. Only None means
        # "no restriction configured" -- see read_host_guard's own
        # "RETURN-TYPE FIX".
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

    def test_bare_authority_allowlist_entry_matches_full_url_base(self):
        # An allowlist entry supplied as a bare host[:port] (no scheme) still
        # matches a git_host_base that IS a full URL -- transport.host_match.
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
        # message shape to static labels + caller-supplied
        # git_host_base/allowlist only.
        with pytest.raises(HostDeniedError) as exc_info:
            check_host_allowed(
                "https://attacker.example.net",
                allowed_hosts=frozenset({"https://git-host.example.com"}),
            )
        msg = str(exc_info.value)
        assert "attacker.example.net" in msg
        assert ALLOWED_HOSTS_ENV_VAR in msg


class TestConfigWidenGuard:
    """lr-4ebce1 fold-in #1 (PEACHES 5890696907 BLOCKING, HOLDEN-agreed): a
    caller who controls --git-host-base-url also controls --allowed-host /
    CLAGENTIC_LOADOUT_READ_ALLOWED_HOSTS in the SAME invocation, so those two
    caller-settable sources could never actually protect against that same
    caller -- only an operator-controlled, non-per-call source (the
    user-level config file) can WIDEN the effective allowlist. See
    read_host_guard's module docstring, "CALLER-WIDENING DEFECT + FIX", for
    the full argument this class proves."""

    def test_flag_cannot_add_a_host_absent_from_config(self, tmp_path):
        """The core fix: --allowed-host (explicit) naming a host NOT present
        in the configured allowlist must NOT appear in the resolved set --
        the config tier is a ceiling, not a co-equal source, once it is
        set."""
        config_root = tmp_path / "config-root"
        _write_config(
            config_root,
            section=READ_HOST_CONFIG_SECTION,
            key=READ_HOST_CONFIG_KEY,
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
        # this result never even gets a chance to mint against it (proven
        # end-to-end, at the git_host_api integration layer, by
        # test_transport_git_host_api.py's own TestMainReadHostGuard
        # coverage of the "provider never called" invariant).
        with pytest.raises(HostDeniedError):
            check_host_allowed("https://attacker.example.net", allowed_hosts=result)

    def test_env_cannot_add_a_host_absent_from_config(self, tmp_path):
        """Same fix, via the env var instead of the explicit flag -- the env
        var is equally caller-settable per the task's own framing ('the env
        var is equally caller-settable in a command's environment')."""
        config_root = tmp_path / "config-root"
        _write_config(
            config_root,
            section=READ_HOST_CONFIG_SECTION,
            key=READ_HOST_CONFIG_KEY,
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
            section=READ_HOST_CONFIG_SECTION,
            key=READ_HOST_CONFIG_KEY,
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
        not a raw string-equality set intersection -- a configured entry and
        a caller-supplied entry naming the identical authority in a
        different shape (bare authority vs full URL) must still be
        recognized as an overlap, keeping the CONFIGURED entry's own string
        in the result."""
        config_root = tmp_path / "config-root"
        _write_config(
            config_root,
            section=READ_HOST_CONFIG_SECTION,
            key=READ_HOST_CONFIG_KEY,
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
            section=READ_HOST_CONFIG_SECTION,
            key=READ_HOST_CONFIG_KEY,
            value="https://forgejo.example.com:3000,https://other.example.com:3000",
        )
        result = resolve_allowed_hosts(None, env={}, config_root=config_root)
        assert result == frozenset(
            {"https://forgejo.example.com:3000", "https://other.example.com:3000"}
        )

    def test_config_unset_reproduces_pre_fix_precedence_explicit(self, tmp_path):
        """Back-compat: with NO READ_HOST_CONFIG_SECTION written at all,
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
        """Back-compat: with NO READ_HOST_CONFIG_SECTION written at all, the
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
        """Back-compat: with NO READ_HOST_CONFIG_SECTION written and no
        caller value supplied either, the permissive None default is
        unchanged (see RETURN-TYPE FIX for why None, not frozenset())."""
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
            config_root, section=READ_HOST_CONFIG_SECTION, key=READ_HOST_CONFIG_KEY, value=""
        )
        result = resolve_allowed_hosts(
            frozenset({"https://attacker.example.net"}),
            env={},
            config_root=config_root,
        )
        assert result == frozenset()


class TestConfigListForm:
    """lr-4ebce1 fold-in #2, defect 1: FAIL-OPEN ON A
    NON-STRING CONFIG VALUE. The pre-fix _load_configured_allowed_hosts
    treated ANY non-string value -- including the natural YAML list shape
    `allowed_hosts: [a.example, b.example]` -- as "not configured" and
    silently returned None (permissive). A config parse/shape problem must
    never resolve to "no restriction"; it must refuse loudly. See
    read_host_guard's own module docstring, "TWO DEFECTS + FIX", for the
    full argument."""

    def test_yaml_list_form_is_accepted(self, tmp_path):
        """The natural YAML list-authoring shape now resolves exactly like
        the equivalent comma-separated string would -- an operator is no
        longer punished (with silent permissiveness) for reaching for the
        list form first."""
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=READ_HOST_CONFIG_SECTION,
            key=READ_HOST_CONFIG_KEY,
            raw_yaml_value="[forgejo.example.com:3000, other.example.com:3000]",
        )
        result = resolve_allowed_hosts(None, env={}, config_root=config_root)
        assert result == frozenset(
            {"forgejo.example.com:3000", "other.example.com:3000"}
        )

    def test_yaml_list_form_is_enforced_not_permissive(self, tmp_path):
        """End-to-end: a list-configured allowlist actually DENIES a host
        absent from it -- proving the list form is a real enforced
        restriction, not merely "parsed but still permissive" (the exact
        pre-fix failure mode: parsing would have returned None and every
        host would have passed)."""
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=READ_HOST_CONFIG_SECTION,
            key=READ_HOST_CONFIG_KEY,
            raw_yaml_value="[forgejo.example.com:3000]",
        )
        allowed_hosts = resolve_allowed_hosts(None, env={}, config_root=config_root)
        with pytest.raises(HostDeniedError):
            check_host_allowed(
                "https://attacker.example.net", allowed_hosts=allowed_hosts
            )

    def test_string_form_still_accepted_back_compat(self, tmp_path):
        """The original comma-separated-string shape keeps working
        unchanged alongside the new list shape."""
        config_root = tmp_path / "config-root"
        _write_config(
            config_root,
            section=READ_HOST_CONFIG_SECTION,
            key=READ_HOST_CONFIG_KEY,
            value="forgejo.example.com:3000,other.example.com:3000",
        )
        result = resolve_allowed_hosts(None, env={}, config_root=config_root)
        assert result == frozenset(
            {"forgejo.example.com:3000", "other.example.com:3000"}
        )

    def test_int_value_raises_not_permissive(self, tmp_path):
        """A bare int (e.g. an operator's typo, or a YAML authoring
        mistake) is a hard config error -- never silently treated as
        'unconfigured' (which would be permissive)."""
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=READ_HOST_CONFIG_SECTION,
            key=READ_HOST_CONFIG_KEY,
            raw_yaml_value="42",
        )
        with pytest.raises(InvalidReadHostConfigError) as exc_info:
            resolve_allowed_hosts(None, env={}, config_root=config_root)
        msg = str(exc_info.value)
        assert str(config_root / provider_config.USER_CONFIG_FILENAME) in msg
        assert READ_HOST_CONFIG_SECTION in msg
        assert READ_HOST_CONFIG_KEY in msg
        assert "int" in msg

    def test_int_value_never_mints_a_token_no_call_reaches_check_host_allowed(
        self, tmp_path
    ):
        """The config error fires INSIDE resolve_allowed_hosts, before
        check_host_allowed (and therefore before any token mint) is ever
        reached -- asserted here by confirming resolve_allowed_hosts itself
        never returns a value check_host_allowed could act on."""
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=READ_HOST_CONFIG_SECTION,
            key=READ_HOST_CONFIG_KEY,
            raw_yaml_value="42",
        )
        with pytest.raises(InvalidReadHostConfigError):
            resolve_allowed_hosts(None, env={}, config_root=config_root)
        # No allowed_hosts value was ever produced for check_host_allowed to
        # consume -- the exception propagates out of resolve_allowed_hosts
        # itself, so a caller (transport.git_host_api._run) can never reach
        # the check_host_allowed/token-resolution steps that follow it.

    def test_mapping_value_raises_not_permissive(self, tmp_path):
        """A YAML mapping (e.g. a copy-paste/indentation mistake nesting
        another section under this key) is a hard config error, not
        'unconfigured'."""
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=READ_HOST_CONFIG_SECTION,
            key=READ_HOST_CONFIG_KEY,
            raw_yaml_value="{nested: true}",
        )
        with pytest.raises(InvalidReadHostConfigError) as exc_info:
            resolve_allowed_hosts(None, env={}, config_root=config_root)
        msg = str(exc_info.value)
        assert "dict" in msg

    def test_list_with_an_int_entry_raises_not_permissive(self, tmp_path):
        """A list that LOOKS like the accepted list-of-strings shape but
        contains one non-string entry (e.g. an unquoted numeric-looking
        hostname parsed as an int by YAML) is a hard config error -- never
        silently coerced or dropped."""
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=READ_HOST_CONFIG_SECTION,
            key=READ_HOST_CONFIG_KEY,
            raw_yaml_value="[forgejo.example.com:3000, 12345]",
        )
        with pytest.raises(InvalidReadHostConfigError) as exc_info:
            resolve_allowed_hosts(None, env={}, config_root=config_root)
        msg = str(exc_info.value)
        assert "12345" in msg

    def test_read_host_config_is_set_also_raises_on_malformed_value(self, tmp_path):
        """read_host_config_is_set shares _load_configured_allowed_hosts's
        own fail-closed contract -- it never silently reports False (which
        transport.git_host_api._run would otherwise read as 'config UNSET',
        the wrong corrective-message mode) for a malformed value."""
        config_root = tmp_path / "config-root"
        _write_raw_config(
            config_root,
            section=READ_HOST_CONFIG_SECTION,
            key=READ_HOST_CONFIG_KEY,
            raw_yaml_value="42",
        )
        with pytest.raises(InvalidReadHostConfigError):
            read_host_config_is_set(config_root)


class TestCorrectiveMessageMode:
    """lr-4ebce1 fold-in #2, defect 2: MISLEADING REFUSAL
    MESSAGE. check_host_allowed's pre-fix HostDeniedError always pointed at
    --allowed-host/the env var, even when the operator-configured
    READ_HOST_CONFIG_SECTION ceiling was the actual reason those two could
    never have permitted the host (they can only narrow the ceiling, never
    widen past it). See read_host_guard's own module docstring, "TWO
    DEFECTS + FIX", for the full argument."""

    def test_config_unset_message_names_env_var_and_flag(self):
        """config_is_set=False (the default, and the only mode that existed
        pre-fix): the message still tells the caller to set the env var or
        pass --allowed-host -- accurate in this mode, unchanged text."""
        with pytest.raises(HostDeniedError) as exc_info:
            check_host_allowed(
                "https://attacker.example.net",
                allowed_hosts=frozenset({"https://git-host.example.com"}),
                config_is_set=False,
            )
        msg = str(exc_info.value)
        assert ALLOWED_HOSTS_ENV_VAR in msg
        assert "--allowed-host" in msg

    def test_config_set_message_names_config_key_not_env_var_advice(self):
        """config_is_set=True: the message must point at the
        read_host_guard.allowed_hosts config key, and must NOT tell the
        caller that setting the env var or --allowed-host alone will permit
        the host (false in this mode -- both can only narrow the
        configured ceiling)."""
        with pytest.raises(HostDeniedError) as exc_info:
            check_host_allowed(
                "https://attacker.example.net",
                allowed_hosts=frozenset(),
                config_is_set=True,
            )
        msg = str(exc_info.value)
        assert READ_HOST_CONFIG_SECTION in msg
        assert READ_HOST_CONFIG_KEY in msg
        assert "user-level config file" in msg
        assert "can only NARROW" in msg

    def test_config_set_end_to_end_via_resolve_and_check(self, tmp_path):
        """Realistic end-to-end shape: config IS set (narrowing an
        attacker-supplied --allowed-host to nothing), and the resulting
        HostDeniedError message reflects the config-SET mode -- exercised
        through both resolve_allowed_hosts and read_host_config_is_set,
        the same two calls transport.git_host_api._run itself makes."""
        config_root = tmp_path / "config-root"
        _write_config(
            config_root,
            section=READ_HOST_CONFIG_SECTION,
            key=READ_HOST_CONFIG_KEY,
            value="forgejo.example.com:3000",
        )
        allowed_hosts = resolve_allowed_hosts(
            frozenset({"https://attacker.example.net"}),
            env={},
            config_root=config_root,
        )
        config_is_set = read_host_config_is_set(config_root)
        assert config_is_set is True
        with pytest.raises(HostDeniedError) as exc_info:
            check_host_allowed(
                "https://attacker.example.net",
                allowed_hosts=allowed_hosts,
                config_is_set=config_is_set,
            )
        msg = str(exc_info.value)
        assert READ_HOST_CONFIG_SECTION in msg
        assert READ_HOST_CONFIG_KEY in msg

    def test_config_unset_end_to_end_via_resolve_and_check(self, tmp_path):
        """Same shape, config UNSET: read_host_config_is_set is False and
        the message keeps the original env-var/flag corrective text."""
        config_root = tmp_path / "config-root"
        resolved = resolve_allowed_hosts(
            frozenset({"https://attacker.example.net"}),
            env={},
            config_root=config_root,
        )
        # Config UNSET -- explicit still wins outright (byte-for-byte the
        # pre-fix precedence, see TestConfigWidenGuard's back-compat tests).
        assert resolved == frozenset({"https://attacker.example.net"})
        config_is_set = read_host_config_is_set(config_root)
        assert config_is_set is False
        # An unconfigured deployment is permissive by default (see
        # TestResolveAllowedHosts) -- to exercise the denial message in
        # this mode, supply an explicit allowed_hosts set directly instead
        # of relying on resolve_allowed_hosts's permissive None here.
        with pytest.raises(HostDeniedError) as exc_info:
            check_host_allowed(
                "https://attacker.example.net",
                allowed_hosts=frozenset({"https://git-host.example.com"}),
                config_is_set=config_is_set,
            )
        msg = str(exc_info.value)
        assert ALLOWED_HOSTS_ENV_VAR in msg
        assert "--allowed-host" in msg
