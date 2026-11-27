"""session.config -- read and validate the user-level `session_profile:` section.

The section lives only in the user-level loadout config file
(``~/.config/clagentic/loadout/config.yaml``), read through the shared
``load_user_config_section`` loader; a repository's own config file is never
read, so a cloned repository cannot steer a session's sandbox.

    session_profile:
      scratch_root: /srv/scratch/{role}      # required; absolute after {role} substitution
      weaker_nested_sandbox: false           # default false
      runtime_command: claude                # default "claude"
      pin_same_host: true                    # default true
      hosts: [api.example.test:443]          # host or host:port, shared by every role
      credential_paths: [/etc/example/secret.env]
      mcp_servers: [notes]
      roles:
        builder:
          hosts: [git.example.test:443]
          credential_paths: []
          mcp_servers: []

Roles are opaque keys: they are never checked against a roster. A malformed
value raises ``SessionConfigError`` naming the key and the value it resolved to;
a missing file, section or required key is reported through ``LoadResult``
instead, so the verb can say what is missing and still exit 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from clagentic_loadout.transport.provider_config import (
    DEFAULT_USER_CONFIG_ROOT,
    USER_CONFIG_FILENAME,
    load_user_config_section,
)

CONFIG_SECTION = "session_profile"

KEY_SCRATCH_ROOT = "scratch_root"
KEY_WEAKER_NESTED = "weaker_nested_sandbox"
KEY_RUNTIME_COMMAND = "runtime_command"
KEY_PIN_SAME_HOST = "pin_same_host"
KEY_HOSTS = "hosts"
KEY_CREDENTIAL_PATHS = "credential_paths"
KEY_MCP_SERVERS = "mcp_servers"
KEY_ROLES = "roles"

REQUIRED_KEYS = (KEY_SCRATCH_ROOT,)
_LIST_KEYS = (KEY_HOSTS, KEY_CREDENTIAL_PATHS, KEY_MCP_SERVERS)
_KNOWN_KEYS = frozenset(
    {
        KEY_SCRATCH_ROOT,
        KEY_WEAKER_NESTED,
        KEY_RUNTIME_COMMAND,
        KEY_PIN_SAME_HOST,
        KEY_ROLES,
        *_LIST_KEYS,
    }
)
_KNOWN_ROLE_KEYS = frozenset(_LIST_KEYS)

DEFAULT_RUNTIME_COMMAND = "claude"


class SessionConfigError(ValueError):
    """A `session_profile` value is present but unusable. The message names the
    key and the value it resolved to."""


@dataclass(frozen=True)
class RoleLists:
    hosts: tuple[str, ...] = ()
    credential_paths: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionProfile:
    config_path: Path
    scratch_root: str
    weaker_nested_sandbox: bool
    runtime_command: str | None
    pin_same_host: bool
    shared: RoleLists
    roles: dict[str, RoleLists]
    unknown_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoadResult:
    """What reading the config produced: a profile, or the reasons there is none."""

    config_path: Path
    profile: SessionProfile | None = None
    missing: tuple[str, ...] = field(default_factory=tuple)


def _check_bool(value: object, key: str) -> bool:
    if not isinstance(value, bool):
        raise SessionConfigError(
            f"{CONFIG_SECTION}.{key} must be true or false, got {value!r}"
        )
    return value


def _check_str_list(value: object, key: str, *, absolute: bool = False) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SessionConfigError(f"{CONFIG_SECTION}.{key} must be a list, got {value!r}")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or item != item.strip():
            raise SessionConfigError(
                f"{CONFIG_SECTION}.{key} entries must be non-empty strings without "
                f"surrounding whitespace, got {item!r}"
            )
        if any(ch.isspace() for ch in item) and not absolute:
            raise SessionConfigError(
                f"{CONFIG_SECTION}.{key} entries must not contain whitespace, got {item!r}"
            )
        if absolute and not item.startswith("/"):
            raise SessionConfigError(
                f"{CONFIG_SECTION}.{key} entries must be absolute paths, got {item!r}"
            )
        out.append(item)
    return tuple(out)


def _role_lists(raw: dict, where: str) -> RoleLists:
    return RoleLists(
        hosts=_check_str_list(raw.get(KEY_HOSTS), f"{where}{KEY_HOSTS}"),
        credential_paths=_check_str_list(
            raw.get(KEY_CREDENTIAL_PATHS), f"{where}{KEY_CREDENTIAL_PATHS}", absolute=True
        ),
        mcp_servers=_check_str_list(raw.get(KEY_MCP_SERVERS), f"{where}{KEY_MCP_SERVERS}"),
    )


def _parse_roles(raw: object) -> tuple[dict[str, RoleLists], list[str]]:
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        raise SessionConfigError(f"{CONFIG_SECTION}.{KEY_ROLES} must be a mapping, got {raw!r}")
    roles: dict[str, RoleLists] = {}
    unknown: list[str] = []
    for name, entry in raw.items():
        if not isinstance(name, str) or not name:
            raise SessionConfigError(
                f"{CONFIG_SECTION}.{KEY_ROLES} keys must be non-empty strings, got {name!r}"
            )
        if entry is None:
            entry = {}
        if not isinstance(entry, dict):
            raise SessionConfigError(
                f"{CONFIG_SECTION}.{KEY_ROLES}.{name} must be a mapping, got {entry!r}"
            )
        unknown.extend(
            f"{KEY_ROLES}.{name}.{k}" for k in entry if k not in _KNOWN_ROLE_KEYS
        )
        roles[name] = _role_lists(entry, f"{KEY_ROLES}.{name}.")
    return roles, unknown


def load_session_profile(*, config_root: str | Path | None = None) -> LoadResult:
    """Read the section. Missing file/section/required keys come back in
    ``LoadResult.missing``; a malformed value raises ``SessionConfigError``."""
    root = Path(config_root) if config_root is not None else DEFAULT_USER_CONFIG_ROOT
    path = root / USER_CONFIG_FILENAME
    if not path.exists():
        return LoadResult(path, missing=(f"no user-level config file at {path}",))
    section = load_user_config_section(CONFIG_SECTION, config_root=root)
    if not section:
        return LoadResult(
            path,
            missing=(
                f"no readable `{CONFIG_SECTION}` section in {path}; required keys: "
                f"{', '.join(REQUIRED_KEYS)}",
            ),
        )
    absent = [k for k in REQUIRED_KEYS if section.get(k) in (None, "")]
    if absent:
        return LoadResult(
            path,
            missing=(
                f"`{CONFIG_SECTION}` in {path} is missing required key(s): {', '.join(absent)}",
            ),
        )

    scratch = section[KEY_SCRATCH_ROOT]
    if not isinstance(scratch, str):
        raise SessionConfigError(
            f"{CONFIG_SECTION}.{KEY_SCRATCH_ROOT} must be a string, got {scratch!r}"
        )
    weaker = _check_bool(section.get(KEY_WEAKER_NESTED, False), KEY_WEAKER_NESTED)
    pin = _check_bool(section.get(KEY_PIN_SAME_HOST, True), KEY_PIN_SAME_HOST)
    runtime = section.get(KEY_RUNTIME_COMMAND)
    if runtime is not None and (not isinstance(runtime, str) or not runtime.strip()):
        raise SessionConfigError(
            f"{CONFIG_SECTION}.{KEY_RUNTIME_COMMAND} must be a non-empty string, got {runtime!r}"
        )
    roles, unknown_in_roles = _parse_roles(section.get(KEY_ROLES))
    unknown = [k for k in section if k not in _KNOWN_KEYS] + unknown_in_roles
    profile = SessionProfile(
        config_path=path,
        scratch_root=scratch,
        weaker_nested_sandbox=weaker,
        runtime_command=runtime,
        pin_same_host=pin,
        shared=_role_lists(section, ""),
        roles=roles,
        unknown_keys=tuple(str(k) for k in unknown),
    )
    return LoadResult(path, profile=profile)
