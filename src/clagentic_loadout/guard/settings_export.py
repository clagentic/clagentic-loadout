"""guard.settings_export — SINGLE SOURCE, BOTH SINKS fragment generator for
every landed guard module with an operator-facing allowlist counterpart
(lr-5a8d, task comment #2, point 3; closing coverage slice lr-278e2e).

The failure this module exists to prevent: a guard-layer Bash/Write/Edit
pre-check and an outer harness-level permission classifier (e.g. Claude
Code's ``settings.json`` ``permissions.allow`` glob list) disagreeing about
whether a given call is admitted — precisely the shape that produced the
original operator-prompt symptom (task lr-5a8d comment #2's OBSERVED
section: the guard admitted a scratch ``mkdir`` but the outer classifier had
never been told about it, so the operator still saw a prompt).

CONTRACT: single source, both sinks WHERE BOTH EXIST. Every function below
reads its verb/path list from the SAME policy object a caller's guard-layer
check consumes directly — never a second, independently-maintained copy.
This is NOT "force every guard into permissions.allow": several landed guard
modules are GUARD-LAYER-ONLY by nature (a pure denylist, a warn-only
advisory, a classify-then-deny check, or an admission surface keyed on
caller-supplied ``re.Pattern`` objects with no mechanical glob translation)
— see "GUARD-LAYER-ONLY MODULES" below for the explicit per-module
rationale, and docs/guard-policy.md's "Coverage" section for the same table
in product-doc form.

COVERAGE (dual-sink — this module emits a settings-fragment sink for both):

  - ``guard.scratch_policy`` (landed PR #110) — ``scratch_permission_fragment``.
    Source: ``SCRATCH_SAFE_VERBS``. Sink: ``Bash(<verb> *)``/``Bash(<verb>:*)``
    per verb.
  - ``guard.write_scope`` (``WriteRole.SCOPED``) — ``write_scope_permission_
    fragment``. Source: the SAME ``WriteScopeConfig.allowed_paths`` tuple
    ``guard.write_scope.check_write_scope`` evaluates per call. Sink:
    ``Edit(<glob>)``/``Write(<glob>)`` per allowed-path glob. See that
    function's own docstring for why ``allow_all`` and ``blocked_paths``
    are DELIBERATELY NOT emitted as settings-fragment entries.

GUARD-LAYER-ONLY MODULES (no operator-facing allowlist counterpart — the
contract is "both sinks where both exist," not "force a dual-sink onto
every guard"):

  - ``guard.env_prefix`` — a prefix-STRIPPING classifier consumed BEFORE a
    caller's own verb dispatch, never itself a `Bash(...)` argv[0]/prefix
    shape a settings allowlist glob could name. There is no verb here to
    grant; ``strip_allowed_env_prefix`` either strips a safe prefix or
    passes the command through unchanged for a caller's OWN downstream
    allowlist check to evaluate.
  - ``guard.credential_paths`` — a pure DENYLIST (Read/Glob/Bash credential-
    file-shape denial). The operator-facing counterpart of a deny check is
    the ABSENCE of a matching settings-fragment allow entry, not an
    addition to one — there is no positive grant to mirror.
  - ``guard.shell_parsing`` — pure text-processing primitives (word
    normalization, compound-command detection). No admission decision of
    any kind; nothing for a settings fragment to represent.
  - ``guard.bash_admission`` — building-block PREDICATES
    (``detect_project_tree_write_targets``, ``is_admitted_loadout_family_
    readonly``, ``requires_admission_flag``, ``is_admitted_body_stdin_
    pipe``) consumed BY ``guard.role_allowlist``'s per-role checkers, not a
    top-level admission surface with its own verb roster. The one piece
    with a literal `loadout-<verb>` grant shape is already covered by the
    EXISTING, separate `provisioning.allowlist.generate_role_fragment`
    generator (same console-script convention) — this module does not fork
    a second, divergent generator for the same verb vocabulary.
  - ``guard.role_allowlist`` (``BashRole.BUILDER``/``MERGER``/``REVIEWER``/
    ``SECURITY``/``ANALYSIS``/``RESEARCH``/``PLANNING_READER``) — every
    per-role admission surface is configured via caller-supplied
    ``re.Pattern`` objects (``RoleAllowlistConfig.extra_verb_patterns``,
    ``ReviewGateConfig.post_verb_patterns``, ``AnalysisRoleConfig.
    git_readonly_subcommands``, etc.), not literal verb-name strings. A
    compiled regex (alternation, anchors, character classes, lookaheads)
    has no faithful, general mechanical translation into a `Bash(<glob> *)`
    permission-allowlist string — attempting one either silently narrows
    the fragment (covering only the trivial literal-prefix subset of what
    the guard layer actually admits) or silently widens it (a naive
    "strip the regex syntax" transform could turn a narrow anchored
    pattern into an over-broad glob). See lr-278e2e comment thread /
    NEEDS-DECISION escalation for the design question this raises for a
    future slice; this module does not force a lossy translation to claim
    coverage it cannot honestly provide.
  - ``guard.director_mutation`` / ``guard.infra_ops`` (``BashRole.LEAD`` /
    ``BashRole.INFRA``) — same caller-supplied-regex admission shape as
    ``role_allowlist`` above, AND majority DENY/mutation-family
    classifiers rather than affirmative verb grants (a mis-mirrored
    "allow" entry for a deny-oriented checker is a WIDENING bug, not a
    parity fix) — doubly out of scope for a mechanical dual-sink here.
  - ``guard.task_dispatch`` / ``guard.git_write_guard`` — Task-tool
    dispatch rosters and a git/PR write-operation hard-deny respectively;
    neither maps to a `Bash`/`Edit`/`Write` `permissions.allow` glob shape
    a settings file exposes (a Task-dispatch `subagent_type` roster is not
    the same permission surface as a Bash-command allowlist entry).
  - ``guard.dispatch_discipline`` — WARN-ONLY, never denies (see that
    module's own docstring). There is no admission decision to mirror as a
    settings-fragment entry — every call this guard sees is already
    permitted by definition; it only ever attaches advisory context.
"""

from __future__ import annotations

from clagentic_loadout.guard.scratch_policy import SCRATCH_SAFE_VERBS
from clagentic_loadout.guard.write_scope import WriteScopeConfig


def scratch_permission_fragment() -> list[str]:
    """Return the sorted `Bash(...)` permission-allowlist entries for every
    verb in `guard.scratch_policy.SCRATCH_SAFE_VERBS` — the harness-settings
    sink half of the single-source contract (see module docstring).

    Two entries per verb, mirroring `provisioning.allowlist`'s own
    established shape (`Bash(<verb>:*)` and `Bash(<verb> *)` — different
    harnesses record a Bash tool call in either form).

    This fragment grants OUTER-CLASSIFIER visibility only; it never
    replaces the guard layer's own per-invocation containment check
    (`guard.scratch_policy.is_scratch_contained`), which is what actually
    decides whether any GIVEN invocation of one of these verbs is safe. A
    deployment that lands this fragment in its harness settings WITHOUT
    also wiring the guard-layer check would be granting the verb
    unconditionally — this function's return value is not, by itself, a
    complete authorization; the guard layer is the enforcement point.
    """
    entries: list[str] = []
    for verb in SCRATCH_SAFE_VERBS:
        entries.append(f"Bash({verb}:*)")
        entries.append(f"Bash({verb} *)")
    return sorted(entries)


def write_scope_permission_fragment(config: WriteScopeConfig) -> list[str]:
    """Return the sorted `Edit(...)`/`Write(...)` permission-allowlist
    entries for a `WriteRole.SCOPED` identity's `WriteScopeConfig` — the
    harness-settings sink half of the single-source contract for
    `guard.write_scope` (module docstring COVERAGE).

    Source: `config.allowed_paths` — the EXACT SAME fnmatch-glob tuple
    `guard.write_scope.check_write_scope` evaluates a Write/Edit call's
    `file_path` against at guard-layer hook-fire time. Two entries per
    glob (`Edit(<glob>)` and `Write(<glob>)`), mirroring
    `scratch_permission_fragment`'s "both tool-call recording shapes" and
    `provisioning.allowlist`'s "both Bash forms" precedent — a harness may
    record a scoped-write tool call under either tool name.

    DELIBERATELY NOT EMITTED here (both are real single-source decisions,
    not omissions):

      - `config.allow_all` — a caller with `allow_all=True` grants
        unrestricted project-tree write access; a settings-fragment glob
        wide enough to mirror that (e.g. `Edit(**)`) would then be the
        SAME grant regardless of `allowed_paths`, but `blocked_paths`
        still deny-wins UNDERNEATH it at the guard layer (see
        `guard.write_scope.check_write_scope`'s resolution order) — an
        outer settings glob has no equivalent "except these paths" carve-
        out syntax to mirror that deny-wins guarantee. Emitting a
        blanket-allow settings entry for `allow_all` would let the OUTER
        classifier silently admit a `blocked_paths` match that the guard
        layer would still correctly deny if it were ever actually
        reached — not a disagreement in the dangerous direction (the
        guard layer still enforces `blocked_paths` on every call; nothing
        is bypassed), but a needless widening of the outer classifier
        beyond what this function can single-source honestly. A caller
        with `allow_all=True` and no `blocked_paths` legitimately wants
        the outer classifier just as open; that caller is free to land
        its OWN `Edit(**)`/`Write(**)` entry directly — this generator
        only ever emits what it can derive mechanically from
        `allowed_paths` without inventing a negation syntax no harness
        settings format actually has.
      - `config.blocked_paths` — a settings `permissions.allow` list has
        no DENY entry shape; there is nothing for a deny-list to sink
        into here (mirrors `guard.credential_paths`'s module-docstring
        rationale for why a pure denylist has no dual-sink counterpart at
        all).

    A `config` with `allow_all=True` and empty `allowed_paths` returns an
    empty fragment (nothing single-sourceable — see above); this is
    correct, not a gap: the guard layer's own `check_write_scope` still
    grants every non-blocked call for that config, this function simply
    has nothing safe to mirror at the coarser settings layer.
    """
    entries: list[str] = []
    for glob in config.allowed_paths:
        entries.append(f"Edit({glob})")
        entries.append(f"Write({glob})")
    return sorted(entries)


__all__ = [
    "scratch_permission_fragment",
    "write_scope_permission_fragment",
]
