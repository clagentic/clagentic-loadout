"""review.profile_settings — the deployment's own repo profile settings.

The setting belongs to the deployment that reviews a repository, not to the
repository, so it lives in the USER-LEVEL config and is never read from the
repo::

    review:
      repo_profile:
        deny:                     # values a released-tool tree must not carry
          names: [some-agent-name]
          hosts: [host.example.test]
          users: [some-username]

``deny``: the names a released tool must not carry (agent names, hosts, domains,
usernames) are specific to the deployment, and the tool being reviewed is
exactly the place they must not be written down. The list is merged with
whatever a repository's profile adds (review.repo_profile), and a repository
cannot remove an entry from it.

A repository whose base revision has no profile always gets one advisory
finding saying so, and so does a profile that is present but invalid; neither
is configurable.

This module learns no name of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from clagentic_loadout.review.repo_profile import ProfileDeny, ProfileFormatError, parse_deny
from clagentic_loadout.transport.provider_config import load_user_config_section

CONFIG_SECTION_REVIEW = "review"
CONFIG_KEY_REPO_PROFILE = "repo_profile"
CONFIG_KEY_DENY = "deny"


@dataclass(frozen=True)
class ProfileSettings:
    deny: ProfileDeny = field(default_factory=ProfileDeny)


def load_profile_settings(config_root: str | Path | None = None) -> ProfileSettings:
    """The user-level settings, the defaults when none are configured. Raises
    ProfileFormatError when ``review.repo_profile`` is present but malformed."""
    section = load_user_config_section(CONFIG_SECTION_REVIEW, config_root=config_root)
    block = section.get(CONFIG_KEY_REPO_PROFILE) if isinstance(section, dict) else None
    if block is None:
        return ProfileSettings()
    where = f"{CONFIG_SECTION_REVIEW}.{CONFIG_KEY_REPO_PROFILE}"
    if not isinstance(block, dict):
        raise ProfileFormatError(f"{where} must be a mapping")
    unknown = sorted(str(k) for k in block if k != CONFIG_KEY_DENY)
    if unknown:
        raise ProfileFormatError("unknown key: " + ", ".join(f"{where}.{key}" for key in unknown))
    return ProfileSettings(deny=parse_deny(block.get(CONFIG_KEY_DENY), f"{where}.{CONFIG_KEY_DENY}"))


__all__ = ["ProfileSettings", "load_profile_settings"]
