"""push.destination_guard -- refuse to hand a minted token to a git destination
that is not the platform host the token is scoped to (lr-684312).

THE DEFECT THIS CLOSES: the push verb pushes to the branch's tracking remote
(`branch.<name>.remote`, falling back to `origin`) and presents the minted
token through GIT_ASKPASS to whatever host that push reaches. git accepts a
URL as the value of `branch.<name>.remote`, and a configured remote may carry
a `pushurl` that differs from the `url` the verb parses its coordinates from.
Neither value was anchored to the platform host, so a repo-local config value
could route the token to an arbitrary http(s) peer. The same destination is
used by the pre-lease fetch and the post-push readback, both of which also
present the token.

THE RULE (all checks run before any credential is minted):
  - the tracking remote must be a CONFIGURED remote (`git remote` lists it);
    a URL, or any value that is not a remote name, is refused;
  - every push destination (`git remote get-url --push --all`) and every fetch
    destination (`git remote get-url --all`; the pre-lease fetch and the
    post-push readback present the token to it) must be either
    an http(s) URL whose host and port equal the anchored platform authority,
    or a destination that cannot reach a network peer: a plain local path or a
    `file://` URL. Those transports need no credential and the git call that
    reaches them is made without one; refusing them would break local-clone
    workflows. A set that mixes the two kinds is refused;
  - ssh, scp-style, `git://` and every other scheme, and any `<name>::` remote
    helper (`ext::` runs a command), are refused: each can reach a peer or run
    a transport helper.

A LOCAL DESTINATION IS NOT CREDENTIAL-FREE BY ITSELF: a local or `file://`
push runs the destination repository's hooks (pre-receive, update,
post-receive) with the pushing process's environment, and a credentialed
environment carries GIT_ASKPASS and the token file it reads. So a git call
whose destinations are all local runs WITHOUT the token (see
`destinations_are_local`, used by the credentialed call sites), and a
destination set that mixes http(s) with local is refused outright: a
credentialed environment must never reach a local receive-pack.

Destinations are resolved the way the credentialed subprocess resolves them:
under the same neutralized git environment (no global or system config, so no
`insteadOf` rewrite the credentialed call would not see).

Refusals name the category only. The remote name, URL, host and port are
never interpolated into a message (they can carry a credential).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

from clagentic_loadout.push.git_hermeticity import neutralize_ambient_git_env

#: A `scheme://` URL prefix, per RFC 3986's scheme grammar.
_URL_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*)://")

#: git's `<transport>::<address>` remote-helper syntax (`ext::sh -c ...` runs a
#: command; `http::` selects a helper).
_REMOTE_HELPER_RE = re.compile(r"^[A-Za-z0-9+.\-]+::")

_HTTP_SCHEMES = {"http": 80, "https": 443}

_NETWORKLESS_SCHEMES = frozenset({"file"})

#: Characters that make a URL parse differently in urllib and in libcurl.
_AMBIGUOUS_URL_CHARS = re.compile(r"[\\\s]")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

CATEGORY_NOT_CONFIGURED = "not-a-configured-remote"
CATEGORY_OFF_HOST = "off-host"
CATEGORY_UNSUPPORTED_TRANSPORT = "unsupported-transport"
CATEGORY_UNRESOLVED = "unresolved"
CATEGORY_MIXED_TRANSPORT = "mixed-transport"

_MESSAGES = {
    CATEGORY_NOT_CONFIGURED: (
        "the branch's tracking remote is not a configured remote; refusing "
        "before any credential is minted"
    ),
    CATEGORY_OFF_HOST: (
        "a destination of the branch's tracking remote is an http(s) host other "
        "than the platform host the credential is scoped to; refusing before "
        "any credential is minted"
    ),
    CATEGORY_UNSUPPORTED_TRANSPORT: (
        "a destination of the branch's tracking remote uses a transport that "
        "can reach a network peer or run a helper (ssh, git://, a remote "
        "helper); only http(s) on the platform host, a local path or file:// "
        "is allowed; refusing before any credential is minted"
    ),
    CATEGORY_UNRESOLVED: (
        "the destination of the branch's tracking remote could not be "
        "resolved; refusing before any credential is minted"
    ),
    CATEGORY_MIXED_TRANSPORT: (
        "the destinations of the branch's tracking remote mix a network "
        "destination with a local one; a credentialed environment must never "
        "reach a local receive-pack, so refusing before any credential is minted"
    ),
}


class DestinationDeniedError(Exception):
    """A credentialed git destination is not one the token may be sent to.

    `category` is one of the CATEGORY_* constants; the message is fixed text
    per category and carries no remote value."""

    def __init__(self, category: str) -> None:
        super().__init__(_MESSAGES[category])
        self.category = category


def _git_lines(args: list[str], git_cwd: Path | None) -> list[str] | None:
    """stdout lines of `git <args>` under the neutralized environment, or None
    when git exits non-zero. HOME and XDG_CONFIG_HOME point at an empty
    directory so a git older than the GIT_CONFIG_GLOBAL redirect still reads
    no user config."""
    with tempfile.TemporaryDirectory(prefix="loadout_dest_") as empty_home:
        env = neutralize_ambient_git_env(os.environ.copy())
        env["HOME"] = empty_home
        env["XDG_CONFIG_HOME"] = empty_home
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(git_cwd) if git_cwd is not None else None,
        )
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.split("\n") if line != ""]


def _authority(url: str) -> tuple[str, int] | None:
    """(lowercased host, effective port) of an http(s) *url*, or None when it
    cannot be parsed unambiguously."""
    if _AMBIGUOUS_URL_CHARS.search(url):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    default_port = _HTTP_SCHEMES.get(parsed.scheme.lower())
    if not host or default_port is None:
        return None
    return host.lower(), port if port is not None else default_port


def _is_local(url: str) -> bool:
    """True for a destination that needs no credential: a plain local path or a
    `file://` URL. Anything unparseable or helper-shaped is not local."""
    if _CONTROL_CHARS.search(url) or _REMOTE_HELPER_RE.match(url):
        return False
    scheme_match = _URL_SCHEME_RE.match(url)
    if scheme_match:
        return scheme_match.group(1).lower() in _NETWORKLESS_SCHEMES
    # git reads `host:path` as ssh unless a slash comes first.
    colon = url.find(":")
    if colon == -1:
        return True
    slash = url.find("/")
    return slash != -1 and slash < colon


def _violation(url: str, anchor: tuple[str, int]) -> str | None:
    """The refusal category for one destination *url*, or None when a git call
    may reach it (credentialed for http(s), credential-free for a local one)."""
    if _CONTROL_CHARS.search(url):
        return CATEGORY_UNRESOLVED
    if _is_local(url):
        return None
    scheme_match = _URL_SCHEME_RE.match(url)
    if scheme_match and scheme_match.group(1).lower() in _HTTP_SCHEMES:
        destination = _authority(url)
        if destination is None:
            return CATEGORY_UNRESOLVED
        return None if destination == anchor else CATEGORY_OFF_HOST
    return CATEGORY_UNSUPPORTED_TRANSPORT


def _anchor_authority(anchor_url: str) -> tuple[str, int]:
    authority = _authority(anchor_url)
    if authority is None:
        raise DestinationDeniedError(CATEGORY_UNRESOLVED)
    return authority


def _destination_urls(
    tracking_remote_name: str, *, push: bool, git_cwd: Path | None
) -> list[str]:
    """Every URL git would use for *tracking_remote_name*, or raise.

    A name that is not listed by `git remote` is refused first: that is what
    separates a URL-valued `branch.<name>.remote` from an alias."""
    configured = _git_lines(["remote"], git_cwd)
    if configured is None:
        raise DestinationDeniedError(CATEGORY_UNRESOLVED)
    if tracking_remote_name not in configured:
        raise DestinationDeniedError(CATEGORY_NOT_CONFIGURED)
    return _resolved_urls(tracking_remote_name, push=push, git_cwd=git_cwd)


def _resolved_urls(name: str, *, push: bool, git_cwd: Path | None) -> list[str]:
    args = ["remote", "get-url", "--all"]
    if push:
        args.append("--push")
    urls = _git_lines([*args, "--", name], git_cwd)
    if not urls:
        raise DestinationDeniedError(CATEGORY_UNRESOLVED)
    return urls


def _refuse_mixed(urls: list[str]) -> None:
    if len({_is_local(url) for url in urls}) > 1:
        raise DestinationDeniedError(CATEGORY_MIXED_TRANSPORT)


def destinations_are_local(remote: str, *, push: bool, git_cwd: Path | None = None) -> bool:
    """True when every destination a git call to *remote* reaches is local, so
    the call must run without a credential; False when none is. A *remote* that
    is not a configured name is itself the destination (git accepts a URL or a
    path there). A set mixing the two kinds raises DestinationDeniedError: no
    single environment is right for both."""
    configured = _git_lines(["remote"], git_cwd)
    if configured is not None and remote in configured:
        urls = _resolved_urls(remote, push=push, git_cwd=git_cwd)
    else:
        urls = [remote]
    _refuse_mixed(urls)
    return _is_local(urls[0])


def check_push_destination(
    tracking_remote_name: str, *, anchor_url: str, git_cwd: Path | None = None
) -> None:
    """Refuse (DestinationDeniedError) unless every push destination AND every
    fetch destination of *tracking_remote_name* may receive the token. The
    fetch urls are checked too because the pre-lease fetch and the post-push
    readback present the token to them. *anchor_url* is the platform URL whose
    host and port an http(s) destination must equal."""
    anchor = _anchor_authority(anchor_url)
    for push in (True, False):
        urls = _destination_urls(tracking_remote_name, push=push, git_cwd=git_cwd)
        _refuse_mixed(urls)
        for url in urls:
            category = _violation(url, anchor)
            if category is not None:
                raise DestinationDeniedError(category)


__all__ = [
    "CATEGORY_NOT_CONFIGURED",
    "CATEGORY_OFF_HOST",
    "CATEGORY_UNRESOLVED",
    "CATEGORY_UNSUPPORTED_TRANSPORT",
    "CATEGORY_MIXED_TRANSPORT",
    "DestinationDeniedError",
    "check_push_destination",
    "destinations_are_local",
]
