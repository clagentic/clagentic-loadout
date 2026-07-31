"""push.crew_identity — derive bot commit identity from --caller (lr-f145d2).

THE GAP THIS CLOSES: push.identity_config's `builder_identity:` config
section (lr-4e8a43) wires a deployment-wide bot name/email into the push
verb, but ONLY when an operator sets that section explicitly. A deployment
that has already declared a caller -> GitHub App slug mapping
(`transport.github_app_config`'s `github_app.slugs`/`github_app.callers`,
consumed by `review.github_backend.resolve_own_login` and
`merge.reviewer_login.resolve_reviewer_login` for the SAME identity
question on the read side) still gets NO commit re-authoring at all unless
it ALSO configures `builder_identity:` — a second, redundant source of
truth for a fact the deployment already stated once. When that second
section is never set (the observed, live state), commits fall through to
whatever ambient git identity the pushing process happened to inherit —
which is how a crew build agent's commits land attributed to an operator's
own personal account instead of the builder bot (lr-f145d2, restating
lr-0902ba remediation item 1).

THE FIX: for a caller present in the deployment's declared crew-caller
registry (`github_app.callers`), derive the bot identity DETERMINISTICALLY
from that SAME `github_app.slugs.<caller>` entry `push.verb` already has
available -- no new config key, no per-deployment identity string to
restate. A caller NOT in that registry (or a deployment with no
`github_app` section configured at all) gets NO change in behavior --
see `resolve_crew_bot_identity`'s own docstring for the exact precedence
and the external-user-safety argument.

NAME/EMAIL SHAPE: name is `<slug>[bot]`, reusing the EXACT suffix
convention `review.github_backend.resolve_own_login` and
`merge.reviewer_login.resolve_reviewer_login` already established for the
same slug -- one convention, not a second one invented here. Email is
`<slug>[bot]@users.noreply.github.com` on GitHub. This is the noreply
address SHAPE GitHub uses for App-bot accounts, but WITHOUT the numeric
App-bot user-id prefix GitHub actually requires for the email to bind to
that account's own commit-authorship identity (the observed bug's
evidence names the id-prefixed form, e.g. `290147524+clagentic-builder
[bot]@users.noreply.github.com`) -- no numeric App-bot user id is
available anywhere in this deployment's existing config surface (neither
`github_app.slugs` nor any other section names one), and adding a new
required id-mapping key is exactly the class of per-deployment config
step this task's operator directive rules out. See this module's PR body
for the full trade-off: this closes the reported defect (a crew commit no
longer resolves to the operator's own personal account) without claiming
full GitHub bot-account attribution binding, which is a distinct,
narrower follow-up if ever wanted.

FORGEJO HAS NO EQUIVALENT DERIVATION: Forgejo has no GitHub-style App-bot
concept in this codebase's contract (see `merge.reviewer_login`'s own
platform split -- the bare name IS the login on Forgejo). There is no
slug-to-identity derivation to perform on that platform; this module is a
GitHub-only capability and returns "not resolvable" for any other
platform, which its caller (`push.verb`) must treat as FAIL CLOSED for a
registered crew caller (never a silent fall-through to ambient identity).

PROVIDER-SUPPLIED VERIFIED SLUG, A TIER ABOVE CONFIG (lr-43c8d7):
`resolve_crew_bot_identity` gained an OPTIONAL `provider_verified_app_slug`
keyword. When a credential-minting provider reports a verified App slug
alongside the token it minted (`transport.credential_provider.ResolvedToken.app_slug`
-- e.g. a GitHub App installation-token mint that checks its own slug
against the broker at mint time, rather than trusting an operator-typed
config string for the SAME fact), `push.verb` passes it here and it wins
over `github_app.slugs.<caller>`/`github_app.slug` for this call. `None` or
empty (the pre-existing default, and the real state a provider reports for
a role with no App-slug binding configured for this particular mint) falls
through to the config-file resolution below, UNCHANGED from before this
task -- a deployment with no such provider integration sees zero behavior
change. See `resolve_crew_bot_identity`'s own docstring for the exact
precedence.
"""

from __future__ import annotations

from pathlib import Path

from clagentic_loadout.platform_detect import PLATFORM_GITHUB
from clagentic_loadout.transport.github_app_config import (
    GithubAppSlugNotConfiguredError,
    read_configured_callers,
    resolve_github_app_slug,
)

#: GitHub's own documented App-bot login suffix convention (mirrors
#: review.github_backend.resolve_own_login / merge.reviewer_login's
#: identical literal -- one convention, reused, not reinvented here).
_GITHUB_BOT_SUFFIX = "[bot]"

#: Domain GitHub uses for account-noreply commit emails, including App-bot
#: accounts. See module docstring for what this SHAPE does and does not
#: guarantee without the account's own numeric App-bot user id.
_GITHUB_NOREPLY_DOMAIN = "users.noreply.github.com"


def is_recognized_crew_caller(
    caller: str | None,
    platform: str,
    *,
    config_root: str | Path | None = None,
) -> bool:
    """True iff *caller* is a non-empty string present in this deployment's
    declared crew-caller registry (`github_app.callers`) AND *platform* is
    `PLATFORM_GITHUB` -- the only platform this module has any
    slug-to-identity derivation for at all (see module docstring, "FORGEJO
    HAS NO EQUIVALENT DERIVATION").

    PLATFORM-GATED HERE, NOT IN THE RESOLVER (lr-f145d2 follow-up,
    post-review correction): a caller recognized in `github_app.callers`
    is recognized DEPLOYMENT-WIDE -- that registry has no platform
    dimension of its own (a deployment declares a caller a crew caller,
    not a crew caller scoped to one platform). Gating platform HERE means
    a Forgejo push from a recognized caller never enters tier 2 at all
    and falls through to tier 3/4 exactly as it did before this module
    existed -- the pre-existing, unchanged, CORRECT behavior for a
    platform where this module's bug (GitHub App-bot identity derivation)
    cannot even apply, since Forgejo has no App-bot concept to derive.

    An earlier revision of this module gated platform only inside
    `resolve_crew_bot_identity` (which raises
    `CrewBotIdentityNotResolvableError` for a non-GitHub platform) -- that
    made `push.verb`'s tier-2 gate platform-BLIND: it called
    `is_recognized_crew_caller` (True for a recognized caller regardless of
    platform), entered tier 2 unconditionally, and then hit that raise on
    every Forgejo push from a recognized caller, converting into a HARD
    push failure (EXIT_AUTHOR_MISMATCH) something no config change or
    intervention could fix -- Forgejo pushes from a recognized crew caller
    are the OVERWHELMING MAJORITY of this deployment's actual push traffic
    (including this very module's own PRs). That defect is fixed by moving
    the platform condition INTO this gate: `CrewBotIdentityNotResolvableError`
    now means exactly one thing -- GitHub platform, recognized caller,
    still no resolvable identity -- a genuine, actionable configuration gap,
    never a platform mismatch dressed up as one.

    A deployment with no `github_app.callers` key configured at all (the
    external-user default -- see module docstring) always returns False
    here, regardless of *caller* or *platform* -- `read_configured_callers`
    returns `None` for that case (module docstring: "not configured", not
    "empty list"), and this function treats `None` the same as "no
    registry, no recognized caller" rather than guessing a reference
    default. This is the ONLY gate `push.verb` needs to decide whether a
    push may derive bot identity unconditionally and fail closed on
    failure to resolve it, vs. leaving today's ambient-git-config behavior
    untouched for everyone else.
    """
    if platform != PLATFORM_GITHUB:
        return False
    if not caller or not caller.strip():
        return False
    callers = read_configured_callers(config_root=config_root)
    if callers is None:
        return False
    return caller in callers


def resolve_crew_bot_identity(
    caller: str,
    platform: str,
    *,
    config_root: str | Path | None = None,
    provider_verified_app_slug: str | None = None,
) -> tuple[str, str]:
    """Derive (name, email) for *caller* on *platform*.

    SLUG SOURCE PRECEDENCE (lr-43c8d7 ADDITION — inserts a tier ABOVE
    config): *provider_verified_app_slug*, when supplied (non-empty), wins
    over `github_app.slugs.<caller>`/`github_app.slug` config entirely for
    THIS call — it is the BROKER-VERIFIED value a credential-minting
    provider checked against reality at token-mint time (see
    `transport.credential_provider`'s own "PROVIDER-SUPPLIED VERIFIED
    IDENTITY" docstring section), strictly more trustworthy than an
    operator-typed config string naming the SAME fact. `None` or empty
    (the pre-existing default, and ALSO the real state a provider reports
    for a role with no App-slug binding configured — see
    `CommandTokenProvider`'s own docstring) falls through to the config
    resolution below, UNCHANGED from before this task: a deployment with
    no minting-provider identity integration at all sees byte-identical
    behavior.

    Absent that, derives from this deployment's own `github_app.slugs.<caller>`
    entry -- the SAME resolver `review.github_backend.resolve_own_login` and
    `merge.reviewer_login.resolve_reviewer_login` already consult for the
    identical caller/slug question on the read side.

    Only called by `push.verb` for a (caller, platform) pair
    `is_recognized_crew_caller` already confirmed True for -- that gate
    (not this function) owns the platform condition (lr-f145d2 follow-up:
    see `is_recognized_crew_caller`'s own docstring for why the platform
    check moved there). This function itself does not re-check registry
    membership OR platform, so a caller of THIS function is responsible
    for both gates (mirrors `transport.github_app_config`'s own layering,
    where `resolve_github_app_slug` does not know about `callers` either).

    Raises CrewBotIdentityNotResolvableError when:
      - *platform* is not `PLATFORM_GITHUB` -- DEFENSIVE ONLY: this
        function's own contract is GitHub-only (Forgejo has no
        App-bot-slug concept in this contract), and `push.verb` must never
        reach this call with a non-GitHub platform at all (the gate above
        prevents that) -- this raise is a fail-loud invariant check for a
        caller that bypasses the gate, not a code path `push.verb` is
        expected to exercise in normal operation.
      - no *provider_verified_app_slug* was supplied AND no
        `github_app.slugs.<caller>` entry (nor the single-global
        `github_app.slug` fallback) is configured for *caller* -- a
        recognized crew caller with an unresolvable identity must FAIL,
        never silently fall through to ambient git config (task
        requirement). This IS the reachable, actionable failure mode.
    """
    if platform != PLATFORM_GITHUB:
        raise CrewBotIdentityNotResolvableError(
            f"internal invariant violated: resolve_crew_bot_identity was "
            f"called for caller {caller!r} on platform {platform!r}, but "
            f"this function is GitHub-only -- push.verb must gate on "
            f"is_recognized_crew_caller(caller, platform) BEFORE calling "
            f"this function, and that gate should have already excluded "
            f"this platform. This is a defensive check, not an expected "
            f"runtime path."
        )

    if provider_verified_app_slug and provider_verified_app_slug.strip():
        slug = provider_verified_app_slug.strip()
    else:
        try:
            slug = resolve_github_app_slug(caller=caller, config_root=config_root)
        except GithubAppSlugNotConfiguredError as exc:
            raise CrewBotIdentityNotResolvableError(
                f"caller {caller!r} is a recognized crew caller (present in "
                f"this deployment's github_app.callers registry) but has no "
                f"resolvable GitHub App slug: {exc} Refusing to fall back to "
                f"ambient git config for a recognized crew caller -- a "
                f"mis-attributed commit is unrecoverable once merged. Fix: "
                f"add a github_app.slugs.{caller} entry, or set "
                f"{resolve_github_app_slug.__module__}."
                f"GITHUB_APP_SLUG_ENV_VAR, or configure a credential "
                f"provider that supplies a verified app_slug."
            ) from exc

    name = f"{slug}{_GITHUB_BOT_SUFFIX}"
    email = f"{slug}{_GITHUB_BOT_SUFFIX}@{_GITHUB_NOREPLY_DOMAIN}"
    return name, email


class CrewBotIdentityNotResolvableError(ValueError):
    """Raised when a recognized crew caller's bot identity cannot be
    derived. `push.verb` MUST treat this as a fail-closed refusal for a
    recognized crew caller -- never a silent fall-through to whatever
    ambient git identity the pushing process happened to inherit (the
    exact defect class lr-f145d2 exists to close)."""


__all__ = [
    "CrewBotIdentityNotResolvableError",
    "is_recognized_crew_caller",
    "resolve_crew_bot_identity",
]
