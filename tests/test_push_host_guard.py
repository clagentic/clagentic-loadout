"""test_push_host_guard.py — tests for clagentic_loadout.push.host_guard
(lr-0e39f9).

Coverage:
  - resolve_allowed_hosts precedence: explicit > env var > empty (permissive)
    default -- mirrors test_push_namespace_guard.py's own coverage shape for
    the sibling guard.
  - check_host_allowed: permissive when no allowlist configured; deny when
    configured and the derived api_base host does not match any configured
    entry; allow when it does -- both a bare "host[:port]" allowlist entry
    and a full "scheme://host[:port]" entry are accepted (via
    transport.host_match.host_matches).
  - No operator hostname baked in anywhere -- the allowed set is entirely
    caller/config supplied.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.push.errors import HostDeniedError
from clagentic_loadout.push.host_guard import (
    ALLOWED_HOSTS_ENV_VAR,
    check_host_allowed,
    resolve_allowed_hosts,
)


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

    def test_no_explicit_no_env_is_empty_permissive_default(self):
        result = resolve_allowed_hosts(None, env={})
        assert result == frozenset()

    def test_env_var_whitespace_only_treated_as_unset(self):
        result = resolve_allowed_hosts(None, env={ALLOWED_HOSTS_ENV_VAR: "   "})
        assert result == frozenset()


class TestCheckHostAllowed:
    def test_permissive_when_no_allowlist_configured(self):
        # An arbitrary synthetic host -- proves there is no baked allow-list
        # of any particular operator hostname; permissiveness is a property
        # of an EMPTY configured set, not of any specific host value.
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
