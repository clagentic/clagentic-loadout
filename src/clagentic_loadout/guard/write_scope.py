"""guard.write_scope — role-keyed Write/Edit scope enforcement (lr-fd279d,
port of the reference deployment's guard-scope.py; lr-5a8d epic, slice 1 —
the PATTERN-SETTER for the remaining Wave C guard-hook port).

PORT PATTERN (read this first — every later Wave C guard-hook slice mirrors
it, per lr-5a8d comment #8):

1. STRIP THE HOOK SHELL. The reference file is a PreToolUse hook script: it
   reads a JSON payload from stdin, keys behavior off `agent_type` (one of a
   fixed set of internal agent names), and returns a process exit code. None
   of that harness plumbing belongs in a portable policy library (CLAUDE.md
   rule 6a — no hard dependency on a specific harness's hook contract). This
   module keeps ONLY the decision logic — "is this Write/Edit call in scope"
   — as pure functions over explicit inputs. A caller's own harness-specific
   adapter (not part of this port) is responsible for reading stdin, mapping
   its own identity model to a WriteRole, calling into this module, and
   translating the result to whatever the harness expects (exit code,
   webhook response, etc.).

2. AGENT NAMES -> ROLES. The reference file hardcodes a fleet of internal
   agent identities into three behavior buckets (a scope-checked builder
   role, a hard-denied release-gate role, and a set of read-only roles).
   CLAUDE.md rule 1 forbids agent names in product code. This module
   generalizes the three buckets into a `WriteRole` enum:

     WriteRole.SCOPED     — scope.allow_all / allowed_paths / blocked_paths
                             enforcement against a per-project config (the
                             one role whose Write/Edit calls are ever
                             conditionally admitted).
     WriteRole.MERGE_GATE — Write/Edit hard-denied unconditionally; the
                             merge-authority role never authors files.
     WriteRole.READ_ONLY  — Write/Edit hard-denied; defense-in-depth for any
                             role whose own capability set never includes
                             Write/Edit at all.

   A CALLER supplies the WriteRole for its own identity via config (its own
   role-registry, mirroring `clagentic_loadout.provisioning.roles`'s
   role -> verb-set convention) — this module never maps an agent name to a
   role itself, and never hardcodes a name anywhere.

3. LEAD/DIRECTOR SESSIONS -> A SEPARATE, EXPLICIT ROLE. The reference file's
   lead/director hard-deny (an unconditional Write/Edit refusal keyed off a
   fixed set of names plus a "-lead" suffix convention, detected via a
   harness-specific session-sidecar file) is likewise not hardcoded here.
   `WriteRole.LEAD` reuses the SAME hard-deny path as MERGE_GATE (both are
   "this identity never authors files" — the distinction is only the denial
   message a caller wants to surface). A caller's own harness adapter is
   responsible for detecting a lead/director session by whatever means its
   own harness supports (session sidecar, dispatch metadata, etc.) — this
   module has no opinion on that detection mechanism.

4. UNKNOWN ROLE FAILS CLOSED. The reference's "unhandled crew agent" branch
   distinguished "teams context" (deny) from "relay context" (warn+allow) —
   the relay is retired (lr-221a) and the reference file itself documents
   that only the deny path is reachable in practice. This module has no
   permissive fallback at all: `WriteRole` is a closed enum: a caller with a
   role outside these four is a config error, not a runtime "unknown role"
   case reachable through this module's own API.

5. CONTAINMENT/SCOPE LOGIC IS NOT DUPLICATED HERE. The reference file's
   scope-mode resolution (allow_all / allowed_paths / blocked_paths /
   fail-closed / project-root discovery) is reused AS-IS via `WriteScopeConfig`
   / `resolve_write_scope_mode` / `check_write_scope` below, which are a
   loadout-native reimplementation of the SAME mode semantics as the
   reference's canonical scope resolver — deliberately NOT importing
   scratch_policy's containment boundary, which answers a DIFFERENT question
   (is this Bash command's target confined to $TMPDIR/$HOME) from this
   module's question (is this Write/Edit target inside the declared
   project-tree scope). The two never overlap in practice: scratch_policy
   explicitly excludes repo-tree writes from its category grant (see that
   module's "NOT covered" list), and this module only ever evaluates
   repo-tree targets. No containment boundary code is duplicated between the
   two — each owns its own non-overlapping surface.

6. OPAQUE TASK REF. `task_id` never appears in this module — deny reasons
   cite `RULE_LR_XXXX_REFS` style constants defined by a CALLER's own
   annotations if it wants task-ID traceability in its own harness layer;
   this module's reasons are self-contained prose with no internal ticket
   references baked in (CLAUDE.md rule 6a: task_id is an opaque, caller-
   supplied ref, never hardcoded here).
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class WriteRole(Enum):
    """The four ways a caller's identity can relate to Write/Edit scope.

    A caller's own role registry (e.g. a per-project config, mirroring
    `clagentic_loadout.provisioning.roles`'s role -> verb-set convention)
    maps its bare role name (builder, reviewer, merger, lead, ...) to
    exactly one of these. No agent name is ever a member of this enum.
    """

    #: Enforce `WriteScopeConfig` (allow_all / allowed_paths / blocked_paths)
    #: against the target path. This is the only role whose Write/Edit calls
    #: are ever actually admitted conditionally — every other role is an
    #: unconditional hard-deny.
    SCOPED = "scoped"

    #: Unconditional Write/Edit hard-deny: the merge-authority role never
    #: authors files.
    MERGE_GATE = "merge_gate"

    #: Unconditional Write/Edit hard-deny: a lead/director session is
    #: read-only on code by contract.
    LEAD = "lead"

    #: Unconditional Write/Edit hard-deny: defense-in-depth for any role
    #: whose own capability set never includes Write/Edit at all.
    READ_ONLY = "read_only"


@dataclass(frozen=True)
class WriteScopeConfig:
    """The per-project scope declaration a SCOPED role's Write/Edit calls
    are checked against — the loadout-native equivalent of a
    `.crew/<role>.yaml` `scope:` section (reference: `scope.allow_all` /
    `scope.allowed_paths` / `scope.blocked_paths`).

    `allow_all=True` grants unrestricted write access within the project
    tree; `blocked_paths` still applies (deny-wins, always enforced).
    `allowed_paths`, when set and `allow_all` is False, is an explicit
    fnmatch-glob allowlist (matched against the path relative to
    `project_root`). Neither set (the default) resolves to fail-closed —
    an absent scope declaration is never silent permission.
    """

    allow_all: bool = False
    allowed_paths: tuple[str, ...] = ()
    blocked_paths: tuple[str, ...] = ()


class WriteScopeMode(Enum):
    """The resolved scope-check mode for a single Write/Edit call, mirroring
    the reference deployment's scope-resolver mode strings."""

    ALLOW_ALL = "allow_all"
    CONTRACT = "contract"
    FAIL_CLOSED = "fail_closed"


def resolve_write_scope_mode(config: WriteScopeConfig) -> WriteScopeMode:
    """Resolve which mode a `WriteScopeConfig` operates under.

    No dispatch-envelope ceiling layering (the reference's "dispatch" /
    "intersect" modes) is ported here — the relay dispatch-envelope
    mechanism those modes existed to layer against is retired
    (lr-221a, cited by the reference deployment's own scope-resolver
    docstring). A future caller that reintroduces an envelope-level scope
    ceiling can
    layer it on top of this module's `WriteScopeConfig` without needing
    this module itself to know about envelopes (API-first: this module's
    job is the project-tree-scope decision, not envelope layering).
    """
    if config.allow_all or (len(config.allowed_paths) == 1 and config.allowed_paths[0] == "**"):
        return WriteScopeMode.ALLOW_ALL
    if config.allowed_paths:
        return WriteScopeMode.CONTRACT
    return WriteScopeMode.FAIL_CLOSED


def _path_matches(rel: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(rel, pat) for pat in patterns)


def check_write_scope(
    file_path: str,
    project_root: Path,
    config: WriteScopeConfig,
) -> tuple[bool, str]:
    """Check whether *file_path* is in scope for a SCOPED-role Write/Edit
    call, given *config*. Returns (ok, reason) — reason is empty on success.

    Resolution order mirrors the reference deployment's scope-check function
    (lr-cec6, lr-d88d):
      1. Path outside `project_root` entirely -> denied (a caller wanting a
         narrow explicit-intent exception outside the project tree, e.g.
         a single operator-config file, applies that exception BEFORE
         calling this function — it is not this module's concern).
      2. `blocked_paths` match -> denied unconditionally (deny wins, even
         under allow_all).
      3. Mode ALLOW_ALL -> allowed (blocked_paths already checked above).
      4. Mode CONTRACT -> allowed iff `rel` matches `allowed_paths`.
      5. Mode FAIL_CLOSED -> denied with an actionable message naming both
         config knobs a caller can set to authorize writes.
    """
    try:
        rel = str(Path(file_path).resolve().relative_to(project_root))
    except ValueError:
        return False, (
            f"edit target {file_path!r} is outside project root {project_root}; "
            f"writes are scoped to the target project tree only."
        )

    if config.blocked_paths and _path_matches(rel, config.blocked_paths):
        return False, (
            f"edit target {rel!r} matches scope.blocked_paths "
            f"({list(config.blocked_paths)}); escalate for an explicit exception."
        )

    mode = resolve_write_scope_mode(config)

    if mode is WriteScopeMode.ALLOW_ALL:
        return True, ""

    if mode is WriteScopeMode.CONTRACT:
        if not _path_matches(rel, config.allowed_paths):
            return False, (
                f"edit target {rel!r} not in scope.allowed_paths "
                f"({list(config.allowed_paths)}); escalate for an explicit exception."
            )
        return True, ""

    return False, (
        f"edit target {rel!r} denied: no write scope declared. Neither "
        f"scope.allow_all nor scope.allowed_paths is set. To authorize "
        f"writes across the project tree, declare scope.allow_all: true "
        f"(optionally with scope.blocked_paths to protect specific paths), "
        f"or set an explicit scope.allowed_paths allowlist."
    )


def check_write_call(
    role: WriteRole,
    file_path: str,
    project_root: Path | None = None,
    config: WriteScopeConfig | None = None,
) -> tuple[bool, str]:
    """Top-level entry point: is a Write/Edit call for *role* against
    *file_path* permitted?

    - `WriteRole.MERGE_GATE`, `WriteRole.LEAD`, `WriteRole.READ_ONLY`:
      unconditional hard-deny; `file_path`/`project_root`/`config` are
      accepted for a uniform call signature but not consulted.
    - `WriteRole.SCOPED`: requires both `project_root` and `config` (a
      caller invoking a SCOPED role without them is a programming error —
      raises `ValueError` rather than silently choosing a default policy,
      matching this module's no-permissive-fallback contract).
    """
    if role is WriteRole.MERGE_GATE:
        return False, (
            "denied: this identity is the release/merge-authority gate and "
            "does not author files."
        )
    if role is WriteRole.LEAD:
        return False, (
            "denied: lead/director sessions are read-only on code; dispatch "
            "a builder role to author files."
        )
    if role is WriteRole.READ_ONLY:
        return False, "denied: this role has no Write/Edit capability."

    # role is WriteRole.SCOPED
    if project_root is None or config is None:
        raise ValueError(
            "WriteRole.SCOPED requires both project_root and config to "
            "resolve a scope decision."
        )
    return check_write_scope(file_path, project_root, config)


__all__ = [
    "WriteRole",
    "WriteScopeConfig",
    "WriteScopeMode",
    "check_write_call",
    "check_write_scope",
    "resolve_write_scope_mode",
]
