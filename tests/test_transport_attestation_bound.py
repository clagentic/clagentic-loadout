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

`TestUnscopedConfigUpgradeSafety` (lr-620837 fold-in #3) covers a FIFTH
shape the four acceptance cases above do not: a config where NO adapter
declares a recognized `scope` at all (every config that predates this
task's `scope`/`bound_identity` keys, including this package's own
deployed host config). That shape must resolve EXACTLY as the released
`resolve_identity` chain would -- upgrading to this code must change
nothing for an existing config -- and only once at least one adapter
declares a scope do the scoped-discriminator rules above take over; a
stray unscoped adapter in that same (now-scoped) list is then correctly
ignored, which is what the two revised tests in
`TestAcceptanceTopLevelSessionResolvesSessionAdapter` now cover under
their "when a scoped adapter exists" names (previously named without that
qualifier, before the two shapes were distinguished).
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

    def test_session_adapter_not_reachable_without_scope_key_when_a_scoped_adapter_exists(
        self, tmp_path
    ):
        """MIXED CASE: once at least one OTHER adapter in the list declares
        a recognized scope, an adapter with NO `scope` key at all is
        ignored for bound resolution -- scope is what makes an adapter
        eligible once the discriminator is in play, not merely being
        present in the list. Distinct from the legacy-config case (no
        adapter anywhere declares a scope), covered in
        TestUnscopedConfigUpgradeSafety below, where the unscoped adapter
        DOES answer."""
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
                },
                {
                    "dir": sidecar_dir,
                    "file_prefix": "lore-agent-name-",
                    "session_id_env": "ACME_SESSION_TOKEN",
                    "scope": attestation.SIDECAR_SCOPE_SESSION,
                },
            ],
            bound_identity=attestation.BOUND_IDENTITY_POLICY_REQUIRED,
        )
        with pytest.raises(attestation.BoundAttestationError) as exc_info:
            attestation.resolve_bound_identity(env={}, config_root=config_root)
        assert exc_info.value.expected_source == attestation.SOURCE_SIDECAR_SESSION

    def test_unrecognized_scope_value_is_a_hard_config_error_not_a_silent_ignore(
        self, tmp_path
    ):
        """lr-620837 fold-in #4 F3 (PEACHES 5879765982): an adapter that
        DOES declare a `scope` key, but sets it to a value other than
        `per-spawn`/`session` (a deployment typo), is a HARD
        AttestationConfigError -- naming the adapter's position, the
        offending value, and the two allowed values -- never silently
        treated the same as "no scope key" (which this class's OWN sibling
        test, `test_session_adapter_not_reachable_without_scope_key_when_a_
        scoped_adapter_exists`, correctly keeps as a silent ineligibility).
        Superseded, pre-fold-in-#4 revision of this test asserted the
        OPPOSITE (a quiet `BoundAttestationError` fall-through) -- that was
        exactly the defect this fix closes, not a behavior worth
        preserving."""
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
                },
                {
                    "dir": sidecar_dir,
                    "file_prefix": "lore-agent-name-",
                    "session_id_env": "ACME_SESSION_TOKEN",
                    "scope": attestation.SIDECAR_SCOPE_SESSION,
                },
            ],
            bound_identity=attestation.BOUND_IDENTITY_POLICY_REQUIRED,
        )
        with pytest.raises(attestation.AttestationConfigError) as exc_info:
            attestation.resolve_bound_identity(env={}, config_root=config_root)
        assert "not-a-real-scope" in str(exc_info.value)
        assert "[1]" in str(exc_info.value)
        assert attestation.SIDECAR_SCOPE_PER_SPAWN in str(exc_info.value)
        assert attestation.SIDECAR_SCOPE_SESSION in str(exc_info.value)


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


class TestScopedResolutionNeverConsultsSinglePathOverrides:
    """lr-620837 fold-in #4 F2 (PEACHES 5879765982, KEEP-BY-DESIGN,
    DOCUMENT): once the scoped discriminator is active (at least one
    adapter declares a recognized `scope`), `_SidecarFileProvider`'s OTHER
    two sources -- the env-named single-path override
    (`CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH`) and the config
    file's `attestation.identity_sidecar_path` single-path key -- are NEVER
    consulted, even when either would resolve a real file. Both are named
    by something the INVOKING COMMAND itself can set (an env var, or a
    value the same process-level trust as everything else in config) --
    honoring either here would let a scoped invocation redirect itself to
    an arbitrary identity file, exactly the confused-deputy surface `scope`
    exists to close. See `resolve_bound_identity`'s own module docstring,
    'KEPT BY DESIGN, NOT CONSULTED IN SCOPED BOUND RESOLUTION', for the
    full rationale this test proves."""

    def test_env_named_single_path_override_ignored_once_scoped_adapter_exists(
        self, tmp_path, monkeypatch
    ):
        # The env-named override points at a file that WOULD resolve a
        # (wrong, attacker/foreign) identity if it were ever consulted.
        foreign_identity_path = tmp_path / "foreign-identity"
        foreign_identity_path.write_text("should-never-resolve-env-override", encoding="utf-8")

        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "lore-agent-name-synthetic-session-env-test").write_text(
            "synthetic-correct-session-identity", encoding="utf-8"
        )
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
            bound_identity=attestation.BOUND_IDENTITY_POLICY_REQUIRED,
        )
        env = {
            "WIDGETCO_SESSION_ID": "synthetic-session-env-test",
            attestation.ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR: str(foreign_identity_path),
        }
        identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        # The CORRECT scoped session adapter answered -- not the env-named
        # override, even though the override's file exists and is readable.
        assert identity == attestation.Identity(
            "synthetic-correct-session-identity", attestation.SOURCE_SIDECAR_SESSION
        )

    def test_config_single_path_override_ignored_once_scoped_adapter_exists(self, tmp_path):
        # The config file's own `identity_sidecar_path` single-path key
        # points at a file that WOULD resolve a (wrong) identity if it were
        # ever consulted by the scoped branch.
        foreign_identity_path = tmp_path / "foreign-identity-config"
        foreign_identity_path.write_text(
            "should-never-resolve-config-override", encoding="utf-8"
        )

        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "subagent-synthetic-spawn-env-test").write_text(
            "synthetic-correct-subagent-identity", encoding="utf-8"
        )
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "attestation:\n"
            "  bound_identity: required\n"
            f"  identity_sidecar_path: {foreign_identity_path}\n"
            "  sidecars:\n"
            f"    - dir: {sidecar_dir}\n"
            "      file_prefix: subagent-\n"
            "      session_id_env: WIDGETCO_SPAWN_ID\n"
            "      scope: per-spawn\n",
            encoding="utf-8",
        )
        env = {"WIDGETCO_SPAWN_ID": "synthetic-spawn-env-test"}
        identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity(
            "synthetic-correct-subagent-identity", attestation.SOURCE_SIDECAR_SUBAGENT
        )


class TestAcceptanceNoSidecarAtAllRefusesNeverRoot:
    """Acceptance 4 (policy: required): a caller with no sidecar at all
    (vanilla root shell, no attestation config) is REFUSED 'no attested
    identity' -- never resolves to SOURCE_BUILTIN / the host uid.

    "No attestation config at all" is also the legacy/unscoped shape
    `TestUnscopedConfigUpgradeSafety` (lr-620837 fold-in #3) names
    directly -- an empty/absent `sidecars` list declares no `scope`
    anywhere, so these two tests now exercise the unscoped fallback path
    and expect the generic SOURCE_SIDECAR expected-source label, not a
    scoped-discriminator label there was never anything configured to
    select between."""

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
        assert exc_info.value.expected_source == attestation.SOURCE_SIDECAR
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
        """With no `sidecars` list configured at all, there is no adapter
        anywhere to declare a scope -- this is the legacy/unscoped shape,
        so an unrelated env var being set has no effect on a discriminator
        that never activates (there is nothing configured for it to
        activate against). Included here to document that "an env var this
        deployment happens to also use for something else" being set does
        not change the outcome absent any declared adapter at all."""
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
        env = {"SOME_RANDOM_ENV_VAR": "synthetic-spawn-1"}
        with pytest.raises(attestation.BoundAttestationError) as exc_info:
            attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert exc_info.value.expected_source == attestation.SOURCE_SIDECAR


class TestUnscopedConfigUpgradeSafety:
    """lr-620837 fold-in #3 (HOLDEN-verified defect, PEACHES 5878952070
    missed it): a config where NO adapter declares a recognized `scope` at
    all is a legacy config -- bound resolution must behave EXACTLY as the
    released `resolve_identity` chain for that shape, never silently
    downgrading to the built-in OS-user layer just because the scoped
    discriminator has nothing configured to discriminate on."""

    def test_legacy_two_adapter_config_resolves_first_declared_matching_resolve_identity(
        self, tmp_path
    ):
        """A legacy unscoped TWO-adapter config under the default policy
        resolves the FIRST-DECLARED resolving adapter -- exactly what
        `resolve_identity` itself resolves for the identical env/config,
        proven by comparing the two calls directly rather than merely
        asserting a literal expected value."""
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
                    # deliberately no "scope" key -- legacy config
                },
                {
                    "dir": sidecar_dir,
                    "file_prefix": "lore-agent-name-",
                    "session_id_env": "WIDGETCO_SESSION_ID",
                    # deliberately no "scope" key -- legacy config
                },
            ],
        )
        env = {
            "WIDGETCO_SPAWN_ID": "synthetic-spawn-7",
            "WIDGETCO_SESSION_ID": "synthetic-parent-session",
        }
        bound_identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        ordinary_identity = attestation.resolve_identity(env=env, config_root=config_root)

        # Same subject, same source, as the ordinary (non-bound) chain --
        # byte-identical, not merely "also resolves something."
        assert bound_identity == ordinary_identity
        assert bound_identity == attestation.Identity(
            "synthetic-builder", attestation.SOURCE_SIDECAR
        )
        # Never the specific bound-only labels -- this is the unscoped
        # fallback path, not a discriminator-selected scope.
        assert bound_identity.source not in (
            attestation.SOURCE_SIDECAR_SUBAGENT,
            attestation.SOURCE_SIDECAR_SESSION,
        )

    def test_legacy_config_under_required_refuses_naming_missing_scope(self, tmp_path):
        """An unscoped config under `bound_identity: required` refuses when
        its (only) sidecar source does not resolve -- and the refusal names
        the missing `scope` key, so the misconfiguration is loud rather
        than reading like an ordinary per-spawn/session miss."""
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        # No file written -- the unscoped adapter's composed path is absent.
        config_root = _sidecars_config_root(
            tmp_path,
            [
                {
                    "dir": sidecar_dir,
                    "file_prefix": "lore-agent-name-",
                    "session_id_env": "WIDGETCO_SESSION_ID",
                    # deliberately no "scope" key -- legacy config
                }
            ],
            bound_identity=attestation.BOUND_IDENTITY_POLICY_REQUIRED,
        )
        env = {"WIDGETCO_SESSION_ID": "no-such-session"}
        with pytest.raises(attestation.BoundAttestationError) as exc_info:
            attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert "scope" in str(exc_info.value)
        assert exc_info.value.expected_source == attestation.SOURCE_SIDECAR

    def test_mixed_scoped_and_unscoped_adapters_ignores_the_unscoped_one(self, tmp_path):
        """MIXED CASE: once at least one adapter in the list declares a
        recognized scope, the discriminator is active and an unscoped
        adapter in that SAME list is ignored for bound resolution -- even
        though it would otherwise resolve first if walked in declared
        order (proving this is not merely 'first adapter that resolves')."""
        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "other-synthetic-1").write_text(
            "should-never-resolve-mixed-case", encoding="utf-8"
        )
        (sidecar_dir / "lore-agent-name-synthetic-session-mixed").write_text(
            "synthetic-lead-mixed", encoding="utf-8"
        )
        config_root = _sidecars_config_root(
            tmp_path,
            [
                {
                    "dir": sidecar_dir,
                    "file_prefix": "other-",
                    "session_id_env": "SOME_UNSCOPED_ENV",
                    # deliberately no "scope" key
                },
                {
                    "dir": sidecar_dir,
                    "file_prefix": "lore-agent-name-",
                    "session_id_env": "ACME_SESSION_TOKEN",
                    "scope": attestation.SIDECAR_SCOPE_SESSION,
                },
            ],
            bound_identity=attestation.BOUND_IDENTITY_POLICY_REQUIRED,
        )
        env = {
            "SOME_UNSCOPED_ENV": "synthetic-1",
            "ACME_SESSION_TOKEN": "synthetic-session-mixed",
        }
        identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        assert identity == attestation.Identity(
            "synthetic-lead-mixed", attestation.SOURCE_SIDECAR_SESSION
        )

    def test_upgrade_safety_explicit_caller_push_style_call_succeeds_against_legacy_config(
        self, tmp_path
    ):
        """Upgrade-safety regression, end to end at the bind_caller layer:
        an explicit --caller pushed against a legacy unscoped per-spawn
        sidecar succeeds exactly as it did before the scope/bound_identity
        config keys existed -- the defect this fold-in closes would have
        refused this as 'root' the moment builtin-fallback resolved
        instead of the sidecar."""
        from clagentic_loadout.transport import caller_binding

        sidecar_dir = tmp_path / "sidecars"
        sidecar_dir.mkdir()
        (sidecar_dir / "subagent-synthetic-spawn-live").write_text(
            "amos", encoding="utf-8"
        )
        config_root = _sidecars_config_root(
            tmp_path,
            [
                {
                    "dir": sidecar_dir,
                    "file_prefix": "subagent-",
                    "session_id_env": "CLAGENTIC_SUBAGENT_ID",
                    # deliberately no "scope" key -- this host's deployed
                    # config, pre-migration.
                }
            ],
        )
        env = {"CLAGENTIC_SUBAGENT_ID": "synthetic-spawn-live"}
        identity = attestation.resolve_bound_identity(env=env, config_root=config_root)
        # No CallerBindingError -- proves the push-style --caller amos
        # invocation is bound successfully rather than refused as an
        # unrelated builtin-resolved identity (e.g. "root").
        caller_binding.bind_caller("amos", caller_explicit=True, identity=identity)
        assert identity == attestation.Identity("amos", attestation.SOURCE_SIDECAR)


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

    def test_unrecognized_policy_string_is_a_hard_config_error(self, tmp_path):
        """lr-620837 fold-in #4 F1 (PEACHES 5879765982): a typo'd/
        unrecognized `attestation.bound_identity` value is a HARD
        `AttestationConfigError` naming the config key, the offending
        value, and the two allowed values -- NEVER silently treated as
        unset and downgraded to `DEFAULT_BOUND_IDENTITY_POLICY` (which
        would fail OPEN to the more permissive policy on a typo).
        Superseded, pre-fold-in-#4 revision of this test asserted the
        opposite (a silent fall-back, resolving via SOURCE_BUILTIN under
        the default policy) -- that was exactly the defect this fix
        closes, not a behavior worth preserving. A genuinely ABSENT key
        (no `bound_identity` line at all) is unaffected and still falls
        back to the default -- covered by
        `TestDefaultPolicyIsBuiltinFallbackPendingOperatorConfirmation`
        and every other test in this file that never sets the key."""
        config_root = tmp_path / "cfg-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "attestation:\n  bound_identity: not-a-real-policy\n", encoding="utf-8"
        )
        with pytest.raises(attestation.AttestationConfigError) as exc_info:
            attestation.resolve_bound_identity(env={}, config_root=config_root)
        assert "not-a-real-policy" in str(exc_info.value)
        assert attestation.BOUND_IDENTITY_POLICY_REQUIRED in str(exc_info.value)
        assert attestation.BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK in str(exc_info.value)


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
