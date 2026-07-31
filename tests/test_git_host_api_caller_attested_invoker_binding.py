"""test_git_host_api_caller_attested_invoker_binding.py — lr-82c385 (tome
#700): native --caller fail-closed binding to the ATTESTED invoking
identity, in loadout's own transport (transport.git_host_api.bind_caller +
transport.attestation.resolve_identity).

This is the NEW layer (1)->(2) binding this task adds -- distinct from and
complementary to the pre-existing lr-e5eeab contract (--caller/--role is an
already-attested, opaque value consumed by transport.credential_provider /
merge.authority, layer (2)->(3), tested in test_role_invoker_mismatch.py and
test_caller_attested_value_contract.py). Those tests prove the DOWNSTREAM
seams never re-derive identity; this file proves the NEW upstream check
that an EXPLICIT --caller must equal what this process's own attestation
chain resolved, refused fail-closed BEFORE any token mint or network I/O.

Test matrix (absorbs an internal deployment's own lr-522b81 + lr-77ae43),
all in-process fakes -- no real network call anywhere in this file, no
wall-clock dependence:

  1. invoker != caller -- REJECTED fail-closed, before any token resolution
     (the injected TokenProvider is never called).
  2. invoker == caller -- proceeds normally (reaches the token/HTTP layer).
  3. omitted --caller -- unchanged (never checked against the attested
     identity at all; defaults to DEFAULT_ROLE exactly as before this task).
  4. a mismatched --caller is denied even where a NAMED-AGENT ALLOWLIST
     (a permissive TokenProvider that would happily mint for the
     mismatched role) would otherwise admit it -- proving this check runs
     independently of, and strictly before, the credential seam's own
     allow decision.

Both platforms (Forgejo relative-path GET, GitHub absolute-URL GET) are
covered, since bind_caller runs before target-platform routing/token
resolution either way and is platform-agnostic by construction.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.transport import git_host_api
from clagentic_loadout.transport import provider_config
from clagentic_loadout.transport.attestation import Identity


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    """Same isolation rationale as test_transport_git_host_api.py's own
    autouse fixture (lr-396f): bind_caller's identity resolution does not
    itself read this config tier by default in these tests (every test here
    injects identity_provider directly), but keeping this pinned is a
    conformance backstop against a future test in this file that forgets to
    inject one and would otherwise silently fall through to a real
    ~/.config/clagentic/loadout/config.yaml on this host."""
    isolated_root = tmp_path / "isolated-user-config-root"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", isolated_root)


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingTokenProvider:
    """Records every role it was asked to resolve a token for -- used to
    assert the token seam is NEVER reached on a fail-closed mismatch."""

    def __init__(self, token: str = "tok-123"):
        self.token = token
        self.calls: list[str] = []

    def resolve_token(self, role: str) -> str:
        self.calls.append(role)
        return self.token


class _PermissiveNamedAgentAllowlistTokenProvider:
    """A TokenProvider that mints for ANY role presented to it -- the shape
    of a permissive, misconfigured (or intentionally broad) named-agent
    allowlist. Used to prove bind_caller's refusal is NOT bypassed by a
    downstream seam that would otherwise happily admit the mismatched
    role."""

    def __init__(self, token: str = "tok-should-never-be-used"):
        self.token = token
        self.calls: list[str] = []

    def resolve_token(self, role: str) -> str:
        self.calls.append(role)
        return self.token


def _forgejo_get_opener(body: bytes = b'{"ok": true}'):
    def opener(req, timeout=15):
        return _FakeResponse(200, body)

    return opener


def _identity_provider(subject: str, source: str = "configured"):
    """A zero-arg callable matching the `identity_provider` injection point
    on git_host_api.main/._run -- returns a fixed Identity every call, no
    env/config/file I/O, no network."""
    identity = Identity(subject=subject, source=source)
    return lambda: identity


# ---------------------------------------------------------------------------
# (1) invoker != caller -- REJECTED fail-closed, before any token resolution
# ---------------------------------------------------------------------------


class TestExplicitCallerMismatchedWithAttestedIdentityRejected:
    def test_forgejo_relative_path_denied_before_token_resolution(self):
        tokens = _RecordingTokenProvider()
        rc = git_host_api.main(
            ["--caller", "builder", "/api/v1/repos/some-owner/some-repo/pulls/1.diff"],
            token_provider=tokens,
            opener=_forgejo_get_opener(),
            identity_provider=_identity_provider("reviewer"),
        )
        assert rc == git_host_api.EXIT_CALLER_INVOKER_MISMATCH
        # The token provider must NEVER be reached on a fail-closed mismatch
        # -- no credential is minted for a role this process did not attest.
        assert tokens.calls == []

    def test_github_absolute_url_denied_before_token_resolution(self):
        tokens = _RecordingTokenProvider()
        rc = git_host_api.main(
            [
                "--caller",
                "builder",
                "https://api.github.com/repos/some-owner/some-repo/pulls/1",
            ],
            token_provider=tokens,
            opener=_forgejo_get_opener(),
            identity_provider=_identity_provider("reviewer"),
        )
        assert rc == git_host_api.EXIT_CALLER_INVOKER_MISMATCH
        assert tokens.calls == []

    def test_no_network_call_ever_issued_on_mismatch(self):
        """Belt-and-suspenders: the injected opener itself must never be
        invoked either -- the refusal happens strictly before request()."""

        def _opener_must_not_be_called(req, timeout=15):
            raise AssertionError("opener must not be called on a fail-closed mismatch")

        rc = git_host_api.main(
            ["--caller", "builder", "/api/v1/repos/some-owner/some-repo/pulls/1.diff"],
            token_provider=_RecordingTokenProvider(),
            opener=_opener_must_not_be_called,
            identity_provider=_identity_provider("reviewer"),
        )
        assert rc == git_host_api.EXIT_CALLER_INVOKER_MISMATCH


# ---------------------------------------------------------------------------
# (2) invoker == caller -- proceeds normally
# ---------------------------------------------------------------------------


class TestExplicitCallerMatchingAttestedIdentityProceeds:
    def test_forgejo_relative_path_proceeds_to_token_and_request(self):
        tokens = _RecordingTokenProvider()
        rc = git_host_api.main(
            ["--caller", "builder", "/api/v1/repos/some-owner/some-repo/pulls/1.diff"],
            token_provider=tokens,
            opener=_forgejo_get_opener(),
            identity_provider=_identity_provider("builder"),
        )
        assert rc == git_host_api.EXIT_OK
        assert tokens.calls == ["builder"]

    def test_github_absolute_url_proceeds_to_token_and_request(self):
        tokens = _RecordingTokenProvider()
        rc = git_host_api.main(
            [
                "--caller",
                "builder",
                "https://api.github.com/repos/some-owner/some-repo/pulls/1",
            ],
            token_provider=tokens,
            opener=_forgejo_get_opener(),
            identity_provider=_identity_provider("builder"),
        )
        assert rc == git_host_api.EXIT_OK
        assert tokens.calls == ["builder"]


# ---------------------------------------------------------------------------
# (3) omitted --caller -- unchanged: never checked against attested identity
# ---------------------------------------------------------------------------


class TestOmittedCallerUnchanged:
    def test_omitted_caller_never_compared_to_attested_identity(self):
        """No --caller at all defaults to DEFAULT_ROLE and proceeds exactly
        as before this task, REGARDLESS of what the attested identity
        resolves to -- an omitted --caller carries no identity claim for
        bind_caller to check."""
        tokens = _RecordingTokenProvider()
        rc = git_host_api.main(
            ["/api/v1/repos/some-owner/some-repo/pulls/1.diff"],
            token_provider=tokens,
            opener=_forgejo_get_opener(),
            # Attested identity is deliberately something that would NOT
            # match DEFAULT_ROLE -- proving the omitted-caller path never
            # even reaches the comparison.
            identity_provider=_identity_provider("someone-else-entirely"),
        )
        assert rc == git_host_api.EXIT_OK
        from clagentic_loadout.transport.credential_provider import DEFAULT_ROLE

        assert tokens.calls == [DEFAULT_ROLE]

    def test_omitted_caller_identity_provider_never_even_consulted_is_not_required(self):
        """Documents the actual contract precisely: resolve_identity() IS
        still called (bind_caller's own no-op-on-omitted-caller path runs
        after resolution, not instead of it) -- what's asserted is that its
        result is never COMPARED against anything on the omitted-caller
        path, which the prior test already proves via a deliberately
        non-matching identity still reaching EXIT_OK."""
        calls = {"count": 0}

        def counting_identity_provider():
            calls["count"] += 1
            return Identity(subject="whatever", source="configured")

        rc = git_host_api.main(
            ["/api/v1/repos/some-owner/some-repo/pulls/1.diff"],
            token_provider=_RecordingTokenProvider(),
            opener=_forgejo_get_opener(),
            identity_provider=counting_identity_provider,
        )
        assert rc == git_host_api.EXIT_OK
        assert calls["count"] == 1


# ---------------------------------------------------------------------------
# (4) mismatched --caller denied even where a named-agent allowlist (a
# permissive downstream TokenProvider) would admit it
# ---------------------------------------------------------------------------


class TestMismatchDeniedEvenWhenDownstreamAllowlistWouldAdmit:
    def test_permissive_token_provider_never_reached_on_mismatch(self):
        """The credential seam here is configured to mint for ANY role --
        exactly the shape of a permissive/misconfigured named-agent
        allowlist. bind_caller's refusal must still fire first: this proves
        the attested-invoker binding is not merely one input a downstream
        allow decision could override, but an independent gate upstream of
        it."""
        permissive_tokens = _PermissiveNamedAgentAllowlistTokenProvider()
        rc = git_host_api.main(
            ["--caller", "intern", "/api/v1/repos/some-owner/some-repo/pulls/1.diff"],
            token_provider=permissive_tokens,
            opener=_forgejo_get_opener(),
            identity_provider=_identity_provider("builder"),
        )
        assert rc == git_host_api.EXIT_CALLER_INVOKER_MISMATCH
        assert permissive_tokens.calls == []

    def test_permissive_provider_still_works_when_caller_matches_identity(self):
        """Sanity check on the fixture itself: the SAME permissive provider
        does successfully mint when --caller matches the attested identity
        -- proving the denial above is bind_caller's own refusal, not an
        artifact of the fixture being broken."""
        permissive_tokens = _PermissiveNamedAgentAllowlistTokenProvider()
        rc = git_host_api.main(
            ["--caller", "builder", "/api/v1/repos/some-owner/some-repo/pulls/1.diff"],
            token_provider=permissive_tokens,
            opener=_forgejo_get_opener(),
            identity_provider=_identity_provider("builder"),
        )
        assert rc == git_host_api.EXIT_OK
        assert permissive_tokens.calls == ["builder"]


# ---------------------------------------------------------------------------
# bind_caller() unit-level coverage (no CLI/argv layer, no I/O at all)
# ---------------------------------------------------------------------------


class TestBindCallerUnit:
    def test_matching_caller_no_raise(self):
        git_host_api.bind_caller(
            "builder", caller_explicit=True, identity=Identity("builder", "builtin")
        )  # no raise

    def test_mismatched_caller_raises_with_exact_exit_code(self):
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.bind_caller(
                "builder", caller_explicit=True, identity=Identity("reviewer", "builtin")
            )
        assert exc_info.value.code == git_host_api.EXIT_CALLER_INVOKER_MISMATCH

    def test_omitted_caller_never_raises_regardless_of_identity(self):
        git_host_api.bind_caller(
            "release-dispatcher",
            caller_explicit=False,
            identity=Identity("totally-different-identity", "builtin"),
        )  # no raise -- caller_explicit=False short-circuits unconditionally


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
