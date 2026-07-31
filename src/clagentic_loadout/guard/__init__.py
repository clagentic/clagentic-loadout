"""guard — role-keyed guard policy library (lr-5a8d Wave C epic).

This package carries the Wave C guard-hook port (tome #687 Phase 3, lore
epic lr-5a8d) as a harness-agnostic policy library — pure decision
functions over explicit inputs, never a PreToolUse hook script itself. See
docs/guard-policy.md for the full scope-split and per-slice landing record.

Slice 1 (lr-5a8d comment #6, PR #110) — the two acceptance criteria that
gate every later slice, because both are POLICY shapes a hook script
consumes rather than hook mechanics themselves:

  - ``env_prefix``: an exactly-anchored env-var-assignment-prefix admission
    rule (lr-24b2 fold-in) — the reference deployment's equivalent uses an
    unconstrained ``VAR=value`` grammar ahead of a real command, which is
    the defect this module refuses to reproduce.
  - ``scratch_policy``: CATEGORY-based spawn-scratch containment (comment
    #2, operator-driven) — any command whose entire write/effect target
    resolves inside a spawn-isolated scratch root ($TMPDIR/, $HOME/) is
    admitted regardless of verb, with path canonicalization and symlink-
    escape handling; the moment a target escapes containment it is denied.
  - ``settings_export``: single-source, dual-sink fragment generator for
    the scratch-containment verb set.

Slice 2 (lr-fd279d, PATTERN-SETTER for the remaining decomposition) — the
first two small named-hook ports, ROLE-KEYED rather than agent-named
(CLAUDE.md rule 1):

  - ``write_scope``: Write/Edit scope enforcement, port of the reference
    deployment's guard-scope.py. Keys on ``WriteRole`` (SCOPED / MERGE_GATE
    / LEAD / READ_ONLY), never an agent name.
  - ``credential_paths``: Read/Glob/Bash credential-path denial, port of
    the reference deployment's guard-credentials.py. Role-independent (the
    reference applies the same check to every managed identity); takes
    caller-supplied protected-path prefixes instead of a hardcoded
    ``/root/`` assumption.

See ``write_scope``'s module docstring for the full role-keyed port
convention both modules in slice 2 establish, for the guard-bash
decomposition (lr-7ff55e/lr-288fad/lr-19ae42) and remaining small-guard
slices (lr-59dd37) to mirror.

Slice 3 (lr-7ff55e) — the guard-bash decomposition, part 1 of 3 (foundation):

  - ``shell_parsing``: shell-word normalization and compound-command
    detection CORE, port of the reference deployment's guard-bash.py
    parsing layer (reference ll.148-1400). Pure text-processing primitives
    with no policy content — ``compound_check``, ``normalize_shell_words``,
    ``detect_tmp_redirect_target``, and friends — that the containment
    layer (lr-288fad) and per-role allow-checkers (lr-19ae42) build on.

See ``shell_parsing``'s module docstring for the full decomposition
rationale and the exact function-by-function port mapping.

Slice 4 (lr-288fad) — the guard-bash decomposition, part 2 of 3
(containment/scratch/scope):

  - ``bash_admission``: project-tree write-target detection, the generic
    ``loadout-<verb>`` read/validate family grant, the generic
    "METHOD to PATH requires FLAG" comment-post admission check, and the
    loadout-verb ``--body-stdin`` pipe carve-out (reference ll.1400-2560).
    Builds on ``shell_parsing`` for every structural parse and REUSES
    ``scratch_policy``'s containment boundary rather than forking a second
    one — see ``bash_admission``'s module docstring "RECONCILIATION"
    section for why the reference's own mkdir-only containment bridge is
    superseded by ``scratch_policy``, not re-ported.

See ``bash_admission``'s module docstring for the full reconciliation
rationale and the two items inherited from the parsing-core slice
(the body-stdin pipe carve-out, and the ANSI-C-quote evasion wiring).

Slice 5 (lr-19ae42, sub-slice SE1 = lr-7feafc) — the guard-bash
decomposition, part 3 of 3 (per-role allow-checkers), sub-sliced by role
family:

  - ``role_allowlist``: ``BashRole.BUILDER`` and ``BashRole.MERGER``
    Bash-command allow-checkers (reference ``_is_allowed_amos`` /
    ``_is_allowed_naomi``, reference guard-bash.py ll.2564-2932). Composes
    ``shell_parsing.compound_check``, ``scratch_policy.is_scratch_contained``,
    and ``bash_admission.is_admitted_loadout_family_readonly`` — no parsing
    or containment logic is reimplemented in this module.

See ``role_allowlist``'s module docstring for the full agent-name -> role
mapping table and the explicitly flagged lossy-collapse points.

Sub-slice SE2 (lr-a64227) adds five more ``BashRole`` members to the SAME
``role_allowlist`` module: ``REVIEWER``, ``SECURITY``, ``ANALYSIS``,
``RESEARCH``, ``PLANNING_READER`` — ports of the reference's read-only
PR/commit-reviewer, pre-merge security-audit-gate, troubleshooting-
detective, platform-observer, crew-researcher, and build-planning-agent
per-identity checkers. See ``role_allowlist``'s module docstring COLLAPSE
RATIONALE section for why the troubleshooting-detective and
platform-observer checkers collapse into one ``ANALYSIS`` role while the
crew-researcher and build-planning-agent checkers each keep their own role.

Remaining sub-slices (SE3 director/lead checkers, SE4 infra role) add their
own ``BashRole`` members and checkers to the same module, mirroring this
convention.

Slice 6 (lr-59dd37) ports the three remaining small, standalone guard hooks
that are NOT part of the guard-bash decomposition (lr-19ae42, completed by
SE4 above):

  - ``task_dispatch``: Task/Agent-dispatch admission. ``check_task_dispatch``
    reuses ``role_allowlist.BashRole`` (the same role vocabulary, not a
    parallel enum); ``check_lead_agent_dispatch`` is the reference's
    independent second path (a non-crew-role session denied from
    dispatching a caller-named "crew role" directly via the Agent tool).
  - ``dispatch_discipline``: a warn-only in-session-edit advisory — the
    reference's only guard hook that never denies. Returns ``str | None``,
    never a deny-shaped tuple.
  - ``git_write_guard``: git/PR write-operation hard-deny for a non-agent
    session (``git push``, ``gh pr create``/``merge``, curl-shaped
    PR-create/merge patterns). ANSI-C-fragmentation hardening applied in
    mirror-image form to ``role_allowlist``'s bare-verb-grant gate — see
    ``git_write_guard``'s module docstring point 5 and docs/guard-policy.md
    Slice 6 for the worked failure-mode example this slice's own
    development surfaced.

See each module's own docstring for the full port-pattern application and
docs/guard-policy.md's "Slice 6" section for the reference-hook mapping
table.
"""

from __future__ import annotations
