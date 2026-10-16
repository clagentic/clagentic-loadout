"""review.profile_settings — the deployment's own repo profile settings.

Two settings belong to the deployment that reviews a repository, not to the
repository, so they live in the USER-LEVEL config and are never read from the
repo::

    review:
      repo_profile:
        require: true             # a repository with no profile gets a finding
        deny:                     # values a released-tool tree must not carry
          names: [some-agent-name]
          hosts: [host.example.test]
          users: [some-username]

``deny``: the names a released tool must not carry (agent names, hosts, domains,
usernames) are specific to the deployment, and the tool being reviewed is
exactly the place they must not be written down. The list is merged with
whatever a repository's profile adds (review.repo_profile), and a repository
cannot remove an entry from it.

``require``: off by default, so a deployment that configures nothing sees the
review's output exactly as it was. When on, a repository whose base revision has
no profile gets one advisory finding saying so. A profile that is present but
invalid is always reported, because the file is there and is being ignored.

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
CONFIG_KEY_REQUIRE = "require"


@dataclass(frozen=True)
class ProfileSettings:
    deny: ProfileDeny = field(default_factory=ProfileDeny)
    require: bool = False


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
    unknown = sorted(str(k) for k in block if k not in (CONFIG_KEY_DENY, CONFIG_KEY_REQUIRE))
    if unknown:
        raise ProfileFormatError("unknown key: " + ", ".join(f"{where}.{key}" for key in unknown))
    require = block.get(CONFIG_KEY_REQUIRE, False)
    if not isinstance(require, bool):
        raise ProfileFormatError(f"{where}.{CONFIG_KEY_REQUIRE} must be true or false, got {require!r}")
    return ProfileSettings(
        deny=parse_deny(block.get(CONFIG_KEY_DENY), f"{where}.{CONFIG_KEY_DENY}"), require=require
    )


__all__ = ["ProfileSettings", "load_profile_settings"]
