"""identity.config -- the user-level `identity:` config section.

Read through the shared user-level section loader and parsed here, in the seam's
own resolution path. Nothing in this module runs when a verb loads its config, so
a config without `identity.provider` changes nothing for any existing verb.

Keys (all optional at load; `provider` is required when the seam resolves):

  identity.provider           gatekeeper | jwks | static | prefer-assertion | assertion-or-legacy | module
  identity.provider_module    dotted module name implementing `resolve(*, env, config)`
  identity.assertion_provider gatekeeper | jwks; which provider prefer-assertion and
                              assertion-or-legacy answer with
  identity.gatekeeper.jwks    key-set locator of the single trusted gatekeeper issuer
  identity.gatekeeper.issuer  issuer identifier (defaults to a file:/unix: jwks locator)
  identity.issuers            list of {iss, jwks?} trusted by provider jwks
  identity.static.actor       declared actor
  identity.static.roles       declared role list
  identity.caller_names.actor_prefix  caller X names the actor <actor_prefix>X
  identity.caller_names.roles         map of caller name to the one role it binds as
  identity.staging_root       template for the body-staging and scratch root under an
                              assertion provider; placeholders {actor_name}, {sid}, {spawn}
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field
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
PROVIDER_ASSERTION_OR_LEGACY = "assertion-or-legacy"
PROVIDER_MODULE = "module"
PROVIDERS = (
    PROVIDER_GATEKEEPER,
    PROVIDER_JWKS,
    PROVIDER_STATIC,
    PROVIDER_PREFER_ASSERTION,
    PROVIDER_ASSERTION_OR_LEGACY,
    PROVIDER_MODULE,
)
ASSERTION_PROVIDERS = (PROVIDER_GATEKEEPER, PROVIDER_JWKS)
#: Providers whose `identity.assertion_provider` names the assertion class that answers.
ASSERTION_SELECTING_PROVIDERS = (PROVIDER_PREFER_ASSERTION, PROVIDER_ASSERTION_OR_LEGACY)


STAGING_ROOT_KEY = "identity.staging_root"
#: Per-spawn placeholders: a template must name at least one so two spawns, and
#: so two actors, never share a staging root.
STAGING_PER_SPAWN_PLACEHOLDERS = ("sid", "spawn")
STAGING_PLACEHOLDERS = ("actor_name", *STAGING_PER_SPAWN_PLACEHOLDERS)


def _invalid(message: str) -> errors.IdentityError:
    return errors.IdentityError(errors.CONFIG_INVALID, message)


def staging_root_parts(template: object) -> list[tuple[str, str | None]]:
    """Split a `identity.staging_root` template into `(literal, placeholder)` pairs.

    Raises `config_invalid` naming the key and the template when it is not a
    string, uses an unknown placeholder (or a format spec or conversion), or names
    no per-spawn placeholder, or has a literal '.' or '..' path segment."""
    if not isinstance(template, str) or not template:
        raise _invalid(f"{STAGING_ROOT_KEY} must be a non-empty string template, got {template!r}")
    parts: list[tuple[str, str | None]] = []
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise _invalid(f"{STAGING_ROOT_KEY} {template!r} is not a valid template: {exc}") from exc
    for literal, field_name, spec, conversion in parsed:
        if field_name is not None and (
            field_name not in STAGING_PLACEHOLDERS or spec or conversion
        ):
            raise _invalid(
                f"{STAGING_ROOT_KEY} {template!r} has unknown placeholder {{{field_name}}}; "
                f"known placeholders: {', '.join(STAGING_PLACEHOLDERS)}"
            )
        parts.append((literal, field_name))
    if not any(name in STAGING_PER_SPAWN_PLACEHOLDERS for _, name in parts):
        raise _invalid(
            f"{STAGING_ROOT_KEY} {template!r} names no per-spawn placeholder; "
            f"include {{sid}} or {{spawn}} so two spawns never share a root"
        )
    # A literal '.' or '..' segment would collapse distinct sids into one directory.
    opaque = "".join(literal + ("{}" if name else "") for literal, name in parts)
    if any(segment in (".", "..") for segment in opaque.split("/")):
        raise _invalid(
            f"{STAGING_ROOT_KEY} {template!r} has a '.' or '..' path segment; "
            "a staging root must not collapse spawns into one directory"
        )
    return parts


@dataclass(frozen=True)
class TrustedIssuer:
    iss: str
    locator: str


@dataclass(frozen=True)
class CallerNames:
    """How a `--caller`/`--role` value that names the actor, not a role, resolves."""

    actor_prefix: str | None = None
    roles: Mapping[str, str] = field(default_factory=dict)


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

    @property
    def staging_root(self) -> str | None:
        """The validated `identity.staging_root` template, or None when unset."""
        template = self.raw.get("staging_root")
        if template is not None:
            staging_root_parts(template)
        return template

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
        kind = self.assertion_provider if self.provider in ASSERTION_SELECTING_PROVIDERS else self.provider
        if kind == PROVIDER_GATEKEEPER:
            return (kind,), self.gatekeeper_issuers()
        if kind == PROVIDER_JWKS:
            return (kind,), self.jwks_issuers()
        raise _invalid(
            f"verifying an assertion needs {PROVIDER_KEY} gatekeeper or jwks "
            "(or prefer-assertion or assertion-or-legacy with identity.assertion_provider)"
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

    def caller_names(self) -> CallerNames:
        """The optional `caller_names` section; empty when absent."""
        section = self._mapping("caller_names")
        prefix = section.get("actor_prefix")
        roles = section.get("roles")
        if prefix is not None and (not isinstance(prefix, str) or not prefix):
            raise _invalid("identity.caller_names.actor_prefix must be a non-empty string")
        if roles is not None and (
            not isinstance(roles, dict)
            or not all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in roles.items())
        ):
            raise _invalid("identity.caller_names.roles must map a caller name to one role name")
        return CallerNames(actor_prefix=prefix, roles=dict(roles or {}))


def check_assertion_provider(assertion: object) -> None:
    """Raise `config_invalid` when *assertion* is set but names no assertion provider."""
    if assertion is not None and assertion not in ASSERTION_PROVIDERS:
        raise _invalid(
            f"identity.assertion_provider {assertion!r} is not one of {', '.join(ASSERTION_PROVIDERS)}"
        )


def load_identity_config(*, config_root: str | Path | None = None) -> IdentityConfig:
    raw = load_user_config_section(SECTION, config_root=config_root)
    provider = raw.get("provider")
    module = raw.get("provider_module")
    assertion = raw.get("assertion_provider")
    if provider is not None and provider not in PROVIDERS:
        raise _invalid(f"{PROVIDER_KEY} {provider!r} is not one of {', '.join(PROVIDERS)}")
    if module is not None and (not isinstance(module, str) or not module):
        raise _invalid("identity.provider_module must be a dotted module name")
    # assertion-or-legacy reads the assertion config only when a directory is
    # delivered, so a bad value is reported then (`check_assertion_provider`).
    if provider != PROVIDER_ASSERTION_OR_LEGACY:
        check_assertion_provider(assertion)
    if module is not None and provider not in (None, PROVIDER_MODULE):
        raise _invalid(f"identity.provider_module conflicts with {PROVIDER_KEY} {provider!r}")
    if provider == PROVIDER_MODULE and module is None:
        raise _invalid("identity.provider module requires identity.provider_module")
    if raw.get("staging_root") is not None:
        staging_root_parts(raw["staging_root"])
    return IdentityConfig(
        provider=PROVIDER_MODULE if module is not None else provider,
        provider_module=module,
        assertion_provider=assertion,
        raw=raw,
    )
