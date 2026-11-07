"""identity.config -- the user-level `identity:` config section.

Read through the shared user-level section loader and parsed here, in the seam's
own resolution path. Nothing in this module runs when a verb loads its config, so
a config without `identity.provider` changes nothing for any existing verb.

Keys (all optional at load; `provider` is required when the seam resolves):

  identity.provider           gatekeeper | jwks | static | prefer-assertion | module
  identity.provider_module    dotted module name implementing `resolve(*, env, config)`
  identity.assertion_provider gatekeeper | jwks; which provider prefer-assertion answers with
  identity.gatekeeper.jwks    key-set locator of the single trusted gatekeeper issuer
  identity.gatekeeper.issuer  issuer identifier (defaults to a file:/unix: jwks locator)
  identity.issuers            list of {iss, jwks?} trusted by provider jwks
  identity.static.actor       declared actor
  identity.static.roles       declared role list
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from clagentic_loadout.identity import errors
from clagentic_loadout.identity.jwks_source import derive_locator, is_locator
from clagentic_loadout.transport.provider_config import load_user_config_section

SECTION = "identity"
PROVIDER_KEY = "identity.provider"

PROVIDER_GATEKEEPER = "gatekeeper"
PROVIDER_JWKS = "jwks"
PROVIDER_STATIC = "static"
PROVIDER_PREFER_ASSERTION = "prefer-assertion"
PROVIDER_MODULE = "module"
PROVIDERS = (
    PROVIDER_GATEKEEPER,
    PROVIDER_JWKS,
    PROVIDER_STATIC,
    PROVIDER_PREFER_ASSERTION,
    PROVIDER_MODULE,
)
ASSERTION_PROVIDERS = (PROVIDER_GATEKEEPER, PROVIDER_JWKS)


def _invalid(message: str) -> errors.IdentityError:
    return errors.IdentityError(errors.CONFIG_INVALID, message)


@dataclass(frozen=True)
class TrustedIssuer:
    iss: str
    locator: str


@dataclass(frozen=True)
class IdentityConfig:
    provider: str | None
    provider_module: str | None
    assertion_provider: str | None
    raw: Mapping[str, Any]

    def _mapping(self, key: str) -> Mapping[str, Any]:
        value = self.raw.get(key)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise _invalid(f"identity.{key} must be a mapping")
        return value

    def gatekeeper_issuers(self) -> tuple[TrustedIssuer, ...]:
        section = self._mapping("gatekeeper")
        jwks = section.get("jwks")
        if not isinstance(jwks, str) or not is_locator(jwks):
            raise _invalid("identity.gatekeeper.jwks must be a file:///, unix:///, https:// or http:// locator")
        issuer = section.get("issuer")
        if issuer is None and jwks.startswith(("file:///", "unix:///")):
            issuer = jwks
        if not isinstance(issuer, str) or not issuer:
            raise _invalid("identity.gatekeeper.issuer is required when jwks is an http(s) locator")
        return (TrustedIssuer(issuer, jwks),)

    def jwks_issuers(self) -> tuple[TrustedIssuer, ...]:
        entries = self.raw.get("issuers")
        if not isinstance(entries, list) or not entries:
            raise _invalid("identity.issuers must be a non-empty list of {iss, jwks?}")
        out: list[TrustedIssuer] = []
        for index, entry in enumerate(entries, start=1):
            iss = entry.get("iss") if isinstance(entry, dict) else None
            if not isinstance(iss, str) or not iss:
                raise _invalid(f"identity.issuers[{index}].iss must be a non-empty string")
            locator = entry.get("jwks") or derive_locator(iss)
            if not isinstance(locator, str) or not is_locator(locator):
                raise _invalid(
                    f"identity.issuers[{index}].jwks is required: {iss!r} names no key-set locator"
                )
            out.append(TrustedIssuer(iss, locator))
        return tuple(out)

    def assertion_issuers(self) -> tuple[tuple[str, ...], tuple[TrustedIssuer, ...]]:
        """`(kind, issuers)` of the assertion class this config selects, for
        callers that verify a token without resolving an identity."""
        kind = self.assertion_provider if self.provider == PROVIDER_PREFER_ASSERTION else self.provider
        if kind == PROVIDER_GATEKEEPER:
            return (kind,), self.gatekeeper_issuers()
        if kind == PROVIDER_JWKS:
            return (kind,), self.jwks_issuers()
        raise _invalid(
            f"verifying an assertion needs {PROVIDER_KEY} gatekeeper or jwks "
            "(or prefer-assertion with identity.assertion_provider)"
        )

    def static_declaration(self) -> tuple[str, tuple[str, ...]]:
        section = self._mapping("static")
        actor = section.get("actor")
        roles = section.get("roles", [])
        if not isinstance(actor, str) or not actor.strip():
            raise _invalid("identity.static.actor must be a non-empty string")
        if not isinstance(roles, list) or not all(isinstance(r, str) and r for r in roles):
            raise _invalid("identity.static.roles must be a list of role names")
        return actor.strip(), tuple(roles)


def load_identity_config(*, config_root: str | Path | None = None) -> IdentityConfig:
    raw = load_user_config_section(SECTION, config_root=config_root)
    provider = raw.get("provider")
    module = raw.get("provider_module")
    assertion = raw.get("assertion_provider")
    if provider is not None and provider not in PROVIDERS:
        raise _invalid(f"{PROVIDER_KEY} {provider!r} is not one of {', '.join(PROVIDERS)}")
    if module is not None and (not isinstance(module, str) or not module):
        raise _invalid("identity.provider_module must be a dotted module name")
    if assertion is not None and assertion not in ASSERTION_PROVIDERS:
        raise _invalid(
            f"identity.assertion_provider {assertion!r} is not one of {', '.join(ASSERTION_PROVIDERS)}"
        )
    if module is not None and provider not in (None, PROVIDER_MODULE):
        raise _invalid(f"identity.provider_module conflicts with {PROVIDER_KEY} {provider!r}")
    if provider == PROVIDER_MODULE and module is None:
        raise _invalid("identity.provider module requires identity.provider_module")
    return IdentityConfig(
        provider=PROVIDER_MODULE if module is not None else provider,
        provider_module=module,
        assertion_provider=assertion,
        raw=raw,
    )
