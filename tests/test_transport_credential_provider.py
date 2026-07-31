"""test_transport_credential_provider.py — tests for
clagentic_loadout.transport.credential_provider (lr-3ba8, Wave B slice 1).

Coverage:
  - StaticTokenProvider: happy path (reuses secrets_config's role-scoped
    .env reader), missing file / insecure permissions / missing key all
    surface as CredentialProviderError.
  - resolve_token: default provider is StaticTokenProvider; an injected mock
    provider is used instead when supplied; an empty token from any
    provider is rejected even though the provider itself did not raise.
  - No inherited-environment fallback exists anywhere in this module.
  - lr-43c8d7: ResolvedToken / resolve_token_result -- a provider MAY return
    a ResolvedToken carrying a provider-verified app_slug instead of a bare
    string; resolve_token() itself stays byte-identical (str return,
    app_slug discarded) for every existing caller.
  - CommandTokenProvider.emit_structured_output (lr-43c8d7): opt-in only --
    default False is byte-identical bare-token behavior regardless of what
    the configured command prints; True parses stdout as
    {"token": ..., "app_slug": ...} JSON, fail-closed on any shape mismatch.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from clagentic_loadout.transport.credential_provider import (
    CommandTokenProvider,
    CredentialProviderError,
    GIT_HOST_TOKEN_ENV_KEY,
    ResolvedToken,
    StaticTokenProvider,
    TokenProvider,
    resolve_token,
    resolve_token_result,
)

_PY = sys.executable


def _write_env_file(path, content: str, *, mode: int = 0o600) -> None:
    path.write_text(content)
    os.chmod(path, mode)


class TestStaticTokenProvider:
    def test_happy_path_returns_token(self, tmp_path):
        env_file = tmp_path / "builder.env"
        _write_env_file(env_file, f"{GIT_HOST_TOKEN_ENV_KEY}=tok-abc123\n")

        provider = StaticTokenProvider(config_root=tmp_path)
        assert provider.resolve_token("builder") == "tok-abc123"

    def test_missing_file_raises_credential_provider_error(self, tmp_path):
        provider = StaticTokenProvider(config_root=tmp_path)
        with pytest.raises(CredentialProviderError, match="not found"):
            provider.resolve_token("nope")

    def test_insecure_permissions_raises(self, tmp_path):
        env_file = tmp_path / "builder.env"
        _write_env_file(env_file, f"{GIT_HOST_TOKEN_ENV_KEY}=tok-abc123\n", mode=0o644)

        provider = StaticTokenProvider(config_root=tmp_path)
        with pytest.raises(CredentialProviderError, match="insecure permissions"):
            provider.resolve_token("builder")

    def test_missing_key_raises(self, tmp_path):
        env_file = tmp_path / "builder.env"
        _write_env_file(env_file, "OTHER_KEY=x\n")

        provider = StaticTokenProvider(config_root=tmp_path)
        with pytest.raises(CredentialProviderError, match="missing required key"):
            provider.resolve_token("builder")

    def test_path_traversal_role_rejected(self, tmp_path):
        provider = StaticTokenProvider(config_root=tmp_path)
        with pytest.raises(CredentialProviderError, match="invalid characters"):
            provider.resolve_token("../../etc/passwd")

    def test_implements_token_provider_protocol(self, tmp_path):
        provider = StaticTokenProvider(config_root=tmp_path)
        assert isinstance(provider, TokenProvider)

    def test_repo_argument_ignored_static_provider_unaffected(self, tmp_path):
        """lr-ea28: StaticTokenProvider accepts `repo` (it is on the
        TokenProvider protocol) but has no notion of repo scoping -- passing
        a repo must return the exact same token as omitting it."""
        env_file = tmp_path / "builder.env"
        _write_env_file(env_file, f"{GIT_HOST_TOKEN_ENV_KEY}=tok-abc123\n")

        provider = StaticTokenProvider(config_root=tmp_path)
        assert provider.resolve_token("builder", repo="some-owner/some-repo") == "tok-abc123"
        assert provider.resolve_token("builder") == "tok-abc123"


class TestResolveToken:
    def test_default_provider_is_static(self, tmp_path, monkeypatch):
        env_file = tmp_path / "role-x.env"
        _write_env_file(env_file, f"{GIT_HOST_TOKEN_ENV_KEY}=tok-static\n")

        real_static_token_provider = StaticTokenProvider
        import clagentic_loadout.transport.credential_provider as cp
        monkeypatch.setattr(
            cp, "StaticTokenProvider",
            lambda **kw: real_static_token_provider(config_root=tmp_path),
        )

        token = resolve_token("role-x")
        assert token == "tok-static"

    def test_injected_provider_is_used_instead_of_default(self):
        class _FakeProvider:
            def resolve_token(self, role: str) -> str:
                return f"fake-token-for-{role}"

        token = resolve_token("some-role", provider=_FakeProvider())
        assert token == "fake-token-for-some-role"

    def test_empty_token_from_provider_is_rejected(self):
        class _EmptyProvider:
            def resolve_token(self, role: str) -> str:
                return ""

        with pytest.raises(CredentialProviderError, match="empty token"):
            resolve_token("some-role", provider=_EmptyProvider())

    def test_provider_error_propagates(self):
        class _FailingProvider:
            def resolve_token(self, role: str) -> str:
                raise CredentialProviderError("minting service unavailable")

        with pytest.raises(CredentialProviderError, match="minting service unavailable"):
            resolve_token("some-role", provider=_FailingProvider())

    def test_no_inherited_env_fallback(self, monkeypatch):
        """Setting an ambient env var must have zero effect -- resolve_token
        never reads os.environ directly; it only calls the provider."""
        monkeypatch.setenv(GIT_HOST_TOKEN_ENV_KEY, "ambient-should-be-ignored")

        class _FakeProvider:
            def resolve_token(self, role: str) -> str:
                return "from-provider-not-env"

        token = resolve_token("some-role", provider=_FakeProvider())
        assert token == "from-provider-not-env"


class TestResolveTokenRepoContext:
    """lr-ea28: resolve_token(role, provider, *, repo=None). Coverage:
    repo forwarded to a repo-aware provider; a legacy (pre-lr-ea28) provider
    is called with the OLD signature exactly, never breaking, even when the
    caller supplies a repo it cannot use."""

    def test_repo_forwarded_to_repo_aware_provider(self):
        class _RepoAwareProvider:
            def __init__(self):
                self.seen = None

            def resolve_token(self, role: str, *, repo: str | None = None) -> str:
                self.seen = (role, repo)
                return "tok-repo-aware"

        provider = _RepoAwareProvider()
        token = resolve_token("some-role", provider=provider, repo="some-owner/some-repo")
        assert token == "tok-repo-aware"
        assert provider.seen == ("some-role", "some-owner/some-repo")

    def test_repo_none_forwarded_to_repo_aware_provider(self):
        class _RepoAwareProvider:
            def __init__(self):
                self.seen = None

            def resolve_token(self, role: str, *, repo: str | None = None) -> str:
                self.seen = (role, repo)
                return "tok"

        provider = _RepoAwareProvider()
        resolve_token("some-role", provider=provider)
        assert provider.seen == ("some-role", None)

    def test_legacy_provider_without_repo_param_never_breaks(self):
        """The actual backward-compatibility property under test: a
        pre-lr-ea28 custom TokenProvider implementing only
        `resolve_token(self, role)` must keep working UNCHANGED, even when
        the caller supplies a repo -- the legacy provider is simply never
        told about it (it was never capable of using it)."""

        class _LegacyProvider:
            def resolve_token(self, role: str) -> str:
                return f"legacy-tok-for-{role}"

        token = resolve_token(
            "some-role", provider=_LegacyProvider(), repo="some-owner/some-repo"
        )
        assert token == "legacy-tok-for-some-role"

    def test_legacy_provider_with_kwargs_is_treated_as_repo_capable(self):
        class _KwargsProvider:
            def __init__(self):
                self.seen_repo = "unset"

            def resolve_token(self, role: str, **kwargs) -> str:
                self.seen_repo = kwargs.get("repo")
                return "tok-kwargs"

        provider = _KwargsProvider()
        resolve_token("some-role", provider=provider, repo="o/r")
        assert provider.seen_repo == "o/r"


class TestResolveTokenResult:
    """lr-43c8d7: resolve_token_result() normalizes either return shape a
    provider's own resolve_token may produce (bare str, or ResolvedToken)
    into a ResolvedToken; resolve_token() stays str-only and discards
    app_slug -- the actual zero-behavior-change property under test."""

    def test_bare_string_provider_normalizes_to_resolved_token_with_none_slug(self):
        class _BareStringProvider:
            def resolve_token(self, role: str) -> str:
                return "tok-bare"

        result = resolve_token_result("some-role", provider=_BareStringProvider())
        assert result == ResolvedToken(token="tok-bare", app_slug=None)

    def test_resolved_token_provider_passed_through_unchanged(self):
        class _StructuredProvider:
            def resolve_token(self, role: str) -> ResolvedToken:
                return ResolvedToken(token="tok-structured", app_slug="verified-slug")

        result = resolve_token_result("some-role", provider=_StructuredProvider())
        assert result.token == "tok-structured"
        assert result.app_slug == "verified-slug"

    def test_resolve_token_discards_app_slug_zero_behavior_change(self):
        """THE actual backward-compat property: a caller using the
        pre-existing resolve_token() function sees a bare str, identical
        to before this task existed, even against a provider now capable of
        returning a ResolvedToken with a real app_slug."""
        class _StructuredProvider:
            def resolve_token(self, role: str) -> ResolvedToken:
                return ResolvedToken(token="tok-structured", app_slug="verified-slug")

        token = resolve_token("some-role", provider=_StructuredProvider())
        assert token == "tok-structured"
        assert isinstance(token, str)

    def test_empty_token_in_resolved_token_is_rejected(self):
        class _EmptyStructuredProvider:
            def resolve_token(self, role: str) -> ResolvedToken:
                return ResolvedToken(token="", app_slug="some-slug")

        with pytest.raises(CredentialProviderError, match="empty token"):
            resolve_token_result("some-role", provider=_EmptyStructuredProvider())

    def test_default_provider_via_resolve_token_result_is_static(self, tmp_path, monkeypatch):
        env_file = tmp_path / "role-x.env"
        _write_env_file(env_file, f"{GIT_HOST_TOKEN_ENV_KEY}=tok-static\n")

        real_static_token_provider = StaticTokenProvider
        import clagentic_loadout.transport.credential_provider as cp
        monkeypatch.setattr(
            cp, "StaticTokenProvider",
            lambda **kw: real_static_token_provider(config_root=tmp_path),
        )

        result = resolve_token_result("role-x")
        assert result == ResolvedToken(token="tok-static", app_slug=None)


class TestCommandTokenProviderStructuredOutput:
    """lr-43c8d7: CommandTokenProvider(emit_structured_output=...) -- opt-in
    only, default False. See module docstring for the non-negotiable
    zero-behavior-change guarantee this class's default preserves."""

    def test_default_reads_bare_token_unaffected_by_json_looking_output(self):
        """A deployment that never opts in gets bare-stdout behavior even
        when the configured command happens to print something that LOOKS
        like JSON -- this class must never content-sniff to decide."""
        code = "import sys; sys.stdout.write('{\"token\": \"not-parsed\"}')"
        provider = CommandTokenProvider([_PY, "-c", code])
        # Bare-stdout read: the ENTIRE printed string is the "token",
        # including the braces -- proving no JSON parsing was attempted.
        assert provider.resolve_token("some-role") == '{"token": "not-parsed"}'

    def test_emit_structured_output_true_parses_token_and_app_slug(self):
        code = (
            "import sys; "
            "sys.stdout.write('{\"token\": \"tok-abc\", \"app_slug\": \"verified-app\"}')"
        )
        provider = CommandTokenProvider([_PY, "-c", code], emit_structured_output=True)
        result = provider.resolve_token("some-role")
        assert result == ResolvedToken(token="tok-abc", app_slug="verified-app")

    def test_emit_structured_output_true_empty_app_slug_normalizes_to_none(self):
        """The real, reachable gatekeeper case (per this task's own
        dispatch brief): a role with no App-slug binding configured mints
        successfully but reports an EMPTY app_slug, not an absent key."""
        code = (
            "import sys; "
            "sys.stdout.write('{\"token\": \"tok-abc\", \"app_slug\": \"\"}')"
        )
        provider = CommandTokenProvider([_PY, "-c", code], emit_structured_output=True)
        result = provider.resolve_token("some-role")
        assert result == ResolvedToken(token="tok-abc", app_slug=None)

    def test_emit_structured_output_true_missing_app_slug_key_is_none(self):
        code = "import sys; sys.stdout.write('{\"token\": \"tok-abc\"}')"
        provider = CommandTokenProvider([_PY, "-c", code], emit_structured_output=True)
        result = provider.resolve_token("some-role")
        assert result == ResolvedToken(token="tok-abc", app_slug=None)

    def test_emit_structured_output_true_invalid_json_fails_closed(self):
        code = "import sys; sys.stdout.write('not json at all')"
        provider = CommandTokenProvider([_PY, "-c", code], emit_structured_output=True)
        with pytest.raises(CredentialProviderError, match="not valid JSON"):
            provider.resolve_token("some-role")

    def test_emit_structured_output_true_non_object_json_fails_closed(self):
        code = "import sys; sys.stdout.write('[1, 2, 3]')"
        provider = CommandTokenProvider([_PY, "-c", code], emit_structured_output=True)
        with pytest.raises(CredentialProviderError, match="not an object"):
            provider.resolve_token("some-role")

    def test_emit_structured_output_true_missing_token_key_fails_closed(self):
        code = "import sys; sys.stdout.write('{\"app_slug\": \"x\"}')"
        provider = CommandTokenProvider([_PY, "-c", code], emit_structured_output=True)
        with pytest.raises(CredentialProviderError, match="token"):
            provider.resolve_token("some-role")

    def test_emit_structured_output_true_non_string_app_slug_fails_closed(self):
        code = "import sys; sys.stdout.write('{\"token\": \"tok-abc\", \"app_slug\": 123}')"
        provider = CommandTokenProvider([_PY, "-c", code], emit_structured_output=True)
        with pytest.raises(CredentialProviderError, match="app_slug"):
            provider.resolve_token("some-role")

    def test_resolve_token_result_forwards_app_slug_from_command_provider(self):
        code = (
            "import sys; "
            "sys.stdout.write('{\"token\": \"tok-abc\", \"app_slug\": \"verified-app\"}')"
        )
        provider = CommandTokenProvider([_PY, "-c", code], emit_structured_output=True)
        result = resolve_token_result("some-role", provider=provider)
        assert result == ResolvedToken(token="tok-abc", app_slug="verified-app")
        # Zero-behavior-change guarantee holds through the module-level
        # resolve_token() too -- str only, app_slug discarded.
        assert resolve_token("some-role", provider=provider) == "tok-abc"
