"""merge.reviewer_login — platform-aware --required-reviewer login
derivation (lr-2f1378).

MOTIVATION: --required-reviewer's ``name:login`` mapping is
PLATFORM-SPECIFIC — Forgejo uses bare logins (``some-reviewer:some-reviewer``),
GitHub uses App-bot slugs (``some-reviewer:some-app-slug[bot]``). A caller
pre-filling --required-reviewer without knowing the target platform's login
convention can easily get it wrong (observed live: a merge-gate dispatch
retried twice against the wrong login shape before correcting).

THE FIX: when a caller supplies a BARE reviewer name (no ``:login``
suffix), this module derives that reviewer's expected login itself,
platform-aware:

  - platform=forgejo -> the bare name IS the login (``some-reviewer`` ->
    ``some-reviewer``).
  - platform=github  -> ``<resolve_github_app_slug(caller=name)>[bot]``
    (``some-reviewer`` -> ``some-app-slug[bot]``), reusing
    transport.github_app_config.resolve_github_app_slug — the SAME resolver
    review.github_backend.resolve_own_login already uses (lr-b2d1c3), keyed
    by the SAME ``github_app.slugs`` per-caller map lr-46a83a wired. The
    reviewer name is passed as that resolver's ``caller`` argument: a
    required-reviewer's name IS the caller identity key a deployment
    declares its GitHub App slug under.

SECURITY INVARIANT — DO NOT WEAKEN (lr-2b3f): the reviewer-verdict gate
binds reviewer_name -> expected login and verifies the comment's platform
user.login matches (merge.verdict.read_reviewer_verdict's expected_login).
This module keeps the login TOOL-AUTHORITATIVE — resolved from config
(bare name on Forgejo, or the deployment's own github_app.slugs entry on
GitHub) — NEVER from anything a PR comment's author claims. Deriving the
login from comment authorship would BREAK the anti-spoof binding; this
module never reads comment content at all.

The explicit ``name:login`` override form is unaffected by this module —
merge.verb._parse_required_reviewers still parses that form directly and
never calls resolve_reviewer_login for an entry that already carries a
literal login.
"""

from __future__ import annotations

from dataclasses import dataclass

from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.transport.github_app_config import (
    CONFIG_KEY_ROLE_CALLERS,
    CONFIG_KEY_SLUGS,
    CONFIG_SECTION_GITHUB_APP,
    GithubAppSlugNotConfiguredError,
    read_configured_role_callers,
    resolve_github_app_slug,
)

#: GitHub's own documented App-bot-login suffix convention (mirrors
#: review.github_backend.resolve_own_login's identical suffix literal).
_GITHUB_BOT_SUFFIX = "[bot]"


class ReviewerLoginNotConfiguredError(ValueError):
    """Raised when a bare reviewer name is given on platform=github and no
    github_app.slugs.<reviewer_name> (nor a single-global github_app.slug)
    entry is configured to derive its bot login from. FAIL CLOSED — never
    silently skip the reviewer-verdict gate for this reviewer, and never
    fall back to guessing a login from the bare name (that would be a
    Forgejo-shaped guess applied to a GitHub deployment, silently wrong)."""


def resolve_reviewer_login(reviewer_name: str, platform: str) -> str:
    """Derive *reviewer_name*'s expected platform login, platform-aware.

    platform=forgejo: the bare name IS the login (Forgejo has no App-bot
    concept in this contract — the reviewer's own account login is the
    same string a caller already knows).

    platform=github: resolves ``<resolve_github_app_slug(caller=reviewer_name)
    >[bot]`` — the SAME per-caller ``github_app.slugs`` config map
    review.github_backend.resolve_own_login already consults (lr-b2d1c3),
    keyed here by the reviewer's own name (a required reviewer's name IS
    the caller identity a deployment declares its GitHub App slug under).

    Raises ReviewerLoginNotConfiguredError on platform=github when no slug
    is configured for *reviewer_name* (neither a per-caller entry nor the
    single-global fallback) — a fail-closed refusal, never a silent skip or
    a guessed login.

    Raises ValueError for an unrecognized *platform* value (mirrors this
    package's other platform-dispatch guards — see platform_detect.py).
    """
    if platform == PLATFORM_FORGEJO:
        return reviewer_name
    if platform == PLATFORM_GITHUB:
        try:
            slug = resolve_github_app_slug(caller=reviewer_name)
        except GithubAppSlugNotConfiguredError as exc:
            raise ReviewerLoginNotConfiguredError(
                f"no GitHub App slug configured for reviewer {reviewer_name!r} "
                f"-- cannot derive its expected bot login. Pass an explicit "
                f"--required-reviewer {reviewer_name}:<login> instead, or "
                f"configure {exc}"
            ) from exc
        return f"{slug}{_GITHUB_BOT_SUFFIX}"
    raise ValueError(
        f"resolve_reviewer_login: unrecognized platform {platform!r}. Expected "
        f"{PLATFORM_GITHUB!r} or {PLATFORM_FORGEJO!r}."
    )


#: Resolution sources reported by resolve_declared_role.
SOURCE_BARE_NAME = "bare name"
SOURCE_SLUGS = f"{CONFIG_SECTION_GITHUB_APP}.{CONFIG_KEY_SLUGS}"
SOURCE_ROLE_CALLERS = f"{CONFIG_SECTION_GITHUB_APP}.{CONFIG_KEY_ROLE_CALLERS}"


@dataclass(frozen=True)
class DeclaredRoleResolution:
    """How a repo-declared reviewer role resolves on a platform.

    *requirement* is the name the role's verdict is required under: the
    fenced verdict block's own `reviewer` field must equal it, and a
    `--required-reviewer` flag naming the same reviewer is the same
    requirement. It is the role itself unless the deployment maps the role to
    a caller, in which case it is that caller's name.
    """

    role: str
    requirement: str
    login: str
    source: str


def role_caller_mapping_key(role: str) -> str:
    """The config key that maps *role* to a caller (for messages)."""
    return f"{CONFIG_SECTION_GITHUB_APP}.{CONFIG_KEY_ROLE_CALLERS}.{role}"


def resolve_role_via_mapping(
    role: str, unresolved: ReviewerLoginNotConfiguredError
) -> DeclaredRoleResolution:
    """Resolve a declared *role* that `resolve_reviewer_login` could not
    (*unresolved* is its error) through the OPTIONAL
    `github_app.role_callers.<role>` mapping: the mapped caller's
    `github_app.slugs` entry supplies the login, and the verdict is required
    under the caller's name.

    Raises ReviewerLoginNotConfiguredError, naming the mapping key that would
    resolve the role, when no usable mapping exists.
    """
    mapping_key = role_caller_mapping_key(role)
    caller = read_configured_role_callers().get(role)
    if caller is None:
        raise ReviewerLoginNotConfiguredError(
            f"{unresolved} To resolve a declared role whose name is not a caller, map it "
            f"to the caller that posts it with {mapping_key}: <caller>"
        ) from unresolved
    try:
        slug = resolve_github_app_slug(caller=caller)
    except GithubAppSlugNotConfiguredError as slug_exc:
        raise ReviewerLoginNotConfiguredError(
            f"{mapping_key} maps role {role!r} to caller {caller!r}, but no GitHub App "
            f"slug is configured for that caller -- add it under "
            f"{CONFIG_SECTION_GITHUB_APP}.{CONFIG_KEY_SLUGS}.{caller}"
        ) from slug_exc
    return DeclaredRoleResolution(
        role=role,
        requirement=caller,
        login=f"{slug}{_GITHUB_BOT_SUFFIX}",
        source=SOURCE_ROLE_CALLERS,
    )


def resolve_forgejo_declared_role(role: str) -> DeclaredRoleResolution:
    """Resolve a repo-DECLARED reviewer role on Forgejo. Never drops a role.

    A Forgejo login is the caller's account name, so a declared role resolves:

      1. Through the deployment's `role_callers` mapping when it maps the role:
         the mapped caller's name is both the login and the name the role is
         required under.
      2. Otherwise as the bare role, exactly as it was required before the
         mapping existed. `callers` is the harness caller-ID space, not role
         vocabulary, so a role absent from it is NOT evidence that no such
         account exists; dropping it would silently lose a requirement.
    """
    caller = read_configured_role_callers().get(role)
    if caller is not None:
        return DeclaredRoleResolution(
            role=role, requirement=caller, login=caller, source=SOURCE_ROLE_CALLERS
        )
    return DeclaredRoleResolution(role=role, requirement=role, login=role, source=SOURCE_BARE_NAME)


def resolve_declared_role(role: str, platform: str) -> DeclaredRoleResolution:
    """Resolve a repo-DECLARED reviewer role to the login that posts it.

    On GitHub the role is first resolved exactly as `resolve_reviewer_login`
    resolves a `--required-reviewer` name, and any role that resolves that way
    is returned unchanged. Only when that fails (no slug for the role) is the
    OPTIONAL role mapping consulted (`resolve_role_via_mapping`). A deployment
    with no mapping therefore behaves exactly as it did before the mapping
    existed. Forgejo follows `resolve_forgejo_declared_role`.

    Raises ReviewerLoginNotConfiguredError when the role resolves neither way.
    """
    if platform == PLATFORM_FORGEJO:
        return resolve_forgejo_declared_role(role)
    try:
        login = resolve_reviewer_login(role, platform)
    except ReviewerLoginNotConfiguredError as exc:
        return resolve_role_via_mapping(role, exc)
    return DeclaredRoleResolution(role=role, requirement=role, login=login, source=SOURCE_SLUGS)


def renamed_declared_role(role: str, platform: str) -> DeclaredRoleResolution | None:
    """`resolve_declared_role`, reporting None when the role stays required
    under its own name (the resolution's requirement is the role itself).

    For a caller that only needs to act on a role that is renamed: it shares the
    one resolution path rather than re-deriving it, so the two cannot drift.

    Raises ReviewerLoginNotConfiguredError when the role resolves to no login.
    """
    resolution = resolve_declared_role(role, platform)
    return None if resolution.requirement == role else resolution


__all__ = [
    "SOURCE_BARE_NAME",
    "SOURCE_ROLE_CALLERS",
    "SOURCE_SLUGS",
    "DeclaredRoleResolution",
    "ReviewerLoginNotConfiguredError",
    "renamed_declared_role",
    "resolve_declared_role",
    "resolve_forgejo_declared_role",
    "resolve_role_via_mapping",
    "resolve_reviewer_login",
    "role_caller_mapping_key",
]
