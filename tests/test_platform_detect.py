"""test_platform_detect.py — unit tests for clagentic_loadout.platform_detect
(ported lr-b712 from an internal deployment's own platform-autodetect test
module, Wave A slice 2, tome #688).

The original source test module also covered that deployment's own push/
merge-tool call-site wiring (auto-detect on push, mismatch-hint on auth
failure); that wiring is deployment-specific CLI behavior and stays
there — it moves in the Wave B verb-port slice, not here. This module ports
ONLY the shared-detector coverage (sections 1 and 2 of the source file),
since detect_platform_from_url / resolve_platform are the entire surface
this slice extracts.

Historical defect anchors (lr-pinned, CLAUDE.md rule 6 — kept as references,
not identity):
  lr-134e — the reference deployment's own push tool previously hardcoded
    --platform default to 'forgejo', silently mis-routing GitHub pushes
    through the Forgejo credential path.
  lr-1497 — original detection rule (the reference deployment's own merge
    tool).
  lr-adb4 — hardened: owner/namespace name removed as a platform signal,
    because the same owner can exist on both platforms.

Coverage:
  1. detect_platform_from_url: GitHub-hostname https/ssh -> github, a
     neutral non-GitHub host -> forgejo (the core safe-default contract),
     case-insensitive, and the config-extensible ``github_hostname``
     override.
  2. resolve_platform: 3-tier precedence, fail-closed
     (PlatformResolutionError) when no URL and no explicit platform.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.platform_detect import (
    DEFAULT_GITHUB_HOSTNAME,
    PLATFORM_FORGEJO,
    PLATFORM_GITHUB,
    PlatformResolutionError,
    detect_platform_from_url,
    resolve_platform,
)

# Neutral placeholder hosts (task lr-b712) — the source module's docstring
# examples referenced real operator hosts; none of that appears here.
GIT_HOST_URL = "http://git-host.example.com:3000/some-owner/some-repo.git"


class TestDetectPlatformFromUrl:
    """Single source of truth for platform detection."""

    def test_github_https_url_detects_github(self):
        assert (
            detect_platform_from_url("https://github.com/some-owner/some-repo.git")
            == PLATFORM_GITHUB
        )

    def test_github_ssh_scp_style_url_detects_github(self):
        """git@github.com:owner/repo.git (scp-style ssh) must resolve to github."""
        assert (
            detect_platform_from_url("git@github.com:some-owner/some-repo.git")
            == PLATFORM_GITHUB
        )

    def test_neutral_non_github_host_detects_forgejo(self):
        """Core safe-default contract: any non-GitHub host -> forgejo."""
        assert detect_platform_from_url(GIT_HOST_URL) == PLATFORM_FORGEJO

    def test_case_insensitive_github_detection(self):
        assert (
            detect_platform_from_url("https://GITHUB.COM/some-owner/some-repo.git")
            == PLATFORM_GITHUB
        )

    def test_default_github_hostname_constant_matches_documented_default(self):
        assert DEFAULT_GITHUB_HOSTNAME == "github.com"

    def test_github_hostname_override_detects_alternate_host_as_github(self):
        """Config-extensible sentinel (lr-b712): a caller-supplied
        github_hostname override is honored instead of the default."""
        assert (
            detect_platform_from_url(
                "https://ghe.example.org/some-owner/some-repo.git",
                github_hostname="ghe.example.org",
            )
            == PLATFORM_GITHUB
        )

    def test_github_hostname_override_means_default_host_no_longer_matches(self):
        """When overridden, the default 'github.com' substring is no longer
        the active sentinel -- a github.com URL falls through to forgejo."""
        assert (
            detect_platform_from_url(
                "https://github.com/some-owner/some-repo.git",
                github_hostname="ghe.example.org",
            )
            == PLATFORM_FORGEJO
        )


class TestResolvePlatform:
    def test_explicit_platform_wins_over_github_url(self):
        assert (
            resolve_platform("forgejo", "https://github.com/some-owner/some-repo.git")
            == PLATFORM_FORGEJO
        )

    def test_explicit_platform_wins_over_forgejo_url(self):
        assert resolve_platform("github", GIT_HOST_URL) == PLATFORM_GITHUB

    def test_url_used_when_no_explicit_platform_github(self):
        assert (
            resolve_platform(None, "https://github.com/some-owner/some-repo.git")
            == PLATFORM_GITHUB
        )

    def test_url_used_when_no_explicit_platform_forgejo(self):
        assert resolve_platform(None, GIT_HOST_URL) == PLATFORM_FORGEJO

    def test_no_url_no_explicit_platform_raises_fail_closed(self):
        """Tier 3: no URL, no explicit platform -> PlatformResolutionError, never a silent default."""
        with pytest.raises(PlatformResolutionError):
            resolve_platform(None, "")

    def test_github_hostname_override_propagates_through_resolve(self):
        """Config-extensible sentinel applies at the resolve_platform layer too."""
        assert (
            resolve_platform(
                None,
                "https://ghe.example.org/some-owner/some-repo.git",
                github_hostname="ghe.example.org",
            )
            == PLATFORM_GITHUB
        )
