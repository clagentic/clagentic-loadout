"""transport.caller_binding — the shared layer (1)->(2) fail-closed binding
every mutating verb that accepts --caller/--role now calls before it reaches
a credential mint (lr-c75c9a, P1 security fix; OMITTED-CALLER ATTESTATION
FIX below, operator ruling on lr-620837 comment #5).

BACKGROUND: lr-82c385 (tome #700) introduced this binding -- "does the
--caller/--role value on this invocation's argv actually match the identity
this deployment's own attestation source vouches for" -- but shipped it
wired into exactly ONE call site, transport.git_host_api.bind_caller. Every
OTHER mutating verb (push, merge, close-pr, post-merge, review, acquire)
took --caller/--role straight from argv to transport.credential_provider.
resolve_token / merge.authority.check_authority with nothing in between: an
unattested process could act as any agent identity by typing its name on the
command line. lr-c75c9a is the fix -- ONE binding implementation, called
from every verb that mints a credential or checks merge authority against a
--caller/--role value, not a second reimplementation per verb.

WHERE THIS LIVES (lr-c75c9a judgment call 1): bind_caller previously lived
in transport.git_host_api, a module whose own docstring is about the
Forgejo REST-call verb, not about identity binding in general -- every other
verb importing a binding function FROM the git-host-API verb module would
have been a backwards, cross-concern import (the tail wagging the dog: six
verbs depending on the module that happens to have been first). This module
is the correct home: a single-purpose, transport-layer seam alongside
transport.attestation (which resolves WHAT the identity is) and
transport.credential_provider (which resolves a token FOR a role) -- this
module is the third leg, deciding whether the two are allowed to be the
same value. transport.git_host_api re-exports `bind_caller` from here
unchanged (see that module's own import) so its own call site and every
existing test importing `git_host_api.bind_caller` keeps working with no
signature or behavior change.

THE BINDING ITSELF is unchanged from lr-82c385 in every respect that
matters: `identity` is whatever the call site's identity resolver (or an
injected equivalent) resolved for THIS process. Every caller-bound verb's
default resolver is now `transport.attestation.resolve_bound_identity` --
the configured provider, or exactly ONE discriminator-selected sidecar
source; whether an undiscriminated miss on those may still fall through to
the built-in OS-user layer is governed by that function's own
`attestation.bound_identity` config policy (`"required"` never falls
through; `"builtin-fallback"`, the current default, does) -- see that
function's own docstring for the full rule this module's REQUIREMENT 5
below predates and no longer describes the default path (kept here for its
historical rationale, since a deployment MAY still inject the general
`resolve_identity` chain via `identity_provider=`).

FAIL-CLOSED, BEFORE ANY I/O: `caller != identity.subject` on an EXPLICIT
--caller/--role raises CallerBindingError -- no token mint is ever
attempted, no request is ever issued, no merge-authority check ever runs.
There is no override, no allowlist that admits a mismatch: even a role an
operator-configured named-agent allowlist would otherwise grant is refused
here if it does not match this process's own attested identity, because
this check runs BEFORE (and independently of) whatever role-entitlement
decision a TokenProvider/AuthorityProvider would make downstream -- it
answers a different question ("is this process who it claims to be") than
those seams do ("is this claimed role entitled to X").

OMITTED --caller/--role IS POLICY-GATED (lr-620837 fold-in #4, F6,
correcting fold-in #2's own first revision of this behavior):
before lr-620837, `caller_explicit=False` short-circuited
`resolve_for_binding` BEFORE identity resolution ever ran, returning an
inert placeholder Identity (formerly `UNCLAIMED_SOURCE`) that `bind_caller`
never inspected -- an omitted `--caller` therefore minted the DEFAULT_ROLE
credential with NO attested identity behind it at all, on ANY process,
attested or not (a "vanilla root shell with no sidecar" typing no flags at
all sailed straight through). Fold-in #2's operator ruling (comment #5)
correctly identified this as unacceptable UNDER THE STRICT POLICY -- but
its first revision fixed it by making `resolve_for_binding` resolve
identity UNCONDITIONALLY regardless of policy, which broke every existing
unconfigured/`builtin-fallback` deployment's omitted-caller invocations the
moment it shipped: `attestation.bound_identity: builtin-fallback` (this
package's DEFAULT policy, existing before either fold-in) is specifically
the policy that says "an install with no scoped sidecar adapters keeps
working exactly as released" -- an omitted caller unconditionally requiring
attestation contradicted that promise for the omitted-caller path even
though the explicit-caller path, and the scoped/unscoped sidecar-lookup
behavior, both correctly preserved it.

THE FIX (this revision): whether an omitted caller requires attestation is
now the SAME `attestation.bound_identity` policy knob that already governs
everything else about caller-bound resolution, not a separate unconditional
rule:

  - `builtin-fallback` (the default): an omitted `--caller`/`--role`
    behaves EXACTLY as this package originally released it, before either
    fold-in -- `resolve_identity_fn()` is never called on this path, and
    the effective caller is `DEFAULT_ROLE`, no attestation required, no
    refusal possible. `bind_caller` is still called (auditability,
    consistency with the explicit path), but `caller == identity.subject`
    is true by construction (`identity` IS
    `Identity(DEFAULT_ROLE, SOURCE_CONFIGURED)`), so it never raises here.
  - `required`: an omitted `--caller` remains an IMPLICIT claim of "I am
    acting as my own attested identity" -- `resolve_identity_fn()` runs
    exactly like the explicit path, and a process with no attested
    identity at all is refused here exactly like an explicit mismatched
    `--caller` always is. This is fold-in #2's original fix, now correctly
    scoped to the policy a deployment opts into specifically to close the
    unattested-host gap, rather than applied to every deployment
    regardless of policy.

Every call site derives its EFFECTIVE caller from the resolved identity's
own `.subject` on the omitted path (`identity.subject` -- `DEFAULT_ROLE`
under `builtin-fallback`, the attested subject under `required`), then
binds against it via `bind_caller(..., caller_explicit=True, ...)`. See
each call site's own comment for that derivation; this module only
provides the policy-gated primitive, since every one of the seven call
sites needs the same derivation and this is the shared home for that shape
(reuse-first).

THE PER-SPAWN-MISS DISCRIMINATOR REFUSAL STAYS UNCONDITIONAL UNDER BOTH
POLICIES ON EVERY PATH THAT REACHES IT: `resolve_bound_identity`'s own
per-spawn discriminator (a `scope: per-spawn` adapter's `session_id_env`
set, but that adapter's own file misses) refuses regardless of
`bound_identity` policy -- see that function's own docstring. This module's
policy gate only decides WHETHER the omitted path calls
`resolve_identity_fn()` at all; it never weakens what that function does
once called (the explicit path, and the omitted path under `required`,
both still hit the unconditional per-spawn refusal exactly as before).

MIGRATION NOTE for an existing deployment: an omitted `--caller`/`--role`
invocation on a host with NO attestation source configured (no
`attestation.identity_env`, no resolving sidecar) behaves EXACTLY as this
package has always released it under the default `builtin-fallback`
policy -- `DEFAULT_ROLE`, no refusal. A deployment that has opted into
`attestation.bound_identity: required` (see `transport.attestation.
resolve_bound_identity`'s own docstring, and `docs/provisioning.md`'s
"Caller-bound resolution" section) gets the stricter fail-closed omitted-
caller behavior this section originally described -- that is the intended
trade-off of opting into `required`, not a universal default.

This is INDEPENDENT of, and runs strictly BEFORE,
`transport.credential_provider.resolve_token` and
`merge.authority.check_authority` -- neither of those seams is changed by
this function, and neither of them re-verifies what this function already
confirmed (they continue to treat --caller/--role as the already-attested,
opaque value lr-e5eeab established; see each of their own module
docstrings). This module is what makes that treatment SAFE to begin with --
previously it was safe only at the one call site that happened to wire this
check in.

REQUIREMENT 5 -- DOES THE BUILT-IN OS-USER FALLBACK RETAIN WRITE CAPABILITY
(lr-c75c9a judgment call 2, named explicitly per the task rather than
silently decided; SUPERSEDED by the operator ruling introducing
`resolve_bound_identity` -- kept below for its historical rationale, not as
a description of the current default): at the time lr-c75c9a shipped, YES,
the built-in OS-user fallback retained write capability -- `transport.
attestation.resolve_identity`'s layer 3 (`_BuiltinOsUserProvider`,
`getpass.getuser()`) was treated as a REAL, non-degraded attested identity,
so a deployment with no `attestation.identity_env`/sidecar configured still
got real write access through the host uid. THE OPERATOR RULING REJECTS
THIS for the default caller-bound path: "this computer runs on root" is not
an attested identity, full stop -- see `transport.attestation.
resolve_bound_identity`'s own docstring for the replacement rule (never the
built-in fallback; a discriminator-selected sidecar source, or a terminal
refusal naming what was expected). lr-c75c9a's original concern -- that
refusing the fallback would outage every agent in a deployment with no
attestation config wired yet -- is answered differently now: the fix is to
land the deployed-config sidecar adapter(s) (host state, tracked
separately, NOT this module's job), not to keep trusting a host uid as a
agent identity. A deployment that still wants the OLD (fallback-permitted)
behavior can inject the general `resolve_identity` chain via
`identity_provider=` at any call site -- that seam was never removed, only
the DEFAULT changed.
"""

from __future__ import annotations

from typing import Callable

from clagentic_loadout.transport.attestation import (
    BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK,
    Identity,
    SOURCE_CONFIGURED,
    resolve_bound_identity_policy,
)
from clagentic_loadout.transport.credential_provider import DEFAULT_ROLE


def describe_omitted_caller_behavior(*, flag_name: str = "--caller") -> str:
    """Return the ONE accurate, policy-complete description of what an
    OMITTED *flag_name* does, shared verbatim by every caller-bound verb's
    argparse `help=` text (lr-620837 fold-in #5): before this function
    existed, all seven caller-bound verbs (`push`,
    `review`, `acquire`, `merge`, `merge close`, `merge post-merge`,
    `transport.git_host_api`) duplicated an inline help string asserting an
    omitted caller is "never DEFAULT_ROLE by itself" -- true only under
    `attestation.bound_identity: required`, and FALSE under this package's
    own DEFAULT policy, `builtin-fallback`, where an omitted caller becomes
    exactly `DEFAULT_ROLE` with no attestation check at all (see this
    module's own docstring, "OMITTED --caller/--role IS POLICY-GATED", and
    `resolve_for_binding`'s docstring for the full policy-gated rule this
    text summarizes).

    *flag_name* lets a `--role`-named verb (the three merge verbs) get
    grammatically correct text ("an omitted --role") without a second
    hand-maintained copy of the surrounding sentence.
    """
    return (
        f"OMITTED is policy-gated on this deployment's "
        f"attestation.bound_identity setting: under the default "
        f"'builtin-fallback' policy, an omitted {flag_name} resolves to "
        f"{DEFAULT_ROLE!r} with no attestation check and no possible "
        f"refusal (this package's originally-released behavior); under "
        f"'required', an omitted {flag_name} is an IMPLICIT claim of 'act "
        f"as my own attested identity' -- it must resolve to that "
        f"identity's own subject via the same attested-identity resolver "
        f"the explicit path uses, and a process with no attested identity "
        f"at all is refused the same way an explicit mismatch always is."
    )


class CallerBindingError(Exception):
    """Raised when an EXPLICIT --caller/--role value does not match the
    ATTESTED invoking identity this process's own attestation-provider chain
    resolved (transport.attestation.resolve_bound_identity by default, or an
    injected `identity_provider=`). FAILS CLOSED BEFORE
    ANY I/O -- no token mint, no authority check, no request is ever issued.
    An identity may only ever use ITS OWN credential; a caller that presents
    a role other than its own attested identity is refused unconditionally,
    with no override. An OMITTED --caller/--role is bound to this SAME check
    by every call site (see this module's own docstring, "OMITTED
    --caller/--role IS POLICY-GATED"): a call site derives its effective
    caller as `identity.subject` on the omitted path, so
    `caller == identity.subject` there by construction and this never
    raises on an omitted `--caller` alone -- but under
    `attestation.bound_identity: required`, the resolution that produced
    `identity` in the first place can still raise
    `AttestationError`/`BoundAttestationError`, which is the actual refusal
    an unattested omitted-caller invocation hits under that policy. Under
    the default `builtin-fallback` policy, an omitted caller's `identity`
    is always `Identity(DEFAULT_ROLE, SOURCE_CONFIGURED)` and no resolution
    is attempted at all -- see `resolve_for_binding`'s own docstring.

    Carries `.caller` and `.identity` (the compared values) so a catching
    verb can render its own resolved-values error message and exit code
    without re-deriving either from a formatted string."""

    def __init__(self, caller: str, identity: Identity) -> None:
        super().__init__(
            f"--caller/--role {caller!r} does not match the ATTESTED invoking "
            f"identity {identity.subject!r} (resolved via the "
            f"{identity.source!r} attestation layer). An identity may act "
            f"ONLY as its own attested value -- this is refused BEFORE any "
            f"network I/O and before any credential is resolved or merge "
            f"authority is checked, unconditionally, with no override (even "
            f"a role a named-agent allowlist would otherwise admit is "
            f"denied here)."
        )
        self.caller = caller
        self.identity = identity


def bind_caller(caller: str, *, caller_explicit: bool, identity: Identity) -> None:
    """Enforce the layer (1)->(2) binding: an identity may act ONLY as
    ITS OWN attested value (lr-82c385, tome #700; lifted to this shared
    module and wired into every mutating verb by lr-c75c9a).

    See this module's own docstring for the full three-layer trust-model
    statement, the built-in-OS-user-fallback trade-off (requirement 5), and
    why this seam lives here rather than in transport.git_host_api.

    Raises CallerBindingError when `caller != identity.subject`.

    *caller_explicit* is accepted for call-site symmetry and audit-log
    parity with `resolve_for_binding` (every call site passes the same
    `args.caller is not None` value to both), but no longer changes this
    function's own comparison behavior (operator ruling, lr-620837 comment
    #5): both the explicit and the omitted path are bound against the
    resolved identity now -- see this module's own docstring, "OMITTED
    --caller/--role IS NOW ALSO AN ATTESTED-IDENTITY REQUIREMENT". On the
    omitted path, every call site derives `caller` as `identity.subject`
    (never a free-typed value), so this comparison is always true there by
    construction and never itself raises for an omitted `--caller` -- the
    actual refusal for an unattested omitted-caller invocation happens one
    step earlier, when `resolve_for_binding`'s `resolve_identity_fn()` call
    raises `AttestationError`/`BoundAttestationError`.
    """
    if caller != identity.subject:
        raise CallerBindingError(caller, identity)


def resolve_for_binding(
    *,
    caller_explicit: bool,
    caller: str,
    resolve_identity_fn: Callable[[], Identity],
    bound_identity_policy_fn: Callable[[], str] = resolve_bound_identity_policy,
) -> Identity:
    """Resolve the Identity `bind_caller` needs.

    EXPLICIT `--caller`/`--role` (*caller_explicit* True): *resolve_identity_fn*
    is ALWAYS called, and its result is returned directly; a resolution
    failure (`AttestationError`/`BoundAttestationError`, the exception type
    `transport.attestation.resolve_bound_identity` raises when no attested
    source answers) propagates to the caller. Unchanged since lr-c75c9a.

    OMITTED `--caller`/`--role` (*caller_explicit* False) IS POLICY-GATED
    (lr-620837 fold-in #4, F6, correcting fold-in #2's first revision): the
    effective `attestation.bound_identity` policy
    -- read via *bound_identity_policy_fn* -- decides what an omitted
    caller means, mirroring this package's ORIGINALLY-RELEASED semantics
    under the permissive policy rather than unconditionally requiring
    attestation on every deployment regardless of policy:

      - `BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK` (the default): an omitted
        caller behaves EXACTLY as this package originally released it --
        *resolve_identity_fn* is NEVER called, and this returns
        `Identity(DEFAULT_ROLE, SOURCE_CONFIGURED)` unconditionally. No
        attestation is required, no resolution is attempted, and no
        `AttestationError`/`BoundAttestationError` can ever propagate from
        this path under this policy. A caller that types no `--caller`/
        `--role` flag at all gets DEFAULT_ROLE, full stop -- the same
        behavior every pre-lr-620837 release of this package always had.
      - `BOUND_IDENTITY_POLICY_REQUIRED`: an omitted caller is an IMPLICIT
        claim of "act as my own attested identity" -- *resolve_identity_fn*
        is called (exactly like the explicit path), and its result is
        returned directly. A process with no attested identity at all is
        refused here exactly like an explicit mismatched `--caller` always
        is. This is the behavior lr-620837 fold-in #2 introduced
        UNCONDITIONALLY (a defect this fold-in corrects): it is now
        scoped to `required` only, the policy a
        deployment opts into specifically to close the "unattested host
        silently acts as DEFAULT_ROLE" gap.

    THE PER-SPAWN-MISS REFUSAL STAYS UNCONDITIONAL UNDER BOTH POLICIES,
    unaffected by anything in this function: `resolve_bound_identity`'s own
    discriminator refuses a per-spawn-declared invocation whose per-spawn
    sidecar adapter misses regardless of `bound_identity` policy (see that
    function's own docstring, "THE SUBAGENT-DISCRIMINATOR REFUSAL IS
    UNCONDITIONAL"). That refusal fires INSIDE *resolve_identity_fn* itself
    on the EXPLICIT path (always called) and on the omitted path ONLY under
    `required` (the only omitted-path branch that calls
    *resolve_identity_fn* at all) -- there is no omitted-path shape under
    `builtin-fallback` that could ever reach the per-spawn discriminator,
    because that policy never calls the resolver on the omitted path in the
    first place; a per-spawn subagent that wants the discriminator's
    protection while running unattested-by-default elsewhere in the
    deployment must still pass an EXPLICIT `--caller`/`--role`.

    Shared by every one of the seven caller-bound verbs (`push`, `review`,
    `acquire`, `merge`, `merge --close`, `merge --post-merge`,
    `transport.git_host_api` itself), replacing what used to be an
    identical seven-way-duplicated inline block (reuse-first, CLAUDE.md
    code-craft rule 1/2).

    *bound_identity_policy_fn* is an injection point (mirrors
    *resolve_identity_fn*'s own purpose) -- defaults to `transport.
    attestation.resolve_bound_identity_policy` (reads the live
    `attestation.bound_identity` config key), overridable by a caller (or a
    test) that wants a specific policy without writing a config file. Only
    ever called on the omitted-caller path -- the explicit path never reads
    policy at all, matching the "explicit --caller always requires a match"
    invariant that predates this policy knob entirely.

    HISTORICAL RATIONALE (why this function's PRIOR revision resolved
    identity unconditionally on the omitted path too, kept for context):
    lr-620837 fold-in #2's first revision made *resolve_identity_fn* ALWAYS
    called, reasoning that skipping resolution on the omitted path was
    never a safety property, only an optimization that stopped being safe
    once every call site's default *resolve_identity_fn* switched from
    `resolve_identity` (whose built-in-OS-user fallback always resolves
    SOMETHING in a real deployment) to `resolve_bound_identity` (which can
    refuse outright). That reasoning held for the `required` policy but
    over-applied it to `builtin-fallback` too, breaking every existing
    unconfigured install's omitted-caller invocations the moment they
    upgraded (F6) -- this revision narrows the unconditional-resolution
    behavior back to the `required` policy only,
    where an unattested omitted caller SHOULD fail closed, while restoring
    the original DEFAULT_ROLE-no-refusal behavior under the default,
    released `builtin-fallback` policy.

    *caller_explicit* is still accepted (every call site passes
    `args.caller is not None`) because it is a useful audit signal for a
    call site's own logging, and because `bind_caller` accepts the same
    parameter for symmetry.
    """
    if not caller_explicit and bound_identity_policy_fn() == BOUND_IDENTITY_POLICY_BUILTIN_FALLBACK:
        return Identity(DEFAULT_ROLE, SOURCE_CONFIGURED)
    return resolve_identity_fn()


__all__ = [
    "CallerBindingError",
    "bind_caller",
    "describe_omitted_caller_behavior",
    "resolve_for_binding",
]
