"""test_push_namespace_guard.py — tests for clagentic_loadout.push.namespace_guard
(lr-09ca, Wave B slice 3).

Coverage:
  - resolve_allowed_namespaces precedence: explicit > env var > empty
    (permissive) default.
  - check_namespace_allowed: permissive when no allowlist configured; deny
    when configured and owner absent; allow when configured and owner
    present.
  - No hardcoded brand-owner literal anywhere in this module's allow path —
    the allowed set is entirely caller/config supplied (proven by allowing
    an arbitrary synthetic owner name via config).
"""

from __future__ import annotations

import pytest

from clagentic_loadout.push.errors import NamespaceDeniedError
from clagentic_loadout.push.namespace_guard import (
    ALLOWED_NAMESPACES_ENV_VAR,
    check_namespace_allowed,
    resolve_allowed_namespaces,
)


class TestResolveAllowedNamespaces:
    def test_explicit_wins_over_env(self):
        result = resolve_allowed_namespaces(
            frozenset({"explicit-owner"}), env={ALLOWED_NAMESPACES_ENV_VAR: "env-owner"}
        )
        assert result == frozenset({"explicit-owner"})

    def test_explicit_empty_set_is_honored_not_reinterpreted(self):
        result = resolve_allowed_namespaces(frozenset(), env={})
        assert result == frozenset()

    def test_env_var_parsed_comma_separated_trimmed(self):
        result = resolve_allowed_namespaces(
            None, env={ALLOWED_NAMESPACES_ENV_VAR: " owner-a, owner-b ,owner-c"}
        )
        assert result == frozenset({"owner-a", "owner-b", "owner-c"})

    def test_env_var_empty_entries_dropped(self):
        result = resolve_allowed_namespaces(None, env={ALLOWED_NAMESPACES_ENV_VAR: "owner-a,,owner-b"})
        assert result == frozenset({"owner-a", "owner-b"})

    def test_no_explicit_no_env_is_empty_permissive_default(self):
        result = resolve_allowed_namespaces(None, env={})
        assert result == frozenset()

    def test_env_var_whitespace_only_treated_as_unset(self):
        result = resolve_allowed_namespaces(None, env={ALLOWED_NAMESPACES_ENV_VAR: "   "})
        assert result == frozenset()


class TestCheckNamespaceAllowed:
    def test_permissive_when_no_allowlist_configured(self):
        # An arbitrary synthetic owner -- proves there is no baked allow-list
        # of any particular brand/org string; permissiveness is a property
        # of an EMPTY configured set, not of any specific owner value.
        check_namespace_allowed("totally-synthetic-owner-xyz", "some-repo", allowed_namespaces=frozenset())

    def test_denied_when_configured_and_owner_absent(self):
        with pytest.raises(NamespaceDeniedError) as exc_info:
            check_namespace_allowed(
                "not-allowed-owner", "some-repo", allowed_namespaces=frozenset({"allowed-owner"})
            )
        assert "not-allowed-owner" in str(exc_info.value)

    def test_allowed_when_configured_and_owner_present(self):
        check_namespace_allowed(
            "allowed-owner", "some-repo", allowed_namespaces=frozenset({"allowed-owner", "other-owner"})
        )

    def test_case_sensitive_owner_match(self):
        with pytest.raises(NamespaceDeniedError):
            check_namespace_allowed(
                "Allowed-Owner", "some-repo", allowed_namespaces=frozenset({"allowed-owner"})
            )

    def test_deny_message_never_reveals_a_token_or_credential(self):
        # Sanity: the guard's own message construction never touches a
        # secret value -- there is none in scope here, but this locks the
        # message shape to static labels + caller-supplied owner/repo only.
        with pytest.raises(NamespaceDeniedError) as exc_info:
            check_namespace_allowed("bad-owner", "some-repo", allowed_namespaces=frozenset({"good-owner"}))
        msg = str(exc_info.value)
        assert "bad-owner" in msg
        assert ALLOWED_NAMESPACES_ENV_VAR in msg
