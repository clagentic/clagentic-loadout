"""session.pin -- same-host address pinning for the sandbox network allowlist.

A listed ``host:port`` that resolves to an address this machine owns needs the
``<address>:<port>`` entry as well, or the sandbox proxy refuses the service.
The entries are generated at build time from resolution and are never read from
config. Resolution and the local-address check are injectable so tests drive
the real entry point with synthetic names and documentation addresses.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Callable, Sequence

Resolver = Callable[[str], Sequence[str]]
LocalCheck = Callable[[str], bool]


def split_host_port(entry: str) -> tuple[str, str | None]:
    """``host`` or ``host:port`` (also ``[v6]:port``) -> (host, port or None)."""
    if entry.startswith("["):
        host, _, rest = entry[1:].partition("]")
        return host, (rest[1:] if rest.startswith(":") and rest[1:] else None)
    if entry.count(":") == 1:
        host, _, port = entry.partition(":")
        return host, (port or None)
    return entry, None


def system_resolver(host: str) -> list[str]:
    """Every address *host* resolves to, de-duplicated; raises OSError when it
    does not resolve."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    seen: list[str] = []
    for info in infos:
        addr = str(info[4][0]).split("%", 1)[0]
        if addr not in seen:
            seen.append(addr)
    return seen


def system_is_local(address: str) -> bool:
    """Whether this machine owns *address*: loopback, or one a bind succeeds on."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if parsed.is_loopback:
        return True
    family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((address, 0))
    except OSError:
        return False
    return True


def _address_entry(address: str, port: str) -> str:
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


def pinned_entries(
    hosts: Sequence[str], resolver: Resolver, is_local: LocalCheck
) -> tuple[list[str], list[str]]:
    """(address entries, warnings) for the listed *hosts*. Wildcard patterns are
    skipped: they name no single address to resolve."""
    entries: list[str] = []
    warnings: list[str] = []
    for entry in hosts:
        host, port = split_host_port(entry)
        if not host or host.startswith("*"):
            continue
        try:
            addresses = list(resolver(host))
        except OSError as exc:
            warnings.append(
                f"host {entry!r} does not resolve ({exc}); kept by name, no address entry"
            )
            continue
        local = [a for a in addresses if is_local(a)]
        if not local:
            continue
        if port is None:
            warnings.append(
                f"host {entry!r} resolves to this machine but lists no port; no address "
                "entry is added because an address entry without a port would admit every port"
            )
            continue
        for address in local:
            pinned = _address_entry(address, port)
            if pinned not in entries:
                entries.append(pinned)
    return entries, warnings
