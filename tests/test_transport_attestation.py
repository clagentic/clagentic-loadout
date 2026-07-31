"""test_transport_attestation.py — tests for
clagentic_loadout.transport.attestation (lr-82c385, tome #700).

Coverage:
  - Resolution order: configured provider > sidecar adapter > built-in
    OS-user fallback -- each layer's precedence over the next, and correct
    fall-through when a higher layer declines (unset env var, unconfigured
    section, missing/unreadable/empty sidecar file).
  - Env-var tier wins over the config-file tier for BOTH the configured
    provider's identity_env name and the sidecar's path (mirrors
    transport.provider_config's own env-over-config-file precedence).
  - Every layer failing except the built-in fallback still resolves (proves
    "a bare install still has an attested source").
  - AttestationError only when literally nothing resolves, including the
    built-in fallback -- exercised via a fully injected provider chain (no
    real getpass.getuser() call, no wall-clock, no network).
  - `providers=` full-chain injection point used by every test here so
    resolve_identity is exercised deterministically without touching this
    host's real environment, real config root, or real OS user.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.transport import attestation


class _FixedProvider:
    def __init__(self, identity: "attestation.Identity | None"):
        self._identity = identity

    def resolve(self):
        return self._identity


class TestResolveIdentityChainOrder:
    def test_first_provider_wins_when_it_resolves(self):
        identity = attestation.resolve_identity(
            providers=[
                _FixedProvider(attestation.Identity("configured-value", attestation.SOURCE_CONFIGURED)),
                _FixedProvider(attestation.Identity("sidecar-value", attestation.SOURCE_SIDECAR)),
                _FixedProvider(attestation.Identity("builtin-value", attestation.SOURCE_BUILTIN)),
            ]
        )
        assert identity == attestation.Identity("configured-value", attestation.SOURCE_CONFIGURED)

    def test_falls_through_to_second_provider_when_first_declines(self):
        identity = attestation.resolve_identity(
            providers=[
                _FixedProvider(None),
                _FixedProvider(attestation.Identity("sidecar-value", attestation.SOURCE_SIDECAR)),
                _FixedProvider(attestation.Identity("builtin-value", attestation.SOURCE_BUILTIN)),
            ]
        )
        assert identity == attestation.Identity("sidecar-value", attestation.SOURCE_SIDECAR)

    def test_falls_through_to_builtin_when_first_two_decline(self):
        identity = attestation.resolve_identity(
            providers=[
                _FixedProvider(None),
                _FixedProvider(None),
                _FixedProvider(attestation.Identity("builtin-value", attestation.SOURCE_BUILTIN)),
            ]
        )
        assert identity == attestation.Identity("builtin-value", attestation.SOURCE_BUILTIN)

    def test_every_provider_declining_raises_attestation_error(self):
        with pytest.raises(attestation.AttestationError):
            attestation.resolve_identity(
                providers=[_FixedProvider(None), _FixedProvider(None), _FixedProvider(None)]
            )


class TestConfiguredEnvProviderLayer:
    def test_env_var_names_another_env_var_carrying_the_identity(self):
        env = {
            attestation.ATTESTED_IDENTITY_ENV_VAR: "MY_SPAWN_IDENTITY_VAR",
            "MY_SPAWN_IDENTITY_VAR": "resolved-subject",
        }
        identity = attestation.resolve_identity(env=env, config_root="/nonexistent")
        assert identity == attestation.Identity("resolved-subject", attestation.SOURCE_CONFIGURED)

    def test_named_env_var_unset_falls_through_to_builtin(self, monkeypatch):
        # The NAME is configured, but the var it points at is not present at
        # all -- this layer declines, falling through to builtin (getpass).
        monkeypatch.setattr(attestation.getpass, "getuser", lambda: "os-user-fallback")
        env = {attestation.ATTESTED_IDENTITY_ENV_VAR: "UNSET_IDENTITY_VAR"}
        identity = attestation.resolve_identity(env=env, config_root="/nonexistent")
        assert identity == attestation.Identity("os-user-fallback", attestation.SOURCE_BUILTIN)

    def test_named_env_var_present_but_empty_falls_through(self, monkeypatch):
        monkeypatch.setattr(attestation.getpass, "getuser", lambda: "os-user-fallback")
        env = {
            attestation.ATTESTED_IDENTITY_ENV_VAR: "EMPTY_IDENTITY_VAR",
            "EMPTY_IDENTITY_VAR": "   ",
        }
        identity = attestation.resolve_identity(env=env, config_root="/nonexistent")
        assert identity == attestation.Identity("os-user-fallback", attestation.SOURCE_BUILTIN)

    def test_config_file_tier_used_when_env_var_name_absent(self, tmp_path):
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "attestation:\n  identity_env: CONFIG_NAMED_IDENTITY_VAR\n",
            encoding="utf-8",
        )
        env = {"CONFIG_NAMED_IDENTITY_VAR": "from-config-file-tier"}
        identity = attestation.resolve_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity("from-config-file-tier", attestation.SOURCE_CONFIGURED)

    def test_env_var_name_wins_over_config_file_name(self, tmp_path):
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "attestation:\n  identity_env: CONFIG_NAMED_IDENTITY_VAR\n",
            encoding="utf-8",
        )
        env = {
            attestation.ATTESTED_IDENTITY_ENV_VAR: "ENV_NAMED_IDENTITY_VAR",
            "ENV_NAMED_IDENTITY_VAR": "from-env-tier",
            "CONFIG_NAMED_IDENTITY_VAR": "from-config-file-tier-should-be-ignored",
        }
        identity = attestation.resolve_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity("from-env-tier", attestation.SOURCE_CONFIGURED)


class TestSidecarFileProviderLayer:
    def test_sidecar_path_env_var_read_and_stripped(self, tmp_path):
        sidecar = tmp_path / "identity.txt"
        sidecar.write_text("sidecar-subject\n", encoding="utf-8")
        env = {attestation.ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR: str(sidecar)}
        identity = attestation.resolve_identity(env=env, config_root="/nonexistent")
        assert identity == attestation.Identity("sidecar-subject", attestation.SOURCE_SIDECAR)

    def test_missing_sidecar_file_fails_closed_not_falls_through(self, tmp_path, monkeypatch):
        """lr-1e16a4: source (a) -- the env-var single-path override -- is
        an EXPLICIT per-invocation identity claim. When the env var is SET
        to a path but that file is absent, the chain must fail CLOSED
        (AttestationError) rather than silently falling through to a
        lower-precedence source (config sidecar path, session-keyed
        adapter list, or the built-in OS-user fallback) -- any of which
        could resolve a DIFFERENT identity than the one this invocation
        explicitly pointed at. `getpass.getuser` is monkeypatched to a
        sentinel to prove the built-in fallback is NEVER reached."""
        monkeypatch.setattr(
            attestation.getpass,
            "getuser",
            lambda: (_ for _ in ()).throw(
                AssertionError("built-in fallback must not be reached on source-(a) MISS")
            ),
        )
        missing = tmp_path / "does-not-exist.txt"
        env = {attestation.ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR: str(missing)}
        with pytest.raises(attestation.AttestationError):
            attestation.resolve_identity(env=env, config_root="/nonexistent")

    def test_empty_sidecar_file_fails_closed_not_falls_through(self, tmp_path, monkeypatch):
        """lr-1e16a4: same fail-closed treatment as a missing file -- an
        explicitly-requested source-(a) file that exists but is empty is
        still a MISS, not a plain decline."""
        monkeypatch.setattr(
            attestation.getpass,
            "getuser",
            lambda: (_ for _ in ()).throw(
                AssertionError("built-in fallback must not be reached on source-(a) MISS")
            ),
        )
        sidecar = tmp_path / "empty.txt"
        sidecar.write_text("   \n", encoding="utf-8")
        env = {attestation.ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR: str(sidecar)}
        with pytest.raises(attestation.AttestationError):
            attestation.resolve_identity(env=env, config_root="/nonexistent")

    def test_configured_layer_wins_over_sidecar_when_both_present(self, tmp_path):
        sidecar = tmp_path / "identity.txt"
        sidecar.write_text("sidecar-subject", encoding="utf-8")
        env = {
            attestation.ATTESTED_IDENTITY_ENV_VAR: "PRIMARY_IDENTITY_VAR",
            "PRIMARY_IDENTITY_VAR": "configured-subject",
            attestation.ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR: str(sidecar),
        }
        identity = attestation.resolve_identity(env=env, config_root="/nonexistent")
        assert identity == attestation.Identity("configured-subject", attestation.SOURCE_CONFIGURED)

    def test_config_file_tier_names_sidecar_path(self, tmp_path):
        sidecar = tmp_path / "identity.txt"
        sidecar.write_text("sidecar-from-config-tier", encoding="utf-8")
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            f"attestation:\n  identity_sidecar_path: {sidecar}\n",
            encoding="utf-8",
        )
        identity = attestation.resolve_identity(env={}, config_root=config_root)
        assert identity == attestation.Identity(
            "sidecar-from-config-tier", attestation.SOURCE_SIDECAR
        )

    # -----------------------------------------------------------------
    # Symlink refusal (BOBBIE security finding, PR #97 comment 14519):
    # the resolved sidecar path is lstat'd BEFORE any read, and a symlink
    # (or any other non-regular directory entry) is refused with a hard
    # AttestationError rather than silently followed or treated as a
    # benign decline-and-fall-through. Mirrors clagentic-gatekeeper's
    # internal/attestation/sidecar_test.go
    # TestSidecarProvider_SymlinkInDir_Refuses.
    # -----------------------------------------------------------------

    def test_symlinked_sidecar_path_refused_not_followed(self, tmp_path):
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        secret_path = outside_dir / "secret"
        secret_path.write_text("attacker-controlled-identity", encoding="utf-8")

        sidecar_dir = tmp_path / "sidecar-dir"
        sidecar_dir.mkdir()
        link_path = sidecar_dir / "identity.txt"
        link_path.symlink_to(secret_path)

        env = {attestation.ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR: str(link_path)}
        with pytest.raises(attestation.AttestationError) as exc_info:
            attestation.resolve_identity(env=env, config_root="/nonexistent")
        # The refusal must be a hard failure, not a fall-through: no
        # identity is ever returned, and the message never echoes the
        # linked file's content.
        assert "attacker-controlled-identity" not in str(exc_info.value)

    def test_symlinked_sidecar_path_never_falls_through_to_builtin(self, tmp_path, monkeypatch):
        """Belt-and-suspenders: a planted symlink must not be quietly
        demoted to "sidecar merely unconfigured" and fall through to the
        built-in OS-user layer -- that would let an attacker who can plant
        a symlink also silently downgrade the check."""
        monkeypatch.setattr(attestation.getpass, "getuser", lambda: "should-never-be-reached")
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        secret_path = outside_dir / "secret"
        secret_path.write_text("attacker-controlled-identity", encoding="utf-8")

        sidecar_dir = tmp_path / "sidecar-dir"
        sidecar_dir.mkdir()
        link_path = sidecar_dir / "identity.txt"
        link_path.symlink_to(secret_path)

        env = {attestation.ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR: str(link_path)}
        with pytest.raises(attestation.AttestationError):
            attestation.resolve_identity(env=env, config_root="/nonexistent")

    def test_non_regular_file_at_sidecar_path_refused(self, tmp_path):
        """A directory at the configured path (any non-regular directory
        entry, not just a symlink) is refused the same way."""
        directory_path = tmp_path / "identity-dir"
        directory_path.mkdir()
        env = {attestation.ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR: str(directory_path)}
        with pytest.raises(attestation.AttestationError):
            attestation.resolve_identity(env=env, config_root="/nonexistent")

    # -----------------------------------------------------------------
    # Atomic O_NOFOLLOW read (lr-904b1d): closes the residual TOCTOU
    # between the old lstat() check and the separate read_text() open --
    # both the check and the read now happen against the SAME file
    # descriptor. Coverage below exercises the atomic-read success path,
    # the O_NOFOLLOW symlink refusal (same AttestationError as the
    # pre-existing non-regular-file refusal), and that absent/empty file
    # semantics are unchanged.
    # -----------------------------------------------------------------

    def test_atomic_read_success(self, tmp_path):
        sidecar = tmp_path / "identity.txt"
        sidecar.write_text("atomic-subject\n", encoding="utf-8")
        env = {attestation.ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR: str(sidecar)}
        identity = attestation.resolve_identity(env=env, config_root="/nonexistent")
        assert identity == attestation.Identity("atomic-subject", attestation.SOURCE_SIDECAR)

    def test_o_nofollow_symlink_refusal_raises_attestation_error(self, tmp_path):
        """O_NOFOLLOW refuses the open itself (ELOOP) rather than a
        separate lstat check -- must map to the same AttestationError as
        the non-regular-file refusal, preserving symlink hardening."""
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        secret_path = outside_dir / "secret"
        secret_path.write_text("attacker-controlled-identity", encoding="utf-8")

        sidecar_dir = tmp_path / "sidecar-dir"
        sidecar_dir.mkdir()
        link_path = sidecar_dir / "identity.txt"
        link_path.symlink_to(secret_path)

        env = {attestation.ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR: str(link_path)}
        with pytest.raises(attestation.AttestationError) as exc_info:
            attestation.resolve_identity(env=env, config_root="/nonexistent")
        assert "attacker-controlled-identity" not in str(exc_info.value)

    def test_absent_file_fails_closed_under_atomic_open(self, tmp_path, monkeypatch):
        """lr-1e16a4: a genuinely-absent source-(a) sidecar path is a HARD
        FAILURE under the atomic-open implementation -- superseding the
        pre-lr-1e16a4 "plain decline, falls through" contract for THIS
        source specifically (see TestSidecarFileProviderLayer's own
        fail-closed tests above for the full rationale; this class
        exercises the same invariant against the atomic-read code path)."""
        monkeypatch.setattr(
            attestation.getpass,
            "getuser",
            lambda: (_ for _ in ()).throw(
                AssertionError("built-in fallback must not be reached on source-(a) MISS")
            ),
        )
        missing = tmp_path / "still-does-not-exist.txt"
        env = {attestation.ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR: str(missing)}
        with pytest.raises(attestation.AttestationError):
            attestation.resolve_identity(env=env, config_root="/nonexistent")

    def test_blank_content_fails_closed_under_atomic_read(self, tmp_path, monkeypatch):
        """lr-1e16a4: blank/whitespace-only content at an explicitly
        requested source-(a) path is the same MISS as an absent file."""
        monkeypatch.setattr(
            attestation.getpass,
            "getuser",
            lambda: (_ for _ in ()).throw(
                AssertionError("built-in fallback must not be reached on source-(a) MISS")
            ),
        )
        sidecar = tmp_path / "blank.txt"
        sidecar.write_text("\n\n   \n", encoding="utf-8")
        env = {attestation.ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR: str(sidecar)}
        with pytest.raises(attestation.AttestationError):
            attestation.resolve_identity(env=env, config_root="/nonexistent")


class TestSidecarAdapterListLayer:
    """Coverage for the NEW `attestation.sidecars` ordered adapter list
    (lr-8e1593) -- session-keyed sidecar discovery via
    `{dir, file_prefix, session_id_env}` entries, tried in declared order
    AFTER the two existing single-path overrides. Mirrors
    clagentic-gatekeeper's Go reference adapter list
    (`internal/attestation/sidecar.go`, epic lr-0029bf); acceptance
    criteria from lr-8e1593 comment #1.
    """

    def _config_with_sidecars(self, tmp_path, sidecars: list[dict]):
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "attestation:\n  sidecars:\n"
            + "".join(
                "".join(
                    [
                        f"    - dir: {entry['dir']}\n",
                        f"      file_prefix: {entry['file_prefix']}\n",
                        f"      session_id_env: {entry['session_id_env']}\n",
                    ]
                )
                for entry in sidecars
            ),
            encoding="utf-8",
        )
        return config_root

    def test_adapter_precedence_spawn_beats_session(self, tmp_path):
        """Acceptance (a): sidecars=[spawn-adapter, session-adapter] --
        with BOTH session ids present in env, the FIRST declared adapter
        (spawn) wins."""
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "spawn-abc123").write_text("spawn-identity", encoding="utf-8")
        (sidecar_dir / "session-xyz789").write_text("session-identity", encoding="utf-8")

        config_root = self._config_with_sidecars(
            tmp_path,
            [
                {"dir": sidecar_dir, "file_prefix": "spawn-", "session_id_env": "SPAWN_ID"},
                {"dir": sidecar_dir, "file_prefix": "session-", "session_id_env": "SESSION_ID"},
            ],
        )
        env = {"SPAWN_ID": "abc123", "SESSION_ID": "xyz789"}
        identity = attestation.resolve_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity("spawn-identity", attestation.SOURCE_SIDECAR)

    def test_only_session_id_env_present_resolves_session_adapter(self, tmp_path):
        """Acceptance (a): a process env carrying ONLY the session id env
        (the spawn adapter's session_id_env is unset) resolves the
        session sidecar identity -- the spawn adapter is skipped, not an
        error."""
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "session-xyz789").write_text("session-identity", encoding="utf-8")

        config_root = self._config_with_sidecars(
            tmp_path,
            [
                {"dir": sidecar_dir, "file_prefix": "spawn-", "session_id_env": "SPAWN_ID"},
                {"dir": sidecar_dir, "file_prefix": "session-", "session_id_env": "SESSION_ID"},
            ],
        )
        env = {"SESSION_ID": "xyz789"}
        identity = attestation.resolve_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity("session-identity", attestation.SOURCE_SIDECAR)

    def test_two_concurrent_different_identity_spawn_sidecars(self, tmp_path):
        """Two DIFFERENT invocations (different SESSION_ID env values,
        simulating two concurrently-running sessions) each resolve their
        OWN sidecar file via the same adapter config -- proving session-
        keyed discovery actually distinguishes between sessions rather
        than always landing on one fixed file."""
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "spawn-session-one").write_text("identity-one", encoding="utf-8")
        (sidecar_dir / "spawn-session-two").write_text("identity-two", encoding="utf-8")

        config_root = self._config_with_sidecars(
            tmp_path,
            [{"dir": sidecar_dir, "file_prefix": "spawn-", "session_id_env": "SESSION_ID"}],
        )

        identity_one = attestation.resolve_identity(
            env={"SESSION_ID": "session-one"}, config_root=config_root
        )
        identity_two = attestation.resolve_identity(
            env={"SESSION_ID": "session-two"}, config_root=config_root
        )
        assert identity_one == attestation.Identity("identity-one", attestation.SOURCE_SIDECAR)
        assert identity_two == attestation.Identity("identity-two", attestation.SOURCE_SIDECAR)

    def test_env_single_path_override_beats_sidecars_list(self, tmp_path):
        """Acceptance (a): the env single-path override
        (ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR) beats BOTH sidecars-list
        adapters, even when an adapter would otherwise resolve."""
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "spawn-abc123").write_text("adapter-identity", encoding="utf-8")

        override_path = tmp_path / "override-identity.txt"
        override_path.write_text("env-override-identity", encoding="utf-8")

        config_root = self._config_with_sidecars(
            tmp_path,
            [{"dir": sidecar_dir, "file_prefix": "spawn-", "session_id_env": "SPAWN_ID"}],
        )
        env = {
            attestation.ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR: str(override_path),
            "SPAWN_ID": "abc123",
        }
        identity = attestation.resolve_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity("env-override-identity", attestation.SOURCE_SIDECAR)

    def test_config_single_path_beats_sidecars_list(self, tmp_path):
        """The RETAINED config `identity_sidecar_path` single-path key
        also beats the sidecars list -- (b) precedes (c) within layer 2."""
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "spawn-abc123").write_text("adapter-identity", encoding="utf-8")

        single_path = tmp_path / "single-identity.txt"
        single_path.write_text("single-path-identity", encoding="utf-8")

        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "attestation:\n"
            f"  identity_sidecar_path: {single_path}\n"
            "  sidecars:\n"
            f"    - dir: {sidecar_dir}\n"
            "      file_prefix: spawn-\n"
            "      session_id_env: SPAWN_ID\n",
            encoding="utf-8",
        )
        env = {"SPAWN_ID": "abc123"}
        identity = attestation.resolve_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity("single-path-identity", attestation.SOURCE_SIDECAR)

    def test_missing_session_id_env_skips_adapter_falls_through_to_builtin(
        self, tmp_path, monkeypatch
    ):
        """Acceptance: an adapter whose session_id_env is entirely unset
        (not just empty) is skipped, not an error -- chain falls through
        to builtin when no other adapter/layer resolves."""
        monkeypatch.setattr(attestation.getpass, "getuser", lambda: "os-user-fallback")
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        config_root = self._config_with_sidecars(
            tmp_path,
            [{"dir": sidecar_dir, "file_prefix": "spawn-", "session_id_env": "SPAWN_ID"}],
        )
        identity = attestation.resolve_identity(env={}, config_root=config_root)
        assert identity == attestation.Identity("os-user-fallback", attestation.SOURCE_BUILTIN)

    def test_empty_session_id_env_skips_adapter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(attestation.getpass, "getuser", lambda: "os-user-fallback")
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        config_root = self._config_with_sidecars(
            tmp_path,
            [{"dir": sidecar_dir, "file_prefix": "spawn-", "session_id_env": "SPAWN_ID"}],
        )
        identity = attestation.resolve_identity(
            env={"SPAWN_ID": "   "}, config_root=config_root
        )
        assert identity == attestation.Identity("os-user-fallback", attestation.SOURCE_BUILTIN)

    def test_missing_composed_file_skips_adapter_falls_through(self, tmp_path, monkeypatch):
        """The session id env resolves, but the composed file does not
        exist -- decline (not an error), fall through to the next
        adapter/layer."""
        monkeypatch.setattr(attestation.getpass, "getuser", lambda: "os-user-fallback")
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        config_root = self._config_with_sidecars(
            tmp_path,
            [{"dir": sidecar_dir, "file_prefix": "spawn-", "session_id_env": "SPAWN_ID"}],
        )
        identity = attestation.resolve_identity(
            env={"SPAWN_ID": "no-such-session"}, config_root=config_root
        )
        assert identity == attestation.Identity("os-user-fallback", attestation.SOURCE_BUILTIN)

    def test_unsafe_session_id_with_path_separator_refused_not_sanitized(
        self, tmp_path, monkeypatch
    ):
        """A session id value containing a path separator is REFUSED for
        that adapter (decline, fall through) -- never sanitized/joined
        anyway. Proves an attacker-controlled session-id env var cannot
        redirect the composed read outside `dir`."""
        monkeypatch.setattr(attestation.getpass, "getuser", lambda: "os-user-fallback")
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "secret").write_text("attacker-identity", encoding="utf-8")

        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        config_root = self._config_with_sidecars(
            tmp_path,
            [{"dir": sidecar_dir, "file_prefix": "spawn-", "session_id_env": "SPAWN_ID"}],
        )
        identity = attestation.resolve_identity(
            env={"SPAWN_ID": "../outside/secret"}, config_root=config_root
        )
        assert identity == attestation.Identity("os-user-fallback", attestation.SOURCE_BUILTIN)

    def test_unsafe_session_id_dotdot_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(attestation.getpass, "getuser", lambda: "os-user-fallback")
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        config_root = self._config_with_sidecars(
            tmp_path,
            [{"dir": sidecar_dir, "file_prefix": "spawn-", "session_id_env": "SPAWN_ID"}],
        )
        identity = attestation.resolve_identity(env={"SPAWN_ID": ".."}, config_root=config_root)
        assert identity == attestation.Identity("os-user-fallback", attestation.SOURCE_BUILTIN)

    def test_symlink_at_composed_adapter_path_refused_per_adapter(self, tmp_path):
        """Symlink/non-regular-entry refusal applies per-adapter too --
        the SAME hard AttestationError as the single-path sources, never
        silently followed or demoted to a decline."""
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        secret_path = outside_dir / "secret"
        secret_path.write_text("attacker-controlled-identity", encoding="utf-8")

        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        link_path = sidecar_dir / "spawn-abc123"
        link_path.symlink_to(secret_path)

        config_root = self._config_with_sidecars(
            tmp_path,
            [{"dir": sidecar_dir, "file_prefix": "spawn-", "session_id_env": "SPAWN_ID"}],
        )
        env = {"SPAWN_ID": "abc123"}
        with pytest.raises(attestation.AttestationError) as exc_info:
            attestation.resolve_identity(env=env, config_root=config_root)
        assert "attacker-controlled-identity" not in str(exc_info.value)

    def test_empty_sidecars_list_is_todays_behavior(self, tmp_path, monkeypatch):
        """Acceptance (b): an EMPTY `attestation.sidecars` list is
        byte-identical to today's behavior (falls through to builtin,
        same as no attestation config at all)."""
        monkeypatch.setattr(attestation.getpass, "getuser", lambda: "os-user-fallback")
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "attestation:\n  sidecars: []\n", encoding="utf-8"
        )
        identity = attestation.resolve_identity(env={}, config_root=config_root)
        assert identity == attestation.Identity("os-user-fallback", attestation.SOURCE_BUILTIN)

    def test_no_attestation_block_is_todays_behavior(self, tmp_path, monkeypatch):
        """Acceptance (b): no `attestation:` section at all -- regression
        baseline, zero behavior change for existing deployments."""
        monkeypatch.setattr(attestation.getpass, "getuser", lambda: "os-user-fallback")
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text("other_section: {}\n", encoding="utf-8")
        identity = attestation.resolve_identity(env={}, config_root=config_root)
        assert identity == attestation.Identity("os-user-fallback", attestation.SOURCE_BUILTIN)

    def test_partially_configured_adapter_entry_treated_as_disabled(
        self, tmp_path, monkeypatch
    ):
        """An adapter entry missing one of the three required keys is
        treated as disabled (skipped), never guessed at -- mirrors the Go
        reference's `enabled()` all-three-required rule."""
        monkeypatch.setattr(attestation.getpass, "getuser", lambda: "os-user-fallback")
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "attestation:\n"
            "  sidecars:\n"
            "    - dir: /tmp/sidecars\n"
            "      session_id_env: SPAWN_ID\n",  # file_prefix missing
            encoding="utf-8",
        )
        identity = attestation.resolve_identity(
            env={"SPAWN_ID": "abc123"}, config_root=config_root
        )
        assert identity == attestation.Identity("os-user-fallback", attestation.SOURCE_BUILTIN)


class TestBuiltinOsUserProviderLayer:
    def test_builtin_fallback_used_when_nothing_else_configured(self, monkeypatch):
        monkeypatch.setattr(attestation.getpass, "getuser", lambda: "real-os-user")
        identity = attestation.resolve_identity(env={}, config_root="/nonexistent")
        assert identity == attestation.Identity("real-os-user", attestation.SOURCE_BUILTIN)

    def test_default_chain_end_to_end_with_no_config_or_sidecar(self, monkeypatch, tmp_path):
        """Full default chain (no `providers=` override), proving the wiring
        in `_default_chain` itself -- not just each layer's class in
        isolation."""
        monkeypatch.setattr(attestation.getpass, "getuser", lambda: "default-chain-user")
        identity = attestation.resolve_identity(env={}, config_root=tmp_path)
        assert identity == attestation.Identity("default-chain-user", attestation.SOURCE_BUILTIN)


class TestAttestedInvokingIdentityParityWithCrewPush:
    """lr-ee9044: the loadout-side half of proving `resolve_identity` /
    `transport.attestation` resolve the SAME "attested invoking identity"
    contract crew_push.py's `resolve_attested_identity()`
    (scripts/_agent_identity_default.py, wrapping
    scripts/hooks/_agent_detect.py) resolves -- read directly from that
    module rather than assumed, per this task's instruction.

    crew's resolve_attested_identity() contract (see
    _agent_identity_default.py's own docstring for the full chain):
      1. Resolves a STRING identity value for "what is this process attested
         as" -- never a credential itself (Class E, lr-8e77: identity
         selection is a separate concern from credential minting).
      2. NEVER substitutes a fallback string on failure to resolve -- returns
         '' when the chain yields nothing (see its docstring: "this function
         returns the RAW result ... never a caller-supplied or hardcoded
         fallback string"), so that an EXPLICIT caller-supplied role can be
         compared against "no attestation available" rather than a
         manufactured identity.
      3. Feeds a fail-closed BINDING check at the call site (crew_push.py's
         explicit-`--agent` handling): a NON-EMPTY attested identity that
         DIFFERS from the explicit value is refused BEFORE any config load,
         self-fetch, or network I/O.

    loadout's `transport.attestation.resolve_identity` +
    `transport.git_host_api.bind_caller` mirror this exactly, at the
    seam push.verb's own token-resolution path shares (git_host_api is the
    module whose `bind_caller`/`--caller` fail-closed check this package
    documents as "mirrors the Go reference contract shipped in
    clagentic-gatekeeper's T0" -- the SAME reference contract crew's own
    resolve_attested_identity chain independently mirrors on the crew side):
      1. `resolve_identity` resolves an `Identity(subject, source)` -- the
         `subject` is the analogous "what is this process attested as"
         string; `source` is provenance, not part of the trust decision.
      2. Unlike crew's bare-string resolver, loadout's `resolve_identity`
         ALWAYS resolves something in a real deployment (its layer 3,
         `_BuiltinOsUserProvider`, is unconditional) -- there is no
         "no attestation available" empty-string case to preserve for an
         explicit-vs-attested comparison. This is a DELIBERATE, documented
         difference (see `transport.attestation`'s own module docstring,
         layer 3: "Always available, so a bare install has an attested
         source rather than failing open with no identity at all") --  not
         an oversight. The property both sides actually need for the
         binding check -- "compare the explicit claim against whatever the
         chain resolved, fail closed on a non-empty mismatch" -- holds
         identically; loadout's chain simply never produces the empty case
         crew's bare-CLI-subprocess chain can.
      3. `bind_caller(caller, caller_explicit=..., identity=...)` is the
         fail-closed comparison: an EXPLICIT caller that does not match
         `identity.subject` is refused via `GitHostApiError(code=
         EXIT_CALLER_INVOKER_MISMATCH)`, BEFORE any token mint or network
         I/O -- the same "explicit claim vs. attested identity, refuse the
         mismatch before touching credentials" shape as crew_push.py's own
         explicit-`--agent` fail-closed block (crew_push.py ~line 2298-2325).
         An OMITTED caller is never checked (mirrors crew's own "explicit
         --agent is required to trigger a fail-closed comparison" contract:
         crew's own resolve_attested_identity is ONLY consulted for a
         mismatch check when --agent was explicitly passed).

    This class exercises `bind_caller` directly (the actual enforcement
    point that consumes `resolve_identity`'s output) rather than merely
    re-asserting `resolve_identity`'s own chain (already covered above) --
    proving the FULL contract crew_push mirrors, not just its identity-
    resolution half.
    """

    def test_explicit_caller_matching_attested_identity_is_permitted(self):
        from clagentic_loadout.transport import git_host_api

        identity = attestation.Identity("builder", attestation.SOURCE_CONFIGURED)
        # No exception -- an explicit caller equal to the attested subject is
        # the same "acting as its own attested identity" case crew_push
        # permits without any --agent-mismatch refusal.
        git_host_api.bind_caller("builder", caller_explicit=True, identity=identity)

    def test_explicit_caller_mismatching_attested_identity_fails_closed(self):
        from clagentic_loadout.transport import git_host_api

        identity = attestation.Identity("holden", attestation.SOURCE_CONFIGURED)
        with pytest.raises(git_host_api.GitHostApiError) as exc_info:
            git_host_api.bind_caller("builder", caller_explicit=True, identity=identity)
        # Mirrors crew_push.py's EXIT_CALLER_MISMATCH: a specific, dedicated
        # exit code distinct from an ordinary token-fetch or usage failure,
        # so an operator can tell "attested-identity mismatch" apart from
        # either of those by exit code alone -- exactly the same
        # distinguishing property crew_push.py's own EXIT_CALLER_MISMATCH
        # (distinct from EXIT_TOKEN_FETCH_FAILED / EXIT_CONFIG_ERROR) gives.
        assert exc_info.value.code == git_host_api.EXIT_CALLER_INVOKER_MISMATCH

    def test_omitted_caller_is_never_checked_against_attested_identity(self):
        """Mirrors crew_push.py's own contract: resolve_attested_identity()
        is consulted for a mismatch check ONLY when --agent was explicitly
        passed (see crew_push.py's `if args.agent is None: ... else:
        _attested_identity = resolve_attested_identity() ...` branching --
        the omitted-flag branch never calls the mismatch-check function at
        all). loadout's bind_caller mirrors this exactly via
        caller_explicit=False: even a wildly different attested identity
        must never raise when the caller value was defaulted, not claimed."""
        from clagentic_loadout.transport import git_host_api

        identity = attestation.Identity("some-other-identity", attestation.SOURCE_BUILTIN)
        # No exception -- caller_explicit=False short-circuits before the
        # comparison is even made.
        git_host_api.bind_caller("builder", caller_explicit=False, identity=identity)

    def test_resolve_identity_never_returns_a_credential_only_a_subject_string(self):
        """Class E parity (lr-8e77, named explicitly in crew's own
        _agent_identity_default.py docstring): identity RESOLUTION and
        credential MINTING are separate concerns on both sides. Proven here
        by asserting resolve_identity's return shape carries only a
        `subject` (a bare string) and a `source` label -- never anything
        that looks like or could be mistaken for a token/secret value."""
        identity = attestation.resolve_identity(
            providers=[_FixedProvider(attestation.Identity("builder", attestation.SOURCE_CONFIGURED))]
        )
        assert isinstance(identity.subject, str)
        assert isinstance(identity.source, str)
        # Identity has exactly these two fields (__slots__) -- no credential
        # field could even be attached without a code change to the class
        # itself, which this test's own re-run would immediately catch.
        assert attestation.Identity.__slots__ == ("subject", "source")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
