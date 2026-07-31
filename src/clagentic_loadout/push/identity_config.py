"""push.identity_config — DEPLOYMENT-TIER builder bot identity (lr-0a03c3).

GAP THIS CLOSES: `push.identity.pin_commits_to_bot_identity` re-authors a
branch's commits to a caller-supplied `bot_name`/`bot_email` pair, but that
module deliberately takes both as plain parameters and reads no config
itself (see its own docstring, "WHAT MOVED / WHAT DIDN'T" — identity-source
resolution is explicitly left to a caller). Without THIS module, a repo
migrating onto loadout-native config had no config-driven way to supply that
pair at all — a caller would have to hardcode it, which is exactly the
identity-in-product-code problem loadout rule 1 forbids.

DEPLOYMENT-TIER, NOT REPO-TIER (lr-0a03c3 design call #1): a builder
identity is an EMAIL and a DISPLAY NAME — it names a specific bot/App
account, the same class of value `transport.provider_config`'s
`credentials:` tier and `transport.github_app_config`'s `github_app:` tier
are already deployment-tier-only for (see those modules' docstrings,
lr-0818's security direction: a cloned repo's own COMMITTED config must
never be able to steer a value this identity-sensitive). A repo-local,
possibly-forked/cloned config file naming which bot identity commits get
re-authored to would let a hostile clone silently attribute its own pushes
to a DIFFERENT deployment's builder identity — the same escalation class
lr-0818 already closed for the credentials tier. This module therefore reads
ONLY the USER-LEVEL `<config_root>/config.yaml` (default
`~/.config/clagentic/loadout/config.yaml`, the SAME file/loader/config-root
convention `transport.provider_config.load_user_config_section` already
established) — there is no repo-root parameter anywhere in this module's
public API, mirroring `transport.github_app_config`'s identical "no
repo-local tier, by construction" choice.

Config surface: a `builder_identity:` top-level section in the user-level
file, two required keys once the section is present at all:

    builder_identity:
      name: "clagentic-builder[bot]"
      email: "123456+clagentic-builder[bot]@users.noreply.github.com"

Mirrors the functional-inventory reference's own `builder_identity.name` /
`.email` shape (display name with a `[bot]` suffix convention, GitHub-style
noreply email or any other platform's equivalent) — but that shape is not
copied verbatim as schema; this module validates only that both keys are
present and non-empty strings once the section exists, imposing no format
requirement on either value (a Forgejo-only deployment's bot account may
have neither a `[bot]` suffix nor a noreply-email shape).

ROLE VOCABULARY: this module has no ROLE dimension at all — `push.identity`
re-authors EVERY commit on a branch to ONE identity per invocation (the
caller's own resolved builder identity for that push), not a per-role
mapping. A deployment running multiple distinctly-identified builder roles
configures multiple DEPLOYMENTS (each with its own user-level config root),
not multiple keys within one `builder_identity:` section — this mirrors
`transport.provider_config`'s own single-identity-per-config-root shape for
the credentials tier.

Never raises on absence: a deployment that never opted into commit
re-authoring gets `(None, None)`, and `push.identity.
pin_commits_to_bot_identity`'s own existing `fail_closed_on_missing`
parameter is the caller's decision point for whether that absence is
acceptable — this module does not itself decide that policy.
"""

from __future__ import annotations

from pathlib import Path

from clagentic_loadout.transport.provider_config import load_user_config_section

#: Top-level section this module owns within the USER-LEVEL config.yaml,
#: alongside provider_config's `credentials:` and github_app_config's
#: `github_app:` sections in the same file.
CONFIG_SECTION_BUILDER_IDENTITY = "builder_identity"
#: Key within CONFIG_SECTION_BUILDER_IDENTITY holding the bot's display name.
CONFIG_KEY_NAME = "name"
#: Key within CONFIG_SECTION_BUILDER_IDENTITY holding the bot's commit email.
CONFIG_KEY_EMAIL = "email"


class InvalidBuilderIdentityConfigError(ValueError):
    """Raised when a `builder_identity:` section is present but malformed
    (missing or empty `name`/`email`). Always reports the RESOLVED config
    root and offending value — conformance rule 4."""


def load_builder_identity(
    *,
    config_root: str | Path | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the deployment's builder identity for commit re-authoring.

    Reads the USER-LEVEL `<config_root>/config.yaml`'s `builder_identity:`
    section (default `~/.config/clagentic/loadout/config.yaml` — see
    `transport.provider_config.DEFAULT_USER_CONFIG_ROOT`). NEVER reads a
    repo-local config file — see module docstring for why (lr-0818-class
    identity-escalation reasoning).

    Returns `(None, None)` when the section, `config_root`, or the file
    itself is missing/unreadable/malformed at the `load_user_config_section`
    layer — a deployment that never opted into commit re-authoring is
    unaffected; the caller (`push.identity.pin_commits_to_bot_identity`)
    already has its own documented `fail_closed_on_missing` behavior for
    this case.

    Returns `(name, email)` on a well-formed section.

    Raises:
        InvalidBuilderIdentityConfigError: the section is present but `name`
            or `email` is missing, empty, or not a string.
    """
    section = load_user_config_section(
        CONFIG_SECTION_BUILDER_IDENTITY, config_root=config_root
    )
    if not section:
        return None, None

    root_label = (
        Path(config_root) / "config.yaml"
        if config_root is not None
        else "<default user config root>/config.yaml"
    )

    name = section.get(CONFIG_KEY_NAME)
    email = section.get(CONFIG_KEY_EMAIL)

    for key, value in ((CONFIG_KEY_NAME, name), (CONFIG_KEY_EMAIL, email)):
        if not isinstance(value, str) or not value.strip():
            raise InvalidBuilderIdentityConfigError(
                f"{root_label}: {CONFIG_SECTION_BUILDER_IDENTITY}.{key} must be "
                f"a non-empty string, got {value!r}."
            )

    return name, email


__all__ = [
    "CONFIG_KEY_EMAIL",
    "CONFIG_KEY_NAME",
    "CONFIG_SECTION_BUILDER_IDENTITY",
    "InvalidBuilderIdentityConfigError",
    "load_builder_identity",
]
