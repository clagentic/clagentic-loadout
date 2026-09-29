"""transport.read_host_guard — config-driven allowed-host anchoring for the
git-host-api (read) verb's credentialed call (lr-4ebce1).

BACKGROUND: unlike `push` (whose credentialed api_base is derived exclusively
from the live git remote and never consults `--git-host-base-url` at all --
see `push.host_guard`'s own module docstring), `git_host_api` treats an
EXPLICIT `--git-host-base-url` as authoritative: `_resolve_git_host_base`
returns it unconditionally when non-empty, and the resolved base is what the
caller's minted git-host token gets attached to (`build_request` -> a bare
`Authorization: token <token>` header). Neither the credential's own scope
(StaticTokenProvider/CommandTokenProvider carry no host concept -- see
`push.host_guard`'s identical rejection of this same candidate) nor a
clone-time pin (this verb has no git remote/clone context at all; a bare
`--repo owner/repo` argument is the only "target" it is given) exists to
cross-check `--git-host-base-url` against. Exactly the same gap
`push.host_guard` closed for push.verb's git-remote-derived `api_base`, on a
DIFFERENT input: here the caller-controlled value the token could be sent to
is a CLI ARGUMENT (or the env vars/config file `_resolve_git_host_base` also
consults), not a repointed git remote.

ANCHORED AGAINST WHAT -- an EXPLICIT, operator-configured allowlist,
mirroring `push.host_guard`'s own chosen shape (CHOSEN there for identical
reasons: no credential-scope anchor, no viable pinned-value anchor). Applies
uniformly to the resolved git-host base regardless of which
`_resolve_git_host_base` tier produced it (explicit flag, env var, config
file, or the placeholder default) -- there is no special-casing of "the flag
value only." See `check_host_allowed`'s own docstring for why.

WHY NOT "explicit must match the non-explicit-resolved chain" (an anchor
candidate that does NOT require new config, unlike the allowlist below):
REJECTED. `docs/integration.md`'s own "Git-host base URL" section documents
`CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL` and its compat alias (tiers 2-3) as
"a per-invocation override for a caller that genuinely needs to point at a
different Forgejo instance for one call, without touching this shared
[config] file" -- i.e. the resolved base LEGITIMATELY differs from whatever
a lower tier (config file, placeholder default) would have produced, by
design, on every documented real-world use of this flag/env var. Comparing
the winning tier against a losing tier would refuse exactly the sanctioned
override use case it is supposed to serve, on every call that uses it,
turning "add safety" into "break the one flag this verb has always
supported." No such comparison is fired here for that reason.

DEFAULT POSTURE -- PERMISSIVE, matching push.host_guard's own precedent
(recorded there as a known, accepted property: "an unconfigured deployment
gets zero protection"). Re-examined for this verb specifically, per the
task's own instruction to argue rather than inherit:

  - The exposure here IS reachable from a lower bar than push's (a caller
    argument/env var, not a repointed git remote) -- this makes the case for
    landing SOME enforcement stronger, not for making it default-ON. loadout
    is a released tool with existing callers (`review.verb`, `merge.verb`,
    `merge.close_verb`, `merge.post_merge_verb`, `acquire.verb`, and this
    verb's own CLI) that ALL pass a real, intentionally-chosen
    `--git-host-base-url` (or rely on the env-var/config tiers) TODAY, with
    no allowlist configured anywhere -- see this task's PR body for the full
    enumeration of legitimate uses. A default-ON allowlist with no seeded
    entries would make EVERY one of those existing, correct calls refuse
    outright the moment this ships, which is a breaking change to a
    verb's basic operability, not an additive safety net.
  - There is no way to seed a "correct" default allowlist automatically --
    unlike `_absolute_url_host_matches_git_host_base` (which anchors an
    absolute-URL PATH argument against the ALREADY-RESOLVED base, a value
    that always exists), there is no second, independently-derived
    reference value for `--git-host-base-url` itself to be checked against
    (see the rejected candidate above) -- the allowlist is necessarily
    something an operator must configure, and permissive-until-configured is
    this package's own established default posture for exactly that shape
    of guard (see `push.namespace_guard`, `push.host_guard`,
    `transport.git_host_api`'s own `known_bad_owners`, `review.contract`'s
    `ReviewBackend`).
  - A deployment that wants this closed sets the allowlist below -- opt-in,
    zero blast radius on an unconfigured install, matching every sibling
    guard in this package.

OWN ALLOWLIST, NOT SHARED WITH push's (`CLAGENTIC_LOADOUT_PUSH_ALLOWED_HOSTS`)
-- decided and argued explicitly per the task's own instruction:
  - The two guards anchor DIFFERENT inputs, derived from DIFFERENT trust
    boundaries: push's `api_base` comes from the live git remote (something
    only a repo's own `.git/config` -- a comparatively low-churn,
    repo-scoped value -- controls); this verb's resolved base comes from a
    CLI flag / env var / user-level config file supplied fresh on every
    invocation, including by a caller/script this deployment never
    anticipated (per lr-4ebce1's own filing, "reachable by ANY caller").
    Coupling one allowlist across both would force an operator who wants to
    scope push narrowly (e.g. "only known release repos") to ALSO scope
    every read call identically, or vice versa -- a real coupling cost with
    no offsetting benefit, since nothing requires the two surfaces to share
    a threat model just because both attach a bearer token to a resolved
    host.
  - Precedent inside this same package already treats a shared PATTERN
    (env-var + repeatable-flag, explicit > env, permissive when both unset)
    as the thing to reuse, while giving each guarded surface ITS OWN
    variable name -- see `push.namespace_guard.ALLOWED_NAMESPACES_ENV_VAR`
    vs `push.host_guard.ALLOWED_HOSTS_ENV_VAR`: two independent env vars for
    two independent dimensions of the SAME verb's own target, not shared
    across verbs. This module follows that same precedent one level up: the
    shape is reused (indeed the exact same `check_host_allowed`-style
    function below), the CONFIGURATION SURFACE is not.

REUSE, NOT A SECOND IMPLEMENTATION: host comparison itself
(`transport.host_match.host_matches`) is the SAME predicate
`transport.git_host_api._absolute_url_host_matches_git_host_base` already
delegates to (extracted lr-0e39f9) and `push.host_guard` also uses -- this
module is the predicate's third caller, per lr-0e39f9's own closing comment
("deliberately avoiding a second drifting implementation").
"""

from __future__ import annotations

import os

from clagentic_loadout.transport.host_match import host_matches

#: Env var carrying a comma-separated allowed-host list for the git-host-api
#: (read) verb's resolved git-host base (each entry a bare "host[:port]"
#: authority or a full "scheme://host[:port]" URL -- both shapes accepted,
#: see transport.host_match.host_matches). Unset or empty means "no
#: allowlist configured" (permissive -- see module docstring). Deliberately
#: NOT push.host_guard.ALLOWED_HOSTS_ENV_VAR -- see module docstring, "OWN
#: ALLOWLIST, NOT SHARED WITH push's".
ALLOWED_HOSTS_ENV_VAR = "CLAGENTIC_LOADOUT_READ_ALLOWED_HOSTS"


def resolve_allowed_hosts(
    explicit: frozenset[str] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> frozenset[str]:
    """Resolve the allowed-host set for the read verb's credentialed call.

    Precedence (mirrors push.host_guard.resolve_allowed_hosts /
    push.namespace_guard.resolve_allowed_namespaces exactly):
      1. *explicit* (caller-supplied set, e.g. a --allowed-host CLI flag
         repeated N times) -- always wins when not None, even if empty (an
         explicit empty set is a real choice: "restrict to nothing", handled
         by the caller's own validation, not silently reinterpreted as
         permissive here).
      2. ALLOWED_HOSTS_ENV_VAR, comma-separated, whitespace-trimmed, empty
         entries dropped.
      3. Empty frozenset (no restriction configured -- permissive default,
         see module docstring for why this posture was re-examined and kept
         for this verb).

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


class HostDeniedError(Exception):
    """Raised when the git-host-api verb's resolved git-host base is not
    present in the caller-configured allowed-host set (see
    check_host_allowed below, lr-4ebce1). Caught at the CLI boundary in
    transport.git_host_api._run/main and mapped to EXIT_HOST_DENIED --
    fires BEFORE any credential is resolved or request issued, mirroring
    push.errors.HostDeniedError's own posture for the sibling guard."""


def check_host_allowed(git_host_base: str, *, allowed_hosts: frozenset[str]) -> None:
    """Refuse *git_host_base* if an allowlist is configured and no entry in
    it matches *git_host_base*'s host:port (via
    transport.host_match.host_matches).

    An EMPTY allowed_hosts means "no allowlist configured" -- every host is
    permitted (permissive default, see module docstring). A NON-EMPTY
    allowed_hosts enforces membership: *git_host_base* must match at least
    one configured entry.

    Raises HostDeniedError BEFORE any credential is resolved or request
    issued -- a host refusal is deterministic and must never partially
    execute, mirroring push.host_guard.check_host_allowed's own
    fail-closed-before-token-resolution posture. Caught at the CLI boundary
    in transport.git_host_api._run and mapped to EXIT_HOST_DENIED (a
    GitHostApiError, this module's caller's own vocabulary -- this module
    stays a pure predicate with no dependency back on the verb it guards,
    exactly like push.host_guard's relationship to push.errors).
    """
    if not allowed_hosts:
        return
    if any(host_matches(git_host_base, entry) for entry in allowed_hosts):
        return
    raise HostDeniedError(
        f"git-host-api target host {git_host_base!r} (the resolved "
        f"git-host base -- see --git-host-base-url) is not in the "
        f"configured allowed-host set ({sorted(allowed_hosts)!r}). Set "
        f"{ALLOWED_HOSTS_ENV_VAR} (comma-separated) or pass an explicit "
        f"--allowed-host value (repeatable) to permit this host. Refusing "
        f"before any credential is resolved or request is issued -- this "
        f"refusal is deterministic; do not retry without changing the "
        f"configured allowlist or the resolved git-host base."
    )


__all__ = [
    "ALLOWED_HOSTS_ENV_VAR",
    "HostDeniedError",
    "check_host_allowed",
    "resolve_allowed_hosts",
]
