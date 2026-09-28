"""test_transport_attestation_bound.py — regression coverage for
`transport.attestation.resolve_bound_identity` (operator ruling, comment #5
on the task this fix implements).

Every caller-BOUND verb (`push`, `review`, `acquire`, `merge`, `merge
--close`, `merge --post-merge`, `git_host_api` itself) now resolves its
default attested identity via `resolve_bound_identity` instead of the
general `resolve_identity` chain -- this file exercises that function
directly, on a synthetic registry with invented adapter dirs/env-var
names/subject strings (CLAUDE.md hard rule 6: no real deployment identity
is required to prove the code correct).

Coverage maps directly onto the four acceptance cases from the operator
ruling:

  1. A top-level HOLDEN-shaped session (CLAGENTIC_SUBAGENT_ID unset) that
     runs the bound resolver as itself resolves via the session-scoped
     adapter and succeeds -- never via the built-in layer.
  2. A foreground subagent (CLAGENTIC_SUBAGENT_ID set) resolves via the
     per-spawn adapter and lands as itself, never as a parent/session
     identity even when a session-scoped adapter is ALSO configured and
     would otherwise resolve.
  3. A subagent whose per-spawn sidecar is missing is REFUSED
     (BoundAttestationError) naming the per-spawn source as expected --
     never falls through to the session adapter, never the parent.
  4. A caller with no sidecar at all (vanilla root shell, no attestation
     config) is REFUSED "no attested identity" -- never resolves to
     SOURCE_BUILTIN / the host uid.

Plus: layer-1 (configured-provider) precedence is retained unchanged, and
the refusal/success paths correctly name/report the SPECIFIC source that
answered or was expected (SOURCE_SIDECAR_SUBAGENT / SOURCE_SIDECAR_SESSION,
not the generic SOURCE_SIDECAR) -- acceptance 3 of the ruling.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.transport import attestation


def _sidecars_config_root(tmp_path, sidecars: list[dict]):
    config_root = tmp_path / "cfg-root"
    config_root.mkdir()
    body = "".join(
        "".join(
            [
                f"    - dir: {entry['dir']}\n",
                f"      file_prefix: {entry['file_prefix']}\n",
                f"      session_id_env: {entry['session_id_env']}\n",
            ]
        )
        for entry in sidecars
    )
    (config_root / "config.yaml").write_text(
        f"attestation:\n  sidecars:\n{body}", encoding="utf-8"
    )
    return config_root


class TestAcceptanceTopLevelSessionResolvesSessionAdapter:
    """Acceptance 1: a top-level session (CLAGENTIC_SUBAGENT_ID unset) runs
    the bound resolver and resolves via the session-scoped adapter -- never
    the built-in layer."""

    def test_env_unset_resolves_session_adapter(self, tmp_path):
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "lore-agent-name-synthetic-session-42").write_text(
            "synthetic-lead", encoding="utf-8"
        )
        config_root = _sidecars_config_root(
            tmp_path,
            [
                {
                    "dir": sidecar_dir,
                    "file_prefix": "lore-agent-name-",
                    "session_id_env": "CLAUDE_CODE_SESSION_ID",
                }
            ],
        )
        env = {"CLAUDE_CODE_SESSION_ID": "synthetic-session-42"}
        identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity(
            "synthetic-lead", attestation.SOURCE_SIDECAR_SESSION
        )

    def test_session_adapter_not_reachable_via_wrong_session_id_env(self, tmp_path):
        """An adapter declared with a DIFFERENT session_id_env than
        CLAUDE_CODE_SESSION_ID must never answer a top-level (env-unset)
        bound resolution -- the discriminator match is on the exact env-var
        name, not "any adapter that happens to be configured"."""
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "other-synthetic-99").write_text(
            "should-never-resolve", encoding="utf-8"
        )
        config_root = _sidecars_config_root(
            tmp_path,
            [
                {
                    "dir": sidecar_dir,
                    "file_prefix": "other-",
                    "session_id_env": "SOME_OTHER_SESSION_ENV",
                }
            ],
        )
        with pytest.raises(attestation.BoundAttestationError) as exc_info:
            attestation.resolve_bound_identity(env={}, config_root=config_root)
        assert exc_info.value.expected_source == attestation.SOURCE_SIDECAR_SESSION


class TestAcceptanceForegroundSubagentResolvesPerSpawnAdapter:
    """Acceptance 2: a foreground subagent (CLAGENTIC_SUBAGENT_ID set)
    resolves via the per-spawn adapter and lands as itself, never as a
    parent/session identity -- even when a session-scoped adapter is ALSO
    configured and would otherwise resolve."""

    def test_subagent_id_set_resolves_subagent_adapter_never_session(self, tmp_path):
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "subagent-synthetic-spawn-7").write_text(
            "synthetic-builder", encoding="utf-8"
        )
        (sidecar_dir / "lore-agent-name-synthetic-parent-session").write_text(
            "synthetic-parent-lead", encoding="utf-8"
        )
        config_root = _sidecars_config_root(
            tmp_path,
            [
                {
                    "dir": sidecar_dir,
                    "file_prefix": "subagent-",
                    "session_id_env": "CLAGENTIC_SUBAGENT_ID",
                },
                {
                    "dir": sidecar_dir,
                    "file_prefix": "lore-agent-name-",
                    "session_id_env": "CLAUDE_CODE_SESSION_ID",
                },
            ],
        )
        env = {
            "CLAGENTIC_SUBAGENT_ID": "synthetic-spawn-7",
            "CLAUDE_CODE_SESSION_ID": "synthetic-parent-session",
        }
        identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity(
            "synthetic-builder", attestation.SOURCE_SIDECAR_SUBAGENT
        )
        # Never the parent's identity -- proves no confused-deputy fallthrough.
        assert identity.subject != "synthetic-parent-lead"

    def test_adapter_declaration_order_does_not_matter(self, tmp_path):
        """The discriminator match is by session_id_env value, not by
        adapter list position -- a deployment that declares the session
        adapter FIRST still gets the correct per-spawn answer when
        CLAGENTIC_SUBAGENT_ID is set. Proves 'enforced in code, not by
        adapter ordering' (the ruling's explicit requirement)."""
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "subagent-synthetic-spawn-7").write_text(
            "synthetic-builder", encoding="utf-8"
        )
        (sidecar_dir / "lore-agent-name-synthetic-parent-session").write_text(
            "synthetic-parent-lead", encoding="utf-8"
        )
        config_root = _sidecars_config_root(
            tmp_path,
            [
                {
                    "dir": sidecar_dir,
                    "file_prefix": "lore-agent-name-",
                    "session_id_env": "CLAUDE_CODE_SESSION_ID",
                },
                {
                    "dir": sidecar_dir,
                    "file_prefix": "subagent-",
                    "session_id_env": "CLAGENTIC_SUBAGENT_ID",
                },
            ],
        )
        env = {
            "CLAGENTIC_SUBAGENT_ID": "synthetic-spawn-7",
            "CLAUDE_CODE_SESSION_ID": "synthetic-parent-session",
        }
        identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity(
            "synthetic-builder", attestation.SOURCE_SIDECAR_SUBAGENT
        )


class TestAcceptanceMissingPerSpawnSidecarRefuses:
    """Acceptance 3: a subagent whose per-spawn sidecar is missing is
    REFUSED, naming the per-spawn source as expected -- never falls through
    to the session adapter, never the parent, even though the session
    adapter WOULD resolve."""

    def test_missing_subagent_sidecar_refuses_even_when_session_adapter_would_resolve(
        self, tmp_path
    ):
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        # No subagent-synthetic-spawn-9 file written -- the per-spawn
        # adapter's composed file is genuinely absent.
        (sidecar_dir / "lore-agent-name-synthetic-parent-session").write_text(
            "synthetic-parent-lead", encoding="utf-8"
        )
        config_root = _sidecars_config_root(
            tmp_path,
            [
                {
                    "dir": sidecar_dir,
                    "file_prefix": "subagent-",
                    "session_id_env": "CLAGENTIC_SUBAGENT_ID",
                },
                {
                    "dir": sidecar_dir,
                    "file_prefix": "lore-agent-name-",
                    "session_id_env": "CLAUDE_CODE_SESSION_ID",
                },
            ],
        )
        env = {
            "CLAGENTIC_SUBAGENT_ID": "synthetic-spawn-9",
            "CLAUDE_CODE_SESSION_ID": "synthetic-parent-session",
        }
        with pytest.raises(attestation.BoundAttestationError) as exc_info:
            attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert exc_info.value.expected_source == attestation.SOURCE_SIDECAR_SUBAGENT
        # The refusal names the expected source explicitly.
        assert attestation.SUBAGENT_SESSION_ID_ENV_VAR in str(exc_info.value)
        # Never silently resolves the parent's identity.
        assert "synthetic-parent-lead" not in str(exc_info.value)

    def test_no_subagent_adapter_configured_at_all_refuses(self, tmp_path):
        """CLAGENTIC_SUBAGENT_ID is set but the deployment's config declares
        NO adapter with that session_id_env at all -- a config gap, not a
        missing file -- still refuses, never falls through to whatever
        adapter IS configured (even a session adapter present in the same
        list)."""
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "lore-agent-name-synthetic-parent-session").write_text(
            "synthetic-parent-lead", encoding="utf-8"
        )
        config_root = _sidecars_config_root(
            tmp_path,
            [
                {
                    "dir": sidecar_dir,
                    "file_prefix": "lore-agent-name-",
                    "session_id_env": "CLAUDE_CODE_SESSION_ID",
                }
            ],
        )
        env = {
            "CLAGENTIC_SUBAGENT_ID": "synthetic-spawn-9",
            "CLAUDE_CODE_SESSION_ID": "synthetic-parent-session",
        }
        with pytest.raises(attestation.BoundAttestationError) as exc_info:
            attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert exc_info.value.expected_source == attestation.SOURCE_SIDECAR_SUBAGENT


class TestAcceptanceNoSidecarAtAllRefusesNeverRoot:
    """Acceptance 4: a caller with no sidecar at all (vanilla root shell, no
    attestation config) is REFUSED 'no attested identity' -- never resolves
    to SOURCE_BUILTIN / the host uid. This is the env-UNSET subagent path
    the pre-existing lr-1e16a4 backstop does not cover (it only fires when
    the env var is SET but the file is absent) -- this drives the
    CLAGENTIC_SUBAGENT_ID-unset, nothing-configured-at-all case directly."""

    def test_nothing_configured_refuses_never_builtin(self, tmp_path, monkeypatch):
        # Prove the built-in layer is never reached at all, even as a
        # sentinel -- getpass.getuser() would raise if called.
        monkeypatch.setattr(
            attestation.getpass,
            "getuser",
            lambda: (_ for _ in ()).throw(
                AssertionError(
                    "built-in fallback must never be reached by a bound resolution"
                )
            ),
        )
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text("other_section: {}\n", encoding="utf-8")
        with pytest.raises(attestation.BoundAttestationError) as exc_info:
            attestation.resolve_bound_identity(env={}, config_root=config_root)
        assert exc_info.value.expected_source == attestation.SOURCE_SIDECAR_SESSION
        assert "no attested identity" in str(exc_info.value)

    def test_no_config_file_at_all_refuses(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            attestation.getpass,
            "getuser",
            lambda: (_ for _ in ()).throw(
                AssertionError(
                    "built-in fallback must never be reached by a bound resolution"
                )
            ),
        )
        with pytest.raises(attestation.BoundAttestationError):
            attestation.resolve_bound_identity(env={}, config_root=tmp_path / "no-such-dir")

    def test_subagent_id_set_but_nothing_configured_refuses_naming_subagent_source(
        self, tmp_path, monkeypatch
    ):
        """The env-UNSET case above drives the session-source refusal; this
        drives the SET case the same way -- both branches of the
        discriminator refuse cleanly with no config at all, never the
        built-in layer."""
        monkeypatch.setattr(
            attestation.getpass,
            "getuser",
            lambda: (_ for _ in ()).throw(
                AssertionError(
                    "built-in fallback must never be reached by a bound resolution"
                )
            ),
        )
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text("other_section: {}\n", encoding="utf-8")
        env = {"CLAGENTIC_SUBAGENT_ID": "synthetic-spawn-1"}
        with pytest.raises(attestation.BoundAttestationError) as exc_info:
            attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert exc_info.value.expected_source == attestation.SOURCE_SIDECAR_SUBAGENT


class TestLayerOnePrecedenceUnchanged:
    """Layer 1 (the configured-provider env var) is a real, deployment-
    declared attested identity, not the built-in fallback this task
    narrows -- it retains its existing precedence over BOTH sidecar
    sources, unchanged."""

    def test_configured_provider_wins_over_session_adapter(self, tmp_path):
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "lore-agent-name-synthetic-session").write_text(
            "should-not-win", encoding="utf-8"
        )
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "attestation:\n"
            "  identity_env: SYNTHETIC_IDENTITY_VAR\n"
            "  sidecars:\n"
            f"    - dir: {sidecar_dir}\n"
            "      file_prefix: lore-agent-name-\n"
            "      session_id_env: CLAUDE_CODE_SESSION_ID\n",
            encoding="utf-8",
        )
        env = {
            "SYNTHETIC_IDENTITY_VAR": "synthetic-configured-subject",
            "CLAUDE_CODE_SESSION_ID": "synthetic-session",
        }
        identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity(
            "synthetic-configured-subject", attestation.SOURCE_CONFIGURED
        )

    def test_configured_provider_wins_over_subagent_adapter(self, tmp_path):
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "subagent-synthetic-spawn").write_text(
            "should-not-win", encoding="utf-8"
        )
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "attestation:\n"
            "  identity_env: SYNTHETIC_IDENTITY_VAR\n"
            "  sidecars:\n"
            f"    - dir: {sidecar_dir}\n"
            "      file_prefix: subagent-\n"
            "      session_id_env: CLAGENTIC_SUBAGENT_ID\n",
            encoding="utf-8",
        )
        env = {
            "SYNTHETIC_IDENTITY_VAR": "synthetic-configured-subject",
            "CLAGENTIC_SUBAGENT_ID": "synthetic-spawn",
        }
        identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity(
            "synthetic-configured-subject", attestation.SOURCE_CONFIGURED
        )


class TestBoundIdentitySourceLabelsAreSpecific:
    """Acceptance 3 (naming which source answered/was expected): a bound
    resolution's SUCCESSFUL Identity.source is the specific
    SOURCE_SIDECAR_SUBAGENT/SOURCE_SIDECAR_SESSION label, never the generic
    SOURCE_SIDECAR the ordinary (non-bound) chain uses -- and that specific
    label flows into transport.caller_binding's own refusal message via
    bind_caller, so a CallerBindingError names which source answered
    instead of a bare 'sidecar' string."""

    def test_subagent_source_label_is_specific_not_generic_sidecar(self, tmp_path):
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "subagent-synthetic-spawn").write_text(
            "synthetic-builder", encoding="utf-8"
        )
        config_root = _sidecars_config_root(
            tmp_path,
            [
                {
                    "dir": sidecar_dir,
                    "file_prefix": "subagent-",
                    "session_id_env": "CLAGENTIC_SUBAGENT_ID",
                }
            ],
        )
        identity = attestation.resolve_bound_identity(
            env={"CLAGENTIC_SUBAGENT_ID": "synthetic-spawn"}, config_root=config_root
        )
        assert identity.source == attestation.SOURCE_SIDECAR_SUBAGENT
        assert identity.source != attestation.SOURCE_SIDECAR

    def test_caller_binding_mismatch_message_names_specific_source(self, tmp_path):
        from clagentic_loadout.transport import caller_binding

        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "subagent-synthetic-spawn").write_text(
            "synthetic-builder", encoding="utf-8"
        )
        config_root = _sidecars_config_root(
            tmp_path,
            [
                {
                    "dir": sidecar_dir,
                    "file_prefix": "subagent-",
                    "session_id_env": "CLAGENTIC_SUBAGENT_ID",
                }
            ],
        )
        identity = attestation.resolve_bound_identity(
            env={"CLAGENTIC_SUBAGENT_ID": "synthetic-spawn"}, config_root=config_root
        )
        with pytest.raises(caller_binding.CallerBindingError) as exc_info:
            caller_binding.bind_caller(
                "synthetic-other-caller", caller_explicit=True, identity=identity
            )
        assert attestation.SOURCE_SIDECAR_SUBAGENT in str(exc_info.value)
        assert "'sidecar'" not in str(exc_info.value)


class TestOrdinaryResolveIdentityUnaffected:
    """The general, non-bound `resolve_identity` chain (and its
    SOURCE_SIDECAR label) is completely unchanged by any of this -- proven
    directly, since `resolve_bound_identity` is a second entry point, not a
    rewrite of the first."""

    def test_resolve_identity_still_falls_through_to_builtin(self, monkeypatch):
        monkeypatch.setattr(attestation.getpass, "getuser", lambda: "synthetic-os-user")
        identity = attestation.resolve_identity(env={}, config_root="/nonexistent")
        assert identity == attestation.Identity(
            "synthetic-os-user", attestation.SOURCE_BUILTIN
        )

    def test_resolve_identity_sidecar_adapter_still_reports_generic_source(self, tmp_path):
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "subagent-synthetic-spawn").write_text(
            "synthetic-builder", encoding="utf-8"
        )
        config_root = _sidecars_config_root(
            tmp_path,
            [
                {
                    "dir": sidecar_dir,
                    "file_prefix": "subagent-",
                    "session_id_env": "CLAGENTIC_SUBAGENT_ID",
                }
            ],
        )
        identity = attestation.resolve_identity(
            env={"CLAGENTIC_SUBAGENT_ID": "synthetic-spawn"}, config_root=config_root
        )
        assert identity == attestation.Identity("synthetic-builder", attestation.SOURCE_SIDECAR)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
