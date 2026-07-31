"""merge.authority — the merge-authority provider seam.

Wave B slice 4 (lr-885f, tome #688). The reference merge gate
(check_merge_authority) ties merge authority to a single hardcoded caller
identity (the release-gate role's fixed agent name) verified against a
specific internal directory service at
a fixed localhost URL. That FAIL-CLOSED SECURITY POSTURE is preserved here in
full — but the *mechanism* (which service, what URL, which identity) is not
loadout's to bake in. Which ROLE may authorize a merge is CONFIG, never a
hardcoded name.

ONE SEAM, TWO PROVIDERS. ``AuthorityProvider`` is the Protocol every merge
gate consumes: ``authority_allows(role, owner, repo, pr_number) -> bool``. A
directory-style trust-label service is the REFERENCE provider — it
implements this same protocol from outside this package (composition, not
inheritance); loadout ships the protocol and a STANDALONE static-role-config
fallback, not a hardcoded client for any one directory.

FAIL-CLOSED (contrast with a human-operator release tool that might warn-and-
allow on an outage): every path through this module that cannot POSITIVELY
confirm authority denies the merge. An unreachable provider, a malformed
response, or a role absent from the configured allow-set are all refusals —
never a silent allow. This mirrors the reference module's own contrast
between its human-operator path (a manual release tool, fails open) and its
autonomous merge-gate path (fails closed): merge.authority is unconditionally
the fail-closed shape; there is no fail-open variant in this package.

THE `role` ARGUMENT IS AN ALREADY-ATTESTED VALUE, NEVER A FREE ARG (tome
#700 correction 3, lr-e5eeab): `authority_allows(role, ...)` receives
whatever string `merge.verb` resolved from its own `--role`/`--caller`
flag — this module does not independently verify that the invoking process
actually IS the identity that string names. See docs/merge-authority.md §4
("There is no external verification of the role claim itself") for the
consumer-facing statement of this boundary, and
transport.credential_provider's module docstring for the parallel
statement on the token-resolution side. Binding a specific credential or
spawn context to a specific role — i.e. confirming the *claim* is true,
not just well-formed — is a MINTING-TIME concern that happens upstream of
this module, in whatever attests the caller's identity before a role
string ever reaches this process's argv (this package's reference
deployment does this at credential-mint time, layering a role-entitlement
check in front of the broker read — see that deployment's own docs, not
this package). loadout MUST NOT try to re-derive or re-verify that
attestation itself here by reaching into a harness-specific identity
sidecar/side-channel: this package is orchestration-agnostic (CLAUDE.md
rule 2) and has no spawn-side visibility into how a harness decided which
role a given process may act as, by design — ingesting one would
coincidentally couple this seam to a single orchestration layer's
transport (the relay lesson).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class AuthorityProviderError(Exception):
    """Raised on any authority-check failure — provider unreachable,
    malformed response, or an explicit deny. Callers translate this to their
    own exit-code convention; this module never calls sys.exit so it stays
    exit-code-agnostic and testable in isolation. FAIL-CLOSED: raising this
    (rather than returning False) is deliberate so a caller cannot
    accidentally treat 'could not determine' the same as 'determined: no
    merge authority' without noticing — both refuse, but the distinction
    matters for diagnosis."""


@runtime_checkable
class AuthorityProvider(Protocol):
    """The merge-authority resolution seam every merge gate consumes.

    A single method, ``authority_allows(role, owner, repo, pr_number)``,
    returning True only when *role* is POSITIVELY confirmed to hold merge
    authority for the given target. Implementations own their own
    verification mechanism entirely (a directory service, a static config
    file, an OPA policy, anything) — loadout's gate code depends only on this
    signature.

    Raise AuthorityProviderError (or return False) on any failure or denial;
    the merge gate treats both uniformly as a refusal. Never raise for an
    ordinary "not authorized" outcome when a definitive False is available —
    reserve the exception for cases where the provider itself could not be
    consulted (network error, malformed response), so a caller's log line can
    distinguish "checked, denied" from "could not check."
    """

    def authority_allows(
        self,
        role: str,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> bool:
        """Return True only if *role* is confirmed to hold merge authority
        for PR *pr_number* in *owner*/*repo*. Return False (never raise) for
        an ordinary, well-formed "not authorized" determination; raise
        AuthorityProviderError only when the determination itself could not
        be made."""
        ...


class StaticRoleAuthorityProvider:
    """Standalone reference AuthorityProvider: a caller-supplied set of role
    names that are always authorized to merge, evaluated locally with no
    network call.

    This is the "no external directory/policy service configured" fallback —
    a deployment with no directory-equivalent provider can still operate by
    passing the set of merger-capable roles at construction time (e.g. from a
    config file or CLI flags). A deployment that DOES have a directory-style
    service wires its own AuthorityProvider implementation instead; this
    class is not the only way to satisfy the protocol.

    An EMPTY authorized_roles set denies every role — this class is
    fail-closed by construction, matching the seam's overall posture: a
    standalone deployment that never configures an authorized role has,
    correctly, configured "nobody may merge" rather than defaulting to
    permissive.
    """

    def __init__(self, authorized_roles: frozenset[str]) -> None:
        self._authorized_roles = frozenset(authorized_roles)

    def authority_allows(
        self,
        role: str,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> bool:
        del owner, repo, pr_number  # role-scoped only; not per-repo in the standalone provider
        return role in self._authorized_roles


def check_authority(
    role: str,
    owner: str,
    repo: str,
    pr_number: int,
    provider: AuthorityProvider,
) -> None:
    """Enforce the merge-authority gate. Raises
    merge.errors.AuthorityDeniedError (FAIL-CLOSED) unless *provider*
    positively confirms *role* holds merge authority for the target.

    A provider that raises AuthorityProviderError (unreachable, malformed
    response) is treated identically to an explicit deny — the caller cannot
    distinguish "the service said no" from "the service could not be
    reached" from the exception type alone, by design: both must refuse the
    merge, and the message text carries the distinction for diagnosis.
    """
    # Local import: keeps this module importable with zero cost when only the
    # Protocol/provider shapes are needed, and avoids a hard import-time
    # coupling from this seam module to the gate's own exception vocabulary.
    from clagentic_loadout.merge.errors import AuthorityDeniedError

    try:
        allowed = provider.authority_allows(role, owner, repo, pr_number)
    except AuthorityProviderError as exc:
        raise AuthorityDeniedError(
            f"merge-authority check DENIED — provider {provider!r} could not "
            f"confirm authority for role {role!r} on PR #{pr_number} in "
            f"{owner}/{repo}: {exc}. FAIL-CLOSED: a provider that cannot be "
            f"consulted refuses the merge, exactly like an explicit deny."
        ) from exc

    if not allowed:
        raise AuthorityDeniedError(
            f"merge-authority check DENIED — role {role!r} is not authorized "
            f"to merge PR #{pr_number} in {owner}/{repo} per provider "
            f"{provider!r}. This is a scope boundary, not a recoverable "
            f"error: the role must be granted merge authority, or a "
            f"different role with merge authority must invoke this verb. "
            f"Do not retry with the same role: this refusal is deterministic."
        )


__all__ = [
    "AuthorityProvider",
    "AuthorityProviderError",
    "StaticRoleAuthorityProvider",
    "check_authority",
]
