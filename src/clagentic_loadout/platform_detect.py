"""platform_detect.py — typed git-remote platform auto-detection.

Ported from the reference implementation's _platform_detect.py (Wave A
slice 2, tome #688). The source copy stays primary until that project's
separate CUT OVER + RETIRE + VERIFY-GONE task for this slice.

Single source of truth for inferring the target platform (Forgejo vs GitHub)
from a git remote URL. Originally duplicated independently across each
verb's own implementation, then extracted to a shared module so every verb
could reuse the identical detection rule instead of hardcoding a Forgejo
default (a recurring class of bug: each standalone implementation drifted,
and one dropped back to a hardcoded default independently of the others).

Rule (lr-1497, unchanged by this port):
    the configured GitHub hostname sentinel (default 'github.com') present
    in the remote URL (case-insensitive) -> github
    anything else                        -> forgejo

This is deliberately a single substring test on the full URL (not just
owner/repo) so a same-named repo cannot be misrouted by owner alone — an
owner/namespace value can exist on BOTH GitHub and a self-hosted Forgejo
instance, so owner is NEVER a valid platform signal (lr-adb4). Handles
https, http, and git@host:owner/repo (scp-style ssh) URL forms because they
all contain the literal GitHub hostname substring.

Callers own their own exit-code/CLI conventions; this module raises
PlatformResolutionError on the fail-closed case (no URL, no explicit
--platform) rather than calling sys.exit directly, so a caller can translate
it to its own EXIT_* constant and message format.

Config-extensible sentinel (task lr-b712, tome #687 §12 hardcode
inventory): the source module hardcoded 'github.com' as the sole detection
signal. That is the ONLY host literal in the *logic* — there was never an
operator-host table to extract (the docstring examples below use neutral
placeholder hosts; see the PR body for the full inventory reasoning).
detect_platform_from_url and resolve_platform both accept an optional
``github_hostname`` override so a deployment can point the sentinel at a
different GitHub-compatible hostname (e.g. a GitHub Enterprise instance)
without editing this module; the default preserves the original behavior
exactly.
"""

from __future__ import annotations

PLATFORM_FORGEJO = "forgejo"
PLATFORM_GITHUB = "github"

#: Default hostname substring used to detect GitHub remotes during
#: auto-detection. Callers may override via the ``github_hostname`` parameter
#: on detect_platform_from_url / resolve_platform (config-extensible
#: sentinel, lr-b712) rather than editing this constant.
DEFAULT_GITHUB_HOSTNAME = "github.com"


class PlatformResolutionError(Exception):
    """
    Raised by resolve_platform() when no explicit platform is given and no
    remote URL is available to auto-detect from (Tier 3, fail-closed).

    Callers catch this and translate it into their own exit-code convention
    — this module does not call sys.exit so it stays exit-code-agnostic and
    testable in isolation.
    """


def detect_platform_from_url(
    remote_url_str: str,
    *,
    github_hostname: str = DEFAULT_GITHUB_HOSTNAME,
) -> str:
    """
    Infer the target platform from a git remote URL (lr-1497).

    Rule: if the URL contains ``github_hostname`` (case-insensitive, default
    'github.com'), return PLATFORM_GITHUB; otherwise return PLATFORM_FORGEJO
    (safe default).

    The full remote URL (not just owner/repo) is the discriminator so that a
    repo with the same name on both platforms cannot be silently misrouted.

    Args:
        remote_url_str: raw git remote URL.
        github_hostname: hostname substring that signals GitHub. Defaults to
            'github.com'; override for a GitHub-compatible host under a
            different name (config-extensible sentinel, lr-b712).

    Examples:
      https://github.com/some-owner/some-repo.git      -> 'github'
      git@github.com:some-owner/some-repo.git           -> 'github'
      http://git-host.example.com:3000/some-owner/some-repo -> 'forgejo'
      http://198.51.100.10:3000/some-owner/some-repo     -> 'forgejo'
    """
    return (
        PLATFORM_GITHUB
        if github_hostname.lower() in remote_url_str.lower()
        else PLATFORM_FORGEJO
    )


def resolve_platform(
    explicit_platform: str | None,
    remote_url_str: str,
    *,
    github_hostname: str = DEFAULT_GITHUB_HOSTNAME,
) -> str:
    """
    Resolve the target platform using a strict precedence order (lr-1497, lr-adb4).

    Tier 1 — explicit_platform wins unconditionally (an explicit override, not
             the default).
    Tier 2 — remote_url_str available -> delegate to detect_platform_from_url.
             The full remote URL is the ONLY valid auto-detect signal;
             ``github_hostname`` -> PLATFORM_GITHUB, any other host ->
             PLATFORM_FORGEJO.
    Tier 3 — no URL obtainable -> raise PlatformResolutionError (fail-closed).
             The caller must supply an explicit platform.

    Owner/repo name is deliberately NOT accepted as an input here: an
    owner/namespace value can exist on both GitHub and a self-hosted
    Forgejo, so it is never a valid platform signal (lr-adb4 removed a
    former owner-name-based shortcut for exactly this reason).

    Args:
        explicit_platform: caller's explicit platform choice (None = auto-detect).
        remote_url_str: raw git remote URL, or "" when genuinely unavailable.
        github_hostname: hostname substring that signals GitHub (config-
            extensible sentinel, lr-b712). Defaults to 'github.com'.

    Returns:
        PLATFORM_GITHUB or PLATFORM_FORGEJO.

    Raises:
        PlatformResolutionError on Tier 3 (no URL, no explicit platform).
    """
    if explicit_platform is not None:
        return explicit_platform

    if remote_url_str:
        return detect_platform_from_url(remote_url_str, github_hostname=github_hostname)

    raise PlatformResolutionError(
        "cannot auto-detect platform: no explicit platform given and no git "
        "remote URL is available. Owner/namespace name is not a valid "
        "platform signal — it can exist on both GitHub and a self-hosted "
        "Forgejo instance."
    )
