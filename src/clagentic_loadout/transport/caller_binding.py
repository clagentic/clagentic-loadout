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
unattested process could act as any crew identity by typing its name on the
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
default resolver is now `transport.attestation.resolve_bound_identity`
(operator ruling, comment #5 on the task that introduced it) -- the
configured provider, or exactly ONE discriminator-selected sidecar source,
NEVER the built-in OS-user fallback; see that function's own docstring for
the full rule this module's REQUIREMENT 5 below predates and no longer
describes the default path (kept here for its historical rationale, since
a deployment MAY still inject the general `resolve_identity` chain via
`identity_provider=`).

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

OMITTED --caller/--role IS NOW ALSO AN ATTESTED-IDENTITY REQUIREMENT
(BEHAVIOR CHANGE, operator ruling on lr-620837 comment #5, a pre-merge
review finding on this fix's own first revision): before this fix,
`caller_explicit=False` short-circuited
`resolve_for_binding` BEFORE identity resolution ever ran, returning an
inert placeholder Identity (formerly `UNCLAIMED_SOURCE`) that `bind_caller`
never inspected -- an omitted `--caller` therefore minted the
DEFAULT_ROLE credential with NO attested identity behind it at all, on ANY
process, attested or not (a "vanilla root shell with no sidecar" typing no
flags at all sailed straight through). The ruling's acceptance is explicit
that this is unacceptable: "a caller with no sidecar at all (vanilla root
shell) -> REFUSED no attested identity" is not scoped to an explicit
--caller, and this binding being pre-existing/unenforced on the omitted
path before this fix is not an exemption from the ruling.

THE FIX: `resolve_for_binding` now resolves identity UNCONDITIONALLY --
omitted or explicit, the SAME `resolve_identity_fn()` call runs, and a
resolution failure (`AttestationError`/`BoundAttestationError`) propagates
to the caller exactly as it already did on the explicit path. There is no
longer a skip-resolution branch and no placeholder Identity; every call
site now derives its EFFECTIVE caller from the resolved identity itself on
the omitted path (`identity.subject`, never a placeholder), then binds
against it via `bind_caller(..., caller_explicit=True, ...)` -- omitted
--caller now behaves as an IMPLICIT claim of "I am acting as my own
attested identity," bound exactly like an explicit one, rather than "no
claim, no check." A verb whose caller-bound token/authority seam is then
handed the resolved identity subject as its role -- never DEFAULT_ROLE by
itself, and never a role this process did not attest to. See each call
site's own comment for the "omitted -> identity.subject" derivation; this
module only provides the always-resolving primitive, since every one of the
seven call sites needs the same derivation and this is the shared home for
that shape, matching this module's whole reason for existing (reuse-first).

MIGRATION NOTE for an existing deployment: an omitted `--caller`/`--role`
invocation that previously succeeded on a host with NO attestation source
configured (no `attestation.identity_env`, no resolving sidecar) now FAILS
CLOSED with the same `AttestationError`/`BoundAttestationError` an explicit
mismatched caller always raised -- there is no longer an unattested
default-role path. A deployment that still wants the OLD (attestation-free)
behavior can inject the general `resolve_identity` chain (whose built-in
OS-user fallback always resolves something) via `identity_provider=` at any
call site -- that injection seam is unchanged, only the module-level
DEFAULT resolver's omitted-caller treatment changed.

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
refusing the fallback would outage every crew agent in a deployment with no
attestation config wired yet -- is answered differently now: the fix is to
land the deployed-config sidecar adapter(s) (host state, tracked
separately, NOT this module's job), not to keep trusting a host uid as a
crew identity. A deployment that still wants the OLD (fallback-permitted)
behavior can inject the general `resolve_identity` chain via
`identity_provider=` at any call site -- that seam was never removed, only
the DEFAULT changed.
"""

from __future__ import annotations

from typing import Callable

from clagentic_loadout.transport.attestation import Identity


class CallerBindingError(Exception):
    """Raised when an EXPLICIT --caller/--role value does not match the
    ATTESTED invoking identity this process's own attestation-provider chain
    resolved (transport.attestation.resolve_bound_identity by default, or an
    injected `identity_provider=`). FAILS CLOSED BEFORE
    ANY I/O -- no token mint, no authority check, no request is ever issued.
    An identity may only ever use ITS OWN credential; a caller that presents
    a role other than its own attested identity is refused unconditionally,
    with no override. An OMITTED --caller/--role is bound to this SAME check
    by every call site (see this module's own docstring, "OMITTED --caller/
    --role IS NOW ALSO AN ATTESTED-IDENTITY REQUIREMENT"): a call site
    derives its effective caller as `identity.subject` on the omitted path,
    so `caller == identity.subject` there by construction and this never
    raises on an omitted `--caller` alone -- but the resolution that
    produced `identity` in the first place can still raise
    `AttestationError`/`BoundAttestationError`, which is the actual refusal
    an unattested omitted-caller invocation now hits.

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
) -> Identity:
    """Resolve the Identity `bind_caller` needs -- UNCONDITIONALLY, on both
    the explicit and the omitted `--caller`/`--role` path (operator ruling,
    lr-620837 comment #5, closing a pre-merge review finding on this fix's
    own first revision).

    Shared by every one of the seven caller-bound verbs (`push`, `review`,
    `acquire`, `merge`, `merge --close`, `merge --post-merge`,
    `transport.git_host_api` itself), replacing what used to be an
    identical seven-way-duplicated inline block (reuse-first, CLAUDE.md
    code-craft rule 1/2).

    BEHAVIOR CHANGE FROM THE PRIOR REVISION OF THIS FUNCTION (see this
    module's own docstring for the full ruling): resolution used to be
    SKIPPED entirely when *caller_explicit* was False, returning an inert
    placeholder Identity `bind_caller` never inspected -- an omitted
    `--caller` therefore minted a credential with NO attested identity
    behind it at all. That placeholder is gone. *resolve_identity_fn* is
    now ALWAYS called, and its result is returned directly; a resolution
    failure (`AttestationError`/`BoundAttestationError`, the exception type
    `transport.attestation.resolve_bound_identity` raises when no attested
    source answers) propagates to the caller exactly as it already did on
    the explicit path -- there is no longer a comparison-avoidance reason to
    skip it, and skipping it was never a safety property, only an
    optimization that stopped being safe once *resolve_identity_fn* stopped
    being a chain that always resolves SOMETHING (see the historical
    rationale kept below).

    *caller_explicit* is still accepted (every call site passes
    `args.caller is not None`) because it is a useful audit signal for a
    call site's own logging, and because `bind_caller` accepts the same
    parameter for symmetry -- but this function no longer branches on it.

    HISTORICAL RATIONALE (why this function skipped resolution before this
    fix, kept for context): *resolve_identity_fn* used to be `transport.
    attestation.resolve_identity`, whose built-in OS-user fallback
    (`SOURCE_BUILTIN`) means it ALWAYS resolves something in a real
    deployment -- so calling it unconditionally, even when `bind_caller`'s
    old no-op-on-omitted-caller short-circuit was about to discard the
    result unused, was wasteful but harmless. Once every caller-bound call
    site switched *resolve_identity_fn* to `transport.attestation.
    resolve_bound_identity` (which never falls through to that fallback),
    an unconditional call turned every omitted-`--caller` invocation on a
    host with no attestation source configured into a hard failure -- which
    this function's prior revision treated as a bug to route around by
    skipping resolution. The operator ruling settles that this is not a
    bug: an omitted `--caller` on an unattested host SHOULD fail closed,
    exactly like an explicit one does, rather than silently minting
    DEFAULT_ROLE with no attestation behind it.
    """
    return resolve_identity_fn()


__all__ = [
    "CallerBindingError",
    "bind_caller",
    "resolve_for_binding",
]
