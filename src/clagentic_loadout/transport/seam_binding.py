"""transport.seam_binding -- adapt the identity provider seam to caller binding.

With `identity.provider` set, the caller-bound verbs take the process's identity
from the seam's `Actor` instead of the legacy attestation chain. This module is
the one place that mapping lives:

* `configured_actor` resolves the seam, or answers None when `identity.provider`
  is unset (or `assertion-or-legacy` without a delivery directory) so the verb
  keeps the legacy chain exactly as before.
* `identity_for_actor` maps an `Actor` to the `Identity` the binding layer
  compares. The role a verb mints is `--caller`/`--role`; omitted, it is the
  actor's single role. An actor that holds several roles, or none, must name one.
  An actor whose answer came from the legacy per-spawn sidecar (origin
  `legacy`) carries no role list, so it binds by name equality as the legacy
  chain always did.
* `resolve_named_role` binds a `--caller`/`--role` that names the actor, not a
  role, to the actor's role; the one place that rule lives.
* `provider_fields` is the additive `provider` / `self_declared` pair every
  result carries.
* `staging_env` and `staging_kwargs` root body staging and acquire scratch for the
  assertion providers: at the rendered `identity.staging_root` template when set,
  else in the verified delivery directory.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Mapping

from clagentic_loadout.identity.actor import Actor
from clagentic_loadout.identity.errors import IdentityError
from clagentic_loadout.transport.attestation import (
    SOURCE_ASSERTION,
    AttestationError,
    Identity,
)
from clagentic_loadout.transport.body_env import BODY_ENV_SUBDIR

LEGACY_ORIGIN = "legacy"
#: `provider` reported for every identity the legacy chain resolves.
PROVIDER_LEGACY = "legacy"
_GATEKEEPER_PROVIDER = "gatekeeper"
_JWKS_PROVIDER_PREFIX = "jwks:"
#: Subdirectory of the verified delivery directory that holds scratch.
DELIVERY_TMP_SUBDIR = "tmp"
#: Config key of the staging-root template; the same string `identity.config`
#: names, held here because that module cannot be imported at this level.
STAGING_ROOT_KEY = "identity.staging_root"


class SeamIdentity(Identity):
    """An `Identity` that came from the provider seam. A subclass so the base
    `Identity` keeps its two-field, credential-free shape; the extra fields are
    additive.

    `provider` names what produced it, `self_declared` is true when nothing
    outside this process vouched for it, `roles` is the role ceiling the actor
    holds (None when the answer carries no role list), and `actor`, `dir` and
    `jti` are the verified actor string, delivery directory and assertion id.
    """

    __slots__ = ("provider", "self_declared", "roles", "actor", "dir", "jti", "sid", "spawn", "resolved_role")

    def __init__(
        self,
        subject: str,
        source: str,
        *,
        provider: str,
        self_declared: bool,
        roles: tuple[str, ...] | None = None,
        actor: str | None = None,
        dir: str | None = None,  # noqa: A002 -- mirrors the claim name
        jti: str | None = None,
        sid: str | None = None,
        spawn: str | None = None,
    ) -> None:
        super().__init__(subject, source)
        self.provider = provider
        self.self_declared = self_declared
        self.roles = roles
        self.actor = actor
        self.dir = dir
        self.jti = jti
        self.sid = sid
        self.spawn = spawn
        # Call sites keep passing the flag value downstream, so this records which held role a name resolved to, for messages and inspection.
        self.resolved_role: str | None = None


def configured_actor(
    *, env: Mapping[str, str] | None = None, config_root: str | Path | None = None
) -> Actor | None:
    """The seam's `Actor`, or None when `identity.provider` is unset, or is
    `assertion-or-legacy` and `CLAGENTIC_IDENTITY_DIR` is unset or blank. In
    both cases the verb runs its existing legacy chain, so its result reports
    provider `legacy` and `self_declared` false.

    A refusal from the seam surfaces as `AttestationError`, the type every
    caller-bound verb already maps to its own mismatch exit code."""
    # Imported here: `identity.config` and `identity.seam` import the transport
    # package, whose initialisation imports this module, so loading the identity
    # package first (`python -m clagentic_loadout.identity.verb`) would otherwise
    # hit a partially initialised module.
    from clagentic_loadout.identity.config import PROVIDER_ASSERTION_OR_LEGACY, load_identity_config
    from clagentic_loadout.identity.seam import identity_dir_is_set, resolve

    try:
        config = load_identity_config(config_root=config_root)
        provider = config.provider
        if provider is None:
            return None
        if provider == PROVIDER_ASSERTION_OR_LEGACY and not identity_dir_is_set(
            os.environ if env is None else env
        ):
            return None
        actor = resolve(env=env, config_root=config_root)
        template = config.staging_root
    except IdentityError as exc:
        raise AttestationError(f"identity provider refused: {exc}") from exc
    # Rendered and prepared here as well as at staging time so a template that
    # cannot render for this actor, or a root that is not a private directory, is
    # refused in the binding step every verb already maps, before any call site
    # reaches `staging_kwargs` (none of which catches the error itself).
    if template is not None and _is_verified(actor.provider) and actor.dir:
        _prepare_staging_root(
            _render_staging_root(template, actor=actor.actor, sid=actor.sid, spawn=actor.spawn)
        )
    return actor


def _default_role(actor: Actor) -> str:
    if len(actor.roles) == 1:
        return actor.roles[0]
    present = ", ".join(actor.roles) or "none"
    raise AttestationError(
        f"actor {actor.actor!r} ({actor.provider}) holds roles [{present}] and "
        "--caller/--role was omitted; name the role to act as"
    )


def identity_for_actor(actor: Actor, *, caller_explicit: bool) -> SeamIdentity:
    """Map *actor* to the binding layer's `Identity`.

    `subject` is the actor string, except when the caller was omitted: then it
    is the role the verb will mint, so a call site that derives its effective
    caller from `identity.subject` gets a role the actor holds."""
    if actor.origin == LEGACY_ORIGIN:
        source = actor.provider.partition(":")[2] or SOURCE_ASSERTION
        return SeamIdentity(
            actor.actor, source, provider=actor.provider, self_declared=actor.self_declared
        )
    subject = actor.actor if caller_explicit else _default_role(actor)
    return SeamIdentity(
        subject,
        SOURCE_ASSERTION,
        provider=actor.provider,
        self_declared=actor.self_declared,
        roles=actor.roles,
        actor=actor.actor,
        dir=actor.dir,
        jti=actor.jti,
        sid=actor.sid,
        spawn=actor.spawn,
    )


class CallerNameRefused(Exception):
    """A `--caller`/`--role` value names the actor but resolves to no role it holds."""


def _caller_names(config_root: str | Path | None):
    # Imported here for the reason `configured_actor` gives. Read only when a
    # value is not already a held role, so a config that never meets a name
    # never has this key looked at.
    from clagentic_loadout.identity.config import load_identity_config

    try:
        return load_identity_config(config_root=config_root).caller_names()
    except IdentityError as exc:
        raise CallerNameRefused(f"identity.caller_names is invalid: {exc}") from exc


def resolve_named_role(
    identity: SeamIdentity, caller: str, *, config_root: str | Path | None = None
) -> str | None:
    """The role *caller* binds as when it names the actor, else None.

    *caller* names the actor when it is the actor string, or the actor string
    minus the configured `identity.caller_names.actor_prefix`. It then binds as
    `identity.caller_names.roles[caller]` (which must be a role the actor holds)
    or, with no entry, as the actor's single role. Raises `CallerNameRefused`
    when it names the actor but no role results. Only for an identity with a
    role list; a legacy-origin answer has none and never reaches the config."""
    roles = identity.roles or ()
    names = _caller_names(config_root)
    prefix = names.actor_prefix
    if caller != identity.actor and not (prefix and identity.actor == prefix + caller):
        return None
    present = ", ".join(roles) or "none"
    where = f"the actor {identity.actor!r} ({identity.provider}) holds roles [{present}]"
    if caller in names.roles:
        role = names.roles[caller]
        if role not in roles:
            raise CallerNameRefused(
                f"--caller/--role {caller!r} names {where}, but identity.caller_names.roles "
                f"maps it to {role!r}, which the actor does not hold: roles present [{present}]"
            )
        return role
    if len(roles) == 1:
        return roles[0]
    raise CallerNameRefused(
        f"--caller/--role {caller!r} names {where}: roles present [{present}]; name one "
        f"role, or map {caller!r} to one in identity.caller_names.roles"
    )


def provider_fields(identity: Identity) -> dict[str, object]:
    """The additive `provider` / `self_declared` pair for a result or comment."""
    if isinstance(identity, SeamIdentity):
        return {"provider": identity.provider, "self_declared": identity.self_declared}
    return {"provider": PROVIDER_LEGACY, "self_declared": False}


def staging_kwargs(identity: Identity, *, actor_key: str | None = None) -> dict[str, object]:
    """Keyword arguments that root a body-staging or scratch call in the verified
    delivery directory: `env`, plus the staging actor under *actor_key* when one
    is named. Empty unless an assertion provider verified the identity, so a call
    that passes `**staging_kwargs(...)` is byte-for-byte the existing call
    otherwise."""
    env = staging_env(identity)
    if env is None:
        return {}
    out: dict[str, object] = {"env": env}
    if actor_key is not None:
        out[actor_key] = identity.actor
    return out


def _is_verified(provider: str) -> bool:
    return provider == _GATEKEEPER_PROVIDER or provider.startswith(_JWKS_PROVIDER_PREFIX)


def _claim_component(template: str, placeholder: str, value: str | None) -> str:
    """*value* when it is one safe path component, else `AttestationError`."""
    where = f"{STAGING_ROOT_KEY} {template!r}, placeholder {{{placeholder}}}"
    if value is None:
        raise AttestationError(f"{where}: the verified assertion carries no such claim")
    if not value or value in (".", "..") or "/" in value or "\0" in value:
        raise AttestationError(
            f"{where}: claim value {value!r} is not a single safe path component"
        )
    return value


def _render_staging_root(
    template: str, *, actor: str | None, sid: str | None, spawn: str | None
) -> str:
    """Render *template* over the verified claims into an absolute path.

    `{actor_name}` is the actor string after its `kind:` prefix (the whole string
    when it has none). A claim that is absent, or is not one path component, or a
    result that is not absolute, raises `AttestationError` naming the key, the
    template and the offending placeholder or value; there is no fallback root."""
    from clagentic_loadout.identity.config import staging_root_parts

    actor_name = actor.partition(":")[2] if actor and ":" in actor else actor
    claims = {"actor_name": actor_name, "sid": sid, "spawn": spawn}
    try:
        parts = staging_root_parts(template)
    except IdentityError as exc:
        raise AttestationError(f"identity provider refused: {exc}") from exc
    rendered = "".join(
        literal + (_claim_component(template, name, claims[name]) if name else "")
        for literal, name in parts
    )
    if not os.path.isabs(rendered):
        raise AttestationError(
            f"{STAGING_ROOT_KEY} {template!r} renders to {rendered!r}, which is not an absolute path"
        )
    return rendered


def _require_private_dir(path: Path) -> None:
    """Refuse *path* unless it is a real directory owned by this process's
    effective user with no group or other write bit. Never repairs it: a
    pre-existing directory is not ours to chmod or chown."""
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        problem = "a symlink" if stat.S_ISLNK(info.st_mode) else "not a directory"
    elif info.st_uid != os.geteuid():
        problem = f"owner uid {info.st_uid} not {os.geteuid()}"
    elif info.st_mode & 0o022:
        problem = f"mode {oct(stat.S_IMODE(info.st_mode))} is group or other writable"
    else:
        return
    raise AttestationError(f"{STAGING_ROOT_KEY}: staging directory {str(path)!r} is refused: {problem}")


def _prepare_staging_root(root: str) -> None:
    """Create *root* and its body-staging subdirectory, mode 0700, when absent,
    and refuse either when it is not a private directory of this user. The
    parents above *root* are not checked."""
    try:
        Path(root).mkdir(mode=0o700, parents=True, exist_ok=True)
        _require_private_dir(Path(root))
        (Path(root) / BODY_ENV_SUBDIR).mkdir(mode=0o700, exist_ok=True)
        _require_private_dir(Path(root) / BODY_ENV_SUBDIR)
    except OSError as exc:
        raise AttestationError(f"{STAGING_ROOT_KEY}: cannot create staging root {root!r}: {exc}") from exc


def staging_temp_parent(
    identity: Identity | None,
    *,
    config_root: str | Path | None = None,
) -> str | None:
    """The directory a verb's own temporary directories are created under: the
    rendered `identity.staging_root` (created 0700 if absent) for an identity
    verified by an assertion provider when that key is set, else None.

    None means "keep today's location" (the temp-directory default, or the
    inherited TMPDIR). Unlike `staging_env`, an unset key never falls back to the
    delivery directory's `tmp/`, so a verb that does not opt in to the key sees
    no change. Static, legacy and module identities are never rooted here."""
    from clagentic_loadout.identity.config import load_identity_config

    if not isinstance(identity, SeamIdentity) or not identity.dir:
        return None
    if not _is_verified(identity.provider):
        return None
    try:
        template = load_identity_config(config_root=config_root).staging_root
    except IdentityError as exc:
        raise AttestationError(f"identity provider refused: {exc}") from exc
    if template is None:
        return None
    root = _render_staging_root(template, actor=identity.actor, sid=identity.sid, spawn=identity.spawn)
    _prepare_staging_root(root)
    return root


def staging_env(
    identity: Identity,
    env: Mapping[str, str] | None = None,
    *,
    config_root: str | Path | None = None,
) -> dict[str, str] | None:
    """An environment whose TMPDIR roots staging for an identity verified by an
    assertion provider, else None (the caller's own TMPDIR applies).

    The root is `identity.staging_root` rendered over the verified claims when
    that key is set (created 0700 if absent), else `<dir>/tmp`. Both come from the
    verified claims, so two actors never share a staging root. Static, legacy and
    module identities have no verified directory and the key has no effect."""
    from clagentic_loadout.identity.config import load_identity_config

    if not isinstance(identity, SeamIdentity) or not identity.dir:
        return None
    if not _is_verified(identity.provider):
        return None
    try:
        template = load_identity_config(config_root=config_root).staging_root
    except IdentityError as exc:
        raise AttestationError(f"identity provider refused: {exc}") from exc
    out = dict(os.environ if env is None else env)
    if template is None:
        out["TMPDIR"] = str(Path(identity.dir) / DELIVERY_TMP_SUBDIR)
        return out
    root = _render_staging_root(template, actor=identity.actor, sid=identity.sid, spawn=identity.spawn)
    _prepare_staging_root(root)
    out["TMPDIR"] = root
    return out
