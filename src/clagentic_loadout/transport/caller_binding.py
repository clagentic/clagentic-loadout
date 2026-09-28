"""transport.caller_binding — the shared layer (1)->(2) fail-closed binding
every mutating verb that accepts --caller/--role now calls before it reaches
a credential mint (lr-c75c9a, P1 security fix).

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
`identity_provider=`). `caller` is the value --caller/--role resolved to
(already defaulted to DEFAULT_ROLE when omitted, by the call site).

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

`caller_explicit=False` (an OMITTED --caller/--role, defaulted to
DEFAULT_ROLE by the call site) is NEVER checked against `identity` --
this preserves the pre-existing, unchanged "omitted --caller behaves
exactly as before" contract (lr-82c385's own test-matrix requirement,
carried forward unchanged by this task). An omitted --caller/--role is not
an identity CLAIM at all; there is nothing to bind.

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

#: Identity.source label for the placeholder Identity `resolve_for_binding`
#: returns when --caller was omitted -- never compared against anything
#: (bind_caller's own no-op path short-circuits before touching it), so its
#: `subject`/`source` values are inert filler, not a resolved attestation.
UNCLAIMED_SOURCE = "unclaimed"


class CallerBindingError(Exception):
    """Raised when an EXPLICIT --caller/--role value does not match the
    ATTESTED invoking identity this process's own attestation-provider chain
    resolved (transport.attestation.resolve_bound_identity by default, or an
    injected `identity_provider=`). FAILS CLOSED BEFORE
    ANY I/O -- no token mint, no authority check, no request is ever issued.
    An identity may only ever use ITS OWN credential; a caller that presents
    a role other than its own attested identity is refused unconditionally,
    with no override. An OMITTED --caller/--role never triggers this (see
    `bind_caller`'s own docstring) -- it is unchanged, existing behavior.

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

    Raises CallerBindingError when `caller_explicit` is True and `caller !=
    identity.subject`. A no-op (returns None) when `caller_explicit` is
    False -- an omitted --caller/--role carries no identity claim to bind.
    """
    if not caller_explicit:
        return
    if caller != identity.subject:
        raise CallerBindingError(caller, identity)


def resolve_for_binding(
    *,
    caller_explicit: bool,
    caller: str,
    resolve_identity_fn: Callable[[], Identity],
) -> Identity:
    """Resolve the Identity `bind_caller` needs -- SKIPPING resolution
    entirely when *caller_explicit* is False, rather than resolving an
    unconditionally-discarded value.

    Shared by every one of the six caller-bound verbs (`push`, `review`,
    `acquire`, `merge`, `merge --close`, `merge --post-merge`) plus
    `transport.git_host_api` itself, replacing what used to be an identical
    seven-way-duplicated inline block (reuse-first, CLAUDE.md code-craft
    rule 1/2).

    WHY THIS MATTERS NOW (operator ruling, resolve_bound_identity): before
    this task, *resolve_identity_fn* was `transport.attestation.
    resolve_identity`, whose built-in OS-user fallback (`SOURCE_BUILTIN`)
    means it ALWAYS resolves something in a real deployment -- so calling
    it unconditionally, even when `bind_caller`'s own no-op-on-omitted-
    caller path was about to discard the result unused, was wasteful but
    harmless. *resolve_identity_fn* is now `transport.attestation.
    resolve_bound_identity` at every one of these call sites, which NEVER
    falls through to that fallback (the whole point of the ruling this
    task implements) -- so an unconditional call would turn every omitted-
    `--caller` invocation on a host with no attestation source configured
    into a hard failure for a comparison `bind_caller` was never going to
    make anyway. Gating resolution on the SAME condition that already
    gated the comparison (`caller_explicit`) removes that failure mode
    without changing bind_caller's own contract at all.

    On the omitted-caller path (`caller_explicit=False`), returns a
    placeholder `Identity(subject=caller, source=UNCLAIMED_SOURCE)` --
    inert filler `bind_caller` never inspects on that path (its own no-op
    short-circuit runs before touching `identity`), never a real resolved
    value.
    """
    if not caller_explicit:
        return Identity(subject=caller, source=UNCLAIMED_SOURCE)
    return resolve_identity_fn()


__all__ = [
    "UNCLAIMED_SOURCE",
    "CallerBindingError",
    "bind_caller",
    "resolve_for_binding",
]
