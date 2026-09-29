"""push.host_guard — config-driven allowed-host anchoring for the push
verb's credentialed calls (lr-0e39f9; WIDEN/NARROW + empty-set-collapse fix
lr-57573e, mirroring transport.read_host_guard's own PR #33 design).

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
     unconfigured deployment is PERMISSIVE (no restriction enforced) --
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

CALLER-WIDENING + EMPTY-SET-COLLAPSE FIX (lr-57573e, pre-merge review
finding while auditing the sibling read-verb guard's own fix): this
module's ORIGINAL `resolve_allowed_hosts` let
*explicit* (--allowed-host) and ALLOWED_HOSTS_ENV_VAR win outright with NO
operator-controlled ceiling at all -- unlike transport.read_host_guard
(fixed by lr-4ebce1/PR #33 for the sibling read-verb guard), there was no
third, config-file tier that only an operator could widen. Both
--allowed-host and CLAGENTIC_LOADOUT_PUSH_ALLOWED_HOSTS are set by the SAME
process invocation that can also repoint the live git remote (lr-0e39f9's
own threat model) -- a caller able to redirect the push target is, by
construction, equally able to widen the allowlist to match in that same
invocation, so the pre-fix allowlist protected against nothing from that
caller. SEPARATELY, the pre-fix `check_host_allowed` treated ANY empty
`allowed_hosts` (`if not allowed_hosts: return`) as "no restriction
configured" -- but an operator-configured allowlist can legitimately
resolve to an EMPTY set (an explicit "restrict to nothing" choice, or a
caller-supplied narrowing value with zero overlap against the config
ceiling), and that must DENY, not silently permit, every host.

FIX: mirrors transport.read_host_guard's own design (PR #33) exactly, via
the shared resolver `transport.host_guard_resolve` (lr-57573e extracts the
common algorithm so the two guards do not carry independently-drifting
copies of the same precedence/parsing logic -- see that module's own
docstring):
  - A THIRD tier, PUSH_HOST_CONFIG_SECTION.PUSH_HOST_CONFIG_KEY in the
    user-level <config_root>/config.yaml (named consistently with
    transport.read_host_guard.READ_HOST_CONFIG_SECTION/_KEY, but its OWN
    section/key -- see "OWN ALLOWLIST, NOT SHARED" below) is the ONLY
    source that WIDENS the effective set. It is operator-written ahead of
    time, outside any per-call argv/environment the caller controls.
  - Config UNSET: *explicit* > env var > None (permissive) --
    BYTE-FOR-BYTE the pre-fix precedence, so an unconfigured deployment
    (or one that has not yet opted into the config-file ceiling) sees NO
    behavior change.
  - Config SET: the config-file set is the ceiling; *explicit*/env can only
    NARROW it (intersection via transport.host_match.host_matches, not raw
    string equality), never widen past it. May resolve to an EMPTY
    frozenset -- a real "deny everything" outcome.
  - `resolve_allowed_hosts` now returns `frozenset[str] | None` (a BREAKING
    return-type change from the pre-fix bare `frozenset[str]`): None means
    "no restriction configured anywhere" (permissive); any frozenset,
    INCLUDING AN EMPTY ONE, means a restriction IS configured/resolved and
    membership is enforced. `check_host_allowed`'s own `if not
    allowed_hosts: return` collapse is GONE -- it now checks `allowed_hosts
    is None` explicitly, so an empty-but-configured set correctly denies
    every host instead of silently permitting all of them (the defect this
    fix closes).
  - A malformed or present-null PUSH_HOST_CONFIG_KEY value is a hard
    config error (InvalidPushHostConfigError, push.errors) raised BEFORE
    any credential is resolved -- never degraded to permissive. Only a
    GENUINELY ABSENT key means "unconfigured."
  - `check_host_allowed`'s refusal message is mode-aware
    (*config_is_set*): config UNSET points at --allowed-host/the env var
    (the thing that would have permitted the host in that mode); config SET
    points at the PUSH_HOST_CONFIG_SECTION.PUSH_HOST_CONFIG_KEY config key
    instead, and says the flag/env var alone can never widen past it.

OWN ALLOWLIST, NOT SHARED WITH transport.read_host_guard's own config
tier: the two guards anchor DIFFERENT inputs from DIFFERENT trust
boundaries (this module's target host comes from the comparatively
low-churn, repo-scoped live git remote; the read verb's resolved base comes
from a CLI flag/env var/config file supplied fresh on every invocation) --
see transport.read_host_guard's own module docstring, "OWN ALLOWLIST, NOT
SHARED WITH push's", for the symmetric argument from that module's side.
Each guard keeps its OWN env var (ALLOWED_HOSTS_ENV_VAR here is unchanged:
CLAGENTIC_LOADOUT_PUSH_ALLOWED_HOSTS) and its OWN config section/key
(PUSH_HOST_CONFIG_SECTION="push_host_guard", distinct from
transport.read_host_guard.READ_HOST_CONFIG_SECTION="read_host_guard") --
only the RESOLUTION ALGORITHM is shared, via
transport.host_guard_resolve, never the configuration surface.
"""

from __future__ import annotations

import os
from pathlib import Path

from clagentic_loadout.push.errors import HostDeniedError, InvalidPushHostConfigError
from clagentic_loadout.transport.host_guard_resolve import (
    config_is_set as _config_is_set,
    resolve_ceiling_hosts as _resolve_ceiling_hosts,
)
from clagentic_loadout.transport.host_match import host_matches

#: Env var carrying a comma-separated allowed-host list (each entry a bare
#: "host[:port]" authority or a full "scheme://host[:port]" URL -- both
#: shapes are accepted, see transport.host_match.host_matches). Unset or
#: empty means "no allowlist configured" (permissive -- see module
#: docstring). Unchanged name/behavior from before lr-57573e.
#:
#: CALLER-SETTABLE -- can only NARROW once PUSH_HOST_CONFIG_SECTION is
#: configured; see module docstring, "CALLER-WIDENING + EMPTY-SET-COLLAPSE
#: FIX".
ALLOWED_HOSTS_ENV_VAR = "CLAGENTIC_LOADOUT_PUSH_ALLOWED_HOSTS"

#: Top-level section in the USER-LEVEL <config_root>/config.yaml carrying
#: this verb's OPERATOR-CONTROLLED push allowlist -- the only source that
#: can WIDEN the effective set (lr-57573e). Named consistently with
#: transport.read_host_guard.READ_HOST_CONFIG_SECTION ("read_host_guard")
#: but its OWN section -- see module docstring, "OWN ALLOWLIST, NOT
#: SHARED". Read via transport.provider_config.load_user_config_section,
#: the SAME loader/config-root convention every other user-level config
#: tier in this package already uses (credentials:, forgejo:,
#: read_host_guard:).
PUSH_HOST_CONFIG_SECTION = "push_host_guard"

#: Key within PUSH_HOST_CONFIG_SECTION carrying the comma-separated (or
#: YAML-list) allowed-host list, same entry shape and parsing rule as
#: transport.read_host_guard.READ_HOST_CONFIG_KEY.
PUSH_HOST_CONFIG_KEY = "allowed_hosts"


def resolve_allowed_hosts(
    explicit: frozenset[str] | None = None,
    *,
    env: dict[str, str] | None = None,
    config_root: str | Path | None = None,
) -> frozenset[str] | None:
    """Resolve the allowed-host set for push's credentialed calls.

    See module docstring, "CALLER-WIDENING + EMPTY-SET-COLLAPSE FIX", for
    the full argument. Precedence (mirrors
    transport.read_host_guard.resolve_allowed_hosts exactly, via the
    shared transport.host_guard_resolve.resolve_ceiling_hosts):

      Config UNSET (no PUSH_HOST_CONFIG_SECTION.PUSH_HOST_CONFIG_KEY in the
      user-level <config_root>/config.yaml) -- BYTE-FOR-BYTE the pre-fix
      precedence, unchanged for back-compat:
        1. *explicit* (caller-supplied set, e.g. a --allowed-host CLI flag
           repeated N times) -- always wins when not None, even if empty (an
           explicit empty set is a real choice: "restrict to nothing",
           handled by the caller's own validation, not silently
           reinterpreted as permissive here).
        2. ALLOWED_HOSTS_ENV_VAR, comma-separated, whitespace-trimmed, empty
           entries dropped.
        3. None (no restriction configured -- permissive default).

      Config SET -- the config-file set is the ceiling; *explicit*/env can
      only NARROW it, never widen it:
        - *explicit* or env supplied -> the subset of the configured set
          that OVERLAPS whatever explicit/env resolved to (via
          transport.host_match.host_matches, not raw string equality). May
          be EMPTY when there is no overlap at all -- a real "deny
          everything" outcome, not permissive.
        - neither supplied -> the full configured set.

    RETURN TYPE (BREAKING from the pre-lr-57573e signature): now returns
    `frozenset[str] | None`, never conflating "no restriction configured
    anywhere" (None) with "a restriction IS configured and it resolved to
    zero permitted hosts" (an EMPTY frozenset). Every call site in this
    package (push.verb) is updated in the same change; a caller outside
    this package upgrading past this fix must treat `is None` as the
    permissive case instead of a bare truthiness check on the old
    always-frozenset return.

    *env* overrides os.environ for tests; defaults to the real process
    environment. *config_root* overrides the user-level config root the
    config-file tier reads from (mainly for tests), mirroring
    transport.read_host_guard.resolve_allowed_hosts's own `config_root`
    parameter.
    """
    active_env = env if env is not None else os.environ
    return _resolve_ceiling_hosts(
        explicit,
        env=active_env,
        env_var=ALLOWED_HOSTS_ENV_VAR,
        config_root=config_root,
        config_section=PUSH_HOST_CONFIG_SECTION,
        config_key=PUSH_HOST_CONFIG_KEY,
        invalid_config_error=InvalidPushHostConfigError,
    )


def push_host_config_is_set(config_root: str | Path | None = None) -> bool:
    """True iff PUSH_HOST_CONFIG_SECTION.PUSH_HOST_CONFIG_KEY is PRESENT in
    the user-level config file *config_root* points at -- i.e. the
    config-file tier is the ceiling for this resolution (see
    resolve_allowed_hosts's "Config SET" precedence).

    Lets push.verb build a corrective refusal message that names the RIGHT
    remediation for the mode actually in effect (lr-57573e, mirroring
    transport.read_host_guard.read_host_config_is_set exactly) -- see
    check_host_allowed's own `config_is_set` parameter. Raises
    InvalidPushHostConfigError under the same condition
    resolve_allowed_hosts itself would (a malformed configured value) --
    this function does not shield that call from its own fail-closed
    contract.
    """
    return _config_is_set(
        config_root=config_root,
        config_section=PUSH_HOST_CONFIG_SECTION,
        config_key=PUSH_HOST_CONFIG_KEY,
        invalid_config_error=InvalidPushHostConfigError,
    )


def check_host_allowed(
    api_base: str,
    *,
    allowed_hosts: frozenset[str] | None,
    config_is_set: bool = False,
) -> None:
    """Refuse *api_base* if an allowlist is configured and no entry in it
    matches *api_base*'s host:port (via transport.host_match.host_matches).

    *allowed_hosts* is None when NO restriction is configured anywhere --
    every host is permitted (permissive default, see module docstring). Any
    frozenset value, INCLUDING AN EMPTY ONE, means a restriction IS
    configured and membership is enforced: *api_base* must match at least
    one entry, and an empty frozenset (an operator's real "restrict to
    nothing" choice, or a caller-supplied value narrowed to zero overlap
    with the operator-configured ceiling) matches NOTHING and denies
    unconditionally.

    EMPTY-SET-COLLAPSE FIX (lr-57573e): the pre-fix version of this
    function treated ANY empty `allowed_hosts` (`if not allowed_hosts:
    return`) as "no restriction configured" -- silently permitting every
    host even when an operator had configured a real, empty "restrict to
    nothing" ceiling, or when a caller's own --allowed-host/env value
    narrowed the config ceiling to zero overlap. FIXED: this now checks
    `allowed_hosts is None` explicitly, mirroring
    transport.read_host_guard.check_host_allowed's own None/empty-frozenset
    split exactly -- collapsing "not configured" and "configured but empty"
    to the same falsy value would silently re-permit every host on exactly
    the caller-widening path the config-ceiling fix (above) exists to
    close.

    *config_is_set* (lr-57573e, mirrors
    transport.read_host_guard.check_host_allowed's own parameter): tells
    the refusal message construction which MODE produced *allowed_hosts*,
    so the corrective text is accurate in both. Defaults to False (the
    caller-settable corrective text) for back-compat with any direct caller
    that predates this parameter and still only ever runs in the
    config-UNSET mode where that text was always accurate.

      - config_is_set=False (config UNSET): --allowed-host / the env var ARE
        the thing that would have let this host through -- the ORIGINAL
        corrective text (set the env var, or pass an explicit allowed-host
        list) is accurate here and is kept.
      - config_is_set=True (config SET): --allowed-host / the env var can
        only NARROW the configured ceiling, never widen past it -- telling
        the operator to set either of those to permit this host would be
        FALSE; only editing the push_host_guard.allowed_hosts key in the
        user-level config file actually widens the effective set.

    Raises HostDeniedError before any credential is resolved or git
    operation attempted -- a host refusal is deterministic and must never
    partially execute, mirroring push.namespace_guard.check_namespace_allowed's
    own fail-closed-before-token-resolution posture.
    """
    if allowed_hosts is None:
        return
    if any(host_matches(api_base, entry) for entry in allowed_hosts):
        return
    if config_is_set:
        corrective = (
            f"This deployment has {PUSH_HOST_CONFIG_SECTION!r}.{PUSH_HOST_CONFIG_KEY!r} "
            f"configured in the user-level config file -- that key is the "
            f"CEILING for this allowlist. Add this host to it to permit "
            f"this call; --allowed-host and {ALLOWED_HOSTS_ENV_VAR} can "
            f"only NARROW the configured ceiling and can NEVER widen past "
            f"it, so setting either alone will not permit this host."
        )
    else:
        corrective = (
            f"Set {ALLOWED_HOSTS_ENV_VAR} (comma-separated) or pass an "
            f"explicit allowed-host list to permit this host."
        )
    raise HostDeniedError(
        f"push target host {api_base!r} (derived from the live git remote) "
        f"is not in the configured allowed-host set "
        f"({sorted(allowed_hosts)!r}). {corrective} Refusing before any "
        f"credential is resolved or git operation attempted -- this "
        f"refusal is deterministic; do not retry without changing the "
        f"configured allowlist or the git remote."
    )


__all__ = [
    "ALLOWED_HOSTS_ENV_VAR",
    "PUSH_HOST_CONFIG_KEY",
    "PUSH_HOST_CONFIG_SECTION",
    "check_host_allowed",
    "push_host_config_is_set",
    "resolve_allowed_hosts",
]
