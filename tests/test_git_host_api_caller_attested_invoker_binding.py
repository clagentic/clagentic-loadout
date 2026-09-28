"""test_git_host_api_caller_attested_invoker_binding.py — lr-82c385 (tome
#700): native --caller fail-closed binding to the ATTESTED invoking
identity, in loadout's own transport (transport.git_host_api.bind_caller +
transport.attestation.resolve_bound_identity).

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
  3. OMITTED --caller is POLICY-GATED (lr-620837 fold-in #4, F6): under
     the default `attestation.bound_identity: builtin-fallback` policy, an
     omitted --caller behaves exactly as originally released -- DEFAULT_ROLE,
     no attestation attempted, no refusal possible (see
     TestOmittedCallerUnderDefaultBuiltinFallbackPolicy below). Under a
     deployment-opted-in `required` policy, the effective caller becomes
     the resolved identity's own subject, and a process with no attested
     identity at all is refused exactly like an explicit mismatch always
     was (see TestOmittedCallerUnderRequiredPolicy below).
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

from clagentic_loadout.transport import attestation, git_host_api
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
    ~/.config/clagentic/loadout/config.yaml on this host. Patches BOTH
    `provider_config`'s copy of the name AND `attestation`'s own
    independently-bound copy (lr-620837 fold-in #4: an omitted --caller now
    reads `attestation.bound_identity` policy via `resolve_bound_identity_
    policy`, which resolves against `attestation.DEFAULT_USER_CONFIG_ROOT`,
    not `provider_config`'s -- see tests/conftest.py's own
    `_isolate_real_attestation_chain` docstring for the same two-copies
    caveat this mirrors)."""
    isolated_root = tmp_path / "isolated-user-config-root"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", isolated_root)
    monkeypatch.setattr(attestation, "DEFAULT_USER_CONFIG_ROOT", isolated_root)


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
# (3) omitted --caller IS NOW ALSO bound to the attested identity
# (BEHAVIOR CHANGE, lr-620837 operator ruling, PEACHES finding 5876767198)
# ---------------------------------------------------------------------------


class TestOmittedCallerUnderDefaultBuiltinFallbackPolicy:
    """lr-620837 fold-in #4 (F6, correcting fold-in #2's own first
    revision): under the DEFAULT `attestation.bound_identity:
    builtin-fallback` policy (no config written at all, matching every
    test in this class -- see `_isolate_user_config_root` above), an
    omitted --caller behaves EXACTLY as this package originally released
    it, BEFORE lr-620837 existed at all: the effective caller is
    DEFAULT_ROLE, no identity resolution is attempted, and no refusal is
    possible. This supersedes fold-in #2's OWN first-revision tests (which
    asserted resolution ran unconditionally on the omitted path regardless
    of policy) -- those asserted exactly the regression this fold-in
    fixes, not a behavior worth preserving."""

    def test_omitted_caller_proceeds_as_default_role_identity_provider_never_consulted(self):
        """No --caller at all, under the default policy: the injected
        identity_provider is NEVER called (resolve_for_binding short-
        circuits before it), and the token mint proceeds for DEFAULT_ROLE
        -- never the (unconsulted) injected identity's subject."""
        from clagentic_loadout.transport.credential_provider import DEFAULT_ROLE

        calls = {"count": 0}

        def unreachable_identity_provider():
            calls["count"] += 1
            return Identity(subject="synthetic-attested-subject", source="configured")

        tokens = _RecordingTokenProvider()
        rc = git_host_api.main(
            ["/api/v1/repos/some-owner/some-repo/pulls/1.diff"],
            token_provider=tokens,
            opener=_forgejo_get_opener(),
            identity_provider=unreachable_identity_provider,
        )
        assert rc == git_host_api.EXIT_OK
        assert tokens.calls == [DEFAULT_ROLE]
        assert calls["count"] == 0


class TestOmittedCallerUnderRequiredPolicy:
    """The `required`-policy half of the SAME lr-620837 fold-in #4 (F6)
    contract: a deployment that has explicitly opted into
    `attestation.bound_identity: required` (written to the isolated user
    config root here) gets fold-in #2's original fail-closed omitted-
    caller behavior -- the effective caller becomes the resolved
    identity's own subject, and a process with no attested identity at all
    REFUSES on the omitted path too, exactly the fail-closed posture an
    explicit mismatched --caller always had."""

    @staticmethod
    def _write_required_policy_config(tmp_path, monkeypatch) -> None:
        isolated_root = tmp_path / "isolated-user-config-root"
        isolated_root.mkdir(exist_ok=True)
        (isolated_root / "config.yaml").write_text(
            "attestation:\n  bound_identity: required\n", encoding="utf-8"
        )
        monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", isolated_root)
        monkeypatch.setattr(attestation, "DEFAULT_USER_CONFIG_ROOT", isolated_root)

    def test_omitted_caller_proceeds_as_the_resolved_identity_subject(self, tmp_path, monkeypatch):
        """No --caller at all resolves identity via the injected provider
        (a synthetic attested identity, never a no-resolve path) and mints
        a token for THAT identity's own subject -- never DEFAULT_ROLE."""
        self._write_required_policy_config(tmp_path, monkeypatch)
        tokens = _RecordingTokenProvider()
        rc = git_host_api.main(
            ["/api/v1/repos/some-owner/some-repo/pulls/1.diff"],
            token_provider=tokens,
            opener=_forgejo_get_opener(),
            identity_provider=_identity_provider("synthetic-attested-subject"),
        )
        assert rc == git_host_api.EXIT_OK
        assert tokens.calls == ["synthetic-attested-subject"]

    def test_omitted_caller_identity_provider_is_consulted_under_required(
        self, tmp_path, monkeypatch
    ):
        """Under `required`, resolution runs on the omitted path exactly
        like the explicit path -- the identity_provider injection point (a
        synthetic attested identity here, never real env/config/file I/O)
        is called exactly once."""
        self._write_required_policy_config(tmp_path, monkeypatch)
        calls = {"count": 0}

        def counting_identity_provider():
            calls["count"] += 1
            return Identity(subject="synthetic-whoever", source="configured")

        rc = git_host_api.main(
            ["/api/v1/repos/some-owner/some-repo/pulls/1.diff"],
            token_provider=_RecordingTokenProvider(),
            opener=_forgejo_get_opener(),
            identity_provider=counting_identity_provider,
        )
        assert rc == git_host_api.EXIT_OK
        assert calls["count"] == 1

    def test_omitted_caller_with_no_attested_identity_refuses(self, tmp_path, monkeypatch):
        """No sidecar, no configured provider (a synthetic
        BoundAttestationError-raising identity_provider standing in for
        `resolve_bound_identity`'s own "no attested identity" refusal) --
        the omitted-caller path REFUSES under `required`, never silently
        proceeding as DEFAULT_ROLE with no attestation behind it. This is
        the exact vanilla-root-shell-with-no-sidecar acceptance case from
        the operator ruling (lr-620837 comment #5), scoped to the policy a
        deployment opts into for it."""
        self._write_required_policy_config(tmp_path, monkeypatch)
        from clagentic_loadout.transport.attestation import BoundAttestationError

        def refusing_identity_provider():
            raise BoundAttestationError(
                "attestation FAILED -- no attested identity (synthetic, test-injected).",
                expected_source="sidecar-session",
            )

        tokens = _RecordingTokenProvider()
        rc = git_host_api.main(
            ["/api/v1/repos/some-owner/some-repo/pulls/1.diff"],
            token_provider=tokens,
            opener=_forgejo_get_opener(),
            identity_provider=refusing_identity_provider,
        )
        assert rc == git_host_api.EXIT_CALLER_INVOKER_MISMATCH
        # No token is ever minted for an unattested process, omitted
        # --caller or not.
        assert tokens.calls == []


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

    def test_caller_explicit_no_longer_changes_comparison_behavior(self):
        """SUPERSEDES the pre-fix "caller_explicit=False short-circuits
        unconditionally" contract (lr-620837 operator ruling): bind_caller
        no longer branches on caller_explicit at all -- it always compares
        caller to identity.subject. `caller_explicit=False` with a
        mismatched caller/identity pair (a shape no real call site produces
        anymore, since every call site now derives `caller` from
        `identity.subject` on the omitted path -- see transport.
        caller_binding.resolve_for_binding's own docstring) still raises,
        proving there is no remaining special case for the flag value
        itself."""
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.bind_caller(
                "release-dispatcher",
                caller_explicit=False,
                identity=Identity("totally-different-identity", "builtin"),
            )
        assert exc_info.value.code == git_host_api.EXIT_CALLER_INVOKER_MISMATCH

    def test_omitted_caller_derived_from_identity_subject_no_raise(self):
        """The REAL omitted-caller shape every call site now produces:
        caller == identity.subject BY CONSTRUCTION (the call site derives
        it that way), so this never raises -- matching TestOmittedCaller
        BoundToIdentity's CLI-level proof in
        test_git_host_api_caller_attested_invoker_binding.py."""
        git_host_api.bind_caller(
            "resolved-attested-subject",
            caller_explicit=True,
            identity=Identity("resolved-attested-subject", "sidecar-session"),
        )  # no raise -- caller equals identity.subject by construction


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
