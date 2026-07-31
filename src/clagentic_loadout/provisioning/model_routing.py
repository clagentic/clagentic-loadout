"""model_routing.py — the ROLE -> scope-tiered model_chain declaration
(lr-71e3, re-filed from an internal deployment's own tracker item lr-aee8 as
part of the Wave-2 dispatch/envelope-surface move, tome #687 EPIC E).

A role's escalation policy — "which model chain handles this role's work,
given how big the diff under review/build is" — is a REGISTRY/DEPLOYMENT
config decision, never a hardcoded model name in product code (CLAUDE.md
rule 1 and rule 6a: no operator org/host/path hardcodes, no internal
identity). This module resolves that decision the same way roles.py already
resolves ROLE -> verb-set: read a repo-local sectioned config file section,
fall back to a documented reference default when the file/section/role is
absent, and fail closed with resolved values (never a silent guess) when a
scope value can't be routed at all.

Config surface (repo-local, follows the sectioned repo-local config file
convention already established by wait.config, provider_config, and
provisioning.roles — see those modules' docstrings, and `repo_config.py`,
lr-446c35, for the shared path constant/legacy-fallback loader every
section owner in this package now resolves through — default
`.clagentic/loadout/config.yaml`): a top-level `model_routing:` section,
one key per role, each value an ORDERED list of
scope tiers. Each tier names an inclusive upper bound on changed lines
(`max_loc`, or `null` for "no upper bound" — must be last in the list) and
the model_chain (an ordered list of opaque model-id strings; the FIRST entry
is primary, remaining entries are this tier's own fallback order) that
applies at that diff scope:

    model_routing:
      reviewer:
        - max_loc: 10000
          model_chain: ["reviewer-standard"]
        - max_loc: null
          model_chain: ["reviewer-architectural", "reviewer-standard"]
      builder:
        - max_loc: null
          model_chain: ["builder-standard"]

Tiers are evaluated in LIST ORDER; the first tier whose `max_loc` is >= the
requested `changed_lines` wins (a `null` `max_loc` always matches — it is the
open-ended top tier, and config validation requires it to be last so a tier
placed after it could never be reached). This mirrors the reference
deployment's own reviewer gpt-high -> claude-opus escalation shape
(">10k LOC architectural diffs" in this task's own description) without this
module ever naming "gpt-high" or "claude-opus" as a literal — those are
config values a deployment supplies, never a default this module ships.

A repo with NO repo-local config file (or one with no `model_routing:`
section) gets DEFAULT_MODEL_ROUTING — a single-tier, no-escalation reference
mapping covering the same seed roles roles.py defaults for (builder,
reviewer, merger, lead), each pointing at one opaque placeholder model id.
This is A default, not THE routing policy — a deployment that wants real
escalation declares its own `model_routing:` section, which REPLACES the
default mapping entirely for the roles it names (same override-replaces-
default behavior as roles.load_role_verbs and wait.config
load_scoped_test_patterns; a role omitted from a repo's own
`model_routing:` section is simply not routed by this module, rather than
silently inheriting a default entry the repo never asked for — resolve_model_chain
raises for a role absent from either the repo's own declared roles or the
default set, same as roles.resolve_role_verbs).

This module does NOT compute `changed_lines` itself — that is a diff-scope
fact the calling verb/gate already fetches (mirrors merge.diff_scope's own
"caller fetches, this module is a pure policy check" split); passing the
already-known line count keeps this module transport- and credential-free
and easily unit-tested in isolation.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from clagentic_loadout.repo_config import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    resolve_repo_config_path,
)

#: Top-level section key this module owns within the repo-local config
#: file. This module owns `model_routing:`.
CONFIG_SECTION_MODEL_ROUTING = "model_routing"

#: Bare role/model-id token grammar — same shape as provisioning.roles'
#: _TOKEN_RE (alphanumeric plus hyphen/underscore, 1-64 chars, no leading
#: hyphen). Model ids are opaque strings from this module's point of view
#: (never interpreted, never defaulted to a real provider/model literal) —
#: this grammar exists only for clear, unambiguous error reporting.
_TOKEN_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

#: Reference/default role -> scope-tiered model_chain mapping. Single,
#: open-ended (`max_loc: None`) tier per seed role (builder, reviewer,
#: merger, lead — the same set provisioning.roles.DEFAULT_ROLE_VERBS
#: defaults) — i.e. NO escalation by default. Model ids here are opaque
#: placeholders, never a real provider/model literal (rule 1) — a
#: deployment wanting real scope-based escalation (e.g. this task's own
#: ">10k LOC architectural diffs" reference case) declares its own
#: `model_routing:` section, which replaces this entirely for the roles it
#: names.
DEFAULT_MODEL_ROUTING: dict[str, tuple[dict, ...]] = {
    "builder": ({"max_loc": None, "model_chain": ("builder-default",)},),
    "reviewer": ({"max_loc": None, "model_chain": ("reviewer-default",)},),
    "merger": ({"max_loc": None, "model_chain": ("merger-default",)},),
    "lead": ({"max_loc": None, "model_chain": ("lead-default",)},),
}


class InvalidModelRoutingConfigError(ValueError):
    """Raised when a repo's repo-local config `model_routing:` section
    is malformed, or (via `resolve_model_chain`) is asked to route a role or
    a scope value it cannot resolve. Always reports the RESOLVED values
    (path read, role/changed_lines requested, known-good role set) rather
    than a stale guess — conformance rule 4, same discipline as
    provisioning.roles.InvalidRoleConfigError."""


def _read_yaml_mapping(path: Path) -> dict:
    """Read *path* as YAML, returning {} for any missing file rather than
    raising — the repo-local config file is optional, same contract as
    provisioning.roles._read_yaml_mapping."""
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InvalidModelRoutingConfigError(f"{path}: could not be read as YAML: {exc}.") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InvalidModelRoutingConfigError(
            f"{path}: top-level document must be a mapping, got {type(raw).__name__}."
        )
    return raw


def _validate_role_name(role: str, *, source: str) -> None:
    if not _TOKEN_RE.match(role):
        raise InvalidModelRoutingConfigError(
            f"{source}: role name {role!r} is not a bare token (expected "
            f"alphanumeric/hyphen/underscore, 1-64 chars, no leading "
            f"hyphen)."
        )


def _validate_model_chain(role: str, chain: object, *, source: str) -> tuple[str, ...]:
    if not isinstance(chain, list) or not chain:
        raise InvalidModelRoutingConfigError(
            f"{source}: role {role!r} declares a tier with a non-empty "
            f"model_chain list required, got {chain!r}."
        )
    resolved: list[str] = []
    for model_id in chain:
        if not isinstance(model_id, str) or not _TOKEN_RE.match(model_id):
            raise InvalidModelRoutingConfigError(
                f"{source}: role {role!r} declares a malformed model_chain "
                f"entry {model_id!r} (expected a bare token)."
            )
        resolved.append(model_id)
    return tuple(resolved)


def _validate_tier_list(role: str, tiers: object, *, source: str) -> tuple[dict, ...]:
    if not isinstance(tiers, list) or not tiers:
        raise InvalidModelRoutingConfigError(
            f"{source}: role {role!r} must declare a non-empty list of "
            f"scope tiers, got {tiers!r}."
        )
    resolved: list[dict] = []
    for index, tier in enumerate(tiers):
        if not isinstance(tier, dict) or "model_chain" not in tier:
            raise InvalidModelRoutingConfigError(
                f"{source}: role {role!r} tier {index} must be a mapping "
                f"with at least a 'model_chain' key, got {tier!r}."
            )
        max_loc = tier.get("max_loc")
        if max_loc is not None and (not isinstance(max_loc, int) or isinstance(max_loc, bool) or max_loc < 0):
            raise InvalidModelRoutingConfigError(
                f"{source}: role {role!r} tier {index} 'max_loc' must be a "
                f"non-negative integer or null (open-ended), got {max_loc!r}."
            )
        # A `null` max_loc tier is the open-ended top tier and must be LAST —
        # any tier after it could never be reached (resolve_model_chain
        # matches the first tier whose max_loc >= changed_lines, and null
        # always matches).
        if resolved and resolved[-1]["max_loc"] is None:
            raise InvalidModelRoutingConfigError(
                f"{source}: role {role!r} declares a tier after an "
                f"open-ended (max_loc: null) tier — the open-ended tier "
                f"must be last, since it matches every changed_lines value "
                f"and any tier after it could never be reached."
            )
        resolved.append(
            {
                "max_loc": max_loc,
                "model_chain": _validate_model_chain(role, tier["model_chain"], source=source),
            }
        )
    return tuple(resolved)


def load_model_routing(
    repo_root: str | Path | None = None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> dict[str, tuple[dict, ...]]:
    """Resolve the role -> scope-tiered model_chain mapping for a repo.

    Reads `<repo_root>/<config_relative_path>` (default
    `.clagentic/loadout/config.yaml`, falling back to the legacy
    `.loadout/config.yaml` path when only that one exists — see
    `repo_config.resolve_repo_config_path`), expecting a `model_routing:`
    top-level section: a mapping of role name -> non-empty, ordered list of
    scope tiers (see module docstring for shape). When the file, the
    `model_routing:` section, or `repo_root` itself is absent, returns
    DEFAULT_MODEL_ROUTING unchanged — a repo that DOES declare
    `model_routing:` REPLACES the default mapping entirely (mirrors
    provisioning.roles.load_role_verbs and
    wait.config.load_scoped_test_patterns; see module docstring for why this
    is a replace, not a merge).

    Args:
        repo_root: repo root to resolve the config path against. When None,
            DEFAULT_MODEL_ROUTING is returned directly (no file lookup) — a
            caller with no repo context still gets a safe, importable
            default.
        config_relative_path: override the config file's relative path
            (mainly for tests).

    Raises:
        InvalidModelRoutingConfigError: the config file exists but
            `model_routing:` is malformed.
    """
    if repo_root is None:
        return dict(DEFAULT_MODEL_ROUTING)

    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path
    )
    raw = _read_yaml_mapping(config_path)
    routing_section = raw.get(CONFIG_SECTION_MODEL_ROUTING)
    if routing_section is None:
        return dict(DEFAULT_MODEL_ROUTING)
    if not isinstance(routing_section, dict) or not routing_section:
        raise InvalidModelRoutingConfigError(
            f"{config_path}: {CONFIG_SECTION_MODEL_ROUTING!r} section must "
            f"be a non-empty mapping of role name -> scope-tier list, got "
            f"{routing_section!r}."
        )

    resolved: dict[str, tuple[dict, ...]] = {}
    for role, tiers in routing_section.items():
        if not isinstance(role, str):
            raise InvalidModelRoutingConfigError(
                f"{config_path}: role keys must be strings, got {role!r}."
            )
        _validate_role_name(role, source=str(config_path))
        resolved[role] = _validate_tier_list(role, tiers, source=str(config_path))
    return resolved


def resolve_model_chain(
    role: str,
    changed_lines: int,
    *,
    repo_root: str | Path | None = None,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
    model_routing: dict[str, tuple[dict, ...]] | None = None,
) -> tuple[str, ...]:
    """Resolve the model_chain for *role* at the given *changed_lines* diff
    scope.

    Loads the full role -> scope-tiered mapping (via `load_model_routing`,
    unless *model_routing* is supplied directly — mainly for tests/
    composition with an already-loaded mapping), looks up *role*, and
    returns the FIRST tier's model_chain whose `max_loc` is >= *changed_lines*
    (a `null`/None `max_loc` tier always matches — it is this role's
    open-ended top tier). Raises with the RESOLVED role, changed_lines, and
    known-good role set when *role* is not declared anywhere — never a
    silent empty/default chain a caller could mistake for a real routing
    decision.

    Args:
        role: the role whose model_chain is being resolved (a bare role
            token, never an agent name — CLAUDE.md rule 1).
        changed_lines: the diff-scope fact (already fetched by the caller,
            e.g. a merge/review verb's own diff-stat call) driving tier
            selection. Must be a non-negative integer.

    Raises:
        InvalidModelRoutingConfigError: *role* is not declared anywhere, or
            *changed_lines* is negative.
    """
    if not isinstance(changed_lines, int) or isinstance(changed_lines, bool) or changed_lines < 0:
        raise InvalidModelRoutingConfigError(
            f"changed_lines must be a non-negative integer, got {changed_lines!r}."
        )

    mapping = (
        model_routing
        if model_routing is not None
        else load_model_routing(repo_root, config_relative_path=config_relative_path)
    )
    if role not in mapping:
        config_path = (
            Path(repo_root) / config_relative_path if repo_root is not None else "<no repo_root>"
        )
        raise InvalidModelRoutingConfigError(
            f"role {role!r} is not declared (config: {config_path}). Known "
            f"roles: {', '.join(sorted(mapping)) or '<none>'}."
        )

    for tier in mapping[role]:
        max_loc = tier["max_loc"]
        if max_loc is None or changed_lines <= max_loc:
            return tier["model_chain"]

    # Unreachable when config was loaded through load_model_routing (its
    # tier-ordering validation guarantees an open-ended tier exists), but a
    # caller-supplied `model_routing=` mapping bypasses that validation —
    # fail closed with resolved values rather than returning an empty chain.
    config_path = (
        Path(repo_root) / config_relative_path if repo_root is not None else "<no repo_root>"
    )
    raise InvalidModelRoutingConfigError(
        f"role {role!r} (config: {config_path}) has no tier covering "
        f"changed_lines={changed_lines!r} — its declared tiers do not "
        f"include an open-ended (max_loc: null) top tier."
    )


__all__ = [
    "CONFIG_SECTION_MODEL_ROUTING",
    "DEFAULT_CONFIG_RELATIVE_PATH",
    "DEFAULT_MODEL_ROUTING",
    "InvalidModelRoutingConfigError",
    "load_model_routing",
    "resolve_model_chain",
]
