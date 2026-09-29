"""transport.host_guard_resolve — shared allowed-host CEILING resolution,
factored out of `transport.read_host_guard` (lr-4ebce1) so `push.host_guard`
(lr-57573e) can adopt the identical WIDEN/NARROW + fail-closed-config
contract without a second, independently-drifting copy of the same
precedence logic.

WHAT THIS MODULE OWNS: the four rules every allowed-host guard in this
package now shares, verbatim:

  1. **The user-level config-file key is a CEILING, not a co-equal source.**
     Once PRESENT, a caller-supplied value (an explicit CLI flag or an env
     var — both settable by the SAME invocation that controls the value
     being anchored) can only NARROW the configured set via intersection,
     never widen past it. Config UNSET reproduces each guard's own pre-fix,
     caller-settable-only precedence byte-for-byte (explicit > env > a
     permissive default) — see `resolve_allowed_hosts` below.
  2. **`None` vs. an empty `frozenset` are never conflated.** `None` means
     "no restriction configured anywhere" (permissive). Any `frozenset`,
     INCLUDING AN EMPTY ONE, means a restriction IS configured/resolved and
     membership is enforced — an empty set denies every host. Collapsing
     these to one falsy value would silently re-permit every host on
     exactly the caller-widening path rule 1 exists to close (this is the
     empty-set-collapse defect lr-57573e fixes for `push.host_guard`, which
     pre-dates this module: its own `check_host_allowed` treated ANY empty
     set, including an operator's real "restrict to nothing" choice, as
     permissive).
  3. **A malformed or present-null config value is a hard error, never a
     silent fallback to permissive.** Only a GENUINELY ABSENT key means
     "unconfigured" — a present value of any other shape (an int, a
     mapping, a list containing a non-string entry, or an explicit `null`)
     raises before any credential is resolved. `dict.get(key)` alone cannot
     distinguish "absent" from "present with value `None`", so every caller
     checks membership explicitly (`key in section`) before reading the
     value — see `resolve_ceiling_hosts` below.
  4. **The refusal message is mode-aware.** A guard's own `check_*_allowed`
     call decides which corrective text to build from `config_is_set`: when
     the config-file ceiling is unset, the flag/env var IS the thing that
     would have permitted the denied host (accurate, keep the original
     text); once the ceiling IS set, the flag/env var can only narrow it,
     so telling the caller to set either would be false — the message must
     instead name the config key. This module does not itself build that
     message (each guard's own docstring/vocabulary differs) — it exposes
     `config_is_set` so each `check_*_allowed` can.

WHY A SHARED MODULE INSTEAD OF TWO COPIES: `transport.read_host_guard`
(lr-4ebce1) and `push.host_guard` (lr-0e39f9, WIDEN/NARROW + empty-set-
collapse fix lr-57573e) anchor DIFFERENT inputs from DIFFERENT trust
boundaries (a CLI flag/env var/config-file chain vs. the live git remote),
and each keeps its OWN env var name and OWN config section/key (see each
module's own docstring for why the two allowlists are deliberately not
shared) — but the RESOLUTION ALGORITHM (config ceiling, caller-narrow-only,
None-vs-frozenset, list-or-string config parse, malformed-config hard error)
is identical, parameterized only by which env var / config section+key /
error-message vocabulary to use. Landing the fix twice, independently, is
exactly the "copied-and-never-reconciled" defect class lr-cd3113 diagnosed
for a different value in this same package — this module is the one place
that logic lives; each guard's own `resolve_allowed_hosts` is now a thin,
guard-specific wrapper around `resolve_ceiling_hosts` below.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, NamedTuple

from clagentic_loadout.transport.host_match import host_matches
from clagentic_loadout.transport.provider_config import (
    DEFAULT_USER_CONFIG_ROOT,
    USER_CONFIG_FILENAME,
    load_user_config_section,
)


def config_file_path(config_root: str | Path | None) -> Path:
    """Resolve the on-disk path of the user-level config file *config_root*
    (or DEFAULT_USER_CONFIG_ROOT when None) points at — used only to name
    the offending file in a malformed-config error message; never opened
    directly here (load_user_config_section owns the actual read)."""
    root = Path(config_root) if config_root is not None else DEFAULT_USER_CONFIG_ROOT
    return root / USER_CONFIG_FILENAME


def parse_comma_separated(raw: str) -> frozenset[str]:
    """Shared comma-separated-list parse for an env var or a config-file
    STRING value — the same whitespace-trim/empty-entry-drop rule
    everywhere, so no two sources in this package can silently disagree on
    what counts as a valid entry."""
    if not raw.strip():
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def load_configured_hosts(
    *,
    config_root: str | Path | None,
    config_section: str,
    config_key: str,
    invalid_config_error: Callable[..., Exception],
) -> frozenset[str] | None:
    """Read *config_key* from *config_section* in the user-level config
    file. Returns None when the section/key is GENUINELY ABSENT (config
    UNSET — the guard's own pre-fix, caller-settable-only precedence
    applies). Returns a frozenset (possibly empty — an operator's real
    choice to restrict to nothing) when the key IS present and holds one of
    the two accepted shapes: a comma-separated STRING, or a YAML LIST of
    strings.

    *invalid_config_error* is the guard-specific exception FACTORY (a
    callable taking the same keyword shape as
    `read_host_guard.InvalidReadHostConfigError.__init__` minus `self`:
    `(config_path, *, received, detail=None)`) — each guard keeps its OWN
    exception class/vocabulary (naming ITS OWN config section/key/env var in
    the message) rather than sharing one generic exception type across
    unrelated guards, per this package's own "no cross-layer imports" style;
    this module only supplies the shared PARSING/VALIDATION logic, not the
    caller-facing error identity.

    ABSENT-VS-PRESENT-NULL: `dict.get(key)` returns None for BOTH "key not
    in the mapping" and "key in the mapping with value None" — membership is
    checked explicitly (`config_key in section`) BEFORE reading the value,
    so a genuinely absent key is the only way to reach the permissive None
    return; a present-but-null value (or any other type
    `isinstance(raw, (str, list))` does not accept) falls through to
    *invalid_config_error*, fail-closed before any credential is resolved.
    """
    section = load_user_config_section(config_section, config_root=config_root)
    if config_key not in section:
        return None
    raw = section.get(config_key)
    if isinstance(raw, str):
        return parse_comma_separated(raw)
    if isinstance(raw, list):
        non_string_entries = [entry for entry in raw if not isinstance(entry, str)]
        if non_string_entries:
            raise invalid_config_error(
                config_file_path(config_root),
                received=raw,
                detail=(
                    f"list entry {non_string_entries[0]!r} "
                    f"(type {type(non_string_entries[0]).__name__}) is not a string"
                ),
            )
        return frozenset(entry.strip() for entry in raw if entry.strip())
    raise invalid_config_error(config_file_path(config_root), received=raw)


class HostCeilingResolution(NamedTuple):
    """Both outputs a caller ever needs from ONE config-file read: the
    resolved allowed-host set (`allowed_hosts`) and whether the config-file
    ceiling tier is the thing that produced it (`config_is_set`) — see
    `resolve_ceiling` below, the single read this tuple is built from
    (lr-57573e fold-in #1, F2: previously each verb called
    `resolve_ceiling_hosts` and `config_is_set` SEPARATELY, each performing
    its own independent `load_configured_hosts` call — two reads of the
    same config file per invocation for a value that only ever changes
    between process invocations, not within one). `check_*_allowed`'s own
    `config_is_set` parameter and its `allowed_hosts` parameter are both
    populated from the SAME field on this one result, so the message and
    the enforcement decision can never observe two different reads of a
    config file that changed between them."""

    allowed_hosts: frozenset[str] | None
    config_is_set: bool


def resolve_ceiling(
    explicit: frozenset[str] | None,
    *,
    env: dict[str, str],
    env_var: str,
    config_root: str | Path | None,
    config_section: str,
    config_key: str,
    invalid_config_error: Callable[..., Exception],
) -> HostCeilingResolution:
    """Resolve the effective allowed-host set for one guard, AND whether the
    config-file ceiling tier is set — in ONE read of the user-level config
    file (lr-57573e fold-in #1, F2). This is the single implementation both
    `resolve_ceiling_hosts` and `config_is_set` below now wrap, rather than
    each performing its own independent `load_configured_hosts` call for a
    value that cannot change within one process invocation.

    Config UNSET (no *config_key* under *config_section* in the user-level
    config file) — BYTE-FOR-BYTE the guard's own pre-fix precedence,
    unchanged for back-compat:
      1. *explicit* (caller-supplied set) — always wins when not None, even
         if empty (a real "restrict to nothing" choice, not silently
         reinterpreted as permissive).
      2. *env_var*, comma-separated, whitespace-trimmed, empty entries
         dropped.
      3. None (no restriction configured — permissive default; see module
         docstring, rule 2, for why this is None rather than an empty
         frozenset).

    Config SET — the config-file set is the CEILING; *explicit*/env can
    only NARROW it, never widen it:
      - *explicit* or env supplied → the subset of the configured set that
        OVERLAPS whatever explicit/env resolved to, compared via
        `transport.host_match.host_matches` (not raw string equality, so a
        configured entry and a caller-supplied entry naming the identical
        authority in a different shape — bare vs. full URL — are still
        recognized as an overlap). May be EMPTY when there is no overlap at
        all — a real "deny everything" outcome (see module docstring,
        rule 2), never reinterpreted as permissive.
      - neither supplied → the full configured set.

    *env* is the environment mapping to consult (a real caller passes
    `os.environ`; a test passes an isolated dict). *config_root* overrides
    the user-level config root (mainly for tests). *invalid_config_error*
    is forwarded to `load_configured_hosts` — see that function's own
    docstring for its required shape.
    """
    if explicit is not None:
        caller_supplied: frozenset[str] | None = frozenset(explicit)
    else:
        raw_env = env.get(env_var, "")
        caller_supplied = parse_comma_separated(raw_env) if raw_env.strip() else None

    configured = load_configured_hosts(
        config_root=config_root,
        config_section=config_section,
        config_key=config_key,
        invalid_config_error=invalid_config_error,
    )
    if configured is None:
        # Config UNSET -- unchanged pre-fix behavior.
        resolved = frozenset(explicit) if explicit is not None else caller_supplied
        return HostCeilingResolution(allowed_hosts=resolved, config_is_set=False)

    # Config SET -- config is the ceiling; a caller-supplied value can only
    # narrow it, never add a host absent from `configured`.
    if caller_supplied is not None:
        resolved = frozenset(
            configured_entry
            for configured_entry in configured
            if any(
                host_matches(configured_entry, caller_entry)
                for caller_entry in caller_supplied
            )
        )
        return HostCeilingResolution(allowed_hosts=resolved, config_is_set=True)
    return HostCeilingResolution(allowed_hosts=configured, config_is_set=True)


def config_is_set(
    *,
    config_root: str | Path | None,
    config_section: str,
    config_key: str,
    invalid_config_error: Callable[..., Exception],
) -> bool:
    """True iff *config_key* is PRESENT in *config_section* in the
    user-level config file — i.e. the config-file tier is the ceiling for
    this resolution. Raises *invalid_config_error* under the same condition
    `load_configured_hosts` itself would (a malformed configured value) —
    this does not shield that call from its own fail-closed contract.

    KEPT for a caller that only ever needs this one boolean without an
    `explicit`/`env_var` pair to resolve alongside it (e.g. a read-only
    inspector) — every caller that also needs `allowed_hosts` should prefer
    `resolve_ceiling` instead, which produces both from a single read (see
    `HostCeilingResolution`'s own docstring, "F2")."""
    return (
        load_configured_hosts(
            config_root=config_root,
            config_section=config_section,
            config_key=config_key,
            invalid_config_error=invalid_config_error,
        )
        is not None
    )


def resolve_ceiling_hosts(
    explicit: frozenset[str] | None,
    *,
    env: dict[str, str],
    env_var: str,
    config_root: str | Path | None,
    config_section: str,
    config_key: str,
    invalid_config_error: Callable[..., Exception],
) -> frozenset[str] | None:
    """KEPT for a caller that only ever needs the resolved set, not
    `config_is_set` alongside it — see `resolve_ceiling`'s own docstring for
    the full precedence this wraps, and `HostCeilingResolution`'s docstring
    for why a caller needing BOTH values should call `resolve_ceiling`
    directly instead of this plus a separate `config_is_set` call (two reads
    of the same config file for one invocation)."""
    return resolve_ceiling(
        explicit,
        env=env,
        env_var=env_var,
        config_root=config_root,
        config_section=config_section,
        config_key=config_key,
        invalid_config_error=invalid_config_error,
    ).allowed_hosts


__all__ = [
    "HostCeilingResolution",
    "config_file_path",
    "config_is_set",
    "load_configured_hosts",
    "parse_comma_separated",
    "resolve_ceiling",
    "resolve_ceiling_hosts",
]
