"""identity.jwks_source -- locate and fetch an issuer's key set (contract section 5).

A locator is `file:///path`, `https://...`, `http://...` (loopback hosts only) or
`unix:///socket` (served at `/.well-known/jwks.json`). Fetching is bounded: a
timeout, a 65536-byte cap and no redirect. A file is read without following a
symlink, must be a regular file and must not be writable by group or other. A key
set carrying private key parameters is refused.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import stat
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from clagentic_loadout.identity import errors
from clagentic_loadout.transport.redirect_guard import no_redirect_opener

MAX_JWKS_BYTES = 65536
DEFAULT_TIMEOUT_SECONDS = 5.0
_WELL_KNOWN = "/.well-known/jwks.json"
_PRIVATE_PARAMS = ("d", "p", "q", "dp", "dq", "qi", "k")
_LOCATOR_PREFIXES = ("file:///", "https://", "http://", "unix:///")


def _unavailable(locator: str, why: str) -> errors.IdentityError:
    return errors.IdentityError(errors.JWKS_UNAVAILABLE, f"key set {locator!r}: {why}")


def is_locator(value: str) -> bool:
    return value.startswith(_LOCATOR_PREFIXES)


def derive_locator(iss: str) -> str | None:
    """The key-set locator an issuer identifier names by itself, or None."""
    if iss.startswith(("file:///", "unix:///")):
        return iss
    if iss.startswith(("https://", "http://")):
        return iss.rstrip("/") + _WELL_KNOWN
    return None


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def _read_file(locator: str) -> bytes:
    path = urllib.parse.unquote(locator[len("file://"):])
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise _unavailable(locator, f"cannot open: {exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _unavailable(locator, "not a regular file")
        if info.st_mode & 0o022:
            raise _unavailable(locator, "writable by group or other")
        return os.read(fd, MAX_JWKS_BYTES + 1)
    except OSError as exc:
        raise _unavailable(locator, f"cannot read: {exc.strerror}") from exc
    finally:
        os.close(fd)


def _read_unix(locator: str, timeout: float) -> bytes:
    path = urllib.parse.unquote(locator[len("unix://"):])
    conn = _UnixConnection(path, timeout)
    try:
        conn.request("GET", _WELL_KNOWN, headers={"Host": "localhost"})
        resp = conn.getresponse()
        if resp.status != 200:
            raise _unavailable(locator, f"HTTP status {resp.status}")
        return resp.read(MAX_JWKS_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise _unavailable(locator, f"request failed: {exc}") from exc
    finally:
        conn.close()


def _read_http(locator: str, timeout: float, opener: Any) -> bytes:
    parts = urllib.parse.urlsplit(locator)
    if parts.scheme == "http" and not _is_loopback(parts.hostname or ""):
        raise _unavailable(locator, "plain http is allowed for loopback hosts only")
    try:
        with (opener or no_redirect_opener()).open(
            urllib.request.Request(locator), timeout=timeout
        ) as resp:
            return resp.read(MAX_JWKS_BYTES + 1)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise _unavailable(locator, f"request failed: {exc}") from exc


def load_jwks(
    locator: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, opener: Any = None
) -> Mapping[str, Any]:
    """Fetch and parse the key set at *locator*, or raise `IdentityError`."""
    if locator.startswith("file:///"):
        raw = _read_file(locator)
    elif locator.startswith("unix:///"):
        raw = _read_unix(locator, timeout)
    elif locator.startswith(("https://", "http://")):
        raw = _read_http(locator, timeout, opener)
    else:
        raise _unavailable(locator, "unrecognized locator form")
    if len(raw) > MAX_JWKS_BYTES:
        raise _unavailable(locator, f"over {MAX_JWKS_BYTES} bytes")
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise _unavailable(locator, "not JSON") from exc
    keys = doc.get("keys") if isinstance(doc, dict) else None
    if not isinstance(keys, list):
        raise _unavailable(locator, "no keys list")
    for key in keys:
        if isinstance(key, dict) and any(name in key for name in _PRIVATE_PARAMS):
            raise _unavailable(locator, "carries private key parameters")
    return doc
