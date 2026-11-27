"""session.layer -- build one role's complete settings layer.

The sandbox bounds shell commands only; the native file and web tools are
bounded by permission rules. Each credential path and each host is therefore
written twice, from ONE resolved list through ONE function, so the sandbox key
and the matching rule cannot drift apart:

  credential_paths -> sandbox.filesystem.denyRead, Read(//<path>), Edit(//<path>)
  hosts            -> sandbox.network.allowedDomains, WebFetch(domain:<hostname>)

There is no shell-command allow rule: sandboxed commands are auto-allowed by the
runtime and the sandbox is the bound.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Sequence

from clagentic_loadout.session.config import (
    CONFIG_SECTION,
    KEY_SCRATCH_ROOT,
    RoleLists,
    SessionConfigError,
    SessionProfile,
)
from clagentic_loadout.session.pin import (
    LocalCheck,
    Resolver,
    pinned_entries,
    split_host_port,
    system_is_local,
    system_resolver,
)

ROLE_PLACEHOLDER = "{role}"


@dataclass(frozen=True)
class BuiltLayer:
    layer: dict[str, Any]
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _merged(*lists: Sequence[str]) -> list[str]:
    out: list[str] = []
    for items in lists:
        for item in items:
            if item not in out:
                out.append(item)
    return out


def credential_rules(paths: Sequence[str]) -> tuple[list[str], list[str]]:
    """(sandbox denyRead entries, permissions.deny rules) from one path list.
    An absolute path takes the runtime's ``//`` form inside a rule."""
    deny_read = list(paths)
    deny_rules: list[str] = []
    for path in paths:
        deny_rules.append(f"Read(/{path})")
        deny_rules.append(f"Edit(/{path})")
    return deny_read, deny_rules


def host_rules(hosts: Sequence[str]) -> tuple[list[str], list[str]]:
    """(sandbox allowedDomains entries, permissions.allow rules) from one host
    list. The entries are kept exactly as written; the web-fetch rule takes the
    hostname without its port."""
    allowed = list(hosts)
    web_rules: list[str] = []
    for entry in hosts:
        rule = f"WebFetch(domain:{split_host_port(entry)[0]})"
        if rule not in web_rules:
            web_rules.append(rule)
    return allowed, web_rules


def resolve_scratch_root(template: str, role: str) -> str:
    resolved = template.replace(ROLE_PLACEHOLDER, role)
    if not os.path.isabs(resolved):
        raise SessionConfigError(
            f"{CONFIG_SECTION}.{KEY_SCRATCH_ROOT} must be an absolute path after "
            f"{ROLE_PLACEHOLDER} substitution, got {resolved!r}"
        )
    return resolved


def build_layer(
    profile: SessionProfile,
    role: str,
    *,
    resolver: Resolver | None = None,
    is_local: LocalCheck | None = None,
) -> BuiltLayer:
    warnings: list[str] = []
    role_lists = profile.roles.get(role)
    if role_lists is None:
        warnings.append(
            f"role {role!r} has no entry under {CONFIG_SECTION}.roles; the shared lists apply"
        )
        role_lists = RoleLists()
    hosts = _merged(profile.shared.hosts, role_lists.hosts)
    paths = _merged(profile.shared.credential_paths, role_lists.credential_paths)
    servers = _merged(profile.shared.mcp_servers, role_lists.mcp_servers)

    allowed, web_rules = host_rules(hosts)
    if profile.pin_same_host:
        extra, pin_warnings = pinned_entries(
            hosts, resolver or system_resolver, is_local or system_is_local
        )
        warnings.extend(pin_warnings)
        allowed = _merged(allowed, extra)
    deny_read, deny_rules = credential_rules(paths)
    scratch = resolve_scratch_root(profile.scratch_root, role)

    layer = {
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "failIfUnavailable": True,
            "enableWeakerNestedSandbox": profile.weaker_nested_sandbox,
            "network": {"allowedDomains": allowed},
            "filesystem": {"denyRead": deny_read},
        },
        "permissions": {
            "defaultMode": "acceptEdits",
            "additionalDirectories": [scratch],
            "allow": [*web_rules, *(f"mcp__{name}" for name in servers)],
            "deny": deny_rules,
        },
    }
    return BuiltLayer(layer=layer, warnings=tuple(warnings))
