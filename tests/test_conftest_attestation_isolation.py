"""test_conftest_attestation_isolation.py — regression coverage for
tests/conftest.py's `_isolate_real_attestation_chain` autouse fixture
(lr-dbc905 follow-up).

Proves the exact defect a coordinator review caught on PR #10: the real,
unwrapped `transport.attestation.resolve_identity()` -- reached whenever a
wrapped verb's `main()` is called without `--caller`/`--role` in argv, so
`tests/conftest.py`'s `_default_identity_matches_parsed_caller` never
injects its own `identity_provider=` -- was previously called with zero
explicit arguments, meaning it read the LIVE process `os.environ` and the
import-time-bound `attestation.DEFAULT_USER_CONFIG_ROOT` with no isolation.
Inside a crew-spawned agent session that resolves a live session-specific
identity via a sidecar adapter (or fails closed with `AttestationError`
when a sidecar env var names a file that happens not to exist at read
time), the suite's result silently depended on which agent session
happened to be running it -- a suite result that is not reproducible
independent of the runner's own identity is not a gate (repo CLAUDE.md
hard rule 6).

These tests exercise `transport.attestation.resolve_identity()` directly
(zero explicit arguments -- the exact call shape the affected verb `main()`
functions use), with the SAME `_isolate_real_attestation_chain` autouse
fixture active that protects every other test in the suite, and prove it
resolves deterministically to the built-in OS-user layer regardless of
what a live crew/harness spawn environment set those variables to BEFORE
this test process started.
"""

from __future__ import annotations

import os

import pytest

from clagentic_loadout.transport import attestation, provider_config


class TestIsolateRealAttestationChainFixture:
    def test_zero_arg_resolve_identity_is_builtin_by_default(self):
        # With the autouse fixture active (every test in this suite),
        # os.environ is scrubbed of the attestation-steering vars and
        # DEFAULT_USER_CONFIG_ROOT points at an empty per-test tmp dir --
        # zero-arg resolve_identity() must fall through to the built-in
        # OS-user layer, never a live sidecar/configured identity, no
        # matter what the process's real ambient environment carried
        # before pytest started.
        identity = attestation.resolve_identity()
        assert identity.source == attestation.SOURCE_BUILTIN

    def test_isolated_config_root_has_no_config_file(self):
        # The config-root half of the fix: DEFAULT_USER_CONFIG_ROOT must
        # point at a directory with no config.yaml, so a live crew
        # deployment's real ~/.config/clagentic/loadout/config.yaml
        # (which may declare a `sidecars:` adapter list keyed on
        # CLAUDE_CODE_SESSION_ID) is never consulted during a test run.
        config_path = attestation.DEFAULT_USER_CONFIG_ROOT / "config.yaml"
        assert not config_path.exists()

    def test_isolated_config_root_is_not_the_real_provider_config_default(self):
        # attestation.py binds its own independent copy of
        # DEFAULT_USER_CONFIG_ROOT via `from ... import
        # DEFAULT_USER_CONFIG_ROOT` at import time -- the fixture must
        # patch THAT copy (attestation.DEFAULT_USER_CONFIG_ROOT), not
        # transport.provider_config's, or resolve_identity's own bare-name
        # lookup would never see the redirect at all.
        assert attestation.DEFAULT_USER_CONFIG_ROOT != provider_config.DEFAULT_USER_CONFIG_ROOT

    def test_attestation_steering_env_vars_scrubbed(self):
        # CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_ENV / _SIDECAR_PATH are the
        # env-tier overrides layers 1/2 of the chain read directly;
        # CLAGENTIC_SUBAGENT_ID / CLAUDE_CODE_SESSION_ID are the session-id
        # env vars a live deployment's config-file `sidecars:` adapter list
        # can key on (see attestation.py's own module docstring, layer 2
        # source (c)). Scrubbed even though the isolated
        # DEFAULT_USER_CONFIG_ROOT (previous tests) already makes any such
        # config-file adapter list unreachable, since a test should not
        # rely on only one of the two independent isolation mechanisms
        # holding, and CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH is
        # read directly regardless of config_root at all (module docstring
        # layer 2, source (a)).
        assert attestation.ATTESTED_IDENTITY_ENV_VAR not in os.environ
        assert attestation.ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR not in os.environ
        assert "CLAGENTIC_SUBAGENT_ID" not in os.environ
        assert "CLAUDE_CODE_SESSION_ID" not in os.environ

    def test_explicit_sidecar_path_override_within_a_test_still_fails_closed_on_miss(
        self, monkeypatch
    ):
        # Sanity check that the isolation fixture does not change
        # resolve_identity's own production fail-closed-on-miss contract
        # (lr-1e16a4) -- a TEST that deliberately re-opts into the env-var
        # override (simulating "a caller made an explicit per-invocation
        # claim") still gets the real, unmodified behavior: a sidecar path
        # naming a missing file is refused, not silently demoted to a
        # lower-precedence source. This is the CORRECT existing behavior;
        # the isolation fixture only removes the AMBIENT, un-asked-for
        # version of this trigger a live crew session could otherwise
        # leave behind for every test that never opted into it.
        monkeypatch.setenv(
            attestation.ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR,
            "/tmp/definitely-does-not-exist-lr-dbc905-proof",
        )
        with pytest.raises(attestation.AttestationError):
            attestation.resolve_identity()
