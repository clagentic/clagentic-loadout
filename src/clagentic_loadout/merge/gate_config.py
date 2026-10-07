"""merge.gate_config — repo-local `merge:` gate-declaration config (lr-0a03c3).

GAP THIS CLOSES: `loadout-merge`'s own gate chain (namespace, authority,
stale-SHA, reviewer verdicts, diff-scope, title, CI-status — see
`merge.verb`'s module docstring) is entirely CLI-flag-driven today:
`--authorized-role`, `--required-reviewer`, `--max-changed-files` all have to
be re-supplied on every invocation with no repo-local config home. A repo
migrating onto loadout-native config with no dispatcher re-deriving those
flags from its own external config LOSES its merge gates the instant it
switches — this module is the missing repo-config home for the gate
DECLARATION half of that CLI surface (which roles/checks/limits apply to
THIS repo), read by a caller (a dispatch/lead layer, or `merge.verb` itself
in a future CLI-wiring slice — see this module's own scope note below)
BEFORE building the CLI invocation, rather than baking the answer into an
external, .crew-shaped config format loadout does not know about.

REPO-TIER (design call #1): every key this module owns is a POLICY value —
"how many reviewers, how wide a diff, which roles" — never an identity, a
login, or a credential. Committed, public-safe, lives in the SAME
`.clagentic/loadout/config.yaml` `merge:` section `merge.post_merge_config`
and `merge.pre_checks_config` already own (one file, one section per verb,
this package's established convention) — NOT a new top-level section.
Contrast with `push.identity_config`'s `builder_identity` (an EMAIL/NAME
pair — identity-bearing, deployment-tier) and `review.login_config`'s
`reviewer_logins` (a per-role LOGIN override — identity-bearing,
deployment-tier): those two modules exist BECAUSE this module's own keys are
deliberately identity-free.

ROLE VOCABULARY ONLY (design call #3, lr-23fe19 consistency): every role
string this module reads (`authorized_roles`, `required_reviewer_roles`) is
a bare role token — builder/reviewer/security/merger/lead, or any other
role name an integrator invents — NEVER an agent name (CLAUDE.md rule 1).
`required_reviewer_roles` feeds STRAIGHT into `merge.verb`'s existing
`--required-reviewer` mechanism, which already resolves a bare role name to
its expected platform login via `merge.reviewer_login.resolve_reviewer_login`
(the bare name IS the login on Forgejo; `github_app.slugs.<role>` + `[bot]`
on GitHub) — this module supplies the ROLE LIST that mechanism already
consumes, not a second login-resolution path. `authorized_roles` feeds
`merge.authority.StaticRoleAuthorityProvider` exactly the same way
`--authorized-role` (repeated) does today — this is the release-gate-class
merge-authority set (the same class of check `merge.authority`'s own
docstring calls "the reference merge gate"), not a new authority mechanism.

REPLACE-NOT-MERGE (design call #2, consistent with `provisioning.roles`):
`required_reviewer_roles` and `authorized_roles` are each a full LIST — a
repo that declares either REPLACES the (empty) default entirely, mirroring
`roles:`'s own "omitted role is simply not provisioned" contract. There is
no partial-override/merge semantics anywhere in this module.
`merge_requirements` is a single mapping of independent boolean/int keys
(`tests_pass`, `ci_pass`, `max_changed_files`) — each key present REPLACES
that key's own default; keys omitted from a partially-declared
`merge_requirements` mapping keep THEIR OWN default (this mirrors how a
mapping of independent settings, not a list, naturally composes — there is
no "the whole section is missing vs. present" ambiguity to resolve the way a
list-valued section has, since a caller reads each key independently).

`ci_pass` maps directly onto `merge.ci_status`'s already-shipped CI-status
gate (empty-evidence-is-a-pass, non-empty gates on combined_state) — this
key is a DECLARATION that the gate should run for this repo at all, not a
reimplementation of the gate itself; `tests_pass` and `max_changed_files`
are equally declarative (a caller wires `tests_pass` to its own test-run
step, `max_changed_files` feeds `merge.diff_scope.check_diff_scope`'s
existing `max_changed_files` parameter directly, same as `--max-changed-files`
does today).

GATE KEYS COME FROM A TRACKED FILE AT THE PR BASE COMMIT. `loadout-merge`
never reads `required_reviewer_roles`, `required_scanners` or `pre_checks`
from the working tree: a tree that holds the PR head would let the PR under
review relax its own gate. `merge.repo_gate_runtime` reads them from the
tracked gate file (`repo_config.TRACKED_GATE_RELATIVE_PATH`) as it exists at
the PR's base commit, via `parse_tracked_gate_text` below. The single-key
loaders in this module still read the working-tree file; they serve doctor
and callers building their own invocation, not the merge gate.

SCOPE NOTE: this module ships the repo-config READ side. `merge.verb` now
consumes `required_reviewer_roles` as a FLOOR beneath `--required-reviewer`
(required = union of the two), and `required_scanners`, through
`merge.repo_gate_runtime`, which owns the fallback and escape-hatch policy
described under BLAST RADIUS below. `authorized_roles` and `merge_requirements`
are still read only by doctor and by a caller building its own invocation.

ABSENCE SEMANTICS (lr-638945 hardening — read this before relying on either
key's default): `required_reviewer_roles` and `authorized_roles` sit in the
SAME `merge:` section but intentionally have DIFFERENT absence contracts,
because they answer different questions:

  - `authorized_roles` absent (or the whole `merge:` section/config file/
    repo_root absent) -> `()`, fail-CLOSED: "no role holds merge authority."
    This has always been safe to leave implicit, because the merge-authority
    gate downstream (`merge.authority.StaticRoleAuthorityProvider`) already
    refuses everyone on an empty set — silence here can never be mistaken
    for "everyone may merge."

  - `required_reviewer_roles` absence is NOT symmetric. When `repo_root` is
    None, the config file is absent, or the `merge:` section itself is
    absent entirely, this still returns `()` — there is nothing to be
    explicit ABOUT (no gate declaration exists at all for this repo). But
    once a repo HAS a `merge:` section (i.e. it is already declaring some
    gate policy), OMITTING `required_reviewer_roles` from it is a config-load
    ERROR (`RequiredReviewerRolesNotDeclaredError`), not a silent `()`. A
    repo that has clearly opted into repo-tier gate config must say, one way
    or the other, whether a reviewer verdict is required before a merge is
    authorized — declare real roles, or declare `required_reviewer_roles: []`
    as an explicit, deliberate "no reviewer gate" opt-out. A silent `()`
    one level down would be indistinguishable from that same explicit
    opt-out, which is exactly the ambiguity this hardening closes: a `merge:`
    section that reads as "this repo's merges are gated" while actually
    gating only WHO may merge, never WHETHER anyone reviewed it.

BLAST RADIUS OF EVERY RAISE THIS MODULE INTRODUCES (bootstrap safety — read
this before wiring any new caller). A declared-but-unread key reads as
protection, so `loadout-merge` now enforces `required_reviewer_roles` as a
floor. That makes a broken gate config capable of blocking the very merge that
would fix it, so the wiring lives in ONE place, `merge.repo_gate_runtime`, and
obeys two rules:

  - A config in which `required_reviewer_roles` or `required_scanners` cannot
    be loaded (unreadable YAML, a `merge:` section that is not a mapping, a
    malformed role list or scanner mapping, an explicit null for either key,
    or a `merge:` section that omits `required_reviewer_roles`) falls back as
    a WHOLE PAIR to flags-only, with a loud stderr warning naming the file and
    the error: neither of those two keys is enforced, not even the one that
    loaded. The fix-the-config merge always lands. Every error this module
    raises is therefore caught there, never propagated out of `loadout-merge`
    or `loadout-push`. `pre_checks` is NOT part of this fallback: it keeps its
    own rule (a malformed or unreadable declaration refuses the merge).
  - A declared role the deployment cannot resolve to a platform login degrades
    PER ROLE, not as a pair (merge.repo_gate_runtime.
    with_resolvable_reviewer_roles): that role drops from the floor and its own
    required_scanners entry is skipped, with a warning naming the role,
    platform and missing mapping; every other role and scanner stays enforced.
  - A config that loads cleanly but cannot be satisfied (a declared role with
    no verdict from it) refuses the merge, with
    `--ignore-repo-gate` as the deliberate, logged escape hatch.

`push.verb` must still never import this module: pushing a corrected config
must never depend on it. `tests/test_doctor_checks.py` locks both properties.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from clagentic_loadout.merge.post_merge_config import CONFIG_SECTION_MERGE
from clagentic_loadout.repo_config import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    resolve_repo_config_path,
)

#: Key within the `merge:` section holding the merge-requirements mapping
#: (tests_pass / ci_pass / max_changed_files).
CONFIG_KEY_MERGE_REQUIREMENTS = "merge_requirements"
#: Key within `merge_requirements` — require a clean local test run.
REQUIREMENT_KEY_TESTS_PASS = "tests_pass"
#: Key within `merge_requirements` — require green CI (see merge.ci_status).
REQUIREMENT_KEY_CI_PASS = "ci_pass"
#: Key within `merge_requirements` — the diff-scope cap (see
#: merge.diff_scope.DEFAULT_MAX_CHANGED_FILES for the built-in fallback a
#: caller that finds no config-declared value here should use instead).
REQUIREMENT_KEY_MAX_CHANGED_FILES = "max_changed_files"
_VALID_REQUIREMENT_KEYS = frozenset(
    {REQUIREMENT_KEY_TESTS_PASS, REQUIREMENT_KEY_CI_PASS, REQUIREMENT_KEY_MAX_CHANGED_FILES}
)

#: Key within the `merge:` section holding the list of ROLE names (never
#: agent names) required to have posted a clean reviewer verdict before a
#: merge is authorized -- the release-gate class. Feeds merge.verb's existing
#: --required-reviewer mechanism (merge.reviewer_login.resolve_reviewer_login
#: resolves each bare role name to its expected platform login).
CONFIG_KEY_REQUIRED_REVIEWER_ROLES = "required_reviewer_roles"

#: Key within the `merge:` section holding the list of ROLE names permitted
#: to hold merge authority -- feeds merge.authority.StaticRoleAuthorityProvider
#: exactly like the CLI's repeated --authorized-role flag does today.
CONFIG_KEY_AUTHORIZED_ROLES = "authorized_roles"

#: Key within the `merge:` section mapping a reviewer role to the scanner names
#: a clean verdict from that role must not report as failed. Names are
#: deployment vocabulary; none is built in.
CONFIG_KEY_REQUIRED_SCANNERS = "required_scanners"


class InvalidMergeGateConfigError(ValueError):
    """Raised when a repo's `merge:` gate-declaration keys
    (`merge_requirements`, `required_reviewer_roles`, `authorized_roles`) are
    malformed. Always reports the RESOLVED config path and offending value
    (conformance rule 4), never a stale guess."""


class RequiredReviewerRolesNotDeclaredError(InvalidMergeGateConfigError):
    """Raised when a repo's `merge:` section is present (i.e. the repo has
    already opted into repo-tier gate config) but OMITS
    `required_reviewer_roles` entirely (lr-638945 hardening).

    A subclass of `InvalidMergeGateConfigError` -- callers that already
    catch the parent (e.g. `doctor.checks.check_repo_loadout_schema`) catch
    this too, without a second except clause, and correctly report it as a
    schema FAIL: an ambiguous gate declaration is worse than an absent one,
    because it reads as protection. See this module's docstring, "ABSENCE
    SEMANTICS," for the full contrast with `authorized_roles`' unchanged
    fail-closed-on-absence behavior. The fix is always the same shape:
    declare the real reviewer roles, or declare `required_reviewer_roles: []`
    as an explicit, deliberate opt-out."""


def _parse_yaml_mapping(text: str, source: object) -> dict:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise InvalidMergeGateConfigError(
            f"{source}: could not be read as YAML: {exc}."
        ) from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InvalidMergeGateConfigError(
            f"{source}: top-level document must be a mapping, got {type(raw).__name__}."
        )
    return raw


def _read_yaml_mapping(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise InvalidMergeGateConfigError(
            f"{path}: could not be read as YAML: {exc}."
        ) from exc
    return _parse_yaml_mapping(text, path)


def _merge_section_from_mapping(
    raw: dict, source: object
) -> tuple[dict, bool]:
    section_present = CONFIG_SECTION_MERGE in raw
    merge_section = raw.get(CONFIG_SECTION_MERGE)
    if merge_section is None:
        return {}, section_present
    if not isinstance(merge_section, dict):
        raise InvalidMergeGateConfigError(
            f"{source}: {CONFIG_SECTION_MERGE!r} section must be a mapping, "
            f"got {type(merge_section).__name__}."
        )
    return merge_section, section_present


def _read_merge_section_with_presence(
    repo_root: str | Path,
    *,
    config_relative_path: str,
) -> tuple[Path, dict, bool]:
    """Same as `_read_merge_section`, but also reports whether the
    top-level `merge:` key was actually PRESENT in the parsed YAML (as
    opposed to merely defaulting to `{}` because it was absent) -- the
    distinction `load_required_reviewer_roles` needs to tell "no merge:
    section at all" (nothing to be explicit about) apart from "merge:
    section present but this key omitted" (an ambiguous gate)."""
    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path
    )
    raw = _read_yaml_mapping(config_path)
    merge_section, section_present = _merge_section_from_mapping(raw, config_path)
    return config_path, merge_section, section_present


def parse_tracked_gate_text(
    text: str, *, source: str
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]], dict]:
    """Parse the TRACKED repo gate file's text (as read at the PR base commit)
    into (required_reviewer_roles, required_scanners, merge_section).

    Same `merge:` section shape and same validation, absence rules and
    explicit-null rules as the working-tree loaders above, from ONE parse of
    *source*, so every key describes the same snapshot. The raw merge section
    is returned so the caller can validate the sibling `pre_checks` key from
    the same parse.

    Raises:
        InvalidMergeGateConfigError: any error the single-key loaders raise.
    """
    merge_section, section_present = parse_tracked_merge_section(text, source=source)
    roles, scanners = reviewer_gate_from_section(source, merge_section, section_present)
    return roles, scanners, merge_section


def parse_tracked_merge_section(text: str, *, source: str) -> tuple[dict, bool]:
    """Parse the tracked gate file's text into its `merge:` section and whether
    that section was present. The step every gate key shares: a failure here
    means no key in the file can be read.

    Raises:
        InvalidMergeGateConfigError: unreadable YAML or a non-mapping document
            or `merge:` section.
    """
    return _merge_section_from_mapping(_parse_yaml_mapping(text, source), source)


def reviewer_gate_from_section(
    source: str, merge_section: dict, section_present: bool
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """Validate `required_reviewer_roles` and `required_scanners` of an already
    parsed `merge:` section. `pre_checks` is validated separately, so a fault in
    one family never decides the other's failure rule.

    Raises:
        InvalidMergeGateConfigError: any error the single-key loaders raise.
    """
    config_path = Path(source)
    return (
        _reviewer_roles_from_section(config_path, merge_section, section_present),
        _required_scanners_from_section(config_path, merge_section),
    )


def _read_merge_section(
    repo_root: str | Path,
    *,
    config_relative_path: str,
) -> tuple[Path, dict]:
    config_path, merge_section, _section_present = _read_merge_section_with_presence(
        repo_root, config_relative_path=config_relative_path
    )
    return config_path, merge_section


def _validate_role_list(value: object, *, key: str, config_path: Path) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InvalidMergeGateConfigError(
            f"{config_path}: {CONFIG_SECTION_MERGE}.{key} must be a list of role "
            f"names, got {type(value).__name__}."
        )
    roles: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise InvalidMergeGateConfigError(
                f"{config_path}: {CONFIG_SECTION_MERGE}.{key} entries must be "
                f"non-empty role-name strings, got {entry!r}."
            )
        # One normalization rule for every role and scanner name read here:
        # surrounding whitespace is stripped at load, so a padded entry can
        # never silently miss the exact-match lookup that consumes it.
        roles.append(entry.strip())
    return tuple(roles)


def load_merge_requirements(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> dict:
    """Resolve the `merge: merge_requirements:` mapping for a repo.

    Returns `{}` (no requirements declared -- every gate a caller might
    apply from this mapping is simply not requested) when *repo_root* is
    None, the config file is absent, the `merge:` section is absent, or the
    `merge_requirements` key is absent within it.

    A present mapping is validated key-by-key: `tests_pass`/`ci_pass` must
    be bool, `max_changed_files` must be a positive int. Unknown keys are
    rejected (resolved-values error naming the bad key and the known-good
    set) rather than silently ignored -- a typo'd requirement key must never
    silently fail to gate anything.

    Raises:
        InvalidMergeGateConfigError: malformed YAML, non-mapping `merge:` or
            `merge_requirements` section, an unknown key, or a key with the
            wrong type.
    """
    if repo_root is None:
        return {}
    config_path, merge_section = _read_merge_section(
        repo_root, config_relative_path=config_relative_path
    )
    requirements = merge_section.get(CONFIG_KEY_MERGE_REQUIREMENTS)
    if requirements is None:
        return {}
    if not isinstance(requirements, dict):
        raise InvalidMergeGateConfigError(
            f"{config_path}: {CONFIG_SECTION_MERGE}.{CONFIG_KEY_MERGE_REQUIREMENTS} "
            f"must be a mapping, got {type(requirements).__name__}."
        )

    unknown = sorted(set(requirements) - _VALID_REQUIREMENT_KEYS)
    if unknown:
        raise InvalidMergeGateConfigError(
            f"{config_path}: {CONFIG_SECTION_MERGE}.{CONFIG_KEY_MERGE_REQUIREMENTS} "
            f"has unknown key(s) {unknown!r}. Known keys: "
            f"{sorted(_VALID_REQUIREMENT_KEYS)!r}."
        )

    for bool_key in (REQUIREMENT_KEY_TESTS_PASS, REQUIREMENT_KEY_CI_PASS):
        if bool_key in requirements and not isinstance(requirements[bool_key], bool):
            raise InvalidMergeGateConfigError(
                f"{config_path}: {CONFIG_SECTION_MERGE}.{CONFIG_KEY_MERGE_REQUIREMENTS}."
                f"{bool_key} must be a boolean, got {requirements[bool_key]!r}."
            )

    if REQUIREMENT_KEY_MAX_CHANGED_FILES in requirements:
        max_files = requirements[REQUIREMENT_KEY_MAX_CHANGED_FILES]
        if not isinstance(max_files, int) or isinstance(max_files, bool) or max_files < 1:
            raise InvalidMergeGateConfigError(
                f"{config_path}: {CONFIG_SECTION_MERGE}.{CONFIG_KEY_MERGE_REQUIREMENTS}."
                f"{REQUIREMENT_KEY_MAX_CHANGED_FILES} must be a positive integer, "
                f"got {max_files!r}."
            )

    return dict(requirements)


def load_required_reviewer_roles(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> tuple[str, ...]:
    """Resolve the `merge: required_reviewer_roles:` list for a repo.

    THREE absence shapes (lr-638945 hardening -- see this module's
    docstring, "ABSENCE SEMANTICS," for the full contrast with
    `authorized_roles`):

      1. *repo_root* is None, the config file is absent, or the `merge:`
         section is absent entirely -> returns `()`. Nothing to be explicit
         about: this repo has no repo-tier gate declaration at all.
      2. `merge:` is present and DECLARES `required_reviewer_roles`
         (including an explicit empty list) -> returns the validated tuple.
         An explicit `[]` is the deliberate "no reviewer gate" opt-out and
         returns `()`, identically to shape 1's return value but for a
         completely different, STATED reason.
      3. `merge:` is present but OMITS `required_reviewer_roles` -> RAISES
         `RequiredReviewerRolesNotDeclaredError`. A repo that has already
         opted into repo-tier gate config must say, one way or the other,
         whether a reviewer verdict gates its own merges.

    Raises:
        InvalidMergeGateConfigError: malformed YAML, a non-mapping `merge:`
            section, or `required_reviewer_roles` present but not a list of
            non-empty role-name strings.
        RequiredReviewerRolesNotDeclaredError: `merge:` is present but omits
            `required_reviewer_roles` (shape 3 above).
    """
    if repo_root is None:
        return ()
    config_path, merge_section, section_present = _read_merge_section_with_presence(
        repo_root, config_relative_path=config_relative_path
    )
    return _reviewer_roles_from_section(config_path, merge_section, section_present)


def _reviewer_roles_from_section(
    config_path: Path, merge_section: dict, section_present: bool
) -> tuple[str, ...]:
    if not section_present:
        return ()
    if CONFIG_KEY_REQUIRED_REVIEWER_ROLES not in merge_section:
        raise RequiredReviewerRolesNotDeclaredError(
            f"{config_path}: {CONFIG_SECTION_MERGE!r} section is present but "
            f"omits {CONFIG_KEY_REQUIRED_REVIEWER_ROLES!r}. Declare the "
            f"role(s) whose clean reviewer verdict this repo requires "
            f"before a merge, or declare "
            f"`{CONFIG_KEY_REQUIRED_REVIEWER_ROLES}: []` as an explicit "
            f"opt-out if this repo intentionally has no reviewer-verdict "
            f"gate."
        )
    raw = merge_section[CONFIG_KEY_REQUIRED_REVIEWER_ROLES]
    return _validate_role_list(
        raw, key=CONFIG_KEY_REQUIRED_REVIEWER_ROLES, config_path=config_path
    )


def load_authorized_roles(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> tuple[str, ...]:
    """Resolve the `merge: authorized_roles:` list for a repo -- the
    release-gate-class merge-authority roster.

    Returns `()` (no role holds merge authority -- the same fail-closed
    default `merge.authority.StaticRoleAuthorityProvider` already has for an
    empty `authorized_roles` set: nobody may merge until this is declared)
    when *repo_root* is None, the config file is absent, the `merge:`
    section is absent, OR the `authorized_roles` key is absent within it.
    UNCHANGED by the lr-638945 hardening: this absence behavior is safe to
    leave implicit precisely because it is already fail-closed all the way
    down -- there is no shape here where silence could be mistaken for "any
    role may merge." Contrast with `load_required_reviewer_roles` above,
    whose absence-within-a-present-`merge:`-section shape now RAISES instead
    of silently returning `()`, because that key's silent absence is
    fail-OPEN (no reviewer verdict required) and indistinguishable from a
    deliberate opt-out one level down -- see this module's docstring,
    "ABSENCE SEMANTICS," for the full contrast.

    Raises:
        InvalidMergeGateConfigError: malformed YAML, a non-mapping `merge:`
            section, or `authorized_roles` present but not a list of
            non-empty role-name strings.
    """
    if repo_root is None:
        return ()
    config_path, merge_section = _read_merge_section(
        repo_root, config_relative_path=config_relative_path
    )
    raw = merge_section.get(CONFIG_KEY_AUTHORIZED_ROLES)
    if raw is None:
        return ()
    return _validate_role_list(raw, key=CONFIG_KEY_AUTHORIZED_ROLES, config_path=config_path)


def load_required_scanners(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> dict[str, tuple[str, ...]]:
    """Resolve the `merge: required_scanners:` mapping (reviewer role ->
    scanner names) for a repo.

    Returns `{}` (no scanner is required) when *repo_root* is None, the config
    file is absent, the `merge:` section is absent, or the key is absent.

    Raises:
        InvalidMergeGateConfigError: malformed YAML, a non-mapping `merge:`
            section, `required_scanners` not a mapping of role name to a list
            of non-empty scanner-name strings.
    """
    if repo_root is None:
        return {}
    config_path, merge_section = _read_merge_section(
        repo_root, config_relative_path=config_relative_path
    )
    return _required_scanners_from_section(config_path, merge_section)


def load_repo_gate_declarations(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """Resolve `required_reviewer_roles` and `required_scanners` from ONE read
    of the config file, so both keys describe the same snapshot.

    Two separate reads could straddle a concurrent edit and pair a role list
    from one version with a scanner map from another, which defeats the
    whole-config fallback rule applied by the caller.

    Raises:
        InvalidMergeGateConfigError: any error either single-key loader raises.
    """
    if repo_root is None:
        return (), {}
    config_path, merge_section, section_present = _read_merge_section_with_presence(
        repo_root, config_relative_path=config_relative_path
    )
    return (
        _reviewer_roles_from_section(config_path, merge_section, section_present),
        _required_scanners_from_section(config_path, merge_section),
    )


def _required_scanners_from_section(
    config_path: Path, merge_section: dict
) -> dict[str, tuple[str, ...]]:
    # Same rule as required_reviewer_roles: a key that is present is validated
    # whatever its value, so an explicit null is malformed (it falls back with
    # a warning), never a silent synonym for "no scanners required".
    if CONFIG_KEY_REQUIRED_SCANNERS not in merge_section:
        return {}
    raw = merge_section[CONFIG_KEY_REQUIRED_SCANNERS]
    if not isinstance(raw, dict):
        raise InvalidMergeGateConfigError(
            f"{config_path}: {CONFIG_SECTION_MERGE}.{CONFIG_KEY_REQUIRED_SCANNERS} must be a "
            f"mapping of reviewer role to a list of scanner names, got {type(raw).__name__}."
        )
    resolved: dict[str, tuple[str, ...]] = {}
    for role, scanners in raw.items():
        if not isinstance(role, str) or not role.strip():
            raise InvalidMergeGateConfigError(
                f"{config_path}: {CONFIG_SECTION_MERGE}.{CONFIG_KEY_REQUIRED_SCANNERS} keys must "
                f"be non-empty role-name strings, got {role!r}."
            )
        normalized_role = role.strip()
        if normalized_role in resolved:
            raise InvalidMergeGateConfigError(
                f"{config_path}: {CONFIG_SECTION_MERGE}.{CONFIG_KEY_REQUIRED_SCANNERS} names "
                f"role {normalized_role!r} more than once (after trimming surrounding "
                f"whitespace)."
            )
        resolved[normalized_role] = _validate_role_list(
            scanners,
            key=f"{CONFIG_KEY_REQUIRED_SCANNERS}.{role}",
            config_path=config_path,
        )
    return resolved


__all__ = [
    "CONFIG_KEY_AUTHORIZED_ROLES",
    "CONFIG_KEY_MERGE_REQUIREMENTS",
    "CONFIG_KEY_REQUIRED_REVIEWER_ROLES",
    "CONFIG_KEY_REQUIRED_SCANNERS",
    "CONFIG_SECTION_MERGE",
    "DEFAULT_CONFIG_RELATIVE_PATH",
    "REQUIREMENT_KEY_CI_PASS",
    "REQUIREMENT_KEY_MAX_CHANGED_FILES",
    "REQUIREMENT_KEY_TESTS_PASS",
    "InvalidMergeGateConfigError",
    "RequiredReviewerRolesNotDeclaredError",
    "load_authorized_roles",
    "load_merge_requirements",
    "load_required_reviewer_roles",
    "load_repo_gate_declarations",
    "load_required_scanners",
    "parse_tracked_gate_text",
    "parse_tracked_merge_section",
    "reviewer_gate_from_section",
]
