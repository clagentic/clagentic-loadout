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
matters: `identity` is whatever `transport.attestation.resolve_identity` (or
an injected equivalent) resolved for THIS process -- the configured
provider, the sidecar adapter, or the built-in OS-user fallback, in that
fixed order (see transport.attestation's own module docstring for the full
three-layer trust-model statement this function is layer (1)->(2) of).
`caller` is the value --caller/--role resolved to (already defaulted to
DEFAULT_ROLE when omitted, by the call site).

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
silently decided): YES, it retains write capability, unchanged from
lr-82c385's original scope. `transport.attestation.resolve_identity`'s
layer 3 (`_BuiltinOsUserProvider`, `getpass.getuser()`) is a REAL,
non-degraded attested identity, not an absence of one -- a deployment that
configures neither `attestation.identity_env` nor a sidecar adapter still
gets a genuine attested subject (the OS-reported invoking user), and this
binding compares --caller/--role against THAT value exactly as it would
against a configured-provider or sidecar-resolved one. The alternative --
treating the built-in fallback as "not really attested" and refusing every
explicit --caller/--role that reaches it -- was rejected: every crew agent
in a deployment with no attestation.* config wired yet pushes/merges/
reviews through these verbs today, and that would turn this fix into an
outage for the entire crew the moment it landed, for every deployment that
has not yet configured layer 1/2 of transport.attestation. That is a
strictly worse security posture than the one being fixed here: the actual
defect (lr-c75c9a's root cause) is that the binding was UNENFORCED on six
verbs, not that the built-in fallback layer is too permissive -- the
fallback's own identity is exactly as trustworthy post-fix as it already
was for the ONE verb (git_host_api) lr-82c385 originally shipped this
check on, and that verb has run this same fallback-permits-a-match
behavior in production since lr-82c385 landed with no reported incident
traceable to it. A deployment that judges the OS-user fallback insufficient
for its threat model configures `attestation.identity_env` or a sidecar
adapter (transport.attestation's own config surface) to require a stronger
attested source; that is a deployment-level policy choice this module does
not make on any deployment's behalf.
"""

from __future__ import annotations

from clagentic_loadout.transport.attestation import Identity


class CallerBindingError(Exception):
    """Raised when an EXPLICIT --caller/--role value does not match the
    ATTESTED invoking identity this process's own attestation-provider chain
    resolved (transport.attestation.resolve_identity). FAILS CLOSED BEFORE
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


__all__ = ["CallerBindingError", "bind_caller"]
