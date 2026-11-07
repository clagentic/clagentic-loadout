"""identity.seam -- `resolve()`, the identity provider seam.

An identity is verified, explicitly declared, or refused; it is never inferred
from a parent session or from the OS user. `resolve()` reads `identity.provider`
and answers from exactly that provider.

* `gatekeeper`, `jwks`: a signed assertion from CLAGENTIC_IDENTITY_DIR, or a
  refusal naming the variable. No fallback of any kind.
* `static`: the declared actor, `self_declared` true, no environment read.
* `module`: a user-supplied provider.
* `prefer-assertion` (transition): the assertion provider answers when the
  directory variable is set. Otherwise the legacy chain runs, but only under the
  `required` bound-identity policy whatever `attestation.bound_identity` says, so
  a per-spawn miss refuses and no parent or OS-user identity is ever returned.
  Each fallback emits one stderr notice and one telemetry event.
* unset: a config with legacy `attestation` keys keeps the legacy chain; a config
  with neither refuses naming `identity.provider`.

This module is the only place the no-default refusal lives. Verbs do not call it
yet, so a config without `identity.provider` behaves for them exactly as before.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from typing import Mapping

from clagentic_loadout.identity import errors
from clagentic_loadout.identity.actor import Actor
from clagentic_loadout.identity.config import (
    ASSERTION_PROVIDERS,
    PROVIDER_GATEKEEPER,
    PROVIDER_JWKS,
    PROVIDER_KEY,
    PROVIDER_MODULE,
    PROVIDER_PREFER_ASSERTION,
    PROVIDER_STATIC,
    IdentityConfig,
    load_identity_config,
)
from clagentic_loadout.identity.delivery import IDENTITY_DIR_ENV
from clagentic_loadout.identity.providers import (
    AssertionProvider,
    ModuleProvider,
    Provider,
    StaticProvider,
)
from clagentic_loadout.telemetry import SinkConfigError, resolve_sink
from clagentic_loadout.transport import attestation
from clagentic_loadout.transport.provider_config import load_user_config_section

FALLBACK_EVENT_SCHEMA = "identity-fallback/v1"
_LEGACY_KEYS = (
    attestation.ATTESTATION_CONFIG_KEY_IDENTITY_ENV,
    attestation.ATTESTATION_CONFIG_KEY_SIDECAR_PATH,
    attestation.ATTESTATION_CONFIG_KEY_SIDECARS,
    attestation.ATTESTATION_CONFIG_KEY_BOUND_IDENTITY,
)


def has_legacy_attestation_config(*, config_root: str | Path | None = None) -> bool:
    section = load_user_config_section(attestation.ATTESTATION_CONFIG_SECTION, config_root=config_root)
    return any(section.get(key) for key in _LEGACY_KEYS)


def _assertion_provider(kind: str, cfg: IdentityConfig) -> Provider:
    issuers = cfg.gatekeeper_issuers() if kind == PROVIDER_GATEKEEPER else cfg.jwks_issuers()
    return AssertionProvider(kind, issuers)


def _legacy_actor(
    env: Mapping[str, str], config_root: str | Path | None, *, policy_override: str | None
) -> Actor:
    """Run the existing chain unchanged and wrap its answer. The legacy chain
    reads sidecars and a configured env value that nothing signed, so the result
    is marked self-declared and given no grade."""
    try:
        identity = attestation.resolve_bound_identity(
            env=dict(env), config_root=config_root, policy_override=policy_override
        )
    except attestation.AttestationError as exc:
        raise errors.IdentityError(errors.ATTESTATION_REFUSED, str(exc)) from exc
    return Actor(
        sub=identity.subject,
        actor=identity.subject,
        chain=(identity.subject,),
        roles=(),
        grade="unverified",
        origin="legacy",
        provider=f"legacy:{identity.source}",
        self_declared=True,
    )


def _announce_fallback(env: Mapping[str, str]) -> None:
    print(
        f"identity: {IDENTITY_DIR_ENV} is unset; prefer-assertion is using the legacy "
        "attestation chain under the required policy.",
        file=sys.stderr,
    )
    record = {
        "schema": FALLBACK_EVENT_SCHEMA,
        "ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": PROVIDER_PREFER_ASSERTION,
        "reason": f"{IDENTITY_DIR_ENV} unset",
    }
    try:
        resolve_sink(dict(env)).emit(record)
    except SinkConfigError as exc:
        print(f"identity: telemetry sink not usable for the fallback event: {exc}", file=sys.stderr)


def resolve(
    *, env: Mapping[str, str] | None = None, config_root: str | Path | None = None
) -> Actor:
    """Return the configured provider's `Actor`, or raise `IdentityError`."""
    active_env = os.environ if env is None else env
    cfg = load_identity_config(config_root=config_root)

    if cfg.provider is None:
        if has_legacy_attestation_config(config_root=config_root):
            return _legacy_actor(active_env, config_root, policy_override=None)
        raise errors.IdentityError(
            errors.PROVIDER_UNSET,
            f"{PROVIDER_KEY} is not set; choose one of gatekeeper, jwks, static, "
            "prefer-assertion or set identity.provider_module. There is no default.",
        )
    if cfg.provider in ASSERTION_PROVIDERS:
        return _assertion_provider(cfg.provider, cfg).resolve(active_env)
    if cfg.provider == PROVIDER_STATIC:
        actor, roles = cfg.static_declaration()
        return StaticProvider(actor, roles).resolve(active_env)
    if cfg.provider == PROVIDER_MODULE:
        return ModuleProvider(cfg.provider_module or "", cfg.raw).resolve(active_env)
    if cfg.provider == PROVIDER_PREFER_ASSERTION:
        if cfg.assertion_provider is None:
            raise errors.IdentityError(
                errors.CONFIG_INVALID,
                f"{PROVIDER_KEY} prefer-assertion requires identity.assertion_provider "
                f"({PROVIDER_GATEKEEPER} or {PROVIDER_JWKS})",
            )
        provider = _assertion_provider(cfg.assertion_provider, cfg)
        if (active_env.get(IDENTITY_DIR_ENV) or "").strip():
            return provider.resolve(active_env)
        _announce_fallback(active_env)
        return _legacy_actor(
            active_env, config_root, policy_override=attestation.BOUND_IDENTITY_POLICY_REQUIRED
        )
    raise errors.IdentityError(errors.CONFIG_INVALID, f"{PROVIDER_KEY} {cfg.provider!r} is not supported")
