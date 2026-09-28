"""transport.attestation — resolves the ATTESTED INVOKING IDENTITY.

Task lr-82c385 (tome #700), the loadout-native half of the three-layer trust
model documented across `docs/merge-authority.md` §4 and
`transport.credential_provider`'s own module docstring: attested invoking
identity (1) -> crew/role (`--caller`, layer 2) -> credential grantor
(layer 3). Every seam downstream of layer (2) -- `credential_provider.
resolve_token`, `merge.authority.check_authority` -- has always, deliberately,
treated `--caller`/`--role` as an ALREADY-ATTESTED, opaque value (lr-e5eeab)
and refused to re-derive or re-verify it, precisely so this package never
reaches into a harness-specific identity sidecar/side-channel (the relay
lesson, CLAUDE.md rule 2). That left the (1)->(2) BINDING ITSELF -- "does the
`--caller` value on this invocation's argv actually match the identity this
deployment's own attestation source vouches for" -- entirely unenforced
inside loadout. This module resolves what the identity IS; `bind_caller` (in
`transport.git_host_api`) is the fail-closed enforcement point that compares
it against `--caller` BEFORE any network I/O.

Mirrors the Go reference contract shipped in clagentic-gatekeeper's T0
(lr-83549f, `internal/attestation`, PR #15 @ 9e9116c) -- SAME resolution
order, SAME "a provider that finds nothing falls through, any other error is
a hard failure" semantics, ported to Python for loadout's own transport
rather than importing across a language boundary. No agent names, org names,
or other deployment-specific identities are hardcoded anywhere in this
module (workspace rule 11 / build-to-share) -- see `resolve_identity`'s
config surface below for exactly what a deployment supplies.

Resolution order, per deployment (fixed; a deployment customizes or omits
each layer via config, but never reorders the chain):

  1. **Configured provider** -- a deployment points this module at its own
     identity source via the `CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_ENV` env
     var (names ANOTHER env var this process's own spawn env already
     carries the attested identity under) or the user-level config file's
     `attestation.identity_env` key (same config-root/loader convention
     `transport.provider_config` and `transport.git_host_api`'s git-host-
     base-URL tier already use). Whichever env var that name points at is
     read verbatim as `Identity(subject=..., source="configured")`. Takes
     precedence whenever it resolves to a non-empty value.
  2. **Sidecar adapter** -- reads a session-scoped identity file written by
     an external harness. Within this layer, THREE sources are tried in
     this fixed order, the first that resolves wins (lr-8e1593):

       (a) `CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH` (env) -- a
           single literal path. HIGHEST precedence within the layer,
           unchanged from before lr-8e1593 -- preserves compatibility with
           a harness that stamps this exact env var into a per-command
           spawn's environment. FAIL-CLOSED ON MISS (lr-1e16a4): unlike
           (b)/(c) below, this source is an EXPLICIT per-invocation claim
           -- the caller is naming a specific file it expects to hold ITS
           OWN identity. If that env var is SET but the file it names is
           absent, empty, or unreadable-as-empty, this layer does NOT fall
           through to (b), (c), or the built-in fallback; the WHOLE
           resolution chain fails closed with `AttestationError`. Falling
           through here would risk silently resolving a DIFFERENT identity
           (most concretely, a lower-precedence session-keyed adapter
           resolving a PARENT session's identity) for whatever this
           process's real identity was supposed to be -- a privilege-
           substitution shape at any caller-bound mint (`bind_caller`).
           Mirrors clagentic-gatekeeper's DomainA2A fail-closed-on-MISS
           fix (lr-2ca216). This trigger is "env var SET to a path that
           does not resolve," never "env var unset" -- an unset env var
           still declines this source ordinarily and falls through to
           (b)/(c) exactly as before (see `_ConfiguredEnvProvider`'s and
           this source's own resolve() for where that branch lives).
       (b) the config file's `attestation.identity_sidecar_path` key -- a
           single literal path, RETAINED for backward compatibility,
           unchanged semantics.
       (c) the config file's `attestation.sidecars` key (NEW, lr-8e1593)
           -- an ORDERED LIST of adapters, each
           `{dir, file_prefix, session_id_env}`. Walked in declared order;
           an adapter is SKIPPED (not an error) when its `session_id_env`
           is unset/empty in this process's environment, or the file it
           composes (`dir/file_prefix<session id>`) is absent. The first
           adapter that both has a non-empty session id AND whose composed
           file exists wins. This is what makes session-keyed sidecar
           discovery possible at all: (a)/(b) can only ever name ONE
           literal path per process, so a harness that runs many
           concurrently-live sessions (each with its own session-scoped
           sidecar file) has no way to point every invocation at "its own"
           file without per-invocation env-stamping -- (c) lets a
           deployment instead point this module at the *shape* of its
           sidecar convention (a directory + filename prefix) and have it
           find the right file for THIS invocation via whatever session-id
           env var this process already carries, no per-command stamping
           required. Mirrors clagentic-gatekeeper's Go reference adapter
           list (`internal/attestation/sidecar.go`, epic lr-0029bf) --
           see that module's `SidecarConfig`/`isSafePathSegment`/
           `requireContained` for the faithful-port source this list is
           ported from. A session id value that is NOT path-safe (contains
           a path separator, or is `.`/`..`) is REFUSED for that adapter
           (skip to the next adapter), never sanitized -- an attacker who
           controls the session-id env var must not be able to redirect
           the composed path outside `dir` by smuggling `../` into it.

     This module does NOT assume any specific harness/sidecar SHAPE beyond
     "a file containing exactly one identity value" (the file's entire
     stripped text content, first line only) -- every path here is
     supplied by config, never hardcoded, and this is the ONLY place a
     sidecar path ever enters this module: it is read, not interpreted as
     belonging to any named harness. Every resolved path (from any of the
     three sources) is opened atomically with `os.O_NOFOLLOW`, and every
     check runs against that same file descriptor (no separate
     lstat-then-reopen sequence, closing the residual TOCTOU window
     lr-904b1d fixed) -- a symlink (or any other non-regular directory
     entry) is refused with a hard `AttestationError`, never silently
     followed, matching the Go reference's own symlink-refusal fix (see
     `_SidecarFileProvider`'s own docstring for the full rationale: a
     planted symlink in a world-writable directory such as `/tmp` must
     never be able to redirect this read to an arbitrary file, since the
     resolved value feeds `bind_caller`'s authorization decision
     directly).

     No adapter across all three sources resolves -> this layer declines
     entirely -> the chain proceeds to the built-in fallback exactly as it
     did before this list existed; a deployment that configures nothing
     new here sees byte-identical behavior (acceptance criterion (b),
     lr-8e1593).
  3. **Built-in fallback** -- the OS-reported invoking user
     (`getpass.getuser()`, which itself falls back through `LOGNAME`/
     `USER`/`LNAME`/`USERNAME` env vars and finally the passwd database on
     POSIX). Always available, so a bare install has an attested source
     rather than failing open with no identity at all.

Every layer that is configured but has nothing to offer (missing env var,
a genuinely ABSENT sidecar file at sources (b)/(c)) falls through to the
next layer -- NOT a hard failure. Three things ARE hard failures, raised
immediately and never swallowed into a fall-through: (a) the sidecar path
resolving to a symlink or other non-regular directory entry (see layer 2
above -- a security refusal, not an ordinary decline), (b) source (a) of
layer 2 (the env-var single-path override) being explicitly SET but its
file being absent/empty/unreadable-as-empty (lr-1e16a4 -- an EXPLICIT
per-invocation claim that misses must not be silently demoted to
"unconfigured, try something lower-precedence"; see source (a) above for
the full rationale), and (c) the built-in fallback itself failing (should
never happen in practice; `getpass.getuser()` degrades all the way to a
`KeyError`/`OSError` on a truly identity-less environment), since there is
nothing left to fall through to either way.

This module resolves WHAT the identity is. It does not itself decide
whether that identity may act as any particular `--caller`/role value --
that binding-and-refusal decision is `transport.git_host_api.bind_caller`'s
job (see that function's docstring for the fail-closed comparison and its
own module docstring's "layer (1)->(2) binding" note).

BOUND RESOLUTION -- `resolve_bound_identity` (mirrors clagentic-gatekeeper's
`DomainLocalSubagent`, `internal/attestation/domain_policy.go`, READ-ONLY
prior art, not imported across the language boundary):

`resolve_identity` above is used in two shapes by this package: (1) a
caller-BOUND verb (`push`, `review`, `acquire`, `merge`, `merge --close`,
`merge --post-merge`, and `git_host_api` itself) that feeds its result
straight into `transport.caller_binding.bind_caller`, and (2) a handful of
non-binding, presence-only callers (`doctor.checks`) that never make a
trust decision from the result. Shape (1) is the one a fail-closed policy
targets: the built-in OS-user layer (`_BuiltinOsUserProvider`,
`SOURCE_BUILTIN`) answers with the literal host uid whenever layers 1/2
have nothing to offer -- on a shared/service host running as `root`, THAT
uid is not an attested identity at all, and `bind_caller` comparing
`--caller`/`--role` against it as though it were one is a confused-deputy
shape (a top-level session naming itself explicitly gets refused as "not
root," while an unauthenticated process typing any name at all that
happens to equal the host uid would be silently admitted).

`resolve_bound_identity` is the caller-bound entry point every `bind_caller`
call site uses INSTEAD of the bare `resolve_identity()` call. It differs
from the ordinary chain in two ways:

  1. **WHETHER the built-in layer may answer is a POLICY, not a constant**
     -- see `ATTESTATION_CONFIG_KEY_BOUND_IDENTITY` /
     `BOUND_IDENTITY_POLICY_REQUIRED` / `BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK`
     below. Under `"required"`, `resolve_bound_identity` never includes
     `_BuiltinOsUserProvider` in its chain at all -- a process with no
     configured-provider identity (layer 1) and no resolving sidecar
     (layer 2) gets a terminal `BoundAttestationError` ("no attested
     identity"), never a host uid standing in for one. Under
     `"builtin-fallback"`, a miss on layers 1/2 falls through to the
     built-in OS-user layer exactly as the general `resolve_identity`
     chain does -- preserving this package's previously-released behavior
     for a deployment that has not yet configured any sidecar adapters.
     `DEFAULT_BOUND_IDENTITY_POLICY` (a single module constant) decides
     which applies when a deployment's config sets no explicit
     `attestation.bound_identity` value.
  2. **A discriminator chooses WHICH sidecar source may answer, not "the
     first one that has a file" -- driven entirely by CONFIG, never a
     hardcoded harness-specific env var name.** Each entry in
     `attestation.sidecars` may declare a `scope` key
     (`SIDECAR_ADAPTER_KEY_SCOPE`): `"per-spawn"`
     (`SIDECAR_SCOPE_PER_SPAWN`) or `"session"` (`SIDECAR_SCOPE_SESSION`).
     An adapter with no `scope` key at all is simply not eligible to answer
     a BOUND resolution -- it remains fully usable by the ordinary
     `resolve_identity` chain, which does not consult `scope` at all (see
     `_SidecarFileProvider._resolve_adapter`). An adapter that DOES declare
     a `scope` key with an unrecognized value is a hard config error (see
     `_any_adapter_declares_recognized_scope`'s own docstring) -- never
     silently treated the same as "no scope key."

     KEPT BY DESIGN, NOT CONSULTED IN SCOPED BOUND RESOLUTION (lr-620837
     fold-in #4, F2): once at least one adapter declares
     a recognized `scope` (the discriminator above is active), this
     function's per-adapter walk (`_resolve_sidecar_adapter`, called only
     against `per_spawn_adapters`/`session_adapters`) is the ENTIRE sidecar
     lookup for that resolution -- `_SidecarFileProvider`'s OTHER two
     sources, the env-named single-path override
     (`ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR`,
     `CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH`) and the config
     single-path override (`ATTESTATION_CONFIG_KEY_SIDECAR_PATH`), are
     NEVER consulted in this scoped branch at all. This is DELIBERATE, not
     an oversight: both of those sources are named by something the
     INVOKING COMMAND itself controls (an env var an arbitrary command can
     set, or a config value read at the SAME process-level trust as the
     `attestation.sidecars` list a command cannot influence per-invocation)
     -- see this module's earlier `HYPOTHESES TESTED AND REFUTED`-adjacent
     finding (comment #4 on lr-620837 the task this module's own docstring
     already cites) that any in-command assignment of the env-named path
     overrides a harness's own stamp. Once a deployment has opted into the
     scoped discriminator, honoring a command-settable path override here
     would reopen exactly the redirection surface `scope` exists to close:
     a per-spawn or session invocation could point itself at a DIFFERENT
     identity's sidecar file simply by setting an env var or (for the
     config-file source) by whatever wrote the config having its own,
     separate compromise. The unscoped/legacy branch above (`resolve_
     identity`-equivalent lookup via `_SidecarFileProvider.resolve()`,
     "UNSCOPED-CONFIG UPGRADE SAFETY") is UNAFFECTED -- it still walks all
     three sources exactly as `resolve_identity` always has, since that
     branch is byte-identical-to-released-behavior BY DESIGN for a
     deployment that has not opted into scoping at all.

     The discriminator itself: for every adapter declared with
     `scope: per-spawn`, check whether THAT adapter's own `session_id_env`
     is set (non-empty) in this process's environment.

       - **Any per-spawn-scoped adapter's `session_id_env` is set:** this
         invocation declares itself a per-spawn subagent (whatever
         mechanism the deployment's harness uses to stamp that env var
         into a per-spawn command's environment -- this module never
         hardcodes the var's NAME). ONLY a `scope: per-spawn` adapter may
         resolve. A miss (no such adapter configured at all, or every
         per-spawn adapter with a set env var still declines -- e.g. its
         composed file is absent) is a terminal `BoundAttestationError`
         naming the per-spawn sidecar as the expected, unmet source -- it
         never falls through to a `scope: session` adapter, even one that
         WOULD resolve, because that fallthrough is the exact
         confused-deputy shape (a subagent minting its parent's identity)
         this discriminator exists to prevent.
       - **No per-spawn-scoped adapter's `session_id_env` is set (this
         also covers the case where NO adapter is declared `scope:
         per-spawn` at all):** this invocation is a top-level session.
         ONLY a `scope: session` adapter may resolve. A miss is a terminal
         `BoundAttestationError` naming the session-scoped sidecar as the
         expected, unmet source.

     This is a HARD requirement, enforced by CODE against the declared
     `scope` value, never by adapter list ORDER in a deployment's config
     file -- a discriminator implemented as "whichever adapter happens to
     be declared first" is not verifiable from the refusal alone, and
     silently degrades the moment a deployment's config drifts.

     Layer 1 (the configured-provider env var, `SOURCE_CONFIGURED`) is
     UNCHANGED and still takes precedence over either sidecar source when
     it resolves -- it is a real, deployment-declared attested identity,
     not the built-in fallback this policy targets, and narrowing it here
     was never in scope.

     THE SUBAGENT-DISCRIMINATOR REFUSAL IS UNCONDITIONAL, REGARDLESS OF
     POLICY: a per-spawn-declared invocation (a set `session_id_env` on a
     `scope: per-spawn` adapter) whose per-spawn adapter misses is REFUSED
     even under `"builtin-fallback"` -- it never falls through to a
     `scope: session` adapter NOR to the built-in layer. This is a
     correctness fix (closing the confused-deputy shape above), not a
     policy choice the `bound_identity` knob is meant to relax; only "may
     an UNDISCRIMINATED miss (no adapter of any scope resolves, or no
     `attestation.sidecars` configured at all) fall through to the
     built-in layer" is what the policy controls.

  Refusal messages and the `Identity.source` value a successful bound
  resolution returns both NAME which source answered or was expected
  (`SOURCE_SIDECAR_SUBAGENT` / `SOURCE_SIDECAR_SESSION`, new and more
  specific than the general chain's single `SOURCE_SIDECAR` label) -- the
  pre-fix `caller_binding.CallerBindingError` message rendered
  `identity.source` as the bare string `'sidecar'` for all three
  sidecar-shaped sources, which made a past incident harder to diagnose
  than it needed to be.

  `resolve_identity`'s own three-layer chain, `Identity.source`'s existing
  `SOURCE_SIDECAR` value on THAT path, and every non-bound caller of
  `resolve_identity` (e.g. `doctor.checks`, which never makes a trust
  decision from the result) are UNCHANGED by any of this -- this is a
  second, bound-specific entry point, not a rewrite of the general chain.

ADAPTER-LESS AND UNSCOPED-ADAPTER BEHAVIOR (well-defined, exercised by
tests):
  - No `attestation.sidecars` list configured at all (or an empty list):
    the discriminator finds no per-spawn-scoped adapter with a set env
    var, so this resolves as a top-level session; there is no
    `scope: session` adapter either, so the session lookup also misses.
    Under `"required"`, this is a terminal refusal. Under
    `"builtin-fallback"`, this falls through to the built-in layer.
  - An adapter with no `scope` key at all: never eligible for EITHER
    bound-resolution branch above (treated as declining the bound
    discriminator entirely, exactly like "not declared") -- it remains
    fully eligible for the ordinary, non-bound `resolve_identity` chain,
    which walks `attestation.sidecars` in declared order with no `scope`
    check at all.
  - An adapter that DOES declare a `scope` key, but sets it to a value
    other than `SIDECAR_SCOPE_PER_SPAWN`/`SIDECAR_SCOPE_SESSION`: a HARD
    `AttestationConfigError` (lr-620837 fold-in #4, F3), never silently
    treated as "not declared" -- see
    `_any_adapter_declares_recognized_scope`'s own docstring for why a
    present-but-misspelled `scope` value must be loud rather than
    quietly demoted to the legacy/unscoped shape above.

UNSCOPED-CONFIG UPGRADE SAFETY (lr-620837 fold-in #3): the behavior above
-- "an unscoped adapter is simply ineligible for bound resolution" -- is
correct ONLY once a deployment has started declaring `scope` on at least
one adapter. It is
WRONG, and was a live defect, for the far more common shape: a deployment
that has configured `attestation.sidecars` at all, but declared `scope` on
NONE of them (every existing config as of this fix, including this
package's own deployed host config). Under that shape, the OLD code walked
straight past every declared adapter (both `per_spawn_adapters` and
`session_adapters` come back empty, since neither list has any entries)
and went directly to the `bound_identity` policy check -- under the
default `"builtin-fallback"` policy, that meant landing on the built-in
OS-user layer despite a perfectly good, resolving sidecar adapter sitting
right there in config, unread. On a shared/service host running as `root`,
that is exactly the confused-deputy shape this whole function exists to
prevent, self-inflicted by the upgrade itself: an existing config silently
downgrades from sidecar identity to host uid the moment a process picks up
this code, and every caller-bound verb that used to resolve correctly
starts resolving as `root` instead -- before any config migration, with no
warning.

THE FIX: `resolve_bound_identity` now distinguishes two distinct "nothing
scoped resolved" shapes, checked BEFORE the discriminator/policy logic
above ever runs:

  - **No adapter across the ENTIRE `attestation.sidecars` list declares a
    recognized `scope` value at all** (a legacy/unscoped config -- this
    covers both "no `sidecars` list configured" and "a `sidecars` list
    exists but every entry omits `scope` or sets an unrecognized value"):
    bound resolution delegates to the SAME sidecar-resolution codepath the
    ordinary `resolve_identity` chain uses (`_SidecarFileProvider`, all
    three of its sources -- the env single-path override, the config
    single-path override, AND the adapter list walked in declared order
    with no `scope` check) and reports the generic `SOURCE_SIDECAR` label
    on success, EXACTLY as `resolve_identity` would for the same
    env/config. A miss there still falls through to the `bound_identity`
    policy (built-in fallback, or a terminal refusal under `"required"`)
    -- unchanged from before. This makes upgrading to this code a NO-OP
    for any deployment that has not yet opted into `scope`-tagged
    adapters: the exact same identity resolves, via the exact same
    source, whether policy is `"required"` or `"builtin-fallback"`, other
    than a `"required"` deployment additionally losing the builtin
    fallback it would have had anyway (working as intended, and the same
    trade-off `"required"` always described).
  - **At least one adapter across the list declares a recognized `scope`**
    (the mixed case included -- some adapters scoped, some not): the
    scoped discriminator rules from the rest of this docstring apply
    exactly as before. Every UNSCOPED adapter in that same list is
    ignored for bound resolution purposes ONLY -- it remains fully usable
    by the ordinary `resolve_identity` chain, which never reads `scope`.
    This is the one case where "no scope key" still means "ineligible":
    once a deployment has started using the discriminator at all, a
    stray unscoped adapter left in the list must not become a silent
    third answer alongside the two recognized scopes.

`ATTESTATION_CONFIG_KEY_BOUND_IDENTITY: "required"` behavior is
UNCHANGED by this fix in the case that actually matters for it: a
legacy/unscoped config under `"required"` still refuses when its sidecar
source does not resolve -- but the refusal message now explicitly says
resolution is unscoped and names the missing `scope` key, rather than the
scoped-source language ("the per-spawn subagent sidecar" / "the
session-scoped sidecar") that made no sense for a config declaring no
scopes at all. This is what makes an unscoped config's misconfiguration
LOUD under the strict policy rather than looking like an ordinary
per-spawn/session refusal.
"""

from __future__ import annotations

import errno
import getpass
import os
import stat
from pathlib import Path
from typing import Protocol, runtime_checkable

from clagentic_loadout.transport.provider_config import (
    DEFAULT_USER_CONFIG_ROOT,
    load_user_config_section,
)

#: Top-level config-file section this module owns within the USER-LEVEL
#: <config_root>/config.yaml -- same file/loader convention every other
#: user-level config tier in this package uses (transport.provider_config's
#: `credentials:` section, transport.git_host_api's `git_host:` section).
ATTESTATION_CONFIG_SECTION = "attestation"

#: Config-file key naming the env var that carries the configured-provider
#: identity value (layer 1). The value of THIS key is a NAME, never the
#: identity value itself -- mirrors CommandTokenProvider's argv-template
#: indirection: config never carries a live credential/identity string, only
#: where to find one.
ATTESTATION_CONFIG_KEY_IDENTITY_ENV = "identity_env"

#: Config-file key naming the sidecar file path (layer 2).
ATTESTATION_CONFIG_KEY_SIDECAR_PATH = "identity_sidecar_path"

#: Config-file key naming the ORDERED sidecar adapter list (layer 2, NEW
#: lr-8e1593): `attestation.sidecars: [{dir, file_prefix, session_id_env}]`.
#: See `_SidecarFileProvider._resolve_adapter` for the per-adapter
#: resolution rule, and this module's own docstring (layer 2, source (c))
#: for the full precedence rationale. No tool/harness name is ever
#: hardcoded here -- a deployment supplies its own `dir`/`file_prefix`/
#: `session_id_env` values; this module only walks the declared shape.
ATTESTATION_CONFIG_KEY_SIDECARS = "sidecars"

#: Per-adapter config keys within one entry of `attestation.sidecars`.
SIDECAR_ADAPTER_KEY_DIR = "dir"
SIDECAR_ADAPTER_KEY_FILE_PREFIX = "file_prefix"
SIDECAR_ADAPTER_KEY_SESSION_ID_ENV = "session_id_env"

#: OPTIONAL per-adapter config key declaring this adapter's scope for
#: BOUND resolution only (`resolve_bound_identity` -- the ordinary
#: `resolve_identity` chain never reads this key). A deployment supplies
#: `SIDECAR_SCOPE_PER_SPAWN` or `SIDECAR_SCOPE_SESSION`; an adapter with no
#: `scope` key, or an unrecognized value, is simply ineligible to answer a
#: bound resolution (see this module's own docstring, "ADAPTER-LESS AND
#: UNSCOPED-ADAPTER BEHAVIOR") -- it remains fully usable by the ordinary
#: chain. No harness-specific env var name is ever hardcoded here; a
#: deployment names its OWN `session_id_env` value per adapter, and this
#: key only says which of the two bound-resolution roles that adapter
#: plays.
SIDECAR_ADAPTER_KEY_SCOPE = "scope"

#: `attestation.sidecars[].scope` value for an adapter keyed on a per-spawn
#: (subagent) identifier -- see `SIDECAR_ADAPTER_KEY_SCOPE`.
SIDECAR_SCOPE_PER_SPAWN = "per-spawn"

#: `attestation.sidecars[].scope` value for an adapter keyed on a
#: top-level-session identifier -- see `SIDECAR_ADAPTER_KEY_SCOPE`.
SIDECAR_SCOPE_SESSION = "session"

#: Config-file key (within the `attestation` section) naming the BOUND-
#: RESOLUTION fallback policy: `BOUND_IDENTITY_POLICY_REQUIRED` or
#: `BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK`. See `resolve_bound_identity`'s
#: own docstring, "BOUND RESOLUTION," item 1, for the full behavior each
#: value selects.
ATTESTATION_CONFIG_KEY_BOUND_IDENTITY = "bound_identity"

#: Strict policy: a caller-bound resolution that finds nothing on layer 1
#: or the ONE discriminator-selected sidecar source is a terminal refusal
#: -- the built-in OS-user layer is never consulted. Omitted `--caller`
#: still requires attestation under this policy.
BOUND_IDENTITY_POLICY_REQUIRED = "required"

#: Compatibility policy: an UNDISCRIMINATED miss (no per-spawn-scoped
#: adapter's env var set, and no matching session-scoped adapter resolves
#: either -- i.e. neither branch of the discriminator produces an answer)
#: falls through to the built-in OS-user layer, preserving this package's
#: previously-released behavior for a deployment with no sidecar adapters
#: configured yet. The subagent-discriminator refusal itself (a per-spawn-
#: declared invocation whose per-spawn adapter misses) stays unconditional
#: even under this policy -- see `resolve_bound_identity`'s own docstring,
#: "THE SUBAGENT-DISCRIMINATOR REFUSAL IS UNCONDITIONAL, REGARDLESS OF
#: POLICY."
BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK = "builtin-fallback"

#: The DEFAULT bound-identity policy applied when a deployment's config
#: sets no explicit `attestation.bound_identity` value. A SINGLE constant
#: so the default can be flipped in one line once an operator confirms the
#: stricter default is safe to roll out; currently
#: `BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK` (non-breaking for an existing
#: install with no `attestation.sidecars` configured yet -- see this
#: package's release notes / the PR that introduced this constant for the
#: pending-confirmation status).
DEFAULT_BOUND_IDENTITY_POLICY = BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK

#: Every recognized `attestation.bound_identity` value. An ABSENT key falls
#: back to `DEFAULT_BOUND_IDENTITY_POLICY`; a PRESENT but unrecognized value
#: is a hard `AttestationConfigError` (lr-620837 fold-in #4, F1) -- see
#: `_resolve_bound_identity_policy`'s own docstring for why those two
#: shapes are treated differently.
_BOUND_IDENTITY_POLICIES = frozenset(
    {BOUND_IDENTITY_POLICY_REQUIRED, BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK}
)

#: Env var naming the env var that carries the configured-provider identity
#: value (layer 1) -- env-tier equivalent of ATTESTATION_CONFIG_KEY_IDENTITY_ENV,
#: takes precedence over the config-file key per this module's resolution
#: order (env wins over config-file, matching transport.provider_config's
#: own per-platform precedence rule).
ATTESTED_IDENTITY_ENV_VAR = "CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_ENV"

#: Env var naming the sidecar file path (layer 2) -- env-tier equivalent of
#: ATTESTATION_CONFIG_KEY_SIDECAR_PATH.
ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR = "CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH"

#: Source labels, mirroring the Go reference's Identity.Source values
#: exactly (configured / sidecar / builtin) so a deployment correlating logs
#: across both languages sees the same vocabulary.
SOURCE_CONFIGURED = "configured"
SOURCE_SIDECAR = "sidecar"
SOURCE_BUILTIN = "builtin"

#: Bound-resolution-only source labels (operator ruling, acceptance 3): more
#: specific than SOURCE_SIDECAR above, used ONLY by `resolve_bound_identity`
#: so a refusal message or a successful resolution names WHICH sidecar
#: source answered rather than the ambiguous generic 'sidecar' string that
#: cost the 2026-08-31 investigation its decisive evidence. The ordinary
#: `resolve_identity` chain (and `_SidecarFileProvider` it composes) is
#: unchanged and continues to report the generic SOURCE_SIDECAR for every
#: sidecar-shaped source -- these two labels exist alongside it, not instead
#: of it.
SOURCE_SIDECAR_SUBAGENT = "sidecar-subagent"
SOURCE_SIDECAR_SESSION = "sidecar-session"



class AttestationError(Exception):
    """Raised when NO layer in the resolution chain -- including the
    built-in OS-user fallback -- can resolve an identity. Distinct from a
    single layer declining (which falls through silently); this is a hard
    failure of the whole chain, expected only on a truly identity-less
    environment (no configured provider, no sidecar, and even
    getpass.getuser() cannot resolve anything)."""


class AttestationConfigError(AttestationError):
    """Raised when a deployment's `attestation:` config section itself is
    malformed in a way that must never be silently downgraded to a default
    (lr-620837 fold-in #4, F1/F3): an unrecognized
    `attestation.bound_identity` value, or an unrecognized
    `attestation.sidecars[].scope` value declared on an adapter, are both
    HARD config errors, raised immediately -- never a quiet fall-through to
    `DEFAULT_BOUND_IDENTITY_POLICY` or to "this adapter is simply unscoped."
    A typo'd policy or scope string is a deployment mistake that must be
    loud, not a shape this module tolerates by guessing the closest
    familiar behavior; see `_resolve_bound_identity_policy` and
    `_any_adapter_declares_recognized_scope`'s own docstrings for the two
    specific cases this covers. Subclasses `AttestationError` so it is still
    caught by every existing `except AttestationError` call site (all seven
    caller-bound verbs) without each of them needing a new except clause."""


class BoundAttestationError(AttestationError):
    """Raised by `resolve_bound_identity` (operator ruling, comment #5) when
    a caller-BOUND resolution finds nothing -- the built-in OS-user layer is
    never consulted on this path, so a miss here is always a terminal
    refusal, never a fall-through to a lower-trust layer. `expected_source`
    names which source was required for this invocation
    (SOURCE_SIDECAR_SUBAGENT or SOURCE_SIDECAR_SESSION), per acceptance 3 --
    the refusal names WHAT was expected, not just that resolution failed."""

    def __init__(self, message: str, *, expected_source: str) -> None:
        super().__init__(message)
        self.expected_source = expected_source


class Identity:
    """The attested invoking identity resolved by `resolve_identity`.

    `subject` is the attested identity value itself (an agent name, a
    service account, an OS username -- whatever the resolving layer
    produced; this module assigns no further meaning to it). `source`
    names which layer resolved it (SOURCE_CONFIGURED / SOURCE_SIDECAR /
    SOURCE_BUILTIN), for audit/debugging -- it is not itself part of any
    trust decision `bind_caller` makes.
    """

    __slots__ = ("subject", "source")

    def __init__(self, subject: str, source: str) -> None:
        self.subject = subject
        self.source = source

    def __repr__(self) -> str:  # pragma: no cover -- debug convenience only
        return f"Identity(subject={self.subject!r}, source={self.source!r})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Identity)
            and self.subject == other.subject
            and self.source == other.source
        )


@runtime_checkable
class IdentityProvider(Protocol):
    """One layer in the attestation-resolution chain.

    A provider that has no identity to offer for the current invocation
    returns None -- `resolve_identity` falls through to the next provider in
    order. A provider is never asked to raise for "I have nothing"; None is
    the well-formed empty answer. Implementations own their own resolution
    mechanism entirely.
    """

    def resolve(self) -> "Identity | None":
        """Return the attested identity, or None if this provider has
        nothing to offer for the current invocation."""
        ...


class _ConfiguredEnvProvider:
    """Layer 1: reads the identity value from an env var NAMED by
    ATTESTED_IDENTITY_ENV_VAR (env) or the config file's `identity_env` key.

    The configured NAME is itself resolved env-var-first, config-file-second
    (mirrors transport.provider_config's per-platform precedence). Returns
    None when no name is configured, or the named env var is unset/empty --
    both are "nothing to offer here," never a hard failure at this layer.
    """

    def __init__(self, *, env: dict[str, str], config_root) -> None:
        self._env = env
        self._config_root = config_root

    def resolve(self) -> "Identity | None":
        var_name = self._env.get(ATTESTED_IDENTITY_ENV_VAR)
        if not var_name:
            section = load_user_config_section(
                ATTESTATION_CONFIG_SECTION, config_root=self._config_root
            )
            var_name = section.get(ATTESTATION_CONFIG_KEY_IDENTITY_ENV)
        if not var_name:
            return None
        value = self._env.get(var_name)
        if not value or not value.strip():
            return None
        return Identity(subject=value.strip(), source=SOURCE_CONFIGURED)


def _is_safe_session_id(session_id: str) -> bool:
    """A session id is an opaque token read from the environment, never a
    path -- reject anything that could traverse out of a sidecar adapter's
    configured `dir` (lr-8e1593). REFUSE, do not sanitize: a value this
    function rejects causes the calling adapter to decline (skip to the
    next adapter / layer), never a best-effort rewrite of the value.
    Mirrors the Go reference's `isSafePathSegment`
    (`internal/attestation/sidecar.go`) -- non-empty, not `.`/`..`, no
    path separator in either OS form, and unchanged by `os.path.normpath`
    (catches anything else path-like our explicit checks missed)."""
    if session_id in ("", ".", ".."):
        return False
    if "/" in session_id or "\\" in session_id:
        return False
    if os.path.normpath(session_id) != session_id:
        return False
    return True


def _read_sidecar_identity_file(
    path_str: str, *, fail_closed_on_miss: bool = False
) -> "Identity | None":
    """Read one sidecar identity file at *path_str*, applying the SAME
    atomic O_NOFOLLOW+fstat read every sidecar source in this layer uses
    (lr-904b1d, extended unchanged to the adapter-list source by
    lr-8e1593) -- see `_SidecarFileProvider`'s own docstring for the full
    TOCTOU/symlink-hardening rationale this function implements once, for
    every caller in this layer.

    Returns None for a genuinely absent or empty-content file (a plain
    decline) -- UNLESS *fail_closed_on_miss* is True, in which case that
    same absent/empty/unreadable-as-empty condition raises
    `AttestationError` instead (lr-1e16a4: see the module docstring's
    "explicit source-(a) requested-but-absent" note below). Raises
    `AttestationError` unconditionally for a symlink or other non-regular
    directory entry at *path_str* (a hard failure, never silently demoted
    to "unconfigured" -- see the module docstring, layer 2), regardless of
    *fail_closed_on_miss*.

    *fail_closed_on_miss* exists because "absent" means something
    different depending on WHICH source resolved *path_str*: sources (b)
    (config single-path) and (c) (adapter list) are optional conveniences
    a deployment MAY configure, so an absent file there is an ordinary
    decline -- there was no explicit per-invocation claim to honor. Source
    (a) (the env-var override) is different: a caller that sets
    `CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH` to a specific path
    is making an explicit, per-spawn claim about WHERE its identity lives.
    If that exact file is missing, falling through to a lower-precedence
    source can resolve a DIFFERENT identity than the one the caller
    pointed at (e.g. a parent session's identity via the source-(c)
    adapter list) -- a privilege-substitution shape for any caller-bound
    mint (`transport.git_host_api.bind_caller`). Mirrors
    clagentic-gatekeeper's `internal/attestation` DomainA2A fail-closed-
    on-MISS fix (lr-2ca216): an explicitly-requested source that misses
    declines the WHOLE chain, not just this one layer.
    """
    try:
        fd = os.open(path_str, os.O_NOFOLLOW | os.O_RDONLY)
    except FileNotFoundError:
        if fail_closed_on_miss:
            raise AttestationError(
                f"attestation FAILED -- "
                f"{ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR} explicitly names "
                f"sidecar identity path {path_str!r}, but that file does "
                f"not exist. This is an explicit per-invocation identity "
                f"claim, not an optional convenience -- a MISS here "
                f"refuses the whole attestation chain rather than falling "
                f"through to a lower-precedence source (config sidecar "
                f"path, session-keyed adapter list, or the built-in "
                f"OS-user fallback), any of which could resolve a "
                f"DIFFERENT identity than the one this env var pointed "
                f"at."
            ) from None
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            # Final path component is a symlink -- O_NOFOLLOW refused
            # the open outright. Same hard-failure treatment as any
            # other non-regular directory entry below: never silently
            # demoted to "unconfigured."
            raise AttestationError(
                f"attestation FAILED -- configured sidecar identity path "
                f"{path_str!r} is a symlink, refused by O_NOFOLLOW. A "
                f"symlink or other non-regular directory entry at this "
                f"path is refused unconditionally -- a planted symlink "
                f"in a world-writable directory must never be able to "
                f"redirect this read to an arbitrary file."
            ) from exc
        raise AttestationError(
            f"attestation FAILED -- could not open the configured "
            f"sidecar identity path {path_str!r}: {exc}."
        ) from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            # Directory, device, socket, FIFO, etc. opened successfully
            # (O_NOFOLLOW only refuses a symlink component) but is not a
            # regular file -- refuse the same way a symlink would be.
            raise AttestationError(
                f"attestation FAILED -- configured sidecar identity "
                f"path {path_str!r} is not a regular file (mode "
                f"{oct(info.st_mode)!r}). A symlink or other "
                f"non-regular directory entry at this path is refused "
                f"unconditionally -- a planted symlink in a "
                f"world-writable directory must never be able to "
                f"redirect this read to an arbitrary file."
            )
        try:
            raw = os.read(fd, info.st_size).decode("utf-8")
        except OSError as exc:
            raise AttestationError(
                f"attestation FAILED -- could not read the configured "
                f"sidecar identity path {path_str!r}: {exc}."
            ) from exc
    finally:
        os.close(fd)

    first_line = raw.splitlines()[0].strip() if raw.strip() else ""
    if not first_line:
        if fail_closed_on_miss:
            raise AttestationError(
                f"attestation FAILED -- "
                f"{ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR} explicitly names "
                f"sidecar identity path {path_str!r}, but that file is "
                f"empty. Same fail-closed treatment as a MISSING file for "
                f"this explicitly-requested source -- see the "
                f"FileNotFoundError branch above for the full rationale."
            )
        return None
    return Identity(subject=first_line, source=SOURCE_SIDECAR)


def _resolve_sidecar_adapter(
    adapter: dict, *, env: dict[str, str], source: str = SOURCE_SIDECAR
) -> "Identity | None":
    """Resolve ONE `attestation.sidecars` list entry, or None when this
    adapter has nothing to offer for the current invocation (missing config
    keys, unset/empty session-id env var, unsafe session-id value, or a
    genuinely absent composed file -- all plain declines, never a hard
    failure; see the module docstring's layer 2, source (c) for the full
    rule). Mirrors clagentic-gatekeeper's Go reference
    `sidecarProvider.Resolve` (`internal/attestation/sidecar.go`, epic
    lr-0029bf): same three required fields, same
    session-id-must-be-a-safe-single-path-component refusal, same
    skip-not-error semantics for anything short of a resolved file.

    Module-level (not a method) so BOTH `_SidecarFileProvider._resolve_adapter`
    (the ordinary chain, walked in declared order, always reports
    SOURCE_SIDECAR) and `resolve_bound_identity` (the caller-bound path,
    which walks only the ONE adapter whose `session_id_env` matches its
    discriminator and reports a more specific source label) share the exact
    same per-adapter safety logic -- the required-field check, the
    session-id safety refusal, and the symlink-hardened read -- rather than
    two independent reimplementations drifting apart over time.

    *source* lets a bound-resolution caller override the `Identity.source`
    label a successful resolve returns (SOURCE_SIDECAR_SUBAGENT /
    SOURCE_SIDECAR_SESSION instead of the generic SOURCE_SIDECAR) --
    acceptance 3 of the operator ruling this function was extracted for.
    """
    directory = adapter.get(SIDECAR_ADAPTER_KEY_DIR)
    file_prefix = adapter.get(SIDECAR_ADAPTER_KEY_FILE_PREFIX)
    session_id_env = adapter.get(SIDECAR_ADAPTER_KEY_SESSION_ID_ENV)
    if not directory or not file_prefix or not session_id_env:
        # Partially configured adapter entry -- treated as disabled,
        # never guessed at (mirrors the Go reference's `enabled()`).
        return None

    session_id = (env.get(session_id_env) or "").strip()
    if not session_id:
        # This adapter's harness is not active in this invocation's
        # environment -- decline, do not error.
        return None

    if not _is_safe_session_id(session_id):
        # A session id is an opaque token from the environment, never a
        # path -- refuse anything that could redirect the composed read
        # (separators, "..") rather than sanitizing it. Skip to the
        # next adapter; an attacker-controlled env var must not be able
        # to demote this to "no adapters configured" either, so this
        # is a decline of THIS adapter only, not the whole layer.
        return None

    path_str = os.path.join(str(directory), f"{file_prefix}{session_id}")
    identity = _read_sidecar_identity_file(path_str)
    if identity is not None and source != SOURCE_SIDECAR:
        identity = Identity(subject=identity.subject, source=source)
    return identity


class _SidecarFileProvider:
    """Layer 2: reads the identity value from a file NAMED by
    ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR (env), the config file's
    `identity_sidecar_path` key, or (lr-8e1593) the first hit in the
    config file's ordered `attestation.sidecars` adapter list.

    Reads the file's entire text content, stripped, first line only -- no
    assumption about the writing harness's own format beyond "one identity
    value, optionally followed by a trailing newline." Returns None when no
    path is configured, or a source (b)/(c) configured path is genuinely
    absent -- never a hard failure at this layer for a plain missing file
    on those two sources (an external harness that has not written its
    sidecar yet, or was never configured to, is not this module's problem
    to fail loudly over; the chain falls through to the built-in fallback
    instead). Source (a) -- the env-var single-path override -- is the ONE
    exception (lr-1e16a4): it is an EXPLICIT per-invocation claim, so a
    miss there fails the whole chain closed instead of falling through;
    see `resolve()`'s own comment at that call site and this module's
    top-level docstring (layer 2, source (a)) for the full rationale.

    ATOMIC O_NOFOLLOW READ (lr-904b1d, closing the residual TOCTOU left by
    an earlier lstat-then-read_text sequence -- pre-merge security-review
    finding on the prior symlink-refusal patch): the configured path is
    opened with `os.O_NOFOLLOW | os.O_RDONLY`
    and every subsequent check (regular-file test, content read) operates on
    the returned file descriptor via `os.fstat`/`os.read`, never re-touching
    the path string. A separate `lstat()` call followed by a *second*,
    independent `read_text()` open leaves a window between the two syscalls
    in which the path entry could be replaced (e.g. a regular file swapped
    for a symlink between the check and the read) -- a classic
    check-then-use race, distinct from (and in addition to) the plain
    symlink case. Collapsing the check and the read onto the SAME open file
    descriptor removes that window entirely: whatever `fstat(fd)` reports is
    guaranteed to describe the exact bytes `os.read(fd, ...)` subsequently
    returns, because both operate on the same already-resolved fd rather
    than re-resolving the path.

    `O_NOFOLLOW` makes `open()` itself refuse a symlink in the final path
    component (raising `OSError` with `errno.ELOOP`), which this method
    maps to the SAME `AttestationError` a non-regular directory entry
    (directory, device, socket, FIFO) gets from the `fstat` check below --
    preserving the exact symlink-hardening property the previous
    lstat-based implementation provided (see clagentic-gatekeeper's
    `internal/attestation/sidecar.go` reference and the still-relevant
    rationale below), just via a single atomic syscall pair instead of two
    independent ones.

    A planted symlink at the configured path -- trivial in a world-writable
    directory such as `/tmp`, which is exactly where a deployment's sidecar
    convention commonly lives -- must never be able to redirect this read
    to an arbitrary file elsewhere on disk; the resolved `subject` feeds
    `bind_caller`'s authorization decision directly, so a redirected read is
    a privilege-escalation primitive, not a cosmetic bug. Unlike a genuinely
    absent file (declines, falls through), a symlink or any other
    non-regular directory entry at the configured path is a HARD FAILURE:
    this layer raises `AttestationError` rather than silently declining, so
    a planted symlink can never be quietly bypassed by falling through to a
    lower-trust layer -- an attacker who can plant a symlink at this path
    should not be able to also demote the check to "as if the sidecar were
    merely unconfigured."
    """

    def __init__(self, *, env: dict[str, str], config_root) -> None:
        self._env = env
        self._config_root = config_root

    def resolve(self) -> "Identity | None":
        # (a) env single-path override -- HIGHEST precedence within this
        # layer, unchanged (preserves per-command subagent env stamping).
        # lr-1e16a4: this source is an EXPLICIT per-invocation claim, so a
        # miss here (absent/empty/unreadable-as-empty file) fails the
        # WHOLE chain closed rather than falling through to (b)/(c)/layer
        # 3 -- see `_read_sidecar_identity_file`'s `fail_closed_on_miss`
        # docstring for the full rationale. A symlink/non-regular entry at
        # this path is ALREADY a hard failure regardless (unchanged).
        path_str = self._env.get(ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR)
        if path_str:
            return _read_sidecar_identity_file(path_str, fail_closed_on_miss=True)

        section = load_user_config_section(
            ATTESTATION_CONFIG_SECTION, config_root=self._config_root
        )

        # (b) config single-path override -- retained, unchanged semantics.
        path_str = section.get(ATTESTATION_CONFIG_KEY_SIDECAR_PATH)
        if path_str:
            return _read_sidecar_identity_file(path_str)

        # (c) NEW (lr-8e1593): ordered adapter list. Walked in declared
        # order; the first adapter whose session id resolves AND whose
        # composed file exists wins. An adapter that has nothing to offer
        # is skipped (not an error) -- see module docstring, layer 2,
        # source (c).
        adapters = section.get(ATTESTATION_CONFIG_KEY_SIDECARS)
        if isinstance(adapters, list):
            for adapter in adapters:
                if not isinstance(adapter, dict):
                    continue
                identity = self._resolve_adapter(adapter)
                if identity is not None:
                    return identity

        return None

    def _resolve_adapter(self, adapter: dict) -> "Identity | None":
        """Resolve ONE `attestation.sidecars` list entry -- thin instance
        wrapper around the module-level `_resolve_sidecar_adapter` (lifted
        out by the bound-resolution work, `resolve_bound_identity`, so the
        SAME per-adapter logic -- required-field check, session-id safety
        refusal, symlink-hardened read -- is not reimplemented for a
        session_id_env-scoped walk). See `_resolve_sidecar_adapter`'s own
        docstring for the full per-adapter resolution rule."""
        return _resolve_sidecar_adapter(adapter, env=self._env)


class _BuiltinOsUserProvider:
    """Layer 3: the OS-reported invoking user (`getpass.getuser()`), which
    itself falls through `LOGNAME`/`USER`/`LNAME`/`USERNAME` and finally the
    passwd database on POSIX. Always available in practice -- this is the
    "a bare install still has an attested source" guarantee -- so this is
    the ONE layer whose failure is NOT swallowed as "nothing to offer";
    `resolve_identity` treats a `getpass.getuser()` exception here as the
    whole chain's terminal failure (AttestationError), since there is
    nothing left to fall through to.
    """

    def resolve(self) -> "Identity | None":
        subject = getpass.getuser()
        if not subject:
            return None
        return Identity(subject=subject, source=SOURCE_BUILTIN)


def _default_chain(*, env: dict[str, str], config_root) -> list[IdentityProvider]:
    return [
        _ConfiguredEnvProvider(env=env, config_root=config_root),
        _SidecarFileProvider(env=env, config_root=config_root),
        _BuiltinOsUserProvider(),
    ]


def resolve_identity(
    *,
    env: dict[str, str] | None = None,
    config_root: str | Path | None = None,
    providers: list[IdentityProvider] | None = None,
) -> Identity:
    """Walk the attestation-resolution chain in FIXED order (configured
    provider -> sidecar adapter -> built-in OS-user fallback) and return the
    first identity found.

    Args:
        env: override the environment mapping (mainly for tests). Defaults
            to os.environ.
        config_root: override the user-level config root the configured/
            sidecar tiers' config-file lookups read from (mainly for
            tests). Defaults to
            transport.provider_config.DEFAULT_USER_CONFIG_ROOT.
        providers: override the ENTIRE provider chain (mainly for tests --
            e.g. injecting a fake configured-provider identity without
            touching real env/config). When supplied, `env`/`config_root`
            are ignored for chain CONSTRUCTION (the injected providers are
            used exactly as given).

    Raises:
        AttestationError: every provider in the chain -- including the
            built-in fallback -- returned None or raised. Expected only on a
            truly identity-less environment; a production process almost
            always resolves at least the built-in layer.
    """
    active_env = env if env is not None else dict(os.environ)
    resolved_config_root = config_root if config_root is not None else DEFAULT_USER_CONFIG_ROOT
    chain = (
        providers
        if providers is not None
        else _default_chain(env=active_env, config_root=resolved_config_root)
    )
    for provider in chain:
        identity = provider.resolve()
        if identity is not None:
            return identity
    raise AttestationError(
        "attestation FAILED -- no layer in the resolution chain (configured "
        "provider, sidecar adapter, built-in OS-user fallback) could resolve "
        "an attested invoking identity. This should not happen in practice "
        "(the built-in fallback resolves the OS-reported invoking user "
        "whenever one is available) -- check that this process has a "
        "resolvable OS user, or configure "
        f"{ATTESTED_IDENTITY_ENV_VAR} / the "
        f"{ATTESTATION_CONFIG_SECTION!r}.{ATTESTATION_CONFIG_KEY_IDENTITY_ENV!r} "
        "config key to point at a real identity source."
    )


def _configured_sidecar_adapters(*, config_root) -> list[dict]:
    """Return the configured `attestation.sidecars` list verbatim (every
    dict entry, regardless of `scope`) -- config-shape problems (no
    `attestation:` section, no `sidecars` key, a non-list value) all
    resolve to a plain empty list, never an error here."""
    section = load_user_config_section(ATTESTATION_CONFIG_SECTION, config_root=config_root)
    adapters = section.get(ATTESTATION_CONFIG_KEY_SIDECARS)
    if not isinstance(adapters, list):
        return []
    return [adapter for adapter in adapters if isinstance(adapter, dict)]


def _adapters_with_scope(adapters: list[dict], scope: str) -> list[dict]:
    """Filter *adapters* to those declaring `scope: <scope>` exactly (see
    `SIDECAR_ADAPTER_KEY_SCOPE`) -- an adapter with no `scope` key, or a
    value other than *scope*, is excluded."""
    return [adapter for adapter in adapters if adapter.get(SIDECAR_ADAPTER_KEY_SCOPE) == scope]


def _any_per_spawn_session_id_set(adapters: list[dict], *, env: dict[str, str]) -> bool:
    """The discriminator itself: True when at least one `scope: per-spawn`
    adapter's OWN `session_id_env` is set (non-empty) in *env*. Config
    supplies the env-var NAME per adapter; this module never hardcodes
    one."""
    for adapter in adapters:
        session_id_env = adapter.get(SIDECAR_ADAPTER_KEY_SESSION_ID_ENV)
        if not session_id_env:
            continue
        if (env.get(session_id_env) or "").strip():
            return True
    return False


def _any_adapter_declares_recognized_scope(adapters: list[dict]) -> bool:
    """True when at least one entry in *adapters* declares
    `scope: per-spawn` or `scope: session` (`SIDECAR_ADAPTER_KEY_SCOPE`).
    Drives the upgrade-safety branch in `resolve_bound_identity` (lr-620837
    fold-in #3): a config where NO adapter declares a recognized scope at
    all is a legacy/unscoped config, and bound resolution must fall back to
    the SAME sidecar codepath the ordinary `resolve_identity` chain uses
    rather than the scoped discriminator, which has nothing to discriminate
    on.

    HARD CONFIG ERROR (lr-620837 fold-in #4, F3): an
    adapter that DOES declare a `scope` key, but sets it to a value other
    than `SIDECAR_SCOPE_PER_SPAWN`/`SIDECAR_SCOPE_SESSION`, is a deployment
    typo (e.g. `scope: per_spawn` with an underscore, or `scope: Session`
    mis-cased) -- NOT the same shape as an adapter that omits `scope`
    entirely (a genuinely legacy/pre-scope config entry, still a fully
    legitimate declaration). Silently treating a misspelled scope value as
    "unscoped, ignore it for bound resolution" would leave that adapter
    invisible to the discriminator with no signal to the operator that the
    value they typed was never recognized -- exactly the fail-open-by-typo
    shape this whole function exists to prevent elsewhere. Raises
    `AttestationConfigError` immediately, naming the adapter's position in
    the list (1-indexed, matching how an operator would count entries in
    their own YAML file), the offending value, and the two allowed values."""
    for index, adapter in enumerate(adapters):
        if SIDECAR_ADAPTER_KEY_SCOPE not in adapter:
            continue
        scope_value = adapter.get(SIDECAR_ADAPTER_KEY_SCOPE)
        if scope_value not in (SIDECAR_SCOPE_PER_SPAWN, SIDECAR_SCOPE_SESSION):
            raise AttestationConfigError(
                f"attestation config FAILED -- "
                f"{ATTESTATION_CONFIG_SECTION!r}.{ATTESTATION_CONFIG_KEY_SIDECARS!r}"
                f"[{index + 1}].{SIDECAR_ADAPTER_KEY_SCOPE!r} is set to "
                f"{scope_value!r}, which is not a recognized scope value. "
                f"Allowed values are {SIDECAR_SCOPE_PER_SPAWN!r} or "
                f"{SIDECAR_SCOPE_SESSION!r} -- remove the "
                f"{SIDECAR_ADAPTER_KEY_SCOPE!r} key entirely if this adapter "
                f"is intentionally unscoped (legacy behavior), or correct "
                f"the value to one of the two recognized scopes."
            )
    return any(
        adapter.get(SIDECAR_ADAPTER_KEY_SCOPE) in (SIDECAR_SCOPE_PER_SPAWN, SIDECAR_SCOPE_SESSION)
        for adapter in adapters
    )


def _resolve_bound_identity_policy(*, config_root) -> str:
    """Read `attestation.bound_identity` from config.

    HARD CONFIG ERROR (lr-620837 fold-in #4, F1): an
    unrecognized, non-empty `attestation.bound_identity` value (a typo'd
    policy string, e.g. `"requried"` or `"Required"`) now raises
    `AttestationConfigError` immediately, naming the config key, the
    offending value, and the two allowed values -- it is NEVER silently
    treated as "unset" and downgraded to `DEFAULT_BOUND_IDENTITY_POLICY`.
    A deployment that typo's this key was almost certainly trying to opt
    INTO the stricter `"required"` policy; silently falling open to
    `"builtin-fallback"` instead is exactly the confused-deputy shape this
    whole policy knob exists to close, and a typo must never be the reason
    a deployment ends up on the more permissive policy without ever
    knowing it did. Only a genuinely ABSENT/unset key (the key is not
    present in the `attestation:` section at all) falls back to
    `DEFAULT_BOUND_IDENTITY_POLICY` -- that is a deployment that has not
    yet opted into the policy knob at all, a different and legitimate
    shape from "opted in, but misspelled the value"."""
    section = load_user_config_section(ATTESTATION_CONFIG_SECTION, config_root=config_root)
    configured = section.get(ATTESTATION_CONFIG_KEY_BOUND_IDENTITY)
    if configured is None:
        return DEFAULT_BOUND_IDENTITY_POLICY
    if configured in _BOUND_IDENTITY_POLICIES:
        return configured
    raise AttestationConfigError(
        f"attestation config FAILED -- "
        f"{ATTESTATION_CONFIG_SECTION!r}.{ATTESTATION_CONFIG_KEY_BOUND_IDENTITY!r} "
        f"is set to {configured!r}, which is not a recognized policy value. "
        f"Allowed values are {BOUND_IDENTITY_POLICY_REQUIRED!r} or "
        f"{BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK!r} -- remove the key "
        f"entirely to use the default "
        f"({DEFAULT_BOUND_IDENTITY_POLICY!r}), or correct the value to one "
        f"of the two recognized policies."
    )


#: Public alias (lr-620837 fold-in #4): `transport.caller_binding.
#: resolve_for_binding` needs to read the effective `bound_identity` policy
#: to decide whether an omitted --caller/--role requires attestation
#: (`"required"`) or resolves to `DEFAULT_ROLE` with no resolution attempt
#: at all (`"builtin-fallback"`, matching this package's originally-
#: released omitted-caller behavior) -- see that function's own docstring.
#: This is the SAME function `resolve_bound_identity` itself calls
#: internally (`_resolve_bound_identity_policy`); the public name exists so
#: a module outside this one has a stable, non-underscore-prefixed entry
#: point rather than reaching into a private helper.
def resolve_bound_identity_policy(*, config_root: str | Path | None = None) -> str:
    """Return the effective `attestation.bound_identity` policy
    (`BOUND_IDENTITY_POLICY_REQUIRED` or
    `BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK`) for *config_root* (defaults to
    `DEFAULT_USER_CONFIG_ROOT`, the same resolution `resolve_bound_identity`
    itself uses). Raises `AttestationConfigError` for an unrecognized
    non-empty value -- see `_resolve_bound_identity_policy`'s own
    docstring, which this delegates to unchanged."""
    resolved_config_root = config_root if config_root is not None else DEFAULT_USER_CONFIG_ROOT
    return _resolve_bound_identity_policy(config_root=resolved_config_root)


def resolve_bound_identity(
    *,
    env: dict[str, str] | None = None,
    config_root: str | Path | None = None,
) -> Identity:
    """Resolve the attested identity for a caller-BOUND verb -- every
    `bind_caller` call site uses this INSTEAD of `resolve_identity`. See
    this module's own docstring, "BOUND RESOLUTION," for the full
    rationale; summarized here:

      1. Layer 1 (the configured-provider env var) still takes precedence,
         unchanged -- a real, deployment-declared identity, not the
         built-in fallback this function narrows.
      2. Otherwise, exactly ONE sidecar scope may answer, chosen by the
         discriminator: whether any `scope: per-spawn` adapter's own
         `session_id_env` is set in *env*. Set -> only `scope: per-spawn`
         adapters may resolve. Unset (including "no per-spawn adapter
         configured at all") -> only `scope: session` adapters may
         resolve. This is a HARD requirement, enforced by CODE against the
         declared `scope` value, never by adapter list ORDER in a
         deployment's config file.
      3. An undiscriminated miss (the selected scope's adapters all
         decline, or none are configured) is a terminal
         `BoundAttestationError` under `attestation.bound_identity:
         required`, or falls through to the built-in OS-user layer under
         `attestation.bound_identity: builtin-fallback` (see
         `DEFAULT_BOUND_IDENTITY_POLICY` for which applies when
         unconfigured). The per-spawn discriminator refusal itself is
         UNCONDITIONAL regardless of policy -- see this module's own
         docstring.
      4. UPGRADE SAFETY (lr-620837 fold-in #3): step 2's discriminator only
         applies once at least one configured adapter declares a
         recognized `scope`. When NO adapter in `attestation.sidecars`
         declares one (a legacy/unscoped config -- see this module's own
         docstring, "UNSCOPED-CONFIG UPGRADE SAFETY"), this function
         instead delegates to the SAME sidecar codepath `resolve_identity`
         uses (`_SidecarFileProvider`, all three of its sources) and
         reports the generic `SOURCE_SIDECAR` label -- byte-identical to
         what `resolve_identity` itself would resolve for the same
         env/config, so an existing config with no `scope`-tagged adapters
         sees NO behavior change from adopting this function. Step 3's
         policy fallback still applies to a miss on that unscoped lookup.

    Args:
        env: override the environment mapping (mainly for tests). Defaults
            to os.environ.
        config_root: override the user-level config root (mainly for
            tests). Defaults to transport.provider_config.
            DEFAULT_USER_CONFIG_ROOT (via this module's own bound copy of
            that name, same as `resolve_identity`).

    Raises:
        BoundAttestationError: layer 1 declined, the ONE sidecar scope
            this invocation's discriminator selected did not resolve, AND
            the effective policy is `"required"` (or the effective policy
            is `"builtin-fallback"` but this was a per-spawn-declared miss
            -- see above). `.expected_source` names which source was
            required (SOURCE_SIDECAR_SUBAGENT or SOURCE_SIDECAR_SESSION).
    """
    active_env = env if env is not None else dict(os.environ)
    resolved_config_root = config_root if config_root is not None else DEFAULT_USER_CONFIG_ROOT

    # Layer 1 -- unchanged precedence, a real attested identity, not the
    # built-in fallback this function narrows.
    configured_identity = _ConfiguredEnvProvider(
        env=active_env, config_root=resolved_config_root
    ).resolve()
    if configured_identity is not None:
        return configured_identity

    all_sidecar_adapters = _configured_sidecar_adapters(config_root=resolved_config_root)

    # UPGRADE SAFETY (lr-620837 fold-in #3): a legacy/unscoped config --
    # NO adapter in the list declares a recognized `scope` at all -- has
    # nothing for the discriminator below to discriminate on. Delegate to
    # the exact same sidecar codepath the ordinary `resolve_identity`
    # chain uses (all three sources: env single-path, config single-path,
    # and the adapter list walked in declared order with no `scope`
    # check) so an existing config sees byte-identical resolution to what
    # it got before this function's scoped-discriminator behavior existed
    # -- see this module's own docstring, "UNSCOPED-CONFIG UPGRADE
    # SAFETY," for the full defect this closes.
    if not _any_adapter_declares_recognized_scope(all_sidecar_adapters):
        unscoped_identity = _SidecarFileProvider(
            env=active_env, config_root=resolved_config_root
        ).resolve()
        if unscoped_identity is not None:
            return unscoped_identity

        policy = _resolve_bound_identity_policy(config_root=resolved_config_root)
        if policy == BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK:
            builtin_identity = _BuiltinOsUserProvider().resolve()
            if builtin_identity is not None:
                return builtin_identity

        raise BoundAttestationError(
            f"attestation FAILED -- no attested identity. This is a "
            f"caller-bound resolution against an UNSCOPED "
            f"{ATTESTATION_CONFIG_SECTION!r}.{ATTESTATION_CONFIG_KEY_SIDECARS!r} "
            f"configuration -- no adapter in that list declares a "
            f"recognized `{SIDECAR_ADAPTER_KEY_SCOPE}` key "
            f"({SIDECAR_SCOPE_PER_SPAWN!r} or {SIDECAR_SCOPE_SESSION!r}), so "
            f"the per-spawn/session discriminator has nothing to select "
            f"between. Configure a resolving sidecar source (env "
            f"{ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR}, config "
            f"{ATTESTATION_CONFIG_KEY_SIDECAR_PATH!r}, or an "
            f"{ATTESTATION_CONFIG_KEY_SIDECARS!r} adapter), or add "
            f"`{SIDECAR_ADAPTER_KEY_SCOPE}: {SIDECAR_SCOPE_PER_SPAWN}` / "
            f"`{SIDECAR_ADAPTER_KEY_SCOPE}: {SIDECAR_SCOPE_SESSION}` to opt "
            f"into the stricter per-spawn/session discriminator, before "
            f"retrying.",
            expected_source=SOURCE_SIDECAR,
        )

    per_spawn_adapters = _adapters_with_scope(all_sidecar_adapters, SIDECAR_SCOPE_PER_SPAWN)
    session_adapters = _adapters_with_scope(all_sidecar_adapters, SIDECAR_SCOPE_SESSION)

    # Discriminator: does ANY scope:per-spawn adapter's own session_id_env
    # resolve to a non-empty value in this process's environment? Set ->
    # per-spawn subagent by this deployment's own declaration. Unset
    # (including "no per-spawn adapter declared at all") -> top-level
    # session. Never both, never "whichever resolves first."
    is_per_spawn = _any_per_spawn_session_id_set(per_spawn_adapters, env=active_env)

    if is_per_spawn:
        candidate_adapters = per_spawn_adapters
        bound_source = SOURCE_SIDECAR_SUBAGENT
        expected_description = (
            f"the per-spawn subagent sidecar (an `attestation.sidecars` "
            f"adapter configured with `{SIDECAR_ADAPTER_KEY_SCOPE}: "
            f"{SIDECAR_SCOPE_PER_SPAWN}`) -- this invocation declared itself "
            f"a per-spawn subagent (a configured per-spawn adapter's own "
            f"session_id_env is set), so ONLY that adapter may answer; it "
            f"never falls through to a session-scoped adapter (that "
            f"fallthrough is the parent-identity confused-deputy shape this "
            f"refusal exists to prevent)"
        )
    else:
        candidate_adapters = session_adapters
        bound_source = SOURCE_SIDECAR_SESSION
        expected_description = (
            f"the session-scoped sidecar (an `attestation.sidecars` adapter "
            f"configured with `{SIDECAR_ADAPTER_KEY_SCOPE}: "
            f"{SIDECAR_SCOPE_SESSION}`) -- no configured per-spawn adapter's "
            f"session_id_env is set, so this invocation is a top-level "
            f"session and ONLY that adapter may answer"
        )

    for adapter in candidate_adapters:
        identity = _resolve_sidecar_adapter(adapter, env=active_env, source=bound_source)
        if identity is not None:
            return identity

    # UNCONDITIONAL: a per-spawn-declared invocation whose per-spawn
    # adapter misses is refused regardless of policy -- see this module's
    # own docstring, "THE SUBAGENT-DISCRIMINATOR REFUSAL IS UNCONDITIONAL."
    policy = _resolve_bound_identity_policy(config_root=resolved_config_root)
    if policy == BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK and not is_per_spawn:
        builtin_identity = _BuiltinOsUserProvider().resolve()
        if builtin_identity is not None:
            return builtin_identity

    raise BoundAttestationError(
        f"attestation FAILED -- no attested identity. This is a caller-bound "
        f"resolution: the expected source for this invocation is "
        f"{expected_description}. Configure that adapter in this "
        f"deployment's {ATTESTATION_CONFIG_SECTION!r}.{ATTESTATION_CONFIG_KEY_SIDECARS!r} "
        f"list, or ensure its harness has written the expected sidecar "
        f"file, before retrying.",
        expected_source=bound_source,
    )


__all__ = [
    "ATTESTATION_CONFIG_KEY_BOUND_IDENTITY",
    "ATTESTATION_CONFIG_KEY_IDENTITY_ENV",
    "ATTESTATION_CONFIG_KEY_SIDECAR_PATH",
    "ATTESTATION_CONFIG_KEY_SIDECARS",
    "ATTESTATION_CONFIG_SECTION",
    "ATTESTED_IDENTITY_ENV_VAR",
    "ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR",
    "BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK",
    "BOUND_IDENTITY_POLICY_REQUIRED",
    "DEFAULT_BOUND_IDENTITY_POLICY",
    "SIDECAR_ADAPTER_KEY_DIR",
    "SIDECAR_ADAPTER_KEY_FILE_PREFIX",
    "SIDECAR_ADAPTER_KEY_SCOPE",
    "SIDECAR_ADAPTER_KEY_SESSION_ID_ENV",
    "SIDECAR_SCOPE_PER_SPAWN",
    "SIDECAR_SCOPE_SESSION",
    "SOURCE_BUILTIN",
    "SOURCE_CONFIGURED",
    "SOURCE_SIDECAR",
    "SOURCE_SIDECAR_SESSION",
    "SOURCE_SIDECAR_SUBAGENT",
    "AttestationConfigError",
    "AttestationError",
    "BoundAttestationError",
    "Identity",
    "IdentityProvider",
    "resolve_bound_identity",
    "resolve_bound_identity_policy",
    "resolve_identity",
]
