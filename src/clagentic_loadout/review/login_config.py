"""review.login_config — DEPLOYMENT-TIER per-role reviewer login override
(lr-0a03c3).

GAP THIS CLOSES / WHY THIS IS SMALL: `merge.reviewer_login.resolve_reviewer_login`
already derives a required reviewer's expected platform login PLATFORM-AWARE
with NO new config needed for the common case:

  - platform=forgejo: the bare role name IS the login already (no App-bot
    concept on Forgejo in this contract).
  - platform=github: `github_app.slugs.<role>` + `[bot]` — an EXISTING
    deployment-tier config seam (`transport.github_app_config`), which
    `merge.reviewer_login` already consults.

So the REPO-TIER vs. DEPLOYMENT-TIER split for "review" is: the ROLE LIST
(`merge.gate_config.CONFIG_KEY_REQUIRED_REVIEWER_ROLES`, which roles are
required at all) is repo-tier policy; the LOGIN each role resolves to is
ALREADY deployment-tier via `github_app.slugs` on GitHub and needs no
resolution at all on Forgejo. This module exists ONLY for the residual case
neither of those two paths covers: a FORGEJO deployment whose reviewer bot's
actual platform account login is NOT identical to the bare role name (e.g. a
role called "reviewer" whose Forgejo account is literally named
"clagentic-reviewer-bot") — the same "explicit override wins" escape hatch
`merge.verb._parse_required_reviewers`'s own `name:login` CLI form already
provides, given a config-driven (not CLI-flag-driven) home instead.

DEPLOYMENT-TIER, NOT REPO-TIER (lr-0a03c3 design call #1): a login IS an
identity-bearing value — the same class of value `github_app.slugs` is
already deployment-tier-only for, and for the identical lr-0818 reason: a
repo-local, possibly-cloned config file naming which platform account a
reviewer role's verdict is trusted from would let a hostile clone redirect
the verdict-authorship check (`merge.verdict.read_reviewer_verdict`'s
`expected_login` binding) to an attacker-controlled account. This module
therefore reads ONLY the USER-LEVEL `<config_root>/config.yaml` (default
`~/.config/clagentic/loadout/config.yaml`, the SAME file/loader/config-root
convention every other deployment-tier section in this package already
uses) — no repo-root parameter anywhere in this module's public API,
mirroring `push.identity_config` and `transport.github_app_config`.

Config surface: a `review:` top-level section, key `reviewer_logins`, a
per-role map (mirrors `github_app.slugs`'s own per-caller map shape exactly
— same "map of role -> platform-specific value" pattern, different value
meaning):

    review:
      reviewer_logins:
        reviewer: clagentic-reviewer-bot

SECURITY INVARIANT — DO NOT WEAKEN (mirrors `merge.reviewer_login`'s own
docstring verbatim): a resolved override login is TOOL-AUTHORITATIVE,
resolved from deployment config, never from anything a PR comment claims.
This module never reads comment content.

ROLE VOCABULARY: keyed by bare role name (builder/reviewer/security/merger/
lead, or any role name an integrator invents) — never an agent name.
"""

from __future__ import annotations

from pathlib import Path

from clagentic_loadout.transport.provider_config import load_user_config_section

#: Top-level section this module owns within the USER-LEVEL config.yaml,
#: alongside provider_config's `credentials:`, github_app_config's
#: `github_app:`, and push.identity_config's `builder_identity:` sections in
#: the same file.
CONFIG_SECTION_REVIEW = "review"
#: Key within CONFIG_SECTION_REVIEW holding the per-role login-override map.
CONFIG_KEY_REVIEWER_LOGINS = "reviewer_logins"


def load_reviewer_login_override(
    role: str,
    *,
    config_root: str | Path | None = None,
) -> str | None:
    """Resolve a DEPLOYMENT-CONFIGURED login override for *role*, or None.

    Reads the USER-LEVEL `<config_root>/config.yaml`'s
    `review.reviewer_logins.<role>` entry (default
    `~/.config/clagentic/loadout/config.yaml`). NEVER reads a repo-local
    config file — see module docstring for why (lr-0818-class
    identity-escalation reasoning, mirroring `transport.github_app_config`'s
    identical "no repo-local tier" choice).

    Returns `None` when the section, the `reviewer_logins` map, `role`'s own
    entry, `config_root`, or the file itself is missing/unreadable/
    malformed at the `load_user_config_section` layer — a deployment that
    never opted into overriding a role's derived login is unaffected; the
    caller (a `--required-reviewer` resolution call site) falls through to
    `merge.reviewer_login.resolve_reviewer_login`'s existing platform-aware
    derivation exactly as it does today.

    Never raises: this is an ADDITIVE override tier, mirroring
    `transport.github_app_config`'s own degrade-to-"not configured" posture
    for its per-caller `slugs` map — a malformed `reviewer_logins` value
    (not a mapping, or a non-string entry for the requested role) is treated
    as "no override for this role" rather than blocking gate resolution over
    an optional config seam.
    """
    section = load_user_config_section(CONFIG_SECTION_REVIEW, config_root=config_root)
    logins = section.get(CONFIG_KEY_REVIEWER_LOGINS)
    if not isinstance(logins, dict):
        return None
    login = logins.get(role)
    return login if isinstance(login, str) and login.strip() else None


__all__ = [
    "CONFIG_KEY_REVIEWER_LOGINS",
    "CONFIG_SECTION_REVIEW",
    "load_reviewer_login_override",
]
