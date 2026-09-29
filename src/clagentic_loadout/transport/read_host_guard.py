"""transport.read_host_guard — config-driven allowed-host anchoring for the
git-host-api (read) verb's credentialed call (lr-4ebce1, WIDEN/NARROW fix
lr-4ebce1 fold-in #1; FAIL-OPEN-ON-CONFIG + MISLEADING-MESSAGE fix lr-4ebce1
fold-in #2).

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

CALLER-WIDENING DEFECT + FIX (lr-4ebce1 fold-in #1, pre-merge security
review finding): the ORIGINAL version of this module let *explicit*
(the --allowed-host CLI flag) and ALLOWED_HOSTS_ENV_VAR win outright over an
unconfigured default -- but BOTH of those sources are set by the SAME
process invocation that also supplies --git-host-base-url. A caller who can
pass --git-host-base-url https://evil can, in the identical command line or
its own spawn environment, also pass --allowed-host evil (or export
ALLOWED_HOSTS_ENV_VAR=evil) and make check_host_allowed approve its own
redirect -- the "allowlist" protected against nothing when the thing being
anchored and the thing doing the anchoring share a trust boundary. An
allowlist is only a real control when it widens the permitted set from a
source the caller invoking THIS call cannot itself set.

FIX: a THIRD tier, READ_HOST_CONFIG_SECTION in the USER-LEVEL
<config_root>/config.yaml (the same file/loader
transport.provider_config.load_user_config_section already serves for the
`credentials:` and `forgejo:` sections -- no second YAML parser, no second
config path) is the only source that WIDENS the read allowlist. It is
operator-written to the user-level config file ahead of time, outside any
per-call argv/environment the caller controls -- exactly the same trust
boundary that already makes provider_config's `credentials:` tier safe
against a hostile repo-local override (lr-0818), applied here to a
caller-controlled CLI/env pair instead of a repo-local file.

Effective precedence, per check_host_allowed's ONE caller
(transport.git_host_api._run):
  - Config UNSET (no READ_HOST_CONFIG_SECTION.READ_HOST_CONFIG_KEY in the
    user-level file): *explicit* > env var > empty-permissive, BYTE-FOR-BYTE
    the pre-fix precedence -- an unconfigured deployment sees no behavior
    change, and every existing legitimate caller of --allowed-host/the env
    var (this verb's own documented, released contract) keeps working with
    no forced config write.
  - Config SET: the config-file set is the ceiling. explicit/env, when
    supplied, can only NARROW it (effective = config ∩ (explicit or env));
    when neither is supplied, the full config set is the effective
    allowlist. A caller can never use --allowed-host/the env var to ADD a
    host absent from the configured ceiling -- resolve_allowed_hosts simply
    never returns a wider set than the config tier once that tier is set,
    regardless of what a hostile or careless caller passes on argv/env.

RETURN-TYPE FIX, load-bearing for the above (NOT a cosmetic change):
resolve_allowed_hosts returns `frozenset[str] | None`, never conflating
"no restriction configured anywhere" (None) with "a restriction IS
configured and it resolved to zero permitted hosts" (an EMPTY frozenset --
e.g. config SET but narrowed by a caller value with no overlap, or an
operator's own explicit `allowed_hosts: ""` choice). check_host_allowed's
own pre-fix contract treated an EMPTY allowed_hosts as "permissive" -- if
this module kept returning a bare frozenset() for both cases, a caller
narrowing the configured ceiling to zero overlap (e.g. --allowed-host
pointed at a host absent from config) would resolve to frozenset() and
check_host_allowed would silently PERMIT EVERY HOST, reopening exactly the
caller-widening hole this fix exists to close, just one layer down. None
means "skip the check entirely" (check_host_allowed's own contract); any
frozenset, including an empty one, means "enforce membership against
exactly this set" -- an empty enforced set denies every host, correctly.

A caller-settable allowlist (the pre-fix shape, and the DEFAULT shape here
when config is unset) does NOT protect against that SAME caller -- it only
ever restricts an DIFFERENT, less-trusted caller (e.g. a sub-process this
one spawns with a scrubbed environment) or documents intent for a human
reading the invocation. An operator who actually needs protection against
THIS caller sets READ_HOST_CONFIG_SECTION.READ_HOST_CONFIG_KEY in the
user-level config file -- see docs/integration.md's "Host restriction
(git-host-api read verb)" section for the operator-facing statement of this
same rule.

TWO DEFECTS + FIX (lr-4ebce1 fold-in #2, pre-merge security review finding):

  1. FAIL-OPEN ON A NON-STRING CONFIG VALUE. The pre-fix
     _load_configured_allowed_hosts treated ANY non-string
     READ_HOST_CONFIG_KEY value -- including the natural YAML list shape
     `allowed_hosts: [a.example, b.example]` an operator would reach for
     first when authoring this key by hand -- as "not configured" and
     returned None (PERMISSIVE). An operator who wrote that YAML list,
     believing they had just turned host restriction ON, silently got ZERO
     enforcement, with no error anywhere to say so. FIXED: the key now
     accepts EITHER a comma-separated string OR a YAML list of strings; any
     OTHER type (an int, a mapping, a list containing a non-string entry)
     is a hard config error -- InvalidReadHostConfigError, naming the
     config file, section, key, received type, and the two accepted forms
     -- raised BEFORE any credential is resolved, never degraded to
     permissive. Only an ABSENT key means "unconfigured" now; a PRESENT
     malformed value never does.
  2. MISLEADING REFUSAL MESSAGE. check_host_allowed's pre-fix HostDeniedError
     always told the caller to set ALLOWED_HOSTS_ENV_VAR or pass
     --allowed-host to permit the denied host -- true only in the
     config-UNSET mode. Once READ_HOST_CONFIG_SECTION.READ_HOST_CONFIG_KEY
     is set, that text is FALSE: per this module's own "CALLER-WIDENING
     DEFECT + FIX" above, the env var/flag can only NARROW the configured
     ceiling, never widen past it -- an operator who followed the pre-fix
     message's advice in config-SET mode would edit the wrong thing and
     stay denied, with no clue why. FIXED: check_host_allowed now takes a
     `config_is_set` parameter (see read_host_config_is_set, which the
     caller -- transport.git_host_api._run -- uses to compute it) and
     builds the corrective text for whichever mode actually produced
     *allowed_hosts*: config-UNSET keeps the original env-var/flag text
     (still accurate there); config-SET points at the
     read_host_guard.allowed_hosts key in the user-level config file
     instead, and says explicitly that the flag/env var alone cannot
     widen past it.

ABSENT-VS-PRESENT-NULL FIX (lr-4ebce1 fold-in #3, pre-merge security review
finding): fold-in #2 above stated the "Only an ABSENT key means
'unconfigured'" rule
but did not fully implement it: _load_configured_allowed_hosts read the
config value via a bare `section.get(READ_HOST_CONFIG_KEY)`, which returns
None for BOTH "key not in the section" (unconfigured, correctly permissive)
AND "key in the section with an explicit `allowed_hosts: null` (or
`allowed_hosts:` with no value)" (an operator-authored value that collapsed
to the SAME permissive None return as never having written the key at all
-- the exact fail-open shape fold-in #2 exists to close, one layer up).
FIXED: _load_configured_allowed_hosts now checks
`READ_HOST_CONFIG_KEY in section` explicitly before reading the value, so a
genuinely absent key is the only way to reach the permissive None return; a
present-but-null value falls through to the same InvalidReadHostConfigError
every other malformed-value case raises, fail-closed before any credential
is minted.

SHARED RESOLVER EXTRACTION (lr-57573e): the config-ceiling/caller-narrow-
only resolution algorithm this module pioneered (everything described above
from "CALLER-WIDENING DEFECT + FIX" through "ABSENT-VS-PRESENT-NULL FIX") is
now factored into `transport.host_guard_resolve`, shared with
`push.host_guard` (which mirrors this same design rather than carrying a
second, independently-drifting copy). This module's own public surface
(`resolve_allowed_hosts`, `check_host_allowed`, `read_host_config_is_set`,
`InvalidReadHostConfigError`, the env-var/config-section/config-key
constants) is UNCHANGED — every existing caller and every existing test in
this module's own test file keeps working with no edit required; only the
internal parsing/precedence logic moved.
"""

from __future__ import annotations

import os
from pathlib import Path

from clagentic_loadout.transport.host_guard_resolve import (
    config_is_set as _config_is_set,
    resolve_ceiling_hosts as _resolve_ceiling_hosts,
)
from clagentic_loadout.transport.host_match import host_matches

#: Env var carrying a comma-separated allowed-host list for the git-host-api
#: (read) verb's resolved git-host base (each entry a bare "host[:port]"
#: authority or a full "scheme://host[:port]" URL -- both shapes accepted,
#: see transport.host_match.host_matches). Unset or empty means "no
#: allowlist configured" (permissive -- see module docstring). Deliberately
#: NOT push.host_guard.ALLOWED_HOSTS_ENV_VAR -- see module docstring, "OWN
#: ALLOWLIST, NOT SHARED WITH push's".
#:
#: CALLER-SETTABLE -- can only NARROW once READ_HOST_CONFIG_SECTION is
#: configured; see module docstring, "CALLER-WIDENING DEFECT + FIX".
ALLOWED_HOSTS_ENV_VAR = "CLAGENTIC_LOADOUT_READ_ALLOWED_HOSTS"

#: Top-level section in the USER-LEVEL <config_root>/config.yaml carrying
#: this verb's OPERATOR-CONTROLLED read allowlist -- the only source that
#: can WIDEN the effective set (see module docstring, "CALLER-WIDENING
#: DEFECT + FIX"). Read via transport.provider_config.load_user_config_section,
#: the SAME loader/config-root convention every other user-level config tier
#: in this package already uses (credentials:, forgejo:).
READ_HOST_CONFIG_SECTION = "read_host_guard"

#: Key within READ_HOST_CONFIG_SECTION carrying the comma-separated allowed-
#: host list, same entry shape (bare "host[:port]" or full
#: "scheme://host[:port]") and same parsing rule as ALLOWED_HOSTS_ENV_VAR.
READ_HOST_CONFIG_KEY = "allowed_hosts"


class InvalidReadHostConfigError(Exception):
    """Raised when READ_HOST_CONFIG_SECTION.READ_HOST_CONFIG_KEY in the
    user-level config file holds a value that is not one of the two
    accepted shapes (a comma-separated string, or a YAML list of strings) --
    see _load_configured_allowed_hosts's "FAIL-OPEN FIX" for why this is a
    hard refusal rather than a silent "treat as unconfigured" degrade.

    Fires BEFORE any credential is resolved or request issued -- a
    malformed config value must never let a call proceed as if no
    restriction were configured (lr-4ebce1 fold-in #2). The message names
    the config FILE, SECTION, KEY, the RECEIVED type, and the ACCEPTED
    forms, so an operator can fix the value without reading this module's
    source.
    """

    def __init__(self, config_path: Path, *, received: object, detail: str | None = None) -> None:
        received_type = type(received).__name__
        message = (
            f"{config_path}: [{READ_HOST_CONFIG_SECTION}].{READ_HOST_CONFIG_KEY} "
            f"holds a {received_type} ({received!r}), which is not a valid "
            f"read-host allowlist value"
        )
        if detail:
            message += f" ({detail})"
        message += (
            ". Accepted forms: a comma-separated string "
            '(e.g. "a.example.com,b.example.com:3000"), or a YAML list of '
            'strings (e.g. ["a.example.com", "b.example.com:3000"]). Fix '
            f"the {READ_HOST_CONFIG_KEY!r} value under the "
            f"{READ_HOST_CONFIG_SECTION!r} section in {config_path}. "
            "Refusing before any credential is resolved or request is "
            "issued -- a malformed config value is never treated as "
            "'unconfigured' (which would silently disable the restriction "
            "the operator was trying to set)."
        )
        super().__init__(message)
        self.config_path = config_path
        self.received = received


def resolve_allowed_hosts(
    explicit: frozenset[str] | None = None,
    *,
    env: dict[str, str] | None = None,
    config_root: str | Path | None = None,
) -> frozenset[str] | None:
    """Resolve the allowed-host set for the read verb's credentialed call.

    See module docstring, "CALLER-WIDENING DEFECT + FIX" and "RETURN-TYPE
    FIX", for the full argument. Precedence:

      Config UNSET (no READ_HOST_CONFIG_SECTION.READ_HOST_CONFIG_KEY in the
      user-level <config_root>/config.yaml) -- BYTE-FOR-BYTE the pre-fix
      precedence, unchanged for back-compat:
        1. *explicit* (caller-supplied set, e.g. a --allowed-host CLI flag
           repeated N times) -- always wins when not None, even if empty (an
           explicit empty set is a real choice: "restrict to nothing",
           handled by the caller's own validation, not silently
           reinterpreted as permissive here).
        2. ALLOWED_HOSTS_ENV_VAR, comma-separated, whitespace-trimmed, empty
           entries dropped.
        3. None (no restriction configured -- permissive default; see
           RETURN-TYPE FIX for why this is None rather than an empty
           frozenset).

      Config SET -- the config-file set is the ceiling; *explicit*/env can
      only NARROW it, never widen it:
        - *explicit* or env supplied (non-None/non-empty-string) -> the
          subset of the configured set that OVERLAPS whatever explicit/env
          resolved to (explicit still wins over env when both are given, as
          in the unset-config case -- only ONE of the two is ever matched
          against config, matching the existing "explicit wins" rule one
          level up). May be EMPTY when there is no overlap at all -- a real
          "deny everything" outcome, not permissive (see RETURN-TYPE FIX).
        - neither supplied -> the full configured set (also enforced, never
          reinterpreted as permissive even when the operator configured it
          to be empty).

    *env* overrides os.environ for tests; defaults to the real process
    environment. *config_root* overrides the user-level config root the
    config-file tier reads from (mainly for tests), mirroring
    transport.git_host_api._resolve_git_host_base's own `config_root`
    parameter.

    SHARED RESOLVER (lr-57573e): the precedence above is implemented once,
    in `transport.host_guard_resolve.resolve_ceiling_hosts` -- this is a
    thin, module-specific wrapper supplying READ_HOST_CONFIG_SECTION/
    READ_HOST_CONFIG_KEY/ALLOWED_HOSTS_ENV_VAR/InvalidReadHostConfigError,
    the same shape `push.host_guard.resolve_allowed_hosts` now also wraps.
    """
    active_env = env if env is not None else os.environ
    return _resolve_ceiling_hosts(
        explicit,
        env=active_env,
        env_var=ALLOWED_HOSTS_ENV_VAR,
        config_root=config_root,
        config_section=READ_HOST_CONFIG_SECTION,
        config_key=READ_HOST_CONFIG_KEY,
        invalid_config_error=InvalidReadHostConfigError,
    )


class HostDeniedError(Exception):
    """Raised when the git-host-api verb's resolved git-host base is not
    present in the caller-configured allowed-host set (see
    check_host_allowed below, lr-4ebce1). Caught at the CLI boundary in
    transport.git_host_api._run/main and mapped to EXIT_HOST_DENIED --
    fires BEFORE any credential is resolved or request issued, mirroring
    push.errors.HostDeniedError's own posture for the sibling guard."""


def read_host_config_is_set(config_root: str | Path | None = None) -> bool:
    """True iff READ_HOST_CONFIG_SECTION.READ_HOST_CONFIG_KEY is PRESENT in
    the user-level config file *config_root* (or DEFAULT_USER_CONFIG_ROOT
    when None) points at -- i.e. the config-file tier is the ceiling for
    this resolution (see resolve_allowed_hosts's "Config SET" precedence).

    Lets a caller (transport.git_host_api._run) build a corrective refusal
    message that names the RIGHT remediation for the mode actually in
    effect (lr-4ebce1 fold-in #2, MISLEADING REFUSAL MESSAGE fix -- see
    check_host_allowed's own docstring, "mode" parameter): telling an
    operator who already set this config key to set --allowed-host/the env
    var instead is false -- those can only NARROW the configured ceiling,
    never widen past it. Raises InvalidReadHostConfigError under the same
    condition _load_configured_allowed_hosts itself would (a malformed
    configured value) -- this function does not shield that call from its
    own fail-closed contract; a caller wanting the boolean also accepts the
    error propagating on a malformed config value, exactly like every other
    caller of the config-file tier.
    """
    return _config_is_set(
        config_root=config_root,
        config_section=READ_HOST_CONFIG_SECTION,
        config_key=READ_HOST_CONFIG_KEY,
        invalid_config_error=InvalidReadHostConfigError,
    )


def check_host_allowed(
    git_host_base: str,
    *,
    allowed_hosts: frozenset[str] | None,
    config_is_set: bool = False,
) -> None:
    """Refuse *git_host_base* if an allowlist is configured and no entry in
    it matches *git_host_base*'s host:port (via
    transport.host_match.host_matches).

    *allowed_hosts* is None when NO restriction is configured anywhere --
    every host is permitted (permissive default, see module docstring). Any
    frozenset value, INCLUDING AN EMPTY ONE, means a restriction IS
    configured and membership is enforced: *git_host_base* must match at
    least one entry, and an empty frozenset (an operator's real "restrict to
    nothing" choice, or a caller-supplied value narrowed to zero overlap
    with the operator-configured ceiling -- see read_host_guard's own
    "RETURN-TYPE FIX") matches NOTHING and denies unconditionally. This is
    the load-bearing distinction the None/empty-frozenset split exists for
    -- collapsing "not configured" and "configured but empty" to the same
    falsy value would silently re-permit every host on exactly the
    caller-widening path this fix (lr-4ebce1 fold-in #1) closes.

    *config_is_set* (lr-4ebce1 fold-in #2, MISLEADING REFUSAL MESSAGE fix):
    tells the refusal message construction which MODE produced
    *allowed_hosts*, so the corrective text is accurate in both --
    see read_host_config_is_set, which the caller (transport.git_host_api.
    _run) uses to compute this value. Defaults to False (the caller-settable
    corrective text) for back-compat with any direct caller that predates
    this parameter and still only ever runs in the config-UNSET mode where
    that text was always accurate.

      - config_is_set=False (config UNSET): --allowed-host / the env var ARE
        the thing that would have let this host through -- the ORIGINAL
        corrective text (set the env var, or pass --allowed-host) is
        accurate here and is kept.
      - config_is_set=True (config SET): --allowed-host / the env var can
        only NARROW the configured ceiling, never widen past it (see
        resolve_allowed_hosts's "Config SET" precedence) -- telling the
        operator to set either of those to permit this host would be FALSE;
        only editing the read_host_guard.allowed_hosts key in the
        user-level config file actually widens the effective set.

    Raises HostDeniedError BEFORE any credential is resolved or request
    issued -- a host refusal is deterministic and must never partially
    execute, mirroring push.host_guard.check_host_allowed's own
    fail-closed-before-token-resolution posture. Caught at the CLI boundary
    in transport.git_host_api._run and mapped to EXIT_HOST_DENIED (a
    GitHostApiError, this module's caller's own vocabulary -- this module
    stays a pure predicate with no dependency back on the verb it guards,
    exactly like push.host_guard's relationship to push.errors).
    """
    if allowed_hosts is None:
        return
    if any(host_matches(git_host_base, entry) for entry in allowed_hosts):
        return
    if config_is_set:
        corrective = (
            f"This deployment has {READ_HOST_CONFIG_SECTION!r}.{READ_HOST_CONFIG_KEY!r} "
            f"configured in the user-level config file -- that key is the "
            f"CEILING for this allowlist. Add this host to it to permit "
            f"this call; --allowed-host and {ALLOWED_HOSTS_ENV_VAR} can "
            f"only NARROW the configured ceiling and can NEVER widen past "
            f"it, so setting either alone will not permit this host."
        )
    else:
        corrective = (
            f"Set {ALLOWED_HOSTS_ENV_VAR} (comma-separated) or pass an "
            f"explicit --allowed-host value (repeatable) to permit this "
            f"host."
        )
    raise HostDeniedError(
        f"git-host-api target host {git_host_base!r} (the resolved "
        f"git-host base -- see --git-host-base-url) is not in the "
        f"configured allowed-host set ({sorted(allowed_hosts)!r}). "
        f"{corrective} Refusing before any credential is resolved or "
        f"request is issued -- this refusal is deterministic; do not "
        f"retry without changing the configured allowlist or the resolved "
        f"git-host base."
    )


__all__ = [
    "ALLOWED_HOSTS_ENV_VAR",
    "READ_HOST_CONFIG_KEY",
    "READ_HOST_CONFIG_SECTION",
    "HostDeniedError",
    "InvalidReadHostConfigError",
    "check_host_allowed",
    "read_host_config_is_set",
    "resolve_allowed_hosts",
]
