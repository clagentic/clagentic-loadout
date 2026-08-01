"""push.host_guard — config-driven allowed-host anchoring for the push
verb's credentialed calls (lr-0e39f9).

BACKGROUND: push.verb derives the Forgejo API host it attaches a live
bearer token to (`api_base`) EXCLUSIVELY from the live git remote URL, via
push.git_coords.parse_forgejo_coords(push.git_coords.remote_url(...)) --
see verb.py's own module docstring, "--GIT-HOST-BASE-URL IS CURRENTLY
UNROUTED" (lr-cd3113), for why the config-file/env-resolved
--git-host-base-url value is NOT consulted on this path and is therefore
not a usable anchor. transport.git_host_api, by contrast, has a
host-anchoring check for an absolute-URL PATH argument
(_absolute_url_host_matches_git_host_base / EXIT_ABSOLUTE_URL_HOST_MISMATCH,
lr-69af67) -- but that check answers a different question ("does this
request's URL argument agree with the base already resolved for this
call"), and push.verb has no second, independently-resolved value to
compare `api_base` against at all: whatever the git remote says IS the only
value push.verb has ever had. There is nothing to cross-check without a
new, independent anchor.

ANCHORED AGAINST WHAT (the crux, decided explicitly -- lr-0e39f9 task
description enumerates three candidates):
  1. The credential's own scope -- REJECTED. Neither StaticTokenProvider
     (a role-scoped on-disk .env file) nor CommandTokenProvider (a role- and
     optionally repo-scoped minted token) carries any notion of WHICH HOST
     the token is valid against (see transport.credential_provider's own
     module docstring) -- a token minted for role="builder",
     repo="some-owner/some-repo" is, as far as either provider's contract
     is concerned, equally "valid" whether it is sent to
     git-host-a.example.com or git-host-b.example.com. There is nothing to
     anchor against here.
  2. The remote recorded at clone time -- REJECTED. This codebase has no
     such recorded value anywhere: push.git_coords always re-reads the LIVE
     remote fresh, on every call (remote_url() is a bare `git remote
     get-url` subprocess, never cached) -- inventing a clone-time-pinned
     value would be a new persistence mechanism (where would it live? whose
     job is it to seed it on clone? what happens on a legitimate remote
     change, e.g. a repo migration?), a materially bigger change than this
     task's scope, and one this task does not attempt.
  3. An EXPLICIT, operator-configured allowlist -- CHOSEN. This mirrors
     push.namespace_guard's own shape exactly (this package's existing
     precedent for "a real safety guard whose allowed set is entirely
     caller-supplied input, permissive when unconfigured"): an env var
     (ALLOWED_HOSTS_ENV_VAR) or an explicit CLI-supplied set. An
     unconfigured/empty allowlist is PERMISSIVE (no restriction enforced) --
     restrictive behavior is opt-in, not implicit in the absence of
     configuration, exactly matching namespace_guard's own posture and this
     package's standalone-deployment default elsewhere (transport.
     git_host_api's own known_bad_owners, review.contract's ReviewBackend).
     A deployment that wants push's credentialed calls anchored to a fixed
     set of known-good Forgejo hosts sets the env var (or wires an explicit
     allowed_hosts set); a deployment that has not configured this is
     UNCHANGED from before this task -- api_base still derives from the git
     remote exactly as it always has, this guard simply never fires.

REUSE, NOT A SECOND IMPLEMENTATION: host comparison itself
(transport.host_match.host_matches) is the SAME predicate
transport.git_host_api's own _absolute_url_host_matches_git_host_base now
delegates to (extracted lr-0e39f9) -- this module does not re-derive
urlsplit-and-compare-netloc logic a second time, which is exactly the
copied-and-never-reconciled defect class lr-cd3113 diagnosed for a
different value in this same verb.
"""

from __future__ import annotations

import os

from clagentic_loadout.push.errors import HostDeniedError
from clagentic_loadout.transport.host_match import host_matches

#: Env var carrying a comma-separated allowed-host list (each entry a bare
#: "host[:port]" authority or a full "scheme://host[:port]" URL -- both
#: shapes are accepted, see transport.host_match.host_matches). Unset or
#: empty means "no allowlist configured" (permissive -- see module
#: docstring).
ALLOWED_HOSTS_ENV_VAR = "CLAGENTIC_LOADOUT_PUSH_ALLOWED_HOSTS"


def resolve_allowed_hosts(
    explicit: frozenset[str] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> frozenset[str]:
    """Resolve the allowed-host set.

    Precedence (mirrors push.namespace_guard.resolve_allowed_namespaces
    exactly):
      1. *explicit* (caller-supplied set, e.g. a --allowed-host CLI flag
         repeated N times) -- always wins when not None, even if empty (an
         explicit empty set is a real choice: "restrict to nothing", handled
         by the caller's own validation, not silently reinterpreted as
         permissive here).
      2. ALLOWED_HOSTS_ENV_VAR, comma-separated, whitespace-trimmed, empty
         entries dropped.
      3. Empty frozenset (no restriction configured -- permissive default).

    *env* overrides os.environ for tests; defaults to the real process
    environment.
    """
    if explicit is not None:
        return frozenset(explicit)
    active_env = env if env is not None else os.environ
    raw = active_env.get(ALLOWED_HOSTS_ENV_VAR, "")
    if not raw.strip():
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def check_host_allowed(api_base: str, *, allowed_hosts: frozenset[str]) -> None:
    """Refuse *api_base* if an allowlist is configured and no entry in it
    matches *api_base*'s host:port (via transport.host_match.host_matches).

    An EMPTY allowed_hosts means "no allowlist configured" -- every host is
    permitted (permissive default, see module docstring). A NON-EMPTY
    allowed_hosts enforces membership: *api_base* must match at least one
    configured entry.

    Raises HostDeniedError before any credential is resolved or git
    operation attempted -- a host refusal is deterministic and must never
    partially execute, mirroring push.namespace_guard.check_namespace_allowed's
    own fail-closed-before-token-resolution posture.
    """
    if not allowed_hosts:
        return
    if any(host_matches(api_base, entry) for entry in allowed_hosts):
        return
    raise HostDeniedError(
        f"push target host {api_base!r} (derived from the live git remote) "
        f"is not in the configured allowed-host set "
        f"({sorted(allowed_hosts)!r}). Set {ALLOWED_HOSTS_ENV_VAR} "
        f"(comma-separated) or pass an explicit allowed-host list to permit "
        f"this host. Refusing before any credential is resolved or git "
        f"operation attempted -- this refusal is deterministic; do not "
        f"retry without changing the configured allowlist or the git "
        f"remote."
    )


__all__ = [
    "ALLOWED_HOSTS_ENV_VAR",
    "check_host_allowed",
    "resolve_allowed_hosts",
]
