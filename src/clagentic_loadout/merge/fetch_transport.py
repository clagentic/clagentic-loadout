"""merge.fetch_transport -- the one way the merge verbs run a network `git fetch`.

The merge verbs fetch from the base branch's tracking remote (`branch.<base>.remote`,
falling back to `origin`). That name, the remote's url and any `insteadOf` come
from repo-local git config, so a config entry in the checkout the merger runs in
could steer a fetch to any host, and an ambient credential helper, netrc or
`~/.git-credentials` would then be presented there. This module closes that:

  1. The destination check (`push.destination_guard.check_fetch_destination`)
     runs first, under the neutralized git environment, before any network git
     call: the tracking remote must be a configured remote, and every fetch url
     must be an http(s) URL on the platform host and port, or a local path /
     `file://` URL. ssh, scp-style, `git://` and remote helpers are refused. An
     off-host url is refused, never skipped.
  2. The fetch itself goes through `push.git_push.git_fetch_with_token`, the one
     destination-aware hermetic primitive: no system or global config, no ambient
     credential source. An anchored http(s) destination is authenticated with the
     merger's minted token only; a local destination carries no token and no
     askpass (destination hooks inherit the environment).

Callers that hold no platform credential (a library call with no `fetch_auth`)
get the same checks with no anchor, so only local destinations can be fetched.

Messages name a category only: the remote name, url, host and git's own output
are never printed (they can carry a credential).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from clagentic_loadout.merge.errors import MergeUsageError
from clagentic_loadout.push import destination_guard as guard
from clagentic_loadout.push.git_coords import tracking_remote
from clagentic_loadout.push.git_hermeticity import GitVersionTooOldError, RepoLocalConfigHazardError
from clagentic_loadout.push.git_push import GitFetchError, git_fetch_with_token

_SUFFIX = "refusing before any git network call"

_DENIED_MESSAGES = {
    guard.CATEGORY_NOT_CONFIGURED: (
        f"the base branch's tracking remote is not a configured remote; {_SUFFIX}"
    ),
    guard.CATEGORY_OFF_HOST: (
        "a fetch url of the base branch's tracking remote is an http(s) host other "
        f"than the platform host the merge credential is scoped to; {_SUFFIX}"
    ),
    guard.CATEGORY_UNSUPPORTED_TRANSPORT: (
        "a fetch url of the base branch's tracking remote uses a transport that can "
        "reach a network peer or run a helper (ssh, git://, a remote helper); only "
        f"http(s) on the platform host, a local path or file:// is allowed; {_SUFFIX}"
    ),
    guard.CATEGORY_UNRESOLVED: (
        f"the fetch url of the base branch's tracking remote could not be resolved; {_SUFFIX}"
    ),
    guard.CATEGORY_MIXED_TRANSPORT: (
        "the fetch urls of the base branch's tracking remote mix a network destination "
        f"with a local one; {_SUFFIX}"
    ),
}


class MergeFetchDeniedError(MergeUsageError):
    """A fetch was refused before any network call. A usage error because the
    cause is the checkout's git configuration, not the platform."""


class MergeFetchFailedError(Exception):
    """A permitted `git fetch` exited non-zero. The message carries the exit
    code and no git output."""


@dataclass(frozen=True)
class MergeFetchAuth:
    """What a merge-path fetch may present: the platform URL whose host and port
    an http(s) fetch url must equal, and the merger's minted token for it."""

    anchor_url: str
    token: str = field(repr=False)


def _remote_for(git_tree: Path, base_branch: str) -> str:
    return tracking_remote(base_branch, git_tree) if base_branch else "origin"


def require_fetch_destination(
    git_tree: str | Path, base_branch: str, fetch_auth: MergeFetchAuth | None
) -> None:
    """Raise MergeFetchDeniedError unless a fetch of *base_branch*'s tracking
    remote in *git_tree* may proceed. Makes no network call."""
    git_tree = Path(git_tree)
    try:
        guard.check_fetch_destination(
            _remote_for(git_tree, base_branch),
            anchor_url=fetch_auth.anchor_url if fetch_auth is not None else None,
            git_cwd=git_tree,
        )
    except guard.DestinationDeniedError as exc:
        raise MergeFetchDeniedError(_DENIED_MESSAGES[exc.category]) from exc


def fetch_from_tracking_remote(
    git_tree: str | Path, base_branch: str, target: str, fetch_auth: MergeFetchAuth | None
) -> None:
    """Fetch *target* (a branch name or a commit SHA) from *base_branch*'s tracking
    remote into *git_tree*.

    Raises:
        MergeFetchDeniedError: the destination check, the git version floor or the
            repo-local config hazard check refused; no network call was made.
        MergeFetchFailedError: git exited non-zero.
    """
    git_tree = Path(git_tree)
    require_fetch_destination(git_tree, base_branch, fetch_auth)
    try:
        git_fetch_with_token(
            _remote_for(git_tree, base_branch),
            target,
            fetch_auth.token if fetch_auth is not None else None,
            git_tree,
        )
    except (RepoLocalConfigHazardError, GitVersionTooOldError) as exc:
        raise MergeFetchDeniedError(str(exc)) from exc
    except GitFetchError as exc:
        raise MergeFetchFailedError(str(exc)) from exc


__all__ = [
    "MergeFetchAuth",
    "MergeFetchDeniedError",
    "MergeFetchFailedError",
    "fetch_from_tracking_remote",
    "require_fetch_destination",
]
