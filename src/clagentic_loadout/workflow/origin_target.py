"""workflow.origin_target -- derive a dispatch target from a checkout's origin.

The dispatch verb takes no repo or platform argument. The whole target (host,
owner, repo name) is read from the `origin` remote of the git top-level that
contains the starting directory, and the allow-list is read from that same
top-level. One tree therefore names both the policy and the repo the policy
applies to; there is nothing for a caller to disagree with.

Only the remote called `origin` is consulted. A tracking remote, a fetch URL
under another name, or the current branch's upstream is never used.

The host must be exactly `github.com`. Parsing is anchored: for URL forms the
host is what `urlsplit` reports (so `https://github.com@other.example/o/r`
yields `other.example`, not `github.com`), and for the scp-like form the host
is the text between the optional `user@` and the first `:`. A suffix such as
`notgithub.com` never matches.

This module reads local git state only; it makes no network call.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.push.errors import RemoteResolutionError
from clagentic_loadout.push.git_coords import remote_url
from clagentic_loadout.repo_config import find_git_top_level

#: The one host this verb dispatches against.
GITHUB_HOST = "github.com"

_NAME_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")
_SCP_RE = re.compile(r"\A(?:[A-Za-z0-9._-]+@)?(?P<host>[^:/@\s]+):(?P<path>[^\s]+)\Z")
_URL_SCHEMES = ("https", "ssh")


class OriginTargetError(Exception):
    """The target cannot be derived or is not on the supported host.

    `unsupported_host` is True only when the origin parsed cleanly and named a
    host other than github.com; every other failure is an underivable origin.
    """

    def __init__(self, message: str, *, unsupported_host: bool = False) -> None:
        super().__init__(message)
        self.unsupported_host = unsupported_host


@dataclass(frozen=True)
class OriginTarget:
    top: Path
    owner: str
    repo: str


def _split_owner_repo(path: str, url: str) -> tuple[str, str]:
    cleaned = path.strip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    parts = cleaned.split("/")
    if (
        len(parts) != 2
        or not all(_NAME_RE.match(part) for part in parts)
        or any(part in (".", "..") for part in parts)
    ):
        raise OriginTargetError(
            f"the origin remote {url!r} does not name exactly one owner and one repo "
            f"(expected <host>/<owner>/<repo>)."
        )
    return parts[0], parts[1]


def parse_origin_url(url: str) -> tuple[str, str, str]:
    """Return (host, owner, repo) for an https, ssh or scp-like remote URL.

    The host is lower-cased. Raises OriginTargetError when the URL has none of
    those shapes or its path is not exactly owner/repo.
    """
    if "://" in url:
        parts = urllib.parse.urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            port = -1
        host = (parts.hostname or "").lower()
        if parts.scheme not in _URL_SCHEMES or not host or port is not None:
            raise OriginTargetError(
                f"the origin remote {url!r} is not an https:// or ssh:// URL with a "
                f"plain host and no port."
            )
        owner, repo = _split_owner_repo(parts.path, url)
        return host, owner, repo
    scp = _SCP_RE.match(url)
    if scp is None:
        raise OriginTargetError(
            f"the origin remote {url!r} is not an https://, ssh:// or user@host:path URL."
        )
    owner, repo = _split_owner_repo(scp.group("path"), url)
    return scp.group("host").lower(), owner, repo


def derive_github_target(start: str | Path) -> OriginTarget:
    """Derive the dispatch target from the git top-level containing *start*.

    Refuses (OriginTargetError) when *start* is not inside a git tree, the tree
    has no `origin`, the URL cannot be parsed, or the host is not exactly
    github.com.
    """
    top = find_git_top_level(start)
    if top is None:
        raise OriginTargetError(
            f"{str(start)!r} is not inside a git working tree, so the dispatch target "
            f"cannot be derived from its origin remote."
        )
    try:
        url = remote_url("origin", top)
    except RemoteResolutionError as exc:
        raise OriginTargetError(
            f"the git tree at {str(top)!r} has no readable 'origin' remote: {exc}."
        ) from exc
    host, owner, repo = parse_origin_url(url)
    if host != GITHUB_HOST:
        raise OriginTargetError(
            f"the origin remote of {str(top)!r} is on host {host!r}; this verb supports "
            f"GitHub only ({GITHUB_HOST}).",
            unsupported_host=True,
        )
    return OriginTarget(top=top, owner=owner, repo=repo)


__all__ = ["GITHUB_HOST", "OriginTarget", "OriginTargetError", "derive_github_target", "parse_origin_url"]
