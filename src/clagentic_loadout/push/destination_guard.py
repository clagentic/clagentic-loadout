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
    `file://` URL. Those two transports never consult GIT_ASKPASS, so they
    cannot receive the token; refusing them would add no protection and would
    break local-clone workflows;
  - ssh, scp-style, `git://` and every other scheme, and any `<name>::` remote
    helper (`ext::` runs a command), are refused: each can reach a peer or run
    a transport helper.

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


def _violation(url: str, anchor: tuple[str, int]) -> str | None:
    """The refusal category for one destination *url*, or None when the token
    may be sent to it."""
    if _CONTROL_CHARS.search(url):
        return CATEGORY_UNRESOLVED
    if _REMOTE_HELPER_RE.match(url):
        return CATEGORY_UNSUPPORTED_TRANSPORT
    scheme_match = _URL_SCHEME_RE.match(url)
    if scheme_match:
        scheme = scheme_match.group(1).lower()
        if scheme in _NETWORKLESS_SCHEMES:
            return None
        if scheme not in _HTTP_SCHEMES:
            return CATEGORY_UNSUPPORTED_TRANSPORT
        destination = _authority(url)
        if destination is None:
            return CATEGORY_UNRESOLVED
        return None if destination == anchor else CATEGORY_OFF_HOST
    # git reads `host:path` as ssh unless a slash comes first.
    colon = url.find(":")
    if colon != -1:
        slash = url.find("/")
        if slash == -1 or colon < slash:
            return CATEGORY_UNSUPPORTED_TRANSPORT
    return None


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
    args = ["remote", "get-url", "--all"]
    if push:
        args.append("--push")
    urls = _git_lines([*args, "--", tracking_remote_name], git_cwd)
    if not urls:
        raise DestinationDeniedError(CATEGORY_UNRESOLVED)
    return urls


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
        for url in _destination_urls(tracking_remote_name, push=push, git_cwd=git_cwd):
            category = _violation(url, anchor)
            if category is not None:
                raise DestinationDeniedError(category)


__all__ = [
    "CATEGORY_NOT_CONFIGURED",
    "CATEGORY_OFF_HOST",
    "CATEGORY_UNRESOLVED",
    "CATEGORY_UNSUPPORTED_TRANSPORT",
    "DestinationDeniedError",
    "check_push_destination",
]
