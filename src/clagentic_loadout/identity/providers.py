"""identity.providers -- the provider interface and its implementations.

A provider is anything with `resolve(env) -> Actor` that raises `IdentityError`
instead of returning a partial answer. Three classes ship:

  assertion  gatekeeper and jwks: verify a signed assertion delivered through
             CLAGENTIC_IDENTITY_DIR against configured issuers.
  static     a declared actor; nothing outside this process vouches for it.
  module     a user-supplied module's `resolve(*, env, config)`.

No provider has a fallback: every failure is a refusal.
"""

from __future__ import annotations

import importlib
from dataclasses import replace
from typing import Any, Mapping, Protocol, Sequence

from clagentic_loadout.identity import errors
from clagentic_loadout.identity.actor import Actor, actor_from_claims
from clagentic_loadout.identity.config import PROVIDER_GATEKEEPER, TrustedIssuer
from clagentic_loadout.identity.delivery import read_delivery
from clagentic_loadout.identity.jwks_source import load_jwks
from clagentic_loadout.identity.verifier import MODE_LOCAL, verify

STATIC_PROVIDER_NAME = "declared"


class Provider(Protocol):
    def resolve(self, env: Mapping[str, str]) -> Actor: ...


class AssertionProvider:
    """Class 1 (`gatekeeper`) and class 2 (`jwks`) share one verification path."""

    def __init__(self, kind: str, issuers: Sequence[TrustedIssuer]) -> None:
        self._kind = kind
        self._locators = {i.iss: i.locator for i in issuers}

    def resolve(self, env: Mapping[str, str]) -> Actor:
        token, directory = read_delivery(env)
        claims = verify(
            token,
            trusted_issuers=tuple(self._locators),
            jwks_loader=lambda iss: load_jwks(self._locators[iss]),
            mode=MODE_LOCAL,
            observed_dir=directory,
        )
        name = self._kind if self._kind == PROVIDER_GATEKEEPER else f"jwks:{claims['iss']}"
        return actor_from_claims(claims, provider=name)


class StaticProvider:
    """Class 3: the declared actor. Reads no environment and verifies nothing."""

    def __init__(self, actor: str, roles: Sequence[str]) -> None:
        self._actor = actor
        self._roles = tuple(roles)

    def resolve(self, env: Mapping[str, str]) -> Actor:
        del env
        return Actor(
            sub=self._actor,
            actor=self._actor,
            chain=(self._actor,),
            roles=self._roles,
            grade="declared",
            origin="declared",
            provider=STATIC_PROVIDER_NAME,
            self_declared=True,
        )


class ModuleProvider:
    """A user-supplied provider. Its Actor is used, its provider name recorded as
    `module:<name>` whatever the module put there, so a module cannot present
    itself as a verified issuer."""

    def __init__(self, module_name: str, config: Mapping[str, Any]) -> None:
        self._name = module_name
        self._config = config

    def resolve(self, env: Mapping[str, str]) -> Actor:
        try:
            module = importlib.import_module(self._name)
        except ImportError as exc:
            raise errors.IdentityError(
                errors.PROVIDER_MODULE_INVALID, f"identity.provider_module {self._name!r} cannot be imported"
            ) from exc
        resolve = getattr(module, "resolve", None)
        if not callable(resolve):
            raise errors.IdentityError(
                errors.PROVIDER_MODULE_INVALID, f"module {self._name!r} defines no resolve()"
            )
        try:
            actor = resolve(env=env, config=self._config)
        except errors.IdentityError:
            raise
        except Exception as exc:  # noqa: BLE001 -- re-raised typed, with the cause chained
            raise errors.IdentityError(
                errors.PROVIDER_MODULE_INVALID,
                f"module {self._name!r} resolve() failed: {type(exc).__name__}",
            ) from exc
        if not isinstance(actor, Actor):
            raise errors.IdentityError(
                errors.PROVIDER_MODULE_INVALID, f"module {self._name!r} resolve() did not return an Actor"
            )
        return replace(actor, provider=f"module:{self._name}")
