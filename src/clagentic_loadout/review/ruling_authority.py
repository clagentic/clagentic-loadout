"""review.ruling_authority — which roles may write the ruling registry.

No caller-controlled input decides this: no flag, environment variable,
repository, checkout or caller-selected path contributes. The roles that hold the
``ruling`` verb come from the effective role-to-verb mapping:

  * the ``roles:`` mapping in the USER-LEVEL config file (``config.yaml`` under the
    loadout config root), when that key is present; it REPLACES the built-in
    mapping entirely, as the repo-tier ``roles:`` contract does; otherwise
  * the built-in ``DEFAULT_ROLE_VERBS``, which grants ``ruling`` to ``merger``.

An injected AuthorityProvider (decided by the caller of the verb, in process)
overrides both. The user-level mapping is read here and by nothing else: it does
not feed provisioning, doctor, model routing or any other verb.

Shape a deployment writes, in the user-level config file::

    roles:
      <role>:
        - ruling
        - <other verb labels the role holds>

Trust. The file is read only if it passes the same location check as the ruling
registry (``ruling_provider.untrusted_reason``): not inside a git work tree, and
neither the file nor any directory above it writable by a group or by everyone or
owned by someone else. A file that fails is refused, not skipped.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from clagentic_loadout.provisioning.roles import (
    CONFIG_SECTION_ROLES,
    InvalidRoleConfigError,
    parse_roles_section,
    roles_holding_verb,
)
from clagentic_loadout.review.policy_reader import MAX_POLICY_BYTES
from clagentic_loadout.review.ruling_provider import untrusted_reason
from clagentic_loadout.transport import provider_config

#: The verb label whose holders may write the registry.
AUTHORIZING_VERB = "ruling"


class UntrustedRolesSourceError(Exception):
    """The user-level config file holding ``roles:`` is in a location the pull
    request under review could write, so it is not used."""


def ruling_authorized_roles(config_root: str | Path | None = None) -> frozenset[str]:
    """The roles that hold the ``ruling`` verb in the effective mapping.

    Raises UntrustedRolesSourceError when the user-level config file exists but
    fails the location check, and InvalidRoleConfigError when it exists, passes
    it, and its ``roles:`` value or the file itself is unusable."""
    root = Path(config_root) if config_root is not None else provider_config.DEFAULT_USER_CONFIG_ROOT
    path = root / provider_config.USER_CONFIG_FILENAME
    if not path.exists():
        return roles_holding_verb(AUTHORIZING_VERB)
    reason = untrusted_reason(path)
    if reason is not None:
        raise UntrustedRolesSourceError(
            f"the user-level configuration {path} is not a trusted source for role "
            f"authority: {reason}"
        )
    try:
        raw_bytes = path.read_bytes()
        if len(raw_bytes) > MAX_POLICY_BYTES:
            raise OSError(f"larger than {MAX_POLICY_BYTES} bytes")
        document = yaml.safe_load(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise InvalidRoleConfigError(f"{path}: could not be read as YAML: {exc}.") from exc
    if document is None:
        return roles_holding_verb(AUTHORIZING_VERB)
    if not isinstance(document, dict):
        raise InvalidRoleConfigError(
            f"{path}: top-level document must be a mapping, got {type(document).__name__}."
        )
    if CONFIG_SECTION_ROLES not in document:
        return roles_holding_verb(AUTHORIZING_VERB)
    mapping = parse_roles_section(document[CONFIG_SECTION_ROLES], source=str(path))
    return frozenset(role for role, verbs in mapping.items() if AUTHORIZING_VERB in verbs)


__all__ = ["AUTHORIZING_VERB", "UntrustedRolesSourceError", "ruling_authorized_roles"]
