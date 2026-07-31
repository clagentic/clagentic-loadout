"""github_app_config.py — GitHub App bot-login slug resolution (lr-d31e).

_resolve_own_login (review.github_backend) MUST derive a GitHub App
installation token's own bot login WITHOUT calling GET /app: that endpoint is
JWT-only (Authorization: Bearer <RS256 app JWT>), and this backend only ever
holds an installation token (Authorization: token <installation-token>) — a
live /app call is a deterministic 401 for this credential type, never a
usable identity source (see github_backend's module docstring, "the
JWT-only-GET-/app defect", root-caused per lr-e23a). Instead, the bot login
is resolved DETERMINISTICALLY from deployment config as
"<app-slug>[bot]" — GitHub's own documented App-bot-login convention — using
the SAME env-var-then-user-level-config-file precedence
transport.provider_config already established for per-platform credential
selection (lr-af6e/lr-0818), so a deployment configures the app slug the same
way it already configures a token provider, rather than this module inventing
a third config shape.

Selection precedence, highest first:
  1. Env var: CLAGENTIC_LOADOUT_GITHUB_APP_SLUG. Wins over EVERY other
     source, including a per-caller config entry — a single env override
     still applies uniformly regardless of which caller/role is asking
     (lr-d72d: a deployment overriding for one-off debugging does not have
     to also know or repeat the caller string).
  2. Config file, PER-CALLER map (lr-d72d): USER-LEVEL config,
     <config_root>/config.yaml (default
     ~/.config/clagentic/loadout/config.yaml — the same
     transport.provider_config.DEFAULT_USER_CONFIG_ROOT convention), section
     `github_app:`, key `slugs`, a mapping of `<caller>: <slug>`. Consulted
     only when the caller performing the lookup passes a non-empty `caller`
     string that matches a key in this map — a single global slug (tier 3
     below) cannot serve a role-scoped deployment that runs multiple GitHub
     Apps (builder/reviewer/security/merger = different slugs, each needing
     its own bot-login identity for the verify-readback check).
  3. Config file, single global slug (fallback, pre-lr-d72d shape):
     same file, section `github_app:`, key `slug`. Used when no `caller` was
     supplied, or the supplied `caller` has no entry in `slugs`.

     NEVER repo-local, for the same reason provider_config's credentials tier
     is never repo-local (lr-0818): the app slug determines which bot login a
     verify readback trusts as "the caller's own identity" — a cloned repo's
     committed config choosing that value would let a hostile repo redirect
     the verify check to attacker-controlled criteria. This module accepts
     no repo-root parameter at all, so there is no code path that could even
     be tempted to add one later without a deliberate signature change.
  4. Unconfigured: raises GithubAppSlugNotConfiguredError. Callers (
     github_backend.resolve_own_login) MUST fail closed on this — the trade-
     off named in lr-d31e's PR: this project deliberately does NOT restore a
     live-lookup fallback (that lookup can never work for an installation
     token, which is the entire defect this module exists to close), so an
     unconfigured slug on a 403'd /user is a genuine configuration gap, not a
     transient failure, and is reported as such (which env/config resolves
     it) rather than retried or silently guessed.

Trade-off named for the PR: this couples review.github_backend's App-token
identity resolution to deployment config rather than a live API call. That is
correct here because the API call this replaces could never succeed for this
credential type — there is no live-lookup alternative to give up. A
deployment whose App slug rotates must update this config; that is the
existing failure mode's mirror image (a stale JWT-only lookup silently 401ed
forever) traded for a loud, actionable config error the first time the
(mis)configuration is exercised.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from clagentic_loadout.transport.provider_config import DEFAULT_USER_CONFIG_ROOT, USER_CONFIG_FILENAME

#: Env var naming the GitHub App's slug (CLI-NAMING-STANDARD.md:
#: CLAGENTIC_LOADOUT_* prefix). Highest-precedence source.
GITHUB_APP_SLUG_ENV_VAR = "CLAGENTIC_LOADOUT_GITHUB_APP_SLUG"

#: Top-level section this module owns within the user-level config.yaml,
#: alongside provider_config's `credentials:` section in the same file.
CONFIG_SECTION_GITHUB_APP = "github_app"
#: Key within CONFIG_SECTION_GITHUB_APP holding the single global app slug
#: string (fallback tier, pre-lr-d72d shape — still the whole story for a
#: deployment that only ever runs one GitHub App).
CONFIG_KEY_SLUG = "slug"
#: Key within CONFIG_SECTION_GITHUB_APP holding the OPTIONAL per-caller slug
#: map (lr-d72d): {<caller>: <slug>}. A role-scoped deployment running
#: multiple GitHub Apps (builder/reviewer/security/merger) sets one entry per
#: caller here instead of relying on the single CONFIG_KEY_SLUG value (which
#: can only ever be correct for one of them) or a per-invocation env
#: override.
CONFIG_KEY_SLUGS = "slugs"
#: Key within CONFIG_SECTION_GITHUB_APP holding the OPTIONAL declared-caller
#: registry (lr-46a83a): a list of the exact strings a deployment's own
#: harness passes as `--caller` at runtime. This is the CALLER identity
#: key-space `slugs` above is actually keyed by -- see this module's
#: docstring, "*caller* is deployment-defined": loadout itself never
#: constrains *caller* to a bare role name (review.contract's own "ROLE, NOT
#: AGENT NAME" framing describes loadout's INTENT for what a deployment
#: SHOULD pass, not an enforced grammar). A deployment whose own harness
#: dispatches token minting by a spawned-process/worker IDENTIFIER rather
#: than a bare role name (builder/reviewer/merger/lead) declares that
#: identifier set here so doctor.checks.check_github_app_slugs_coverage
#: validates `slugs` coverage against the SAME key-space the deployment's
#: own token-helper/harness actually uses -- never against
#: provisioning.roles' verb-authorization taxonomy, which answers a
#: completely different question ("which verbs may this role invoke") and
#: has no defined relationship to the credential `caller` identity space at
#: all (the lr-e41f-adjacent conflation this key exists to close,
#: root-caused per lr-46a83a). Omitting this key falls back to the
#: role-taxonomy reference default (see doctor.checks) -- this is a
#: REFERENCE default, not a claim that role names and caller strings are
#: the same key-space for every deployment.
CONFIG_KEY_CALLERS = "callers"


class GithubAppSlugNotConfiguredError(ValueError):
    """Raised when a GitHub App installation token needs its bot login
    resolved (GET /user 403'd, the documented installation-token response)
    but no app slug is configured in either the env var or the user-level
    config file. Callers translate this to their own fail-closed error
    (github_backend.resolve_own_login raises ReviewVerifyError naming this
    condition and both config seams that resolve it) — this module never
    calls sys.exit and never guesses a slug from the caller/role name."""


def _read_yaml_mapping(path: Path) -> dict:
    """Read *path* as YAML, returning {} for any missing/unreadable/
    non-mapping file — mirrors provider_config._read_yaml_mapping's treatment
    of an optional config file (absent/malformed is "no signal", not an
    error) so both config sections in the same file degrade identically."""
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _read_github_app_section(config_root: str | Path | None) -> dict:
    root = Path(config_root) if config_root is not None else DEFAULT_USER_CONFIG_ROOT
    section = _read_yaml_mapping(root / USER_CONFIG_FILENAME).get(CONFIG_SECTION_GITHUB_APP)
    return section if isinstance(section, dict) else {}


def _config_file_slug(config_root: str | Path | None) -> str | None:
    """The single global `github_app.slug` value (fallback tier)."""
    slug = _read_github_app_section(config_root).get(CONFIG_KEY_SLUG)
    return slug if isinstance(slug, str) and slug.strip() else None


def _config_file_caller_slug(config_root: str | Path | None, caller: str | None) -> str | None:
    """The per-caller `github_app.slugs.<caller>` value (lr-d72d), or None
    when *caller* is empty/unset or has no matching entry in the map. A
    `slugs` value that is present but not a mapping (malformed config) is
    treated as "no signal" here — mirrors `_read_yaml_mapping`'s own
    degrade-to-default contract for the rest of this file."""
    if not caller or not caller.strip():
        return None
    slugs = _read_github_app_section(config_root).get(CONFIG_KEY_SLUGS)
    if not isinstance(slugs, dict):
        return None
    slug = slugs.get(caller)
    return slug if isinstance(slug, str) and slug.strip() else None


def read_configured_slugs(config_root: str | Path | None = None) -> dict[str, str]:
    """Return the raw `github_app.slugs` mapping (caller -> slug) configured
    at the user level, or `{}` when no per-caller map is configured at all
    (only the single global `slug` fallback, or nothing).

    Read-only accessor for a caller that needs to inspect the CONFIGURED
    SHAPE of the per-caller slug map itself (doctor.checks.
    check_github_app_slugs_coverage's coverage check) rather than resolve
    one caller's own slug via `resolve_github_app_slug`'s full precedence
    chain (which also consults the env-var override and the single-global
    fallback — neither of which is meaningful for a coverage check asking
    "which callers does the CONFIG FILE'S per-caller map name"). A `slugs`
    value that is present but not a mapping (malformed config) is treated as
    "not configured" here — mirrors every other read in this module's
    degrade-to-default contract for a malformed section.
    """
    slugs = _read_github_app_section(config_root).get(CONFIG_KEY_SLUGS)
    if not isinstance(slugs, dict):
        return {}
    return {
        caller: slug
        for caller, slug in slugs.items()
        if isinstance(caller, str) and isinstance(slug, str) and slug.strip()
    }


def read_configured_callers(config_root: str | Path | None = None) -> list[str] | None:
    """Return the deployment-declared caller registry (`github_app.callers`,
    lr-46a83a) as a list of caller strings, or `None` when the key is absent
    or malformed (not a list, or a list with any non-string/blank entry --
    treated as "not configured" here, same degrade-to-default posture as
    every other read in this module, rather than a partial/best-effort
    list).

    This is the declared CALLER key-space `github_app.slugs` is actually
    keyed by for THIS deployment -- see CONFIG_KEY_CALLERS's own docstring
    for why this is a separate declaration from provisioning.roles' verb
    taxonomy. `None` (vs. an empty list, which is a deliberate "no callers
    expected" declaration and returned as `[]`) signals doctor.checks.
    check_github_app_slugs_coverage should fall back to its own reference
    default instead.
    """
    raw = _read_github_app_section(config_root).get(CONFIG_KEY_CALLERS)
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    callers: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            return None
        callers.append(entry)
    return callers


def resolve_github_app_slug(
    *,
    caller: str | None = None,
    config_root: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Resolve the configured GitHub App slug (see module docstring for the
    full precedence and the "why no repo-local tier" rationale):

      1. Env var CLAGENTIC_LOADOUT_GITHUB_APP_SLUG — wins regardless of
         *caller*.
      2. The user-level config.yaml's `github_app.slugs.<caller>` entry
         (lr-d72d) — consulted only when *caller* is a non-empty string
         that has a matching entry.
      3. The user-level config.yaml's `github_app.slug` single global value
         (fallback, pre-lr-d72d shape).

    Args:
        caller: OPTIONAL role/caller string (e.g. "reviewer", "security") —
            the same caller identity a verb's --caller flag already
            resolves. When supplied and present in `github_app.slugs`, its
            per-caller slug is used ahead of the single global `slug` value.
            A deployment that never sets `slugs` (or omits *caller*
            entirely) gets byte-identical behavior to before lr-d72d.
        config_root: override the user-level config root (mainly for
            tests). Defaults to provider_config.DEFAULT_USER_CONFIG_ROOT
            (~/.config/clagentic/loadout).
        env: override the environment mapping (mainly for tests). Defaults
            to os.environ.

    Raises:
        GithubAppSlugNotConfiguredError: no source names a non-empty slug.
    """
    active_env = env if env is not None else os.environ
    env_slug = active_env.get(GITHUB_APP_SLUG_ENV_VAR)
    if env_slug and env_slug.strip():
        return env_slug.strip()

    caller_slug = _config_file_caller_slug(config_root, caller)
    if caller_slug:
        return caller_slug

    config_slug = _config_file_slug(config_root)
    if config_slug:
        return config_slug

    effective_root = config_root if config_root is not None else DEFAULT_USER_CONFIG_ROOT
    caller_hint = f" (caller={caller!r})" if caller and caller.strip() else ""
    raise GithubAppSlugNotConfiguredError(
        f"GitHub App slug is not configured{caller_hint}: neither "
        f"{GITHUB_APP_SLUG_ENV_VAR} nor the user-level config file's "
        f"{CONFIG_SECTION_GITHUB_APP}.{CONFIG_KEY_SLUGS}.<caller> or "
        f"{CONFIG_SECTION_GITHUB_APP}.{CONFIG_KEY_SLUG} key names one. Set "
        f"{GITHUB_APP_SLUG_ENV_VAR}=<your-app-slug>, or add a "
        f"'{CONFIG_SECTION_GITHUB_APP}: {{{CONFIG_KEY_SLUG}: <your-app-slug>}}' "
        f"(or a per-caller '{CONFIG_KEY_SLUGS}:' mapping) section to "
        f"{effective_root}/{USER_CONFIG_FILENAME}."
    )


__all__ = [
    "CONFIG_KEY_CALLERS",
    "CONFIG_KEY_SLUG",
    "CONFIG_KEY_SLUGS",
    "CONFIG_SECTION_GITHUB_APP",
    "GITHUB_APP_SLUG_ENV_VAR",
    "USER_CONFIG_FILENAME",
    "GithubAppSlugNotConfiguredError",
    "read_configured_callers",
    "read_configured_slugs",
    "resolve_github_app_slug",
]
