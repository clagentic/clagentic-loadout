"""git_origin -- read a working tree's ``origin`` once and bind it exactly.

A tree is trusted to supply configuration only when it is a clone of the
repository the caller names, on the host the caller's platform is configured
for. This module answers that from ONE read of ``origin`` at the tree's git
top-level, parsed by an anchored parser, so every fact (host, owner, name)
comes from the same string. It deliberately does not reuse the substring
platform detection or the tracking-remote reader: a substring test accepts
``https://github.com@other-host/o/r`` and ``https://github.com.example/o/r``,
and the tracking remote of the current branch need not be ``origin``.

Accepted remote shapes (host is compared by equality, never by substring):

  * ``https://[userinfo@]host[:port]/owner/name[.git]`` (also ``http``)
  * ``ssh://[user@]host[:port]/owner/name[.git]`` (also ``git``)
  * ``[user@]host:owner/name[.git]`` (scp-like)

The SSH and HTTPS forms of one host therefore bind identically. Anything else
is unparseable and is refused, never passed.

No message, log line or JSON output built here echoes the origin URL, raw or
redacted. A refusal names only parsed parts: the remote name ``origin``, the
parsed host and owner/name, and the resolved paths. An unparseable origin is
reported without its text.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

_GIT_TIMEOUT_SECONDS = 10
_URL_SCHEMES = frozenset({"http", "https", "ssh", "git", "git+ssh", "ssh+git"})
# One leading dot is allowed so the ".github" org-profile repository parses;
# "." and ".." cannot, since a segment must contain an alphanumeric first.
_SEGMENT_RE = re.compile(r"\A\.?[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SCP_RE = re.compile(r"\A(?:[^@/:\s]+@)?(?P<host>[^@/:\s\[\]]+):(?P<path>[^\s:][^\s]*)\Z")


class OriginBindingError(Exception):
    """The tree cannot be confirmed as a clone of the named repository on the
    expected host. The message names the resolved tree, the remote name and the
    parsed host and owner/name, never the remote's URL text."""


@dataclass(frozen=True)
class OriginTarget:
    """What one ``origin`` URL says: its lowercased host, owner and name (the
    ``.git`` suffix removed) and the git top-level it was read at (empty for a
    bare parse). The URL string itself is deliberately not kept, so nothing
    that prints a target can print a credential embedded in the remote."""

    host: str
    owner: str
    name: str
    top_level: str = ""

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


def host_of(value: str) -> str:
    """The lowercased hostname of *value*, a bare host or a URL; '' when none."""
    text = value.strip()
    if "://" not in text:
        text = f"//{text}"
    try:
        return (urlsplit(text).hostname or "").lower()
    except ValueError:
        return ""


def _owner_and_name(path: str) -> tuple[str, str] | None:
    segments = path.strip("/").split("/")
    if len(segments) != 2:
        return None
    owner, name = segments
    if name.endswith(".git"):
        name = name[: -len(".git")]
    if not _SEGMENT_RE.match(owner) or not _SEGMENT_RE.match(name):
        return None
    return owner, name


def parse_origin_url(url: str) -> OriginTarget | None:
    """The target *url* names, or None when it is not one of the accepted
    shapes. Never raises."""
    text = url.strip()
    if not text:
        return None
    if "://" in text:
        try:
            parts = urlsplit(text)
            hostname = parts.hostname
            _ = parts.port  # a malformed port raises ValueError here, which is the check
        except ValueError:
            return None
        if parts.scheme.lower() not in _URL_SCHEMES or not hostname:
            return None
        if parts.query or parts.fragment:
            return None
        pair = _owner_and_name(parts.path)
        host = hostname.lower()
    else:
        match = _SCP_RE.match(text)
        if match is None:
            return None
        pair = _owner_and_name(match.group("path"))
        host = match.group("host").lower()
    if pair is None:
        return None
    return OriginTarget(host=host, owner=pair[0], name=pair[1])


def _git(tree: str, *args: str) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            ["git", "-C", tree, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def read_origin_target(tree: str | Path) -> OriginTarget:
    """The target of ``origin`` at the git top-level of *tree*, read once.

    Raises OriginBindingError when *tree* is not a directory, is not inside a
    git work tree, has no ``origin``, or its ``origin`` is not a recognised
    remote shape."""
    given = str(tree)
    if not Path(given).is_dir():
        raise OriginBindingError(f"{given!r} does not exist or is not a directory")
    code, top, err = _git(given, "rev-parse", "--show-toplevel")
    if code != 0 or not top:
        raise OriginBindingError(
            f"{given!r} is not inside a git work tree" + (f" ({err})" if err else "")
        )
    code, url, err = _git(top, "remote", "get-url", "origin")
    if code != 0 or not url:
        raise OriginBindingError(
            f"the git tree {top!r} has no readable 'origin' remote" + (f" ({err})" if err else "")
        )
    target = parse_origin_url(url)
    if target is None:
        raise OriginBindingError(
            f"the origin remote of the git tree {top!r} is not a parseable remote URL"
        )
    return OriginTarget(target.host, target.owner, target.name, top)


@dataclass(frozen=True)
class ConfigTreeBinding:
    """The identity a tree must have before its configuration is read: a clone
    of *slug* (``owner/name``) on *expected_host* (a host or a URL naming it)."""

    expected_host: str
    slug: str

    def confirm(self, tree: str | Path) -> OriginTarget:
        """The tree's origin target when it matches, else OriginBindingError."""
        wanted_host = host_of(self.expected_host)
        try:
            target = read_origin_target(tree)
        except OriginBindingError as exc:
            # The refusal names the host the tree was being compared against
            # even when the tree gave nothing to compare.
            raise OriginBindingError(
                f"{exc}; the configured platform host is {wanted_host!r}"
            ) from exc
        if not wanted_host:
            raise OriginBindingError(
                "the configured platform host names no host to compare against the origin "
                f"remote of the git tree {target.top_level!r}"
            )
        if target.host != wanted_host:
            raise OriginBindingError(
                f"the remote origin of the git tree {target.top_level!r} is {target.slug!r} on "
                f"host {target.host!r}, but the configured platform host is {wanted_host!r}"
            )
        if target.slug.lower() != self.slug.strip().lower():
            raise OriginBindingError(
                f"the remote origin of the git tree {target.top_level!r} is {target.slug!r} on "
                f"host {target.host!r}, not {self.slug!r}"
            )
        return target


__all__ = [
    "ConfigTreeBinding",
    "OriginBindingError",
    "OriginTarget",
    "host_of",
    "parse_origin_url",
    "read_origin_target",
]
