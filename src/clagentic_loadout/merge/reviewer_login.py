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

from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.transport.github_app_config import (
    GithubAppSlugNotConfiguredError,
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


__all__ = [
    "ReviewerLoginNotConfiguredError",
    "resolve_reviewer_login",
]
