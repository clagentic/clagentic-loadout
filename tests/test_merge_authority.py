"""test_merge_authority.py — tests for clagentic_loadout.merge.authority
(lr-885f, Wave B slice 4).

Coverage:
  - StaticRoleAuthorityProvider: empty authorized-role set denies every role
    (fail-closed by construction); a configured role is allowed, an
    unconfigured one is denied.
  - check_authority: allow when the provider confirms; FAIL-CLOSED
    AuthorityDeniedError on an explicit deny AND on a provider raising
    AuthorityProviderError (unreachable/malformed-response class).
"""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.authority import (
    AuthorityProviderError,
    StaticRoleAuthorityProvider,
    check_authority,
)
from clagentic_loadout.merge.errors import AuthorityDeniedError


class TestStaticRoleAuthorityProvider:
    def test_empty_authorized_roles_denies_every_role(self):
        provider = StaticRoleAuthorityProvider(frozenset())
        assert provider.authority_allows("merger", "some-owner", "some-repo", 1) is False

    def test_configured_role_allowed(self):
        provider = StaticRoleAuthorityProvider(frozenset({"merger"}))
        assert provider.authority_allows("merger", "some-owner", "some-repo", 1) is True

    def test_unconfigured_role_denied(self):
        provider = StaticRoleAuthorityProvider(frozenset({"merger"}))
        assert provider.authority_allows("some-other-role", "some-owner", "some-repo", 1) is False


class _RaisingProvider:
    def authority_allows(self, role, owner, repo, pr_number):
        raise AuthorityProviderError("directory unreachable")


class _AllowingProvider:
    def authority_allows(self, role, owner, repo, pr_number):
        return True


class _DenyingProvider:
    def authority_allows(self, role, owner, repo, pr_number):
        return False


class TestCheckAuthority:
    def test_allows_when_provider_confirms(self):
        check_authority("merger", "some-owner", "some-repo", 1, _AllowingProvider())  # no raise

    def test_denies_on_explicit_false(self):
        with pytest.raises(AuthorityDeniedError) as exc_info:
            check_authority("merger", "some-owner", "some-repo", 1, _DenyingProvider())
        assert "merger" in str(exc_info.value)

    def test_fail_closed_on_provider_error(self):
        """A provider that cannot be consulted (outage, malformed response)
        must refuse exactly like an explicit deny -- never treated as an
        implicit allow."""
        with pytest.raises(AuthorityDeniedError) as exc_info:
            check_authority("merger", "some-owner", "some-repo", 1, _RaisingProvider())
        assert "could not confirm" in str(exc_info.value)
