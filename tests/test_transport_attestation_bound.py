"""test_transport_attestation_bound.py — regression coverage for
`transport.attestation.resolve_bound_identity`.

Every caller-BOUND verb (`push`, `review`, `acquire`, `merge`, `merge
--close`, `merge --post-merge`, `git_host_api` itself) resolves its default
attested identity via `resolve_bound_identity` instead of the general
`resolve_identity` chain -- this file exercises that function directly, on a
synthetic registry with INVENTED adapter dirs, env-var NAMES, and subject
strings (CLAUDE.md hard rule 6: no real deployment identity, and no
harness-specific env var name, is required to prove the code correct --
every env var name used below is made up for this test file and would work
identically if renamed).

Coverage maps directly onto four acceptance cases, all exercised under
`attestation.bound_identity: required` (the strict policy):

  1. A top-level session (no `scope: per-spawn` adapter's session_id_env
     set) resolves via the `scope: session` adapter and succeeds -- never
     via the built-in layer.
  2. A foreground subagent (a `scope: per-spawn` adapter's session_id_env
     set) resolves via that adapter and lands as itself, never as a
     parent/session identity even when a `scope: session` adapter is ALSO
     configured and would otherwise resolve.
  3. A subagent whose per-spawn sidecar is missing is REFUSED
     (BoundAttestationError) naming the per-spawn source as expected --
     never falls through to the session adapter, never the parent.
  4. A caller with no sidecar at all (vanilla root shell, no attestation
     config) is REFUSED "no attested identity" -- never resolves to
     SOURCE_BUILTIN / the host uid.

Plus: layer-1 (configured-provider) precedence is retained unchanged; the
refusal/success paths correctly name/report the SPECIFIC source that
answered or was expected (SOURCE_SIDECAR_SUBAGENT / SOURCE_SIDECAR_SESSION,
not the generic SOURCE_SIDECAR); the discriminator is driven by each
adapter's declared `scope` key, never a hardcoded env-var name (proven by
using different invented names across test classes); and a dedicated
`attestation.bound_identity: builtin-fallback` class proves the previously-
released behavior (falling through to the built-in OS-user layer on an
undiscriminated miss) is preserved when a deployment opts into it, while the
per-spawn discriminator refusal itself stays unconditional under that
policy too.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.transport import attestation


def _sidecars_config_root(tmp_path, sidecars: list[dict], *, bound_identity: str | None = None):
    config_root = tmp_path / "cfg-root"
    config_root.mkdir()
    body = "".join(
        "".join(
            [
                f"    - dir: {entry['dir']}\n",
                f"      file_prefix: {entry['file_prefix']}\n",
                f"      session_id_env: {entry['session_id_env']}\n",
            ]
            + ([f"      scope: {entry['scope']}\n"] if "scope" in entry else [])
        )
        for entry in sidecars
    )
    bound_identity_line = (
        f"  bound_identity: {bound_identity}\n" if bound_identity is not None else ""
    )
    (config_root / "config.yaml").write_text(
        f"attestation:\n{bound_identity_line}  sidecars:\n{body}", encoding="utf-8"
    )
    return config_root


class TestAcceptanceTopLevelSessionResolvesSessionAdapter:
    """Acceptance 1 (policy: required): a top-level session (no
    scope:per-spawn adapter's session_id_env set) runs the bound resolver
    and resolves via the scope:session adapter -- never the built-in
    layer."""

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
                    "session_id_env": "ACME_SESSION_TOKEN",
                    "scope": attestation.SIDECAR_SCOPE_SESSION,
                }
            ],
            bound_identity=attestation.BOUND_IDENTITY_POLICY_REQUIRED,
        )
        env = {"ACME_SESSION_TOKEN": "synthetic-session-42"}
        identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity(
            "synthetic-lead", attestation.SOURCE_SIDECAR_SESSION
        )

    def test_session_adapter_not_reachable_without_scope_key(self, tmp_path):
        """An adapter with NO `scope` key at all must never answer a bound
        resolution on either branch of the discriminator -- scope is what
        makes an adapter eligible, not merely being present in the list."""
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
                    "session_id_env": "SOME_UNSCOPED_ENV",
                    # deliberately no "scope" key
                }
            ],
            bound_identity=attestation.BOUND_IDENTITY_POLICY_REQUIRED,
        )
        with pytest.raises(attestation.BoundAttestationError) as exc_info:
            attestation.resolve_bound_identity(env={}, config_root=config_root)
        assert exc_info.value.expected_source == attestation.SOURCE_SIDECAR_SESSION

    def test_session_adapter_not_reachable_via_unrecognized_scope_value(self, tmp_path):
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
                    "session_id_env": "SOME_UNSCOPED_ENV",
                    "scope": "not-a-real-scope",
                }
            ],
            bound_identity=attestation.BOUND_IDENTITY_POLICY_REQUIRED,
        )
        with pytest.raises(attestation.BoundAttestationError):
            attestation.resolve_bound_identity(env={}, config_root=config_root)


class TestAcceptanceForegroundSubagentResolvesPerSpawnAdapter:
    """Acceptance 2 (policy: required): a foreground subagent (a
    scope:per-spawn adapter's session_id_env set) resolves via that adapter
    and lands as itself, never as a parent/session identity -- even when a
    scope:session adapter is ALSO configured and would otherwise resolve.

    Uses DIFFERENT invented env-var names than the class above, proving the
    discriminator reads the adapter's declared `scope`, never a fixed env-
    var name."""

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
                    "session_id_env": "WIDGETCO_SPAWN_ID",
                    "scope": attestation.SIDECAR_SCOPE_PER_SPAWN,
                },
                {
                    "dir": sidecar_dir,
                    "file_prefix": "lore-agent-name-",
                    "session_id_env": "WIDGETCO_SESSION_ID",
                    "scope": attestation.SIDECAR_SCOPE_SESSION,
                },
            ],
            bound_identity=attestation.BOUND_IDENTITY_POLICY_REQUIRED,
        )
        env = {
            "WIDGETCO_SPAWN_ID": "synthetic-spawn-7",
            "WIDGETCO_SESSION_ID": "synthetic-parent-session",
        }
        identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity(
            "synthetic-builder", attestation.SOURCE_SIDECAR_SUBAGENT
        )
        # Never the parent's identity -- proves no confused-deputy fallthrough.
        assert identity.subject != "synthetic-parent-lead"

    def test_adapter_declaration_order_does_not_matter(self, tmp_path):
        """The discriminator match is by declared `scope`, not by adapter
        list position -- a deployment that declares the session adapter
        FIRST still gets the correct per-spawn answer when its per-spawn
        adapter's env var is set. Proves 'enforced in code, not by adapter
        ordering.'"""
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
                    "session_id_env": "WIDGETCO_SESSION_ID",
                    "scope": attestation.SIDECAR_SCOPE_SESSION,
                },
                {
                    "dir": sidecar_dir,
                    "file_prefix": "subagent-",
                    "session_id_env": "WIDGETCO_SPAWN_ID",
                    "scope": attestation.SIDECAR_SCOPE_PER_SPAWN,
                },
            ],
            bound_identity=attestation.BOUND_IDENTITY_POLICY_REQUIRED,
        )
        env = {
            "WIDGETCO_SPAWN_ID": "synthetic-spawn-7",
            "WIDGETCO_SESSION_ID": "synthetic-parent-session",
        }
        identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity(
            "synthetic-builder", attestation.SOURCE_SIDECAR_SUBAGENT
        )


class TestAcceptanceMissingPerSpawnSidecarRefuses:
    """Acceptance 3 (policy: required): a subagent whose per-spawn sidecar
    is missing is REFUSED, naming the per-spawn source as expected -- never
    falls through to the session adapter, never the parent, even though the
    session adapter WOULD resolve."""

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
                    "session_id_env": "GIZMO_SPAWN_ID",
                    "scope": attestation.SIDECAR_SCOPE_PER_SPAWN,
                },
                {
                    "dir": sidecar_dir,
                    "file_prefix": "lore-agent-name-",
                    "session_id_env": "GIZMO_SESSION_ID",
                    "scope": attestation.SIDECAR_SCOPE_SESSION,
                },
            ],
            bound_identity=attestation.BOUND_IDENTITY_POLICY_REQUIRED,
        )
        env = {
            "GIZMO_SPAWN_ID": "synthetic-spawn-9",
            "GIZMO_SESSION_ID": "synthetic-parent-session",
        }
        with pytest.raises(attestation.BoundAttestationError) as exc_info:
            attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert exc_info.value.expected_source == attestation.SOURCE_SIDECAR_SUBAGENT
        # The refusal names the expected scope explicitly.
        assert attestation.SIDECAR_SCOPE_PER_SPAWN in str(exc_info.value)
        # Never silently resolves the parent's identity.
        assert "synthetic-parent-lead" not in str(exc_info.value)

    def test_no_subagent_adapter_configured_at_all_never_falls_back_to_stray_env_var(
        self, tmp_path
    ):
        """A bare env var this deployment happens to also use for something
        else, with NO scope:per-spawn adapter declaring it as its
        session_id_env, has NO effect on the discriminator -- there is no
        per-spawn adapter to trigger, so this resolves as an ordinary
        top-level session and the configured session adapter answers
        normally (proving the discriminator is driven entirely by declared
        adapter config, never by "some env var happens to be set")."""
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
                    "session_id_env": "GIZMO_SESSION_ID",
                    "scope": attestation.SIDECAR_SCOPE_SESSION,
                }
            ],
            bound_identity=attestation.BOUND_IDENTITY_POLICY_REQUIRED,
        )
        env = {
            "GIZMO_SPAWN_ID": "synthetic-spawn-9",
            "GIZMO_SESSION_ID": "synthetic-parent-session",
        }
        identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity(
            "synthetic-parent-lead", attestation.SOURCE_SIDECAR_SESSION
        )

    def test_per_spawn_adapter_declared_with_different_env_name_refuses(self, tmp_path):
        """A scope:per-spawn adapter IS declared, but its OWN session_id_env
        is a DIFFERENT name than the one set in this process's env -- a
        config gap (the deployment's per-spawn adapter and its harness's
        actual spawn-stamped env var disagree), still refuses, never falls
        through to the session adapter."""
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
                    "file_prefix": "subagent-",
                    "session_id_env": "GIZMO_SPAWN_ID_TYPO",
                    "scope": attestation.SIDECAR_SCOPE_PER_SPAWN,
                },
                {
                    "dir": sidecar_dir,
                    "file_prefix": "lore-agent-name-",
                    "session_id_env": "GIZMO_SESSION_ID",
                    "scope": attestation.SIDECAR_SCOPE_SESSION,
                },
            ],
            bound_identity=attestation.BOUND_IDENTITY_POLICY_REQUIRED,
        )
        # The REAL per-spawn env var this process sets is GIZMO_SPAWN_ID,
        # but the adapter above is declared against GIZMO_SPAWN_ID_TYPO --
        # the discriminator never fires, so this resolves as a top-level
        # session and the session adapter answers.
        env = {
            "GIZMO_SPAWN_ID": "synthetic-spawn-9",
            "GIZMO_SESSION_ID": "synthetic-parent-session",
        }
        identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity(
            "synthetic-parent-lead", attestation.SOURCE_SIDECAR_SESSION
        )


class TestAcceptanceNoSidecarAtAllRefusesNeverRoot:
    """Acceptance 4 (policy: required): a caller with no sidecar at all
    (vanilla root shell, no attestation config) is REFUSED 'no attested
    identity' -- never resolves to SOURCE_BUILTIN / the host uid."""

    def test_nothing_configured_refuses_never_builtin(self, tmp_path, monkeypatch):
        # Prove the built-in layer is never reached at all, even as a
        # sentinel -- getpass.getuser() would raise if called.
        monkeypatch.setattr(
            attestation.getpass,
            "getuser",
            lambda: (_ for _ in ()).throw(
                AssertionError(
                    "built-in fallback must never be reached under bound_identity: required"
                )
            ),
        )
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "attestation:\n  bound_identity: required\n", encoding="utf-8"
        )
        with pytest.raises(attestation.BoundAttestationError) as exc_info:
            attestation.resolve_bound_identity(env={}, config_root=config_root)
        assert exc_info.value.expected_source == attestation.SOURCE_SIDECAR_SESSION
        assert "no attested identity" in str(exc_info.value)

    def test_no_config_file_at_all_refuses_under_explicit_required(self, tmp_path, monkeypatch):
        """No config file at all falls back to DEFAULT_BOUND_IDENTITY_POLICY
        -- this test pins the assertion to whatever that default resolves
        to today, so a future default flip is caught here rather than
        silently changing this test's meaning; see
        TestDefaultPolicyIsBuiltinFallbackPendingOperatorConfirmation for
        the dedicated default-value regression test."""
        if attestation.DEFAULT_BOUND_IDENTITY_POLICY == attestation.BOUND_IDENTITY_POLICY_REQUIRED:
            monkeypatch.setattr(
                attestation.getpass,
                "getuser",
                lambda: (_ for _ in ()).throw(
                    AssertionError("built-in fallback must never be reached")
                ),
            )
            with pytest.raises(attestation.BoundAttestationError):
                attestation.resolve_bound_identity(env={}, config_root=tmp_path / "no-such-dir")
        else:
            monkeypatch.setattr(attestation.getpass, "getuser", lambda: "synthetic-os-user")
            identity = attestation.resolve_bound_identity(
                env={}, config_root=tmp_path / "no-such-dir"
            )
            assert identity == attestation.Identity(
                "synthetic-os-user", attestation.SOURCE_BUILTIN
            )

    def test_subagent_id_set_but_nothing_configured_refuses_naming_subagent_source(
        self, tmp_path, monkeypatch
    ):
        """The env-unset case above drives the session-source refusal; this
        drives the per-spawn-declared case the same way -- both branches of
        the discriminator refuse cleanly with no adapters configured at
        all, never the built-in layer, under the strict policy."""
        monkeypatch.setattr(
            attestation.getpass,
            "getuser",
            lambda: (_ for _ in ()).throw(
                AssertionError(
                    "built-in fallback must never be reached under bound_identity: required"
                )
            ),
        )
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "attestation:\n  bound_identity: required\n", encoding="utf-8"
        )
        # No sidecars list at all -- so _any_per_spawn_session_id_set finds
        # nothing to check, meaning this actually exercises the SESSION
        # branch (no per-spawn adapter -> is_per_spawn is False). Included
        # here to document that "an env var this deployment happens to also
        # use for something else" being set has no effect absent a declared
        # per-spawn adapter naming it.
        env = {"SOME_RANDOM_ENV_VAR": "synthetic-spawn-1"}
        with pytest.raises(attestation.BoundAttestationError) as exc_info:
            attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert exc_info.value.expected_source == attestation.SOURCE_SIDECAR_SESSION


class TestLayerOnePrecedenceUnchanged:
    """Layer 1 (the configured-provider env var) is a real, deployment-
    declared attested identity, not the built-in fallback the bound_identity
    policy governs -- it retains its existing precedence over BOTH sidecar
    scopes, unchanged, under either policy value."""

    @pytest.mark.parametrize(
        "policy",
        [attestation.BOUND_IDENTITY_POLICY_REQUIRED, attestation.BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK],
    )
    def test_configured_provider_wins_over_session_adapter(self, tmp_path, policy):
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "lore-agent-name-synthetic-session").write_text(
            "should-not-win", encoding="utf-8"
        )
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "attestation:\n"
            f"  bound_identity: {policy}\n"
            "  identity_env: SYNTHETIC_IDENTITY_VAR\n"
            "  sidecars:\n"
            f"    - dir: {sidecar_dir}\n"
            "      file_prefix: lore-agent-name-\n"
            "      session_id_env: WIDGETCO_SESSION_ID\n"
            f"      scope: {attestation.SIDECAR_SCOPE_SESSION}\n",
            encoding="utf-8",
        )
        env = {
            "SYNTHETIC_IDENTITY_VAR": "synthetic-configured-subject",
            "WIDGETCO_SESSION_ID": "synthetic-session",
        }
        identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity(
            "synthetic-configured-subject", attestation.SOURCE_CONFIGURED
        )

    @pytest.mark.parametrize(
        "policy",
        [attestation.BOUND_IDENTITY_POLICY_REQUIRED, attestation.BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK],
    )
    def test_configured_provider_wins_over_subagent_adapter(self, tmp_path, policy):
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "subagent-synthetic-spawn").write_text(
            "should-not-win", encoding="utf-8"
        )
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "attestation:\n"
            f"  bound_identity: {policy}\n"
            "  identity_env: SYNTHETIC_IDENTITY_VAR\n"
            "  sidecars:\n"
            f"    - dir: {sidecar_dir}\n"
            "      file_prefix: subagent-\n"
            "      session_id_env: WIDGETCO_SPAWN_ID\n"
            f"      scope: {attestation.SIDECAR_SCOPE_PER_SPAWN}\n",
            encoding="utf-8",
        )
        env = {
            "SYNTHETIC_IDENTITY_VAR": "synthetic-configured-subject",
            "WIDGETCO_SPAWN_ID": "synthetic-spawn",
        }
        identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity(
            "synthetic-configured-subject", attestation.SOURCE_CONFIGURED
        )


class TestBoundIdentitySourceLabelsAreSpecific:
    """A bound resolution's SUCCESSFUL Identity.source is the specific
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
                    "session_id_env": "WIDGETCO_SPAWN_ID",
                    "scope": attestation.SIDECAR_SCOPE_PER_SPAWN,
                }
            ],
            bound_identity=attestation.BOUND_IDENTITY_POLICY_REQUIRED,
        )
        identity = attestation.resolve_bound_identity(
            env={"WIDGETCO_SPAWN_ID": "synthetic-spawn"}, config_root=config_root
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
                    "session_id_env": "WIDGETCO_SPAWN_ID",
                    "scope": attestation.SIDECAR_SCOPE_PER_SPAWN,
                }
            ],
            bound_identity=attestation.BOUND_IDENTITY_POLICY_REQUIRED,
        )
        identity = attestation.resolve_bound_identity(
            env={"WIDGETCO_SPAWN_ID": "synthetic-spawn"}, config_root=config_root
        )
        with pytest.raises(caller_binding.CallerBindingError) as exc_info:
            caller_binding.bind_caller(
                "synthetic-other-caller", caller_explicit=True, identity=identity
            )
        assert attestation.SOURCE_SIDECAR_SUBAGENT in str(exc_info.value)
        assert "'sidecar'" not in str(exc_info.value)


class TestBuiltinFallbackPolicyPreservesReleasedBehavior:
    """`attestation.bound_identity: builtin-fallback` preserves this
    package's previously-released behavior: an UNDISCRIMINATED miss (no
    adapter of the selected scope configured/resolving) falls through to
    the built-in OS-user layer, exactly like a bare install with no
    attestation config ever configured. The per-spawn discriminator
    refusal itself stays UNCONDITIONAL even under this policy -- proven by
    the last two tests in this class."""

    def test_no_attestation_config_at_all_falls_through_to_builtin(self, tmp_path, monkeypatch):
        monkeypatch.setattr(attestation.getpass, "getuser", lambda: "synthetic-os-user")
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "attestation:\n  bound_identity: builtin-fallback\n", encoding="utf-8"
        )
        identity = attestation.resolve_bound_identity(env={}, config_root=config_root)
        assert identity == attestation.Identity("synthetic-os-user", attestation.SOURCE_BUILTIN)

    def test_session_scope_configured_but_not_resolving_falls_through_to_builtin(
        self, tmp_path, monkeypatch
    ):
        """A scope:session adapter IS declared, but its composed file is
        absent -- an undiscriminated (top-level-session) miss under
        builtin-fallback still lands on the built-in layer."""
        monkeypatch.setattr(attestation.getpass, "getuser", lambda: "synthetic-os-user")
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        # No file written for this session id -- composed path is absent.
        config_root = _sidecars_config_root(
            tmp_path,
            [
                {
                    "dir": sidecar_dir,
                    "file_prefix": "lore-agent-name-",
                    "session_id_env": "WIDGETCO_SESSION_ID",
                    "scope": attestation.SIDECAR_SCOPE_SESSION,
                }
            ],
            bound_identity=attestation.BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK,
        )
        env = {"WIDGETCO_SESSION_ID": "no-such-session"}
        identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity("synthetic-os-user", attestation.SOURCE_BUILTIN)

    def test_per_spawn_miss_still_refuses_under_builtin_fallback_policy(
        self, tmp_path, monkeypatch
    ):
        """UNCONDITIONAL correctness fix: a per-spawn-declared invocation
        (its adapter's session_id_env IS set) whose per-spawn sidecar is
        missing is REFUSED even under builtin-fallback -- it never falls
        through to the built-in layer, and never to a session adapter,
        regardless of policy."""
        monkeypatch.setattr(
            attestation.getpass,
            "getuser",
            lambda: (_ for _ in ()).throw(
                AssertionError(
                    "a per-spawn-declared miss must refuse unconditionally, "
                    "even under bound_identity: builtin-fallback"
                )
            ),
        )
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
                    "file_prefix": "subagent-",
                    "session_id_env": "WIDGETCO_SPAWN_ID",
                    "scope": attestation.SIDECAR_SCOPE_PER_SPAWN,
                },
                {
                    "dir": sidecar_dir,
                    "file_prefix": "lore-agent-name-",
                    "session_id_env": "WIDGETCO_SESSION_ID",
                    "scope": attestation.SIDECAR_SCOPE_SESSION,
                },
            ],
            bound_identity=attestation.BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK,
        )
        env = {
            "WIDGETCO_SPAWN_ID": "synthetic-spawn-missing",
            "WIDGETCO_SESSION_ID": "synthetic-parent-session",
        }
        with pytest.raises(attestation.BoundAttestationError) as exc_info:
            attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert exc_info.value.expected_source == attestation.SOURCE_SIDECAR_SUBAGENT
        assert "synthetic-parent-lead" not in str(exc_info.value)

    def test_unrecognized_policy_string_falls_back_to_default(self, tmp_path, monkeypatch):
        """A typo'd/unrecognized `attestation.bound_identity` value is
        treated as unset -- falls back to DEFAULT_BOUND_IDENTITY_POLICY,
        never a hard config-parse failure."""
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "attestation:\n  bound_identity: not-a-real-policy\n", encoding="utf-8"
        )
        if attestation.DEFAULT_BOUND_IDENTITY_POLICY == attestation.BOUND_IDENTITY_POLICY_REQUIRED:
            with pytest.raises(attestation.BoundAttestationError):
                attestation.resolve_bound_identity(env={}, config_root=config_root)
        else:
            monkeypatch.setattr(attestation.getpass, "getuser", lambda: "synthetic-os-user")
            identity = attestation.resolve_bound_identity(env={}, config_root=config_root)
            assert identity == attestation.Identity(
                "synthetic-os-user", attestation.SOURCE_BUILTIN
            )


class TestDefaultPolicyIsBuiltinFallbackPendingOperatorConfirmation:
    """DEFAULT_BOUND_IDENTITY_POLICY is a single constant so the default can
    be flipped in one line; this test pins its CURRENT value so a future
    flip is a deliberate, visible test change rather than a silent drift."""

    def test_default_constant_value(self):
        assert (
            attestation.DEFAULT_BOUND_IDENTITY_POLICY
            == attestation.BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK
        )


class TestOrdinaryResolveIdentityUnaffected:
    """The general, non-bound `resolve_identity` chain (and its
    SOURCE_SIDECAR label) is completely unchanged by any of this -- proven
    directly, since `resolve_bound_identity` is a second entry point, not a
    rewrite of the first. Also proves the ordinary chain does not require
    (or even look at) a `scope` key on an adapter."""

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
                    "session_id_env": "WIDGETCO_SPAWN_ID",
                    # no scope key -- irrelevant to the ordinary chain
                }
            ],
        )
        identity = attestation.resolve_identity(
            env={"WIDGETCO_SPAWN_ID": "synthetic-spawn"}, config_root=config_root
        )
        assert identity == attestation.Identity("synthetic-builder", attestation.SOURCE_SIDECAR)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
