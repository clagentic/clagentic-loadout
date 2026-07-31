"""roles.py — the ROLE -> verb-set declaration (lr-4e04).

loadout ships 9 console verbs but nothing that tells a consuming agent's
harness which of them a given ROLE is allowed to invoke. This module is
that declaration: a mapping from a bare role name (builder, reviewer,
merger, lead, ... — NEVER an agent name, CLAUDE.md rule 1) to the set of
loadout verb labels that role invokes.

Config surface (repo-local, follows the sectioned repo-local config file
convention already established by wait.config and provider_config — see
those modules' docstrings, and `repo_config.py`, lr-446c35, for the shared
path constant/legacy-fallback loader every section owner in this package
now resolves through — default `.clagentic/loadout/config.yaml`): a
top-level `roles:` section, one key per role, each value a list of verb
labels.

    roles:
      builder:
        - push
      reviewer:
        - git-host-api
        - review-post
        - stage-body
      merger:
        - merge
        - push
        - release-dispatch
        - release-detect
      lead:
        - git-host-api

A repo with NO repo-local config file (or one with no `roles:` section)
gets DEFAULT_ROLE_VERBS — a reference mapping for the seed roles named in
lr-4e04, not the only possible role taxonomy. A repo that declares its own
`roles:` section REPLACES the default mapping entirely (no merge with the
default — a role omitted from a repo's own config is simply not
provisioned, rather than silently inheriting a stale entry from a set of
role names the repo may not even use). This mirrors wait.config's own
override-replaces-default behavior (`scoped_test_patterns`).

Verb labels are the same strings the umbrella CLI (`clagentic_loadout.cli`)
and docs/verbs.md already use for each verb (e.g. "push", "review-post",
"release-dispatch") — this module validates against that same known set so
a typo or a since-renamed verb is caught here, at config-load time, with a
resolved-values error (conformance rule 4), rather than surfacing later as
a silently-empty allowlist fragment.
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
#: file. This module owns `roles:`.
CONFIG_SECTION_ROLES = "roles"

#: Every verb label a role may declare, mirroring the umbrella CLI's own
#: verb registry (clagentic_loadout.cli._SIMPLE_VERBS / _GROUPED_VERBS,
#: flattened to "parent-child" labels) plus the wait primitives, which are
#: invoked directly by name rather than through the umbrella today (see
#: docs/verbs.md). Kept as a local, explicit tuple rather than importing
#: clagentic_loadout.cli's private registries — this module's contract is
#: "which verb LABELS exist," not "how the umbrella dispatches them," and a
#: hardcoded list here does not create an import-order or circular-import
#: coupling with the umbrella CLI module.
KNOWN_VERBS: tuple[str, ...] = (
    "push",
    "review-post",
    "merge",
    "git-host-api",
    "stage-body",
    "release-dispatch",
    "release-detect",
    "poll-wait",
    "scoped-test-wait",
    "doctor",
)

#: Reference/default role -> verb-set mapping for the seed roles named in
#: lr-4e04. This is A default, not THE role taxonomy — a repo overrides it
#: entirely via its own repo-local config `roles:` section (see module
#: docstring). Values are tuples (immutable, safe to expose as a module
#: constant that callers might otherwise be tempted to mutate in place).
DEFAULT_ROLE_VERBS: dict[str, tuple[str, ...]] = {
    "builder": ("push",),
    "reviewer": ("git-host-api", "review-post", "stage-body"),
    "merger": ("merge", "push", "release-dispatch", "release-detect"),
    "lead": ("git-host-api",),
}

#: Bare role/verb-label token grammar. Deliberately loose enough to accept
#: "release-dispatch"-style hyphenated verb labels: alphanumeric plus
#: hyphen/underscore, 1-64 chars, no leading hyphen. This is a SEPARATE
#: grammar from credential_provider._SAFE_ROLE_RE (which additionally
#: forbids a leading hyphen for argv-injection-safety reasons specific to
#: that seam) — this module's role/verb names are never substituted into an
#: exec'd argv, so that stricter concern does not apply here, but a bare,
#: readable-token shape is still enforced for clear error reporting.
_TOKEN_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


class InvalidRoleConfigError(ValueError):
    """Raised when a repo's repo-local config `roles:` section is
    malformed, names an unknown verb, or (via `resolve_role_verbs`) is asked
    to resolve a role that is not declared anywhere. Always reports the
    RESOLVED values (path read, role/verb requested, known-good set) rather
    than a stale guess — conformance rule 4."""


def _read_yaml_mapping(path: Path) -> dict:
    """Read *path* as YAML, returning {} for any missing file rather than
    raising — the repo-local config file is optional. A file that EXISTS but is
    unreadable/malformed YAML raises: unlike provider_config's credentials
    tier (a security-sensitive silent-degrade-to-default is the right call
    there), an operator who wrote a `roles:` section that fails to parse
    wants to know at config-load time, not have their provisioning silently
    fall back to a default they never asked for."""
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InvalidRoleConfigError(f"{path}: could not be read as YAML: {exc}.") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InvalidRoleConfigError(
            f"{path}: top-level document must be a mapping, got {type(raw).__name__}."
        )
    return raw


def _validate_role_name(role: str, *, source: str) -> None:
    if not _TOKEN_RE.match(role):
        raise InvalidRoleConfigError(
            f"{source}: role name {role!r} is not a bare token (expected "
            f"alphanumeric/hyphen/underscore, 1-64 chars, no leading "
            f"hyphen)."
        )


def _validate_verb_list(role: str, verbs: object, *, source: str) -> tuple[str, ...]:
    if not isinstance(verbs, list) or not verbs:
        raise InvalidRoleConfigError(
            f"{source}: role {role!r} must declare a non-empty list of verb "
            f"labels, got {verbs!r}."
        )
    resolved: list[str] = []
    for verb in verbs:
        if not isinstance(verb, str) or not _TOKEN_RE.match(verb):
            raise InvalidRoleConfigError(
                f"{source}: role {role!r} declares a malformed verb label "
                f"{verb!r} (expected a bare token)."
            )
        if verb not in KNOWN_VERBS:
            raise InvalidRoleConfigError(
                f"{source}: role {role!r} declares unknown verb {verb!r}. "
                f"Known verbs: {', '.join(KNOWN_VERBS)}."
            )
        resolved.append(verb)
    return tuple(resolved)


def load_role_verbs(
    repo_root: str | Path | None = None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> dict[str, tuple[str, ...]]:
    """Resolve the role -> verb-set mapping for a repo.

    Reads `<repo_root>/<config_relative_path>` (default
    `.clagentic/loadout/config.yaml`, falling back to the legacy
    `.loadout/config.yaml` path when only that one exists — see
    `repo_config.resolve_repo_config_path`), expecting a `roles:` top-level
    section: a mapping of role name -> non-empty list of verb labels. When
    the file, the `roles:` section, or `repo_root` itself is absent, returns
    DEFAULT_ROLE_VERBS unchanged (the seed-role reference mapping) — a repo
    that DOES declare `roles:` REPLACES the default mapping entirely (see
    module docstring for why this is a replace, not a merge).

    Args:
        repo_root: repo root to resolve the config path against. When None,
            DEFAULT_ROLE_VERBS is returned directly (no file lookup) — a
            caller with no repo context still gets a safe, importable
            default, matching wait.config's own no-repo-root contract.
        config_relative_path: override the config file's relative path
            (mainly for tests).

    Raises:
        InvalidRoleConfigError: the config file exists but `roles:` is
            malformed, or names a verb outside KNOWN_VERBS.
    """
    if repo_root is None:
        return dict(DEFAULT_ROLE_VERBS)

    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path
    )
    raw = _read_yaml_mapping(config_path)
    roles_section = raw.get(CONFIG_SECTION_ROLES)
    if roles_section is None:
        return dict(DEFAULT_ROLE_VERBS)
    if not isinstance(roles_section, dict) or not roles_section:
        raise InvalidRoleConfigError(
            f"{config_path}: {CONFIG_SECTION_ROLES!r} section must be a "
            f"non-empty mapping of role name -> verb list, got "
            f"{roles_section!r}."
        )

    resolved: dict[str, tuple[str, ...]] = {}
    for role, verbs in roles_section.items():
        if not isinstance(role, str):
            raise InvalidRoleConfigError(
                f"{config_path}: role keys must be strings, got {role!r}."
            )
        _validate_role_name(role, source=str(config_path))
        resolved[role] = _validate_verb_list(role, verbs, source=str(config_path))
    return resolved


def resolve_role_verbs(
    role: str,
    *,
    repo_root: str | Path | None = None,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
    role_verbs: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """Resolve the verb set for a single *role*.

    Loads the full role -> verb-set mapping (via `load_role_verbs`, unless
    *role_verbs* is supplied directly — mainly for tests/composition with an
    already-loaded mapping) and looks up *role* in it. Raises with the
    RESOLVED role, the resolved config path, and the known-good role set
    when *role* is not declared anywhere — never a silent empty allowlist.
    """
    mapping = (
        role_verbs
        if role_verbs is not None
        else load_role_verbs(repo_root, config_relative_path=config_relative_path)
    )
    if role not in mapping:
        config_path = (
            Path(repo_root) / config_relative_path if repo_root is not None else "<no repo_root>"
        )
        raise InvalidRoleConfigError(
            f"role {role!r} is not declared (config: {config_path}). Known "
            f"roles: {', '.join(sorted(mapping)) or '<none>'}."
        )
    return mapping[role]


__all__ = [
    "CONFIG_SECTION_ROLES",
    "DEFAULT_CONFIG_RELATIVE_PATH",
    "DEFAULT_ROLE_VERBS",
    "KNOWN_VERBS",
    "InvalidRoleConfigError",
    "load_role_verbs",
    "resolve_role_verbs",
]
