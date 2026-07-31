"""guard.infra_ops — INFRA (host-operator) Bash-command allow-checker
(lr-6f61aa, sub-slice SE4 of the guard-bash per-role checker port; sub-epic
lr-19ae42, grand-epic lr-5a8d Wave C — THIS SLICE COMPLETES lr-19ae42, all
four sub-slices now landed).

WHY A SEPARATE MODULE FROM `role_allowlist.py` (no-god-file, CLAUDE.md,
mirrors `guard.director_mutation`'s own precedent): `role_allowlist.py` is
already >1800 lines after SE1/SE2/SE3. `role_allowlist.BashRole.INFRA` and
`role_allowlist.check_infra_command` (added by this slice) compose this
module exactly as `role_allowlist` already composes `shell_parsing`/
`bash_admission`/`scratch_policy`/`director_mutation` — this module performs
no shell-word normalization or containment of its own beyond what it
delegates to `guard.scratch_policy`.

PORT PATTERN (mirrors `guard.write_scope`/`guard.role_allowlist`/
`guard.director_mutation`'s own documented five-point convention; the short
version applied here):

1. STRIP THE HOOK SHELL. Every function here is a pure function over an
   explicit command string plus caller-supplied config — no stdin read, no
   process exit, no hook-contract awareness (CLAUDE.md rule 6a).
2. AGENT NAME -> ROLE. The reference's `_is_allowed_infra_ops` (reference
   guard-bash.py ll.5095-5553) is the reference deployment's
   MUTATING-INFRASTRUCTURE / HOST-OPERATOR identity checker — its
   highest-blast-radius identity (SSH + credential rotation), scoped to
   exactly five fixed-path op wrappers, each admitted only in a narrow,
   flag-based shape (never a raw command string). This collapses into ONE
   role, `BashRole.INFRA` (added to `role_allowlist.BashRole` by this
   slice) — there is only one reference checker in this family, so there is
   no multi-identity collapse question to resolve (unlike SE2's five-role
   split or SE3's director/lead collapse).
3. NO HARDCODED MACHINE/OPERATOR PATHS OR OP-WRAPPER NAMES. Every reference
   literal naming a fixed absolute script-install path or a fixed op-wrapper
   binary name (`infra-ops-install-binary`, etc.) is generalized to
   `InfraOpsConfig` —  a caller supplies its own installed op-wrapper verb
   names/paths (CLAUDE.md rule 1); no specific binary name or absolute path
   is hardcoded here.
4. NO PERMISSIVE FALLBACK. `check_infra_command` returns a closed (ok,
   reason) tuple for anything not matching one of the five enumerated op
   shapes or the narrow `lore` read/audit-write subset — there is no
   "unhandled op, warn and allow" branch (the reference's own posture: an
   unresolved flag or unexpected positional argument falls through to the
   reference's own generic deny, never caught reactively after the fact).
5. REUSE LANDED PRIMITIVES; NEVER DUPLICATE A BOUNDARY. This module performs
   NO parsing or scratch-containment of its own — it composes
   `guard.shell_parsing.compound_check` and `guard.scratch_policy.
   resolve_scratch_boundary` (the SAME $TMPDIR containment boundary
   `guard.scratch_policy.is_scratch_contained` already establishes for
   every other role in this package — lr-f8649f: $HOME dropped) rather than
   forking a second copy of either.

WHAT THIS MODULE PORTS (reference guard-bash.py ll.5095-5553):

  - `check_infra_op_wrapper` — port of the reference's five fixed-path op
    wrapper admission patterns (`infra-ops-install-binary`,
    `infra-ops-rotate-token`, `infra-ops-restart-service`,
    `infra-ops-install-local-package`, `infra-ops-run-scoped-command` in its
    two forms — `--template-params-json` and `--template-params-file`) plus
    the `--template-params-file` path-containment follow-up check
    (reference `_infra_ops_params_file_path_contained`, lr-c88fba) and the
    narrow `lore observe`/`lore task comment`/`lore task show|list`/`lore
    search` read/audit-write subset (reference lr-b3de6c).

WHY THE INFRA POSTURE GENUINELY DIFFERS FROM EVERY PRIOR ROLE (this task's
dispatch instruction: "its posture may differ from the other roles;
preserve exactly what it admitted/denied"): every prior role in this
decomposition (BUILDER/MERGER/REVIEWER/SECURITY/ANALYSIS/RESEARCH/
PLANNING_READER/LEAD) admits a command by matching a VERB PREFIX against an
otherwise free-form argv tail (`^git(\\s|$)`, `^lore\\s+task\\s+show(\\s|$)`,
etc.) — the command's own arguments are never structurally constrained
beyond that leading token. INFRA is structurally different: the reference's
own module comment (guard-bash.py, above `_INFRA_OPS_INSTALL_PATH_PREFIX`)
states this explicitly — "NARROW BY DESIGN ... each of the five fixed-path
op wrappers is admitted ONLY as a flag-based invocation whose argv carries
EXACTLY the typed fields [the op's] own input-schema.json already validates
for that op — NEVER a raw command string." Every one of the five admission
patterns is an `^...$` WHOLE-STRING anchor (not merely a leading-token
anchor) over an exact, ordered `--flag <value>` sequence with a closed value
grammar — `_INFRA_VALUE` for a plain flag value, `_INFRA_TEMPLATE_PARAMS_
JSON_VALUE` for the `--template-params-json` shape, and `_INFRA_ACK_VALUE_RE`
for the optional leading ack-identity prefix. EVERY ONE of these three value
grammars excludes whitespace and, specifically for the command-injection-
boundary property, excludes `$` and backtick — so no `$(...)`/backtick
command substitution and no `$VAR` expansion can ever satisfy any admitted
value grammar — so no extra flag, positional argument, or trailing shell
content can ever be appended past the last required value, and no value
itself can smuggle a live shell expansion. This module preserves that exact
posture (see `check_infra_op_wrapper`'s own docstring "MANDATORY ANSI-C
ANALYSIS" section for why the SE1/SE2/SE3 bare-verb-grant ANSI-C hard-deny
gate is genuinely inapplicable to this shape, rather than silently omitted).

FLAGGED LOSSY-COLLAPSE / SCOPE POINTS (explicit callouts per this task's
dispatch instruction):

  - The reference's approval-ack env-assignment prefix
    (`INFRA_OPS_APPROVAL_ACKED_BY=<value> `, reference lr-77c9f9) is preserved
    as an OPTIONAL leading prefix on the two `run_scoped_command` shapes
    only (exactly the reference's own scope — the other three op wrappers
    never admit this prefix, matching the reference precisely). The value
    grammar (`_INFRA_ACK_VALUE`) is deliberately narrower than the general
    `_INFRA_VALUE` op-argument grammar — alphanumeric/hyphen/underscore
    only, no `$` — mirroring the reference's own security rationale exactly
    (an acked_by value is a human/operator identity string, never
    shell-interpreted, so it is constrained to the conservative safe-token
    alphabet the reference's own `CREW_SPAWN_AGENT_ID=` hardening precedent
    already established for an inline env-assignment prefix).
  - The `--template-params-file` path-containment follow-up
    (`is_infra_params_file_path_contained`) is a NEW, infra-specific
    containment check in the reference (lr-c88fba), deliberately NOT reusing
    `scratch_policy.is_scratch_contained` itself (that function classifies
    an ENTIRE simple command by verb + every path-shaped argument; this
    check instead extracts ONE specific flag's value out of an
    already-argv-shape-matched op-wrapper invocation and resolves it against
    the SAME `$TMPDIR` boundary model (lr-f8649f: `$HOME` dropped) via
    `scratch_policy.resolve_scratch_boundary` directly) — this module
    reuses that boundary-resolution primitive rather than re-deriving its
    own realpath/symlink-escape logic a second time, exactly as the
    reference's own comment above `_infra_ops_params_file_path_contained`
    describes reusing `_resolve_scratch_boundary` "already generic over
    env_var, not mkdir-specific" rather than duplicating its realpath logic.
  - The reference's `lore` surface for this role is DELIBERATELY NARROWER
    than every other role that admits `lore` at all (reference lr-b3de6c
    comment: "a narrow, enumerated read set only -- no `lore *` wildcard,
    and no lore write/mutation verb beyond the existing observe/task-comment
    (no task close/create/update)"): `lore observe` / `lore task comment`
    (the two AUDIT-WRITE verbs every mutating op must be able to invoke, per
    the reference's own hard constraint 6 — "every mutating op writes an
    audit entry") plus `lore task show|list` / `lore search` (READ-ONLY,
    so this role can read back the task it is acting on and any operator
    lore_comment acknowledgment per the reference's own HITL-gate
    cross-reference, without gaining any new mutation surface). This module
    ports that exact four-pattern subset as a fixed part of
    `check_infra_op_wrapper` — not generalized to caller config, since (per
    the reference's own comment) this is a role-defining SECURITY boundary
    ("no `lore *` wildcard"), not an installation detail a caller should be
    able to widen by config the way `RoleAllowlistConfig.extra_verb_patterns`
    lets a caller widen BUILDER's/MERGER's more permissive `lore`/verb
    surfaces. A caller wanting a DIFFERENT (narrower) `lore` subset for its
    own INFRA-role identity is out of this module's scope entirely — this
    slice preserves the reference's exact subset, not a caller-tunable
    superset or subset of it.
  - No `git *`, no push/PR-transport verb, no Write/Edit admission of any
    kind is ported here at all (reference hard constraints 1 and 5) — these
    are simply ABSENT from `check_infra_op_wrapper`'s admitted-pattern list,
    matching the reference's own posture that this role's Bash surface is
    "exactly these fixed-path op wrappers, each in its narrow flag shape
    only." There is no reactive deny for `git`/Write/Edit to port, because
    the reference never admits them in the first place for this identity —
    the fail-closed default deny at the bottom of `check_infra_op_wrapper`
    already covers this by construction.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from clagentic_loadout.guard.scratch_policy import resolve_scratch_boundary
from clagentic_loadout.guard.shell_parsing import compound_check

#: Shared value-token grammar for every INFRA op-wrapper flag value: a
#: single shell word with no whitespace and no shell metacharacter (matches
#: the reference's `_INFRA_OPS_VALUE` — host FQDN/localhost, package name,
#: version, service names, paths, template_id). Excludes quote/pipe/
#: redirect/substitution characters so a value can never smuggle a compound
#: shell expression even if `compound_check` (run first, see
#: `check_infra_op_wrapper`) were somehow bypassed — belt-and-suspenders,
#: matching the reference's own stated rationale exactly.
_INFRA_VALUE = r"[^\s'\"|&;<>$`]+"

#: `template_params` JSON-object value grammar for the `--template-params-
#: json` shape (reference `_INFRA_OPS_TEMPLATE_PARAMS_JSON_VALUE`): a single,
#: whitespace-free (caller-minified) shell word shaped like a JSON object.
#: JSON legitimately contains double quotes and colons -- only whitespace
#: and genuine shell metacharacters are excluded, since a compact/minified
#: JSON blob never needs any of those. MUST additionally exclude `$` and
#: backtick (command-substitution openers) -- unlike `_INFRA_VALUE`'s single
#: shell-word grammar, a naive `{...}`-bracket grammar that only excludes
#: whitespace/pipe/redirect/quote/semicolon does NOT exclude `$(...)` or
#: `` `...` `` command substitution, since neither uses whitespace or those
#: other metacharacters -- a value like `{"a":"$(id)"}` would otherwise
#: satisfy the grammar while carrying a live command substitution invisible
#: to `compound_check` (which only scans for compound/piped/chained/
#: backgrounded shell operators, never a bare `$(` inside a single word).
#: This closes that gap (pre-merge security-audit finding on this host-
#: mutation op wrapper, lr-6f61aa follow-up) so the module docstring's "no
#: `$`, no command-substitution opener can appear inside an admitted value"
#: invariant holds for EVERY value grammar this module admits, not just
#: `_INFRA_VALUE`.
_INFRA_TEMPLATE_PARAMS_JSON_VALUE = r"\{[^\s'|&;<>$`]*\}"

#: Narrower value grammar for the optional `<ACK_VAR>=<value>` leading
#: env-assignment prefix on the two run_scoped_command shapes only
#: (reference `_INFRA_OPS_APPROVAL_ACKED_BY_PREFIX`): alphanumeric plus
#: hyphen/underscore ONLY, no `$` — an acked_by value is a human/operator
#: identity string, never shell-interpreted, so it is constrained to the
#: same conservative safe-token alphabet the reference's own
#: `CREW_SPAWN_AGENT_ID=` hardening precedent already established.
_INFRA_ACK_VALUE_RE = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"


@dataclass(frozen=True)
class InfraOpWrapper:
    """One admitted INFRA op-wrapper shape (reference: one of the five
    `_INFRA_OPS_*_RE` patterns).

    verb_name: the op wrapper's bare basename (e.g. an installed
        `infra-install-binary`-shaped verb) — a caller supplies its own
        installed verb name here; no specific binary name is hardcoded in
        this module (CLAUDE.md rule 1).
    required_flags: the ordered sequence of `--flag` names this op's
        argv must carry, each followed by a single `_INFRA_VALUE`-grammar
        shell word — mirrors the op's own input-schema `params` shape
        exactly (reference: `_infra_ops_wrapper_pattern`'s `flag_seq`).
    template_params_json_flag: when set (instead of a plain
        `_INFRA_VALUE`-grammar value), the LAST flag in *required_flags*
        takes a whitespace-free JSON-object-shaped value
        (`_INFRA_TEMPLATE_PARAMS_JSON_VALUE`) rather than the generic
        grammar — reference: `infra-ops-run-scoped-command`'s
        `--template-params-json` shape. Mutually exclusive with
        *template_params_file_flag*.
    template_params_file_flag: when set, the LAST flag in *required_flags*
        takes a plain `_INFRA_VALUE`-grammar path token that MUST
        additionally resolve (after `$TMPDIR` expansion — lr-f8649f: `$HOME`
        dropped — and realpath canonicalization) under a configured scratch
        boundary —
        checked by `is_infra_params_file_path_contained`, not by the argv
        pattern alone (reference: `infra-ops-run-scoped-command`'s
        `--template-params-file` shape, lr-c88fba). Mutually exclusive with
        *template_params_json_flag*.
    admits_ack_prefix: True iff this op wrapper additionally admits the
        optional leading `<ack_env_var>=<value> ` env-assignment prefix
        (reference: BOTH `run_scoped_command` shapes only — the other three
        op wrappers never admit this prefix).
    install_path_prefix: optional absolute directory a caller's own
        installed op wrappers additionally resolve from (reference:
        `_INFRA_OPS_INSTALL_PATH_PREFIX`) — a caller with only PATH-based
        bare-basename dispatch may leave this `None`, in which case only
        the bare-basename form is recognized.
    """

    verb_name: str
    required_flags: tuple[str, ...]
    template_params_json_flag: str | None = None
    template_params_file_flag: str | None = None
    admits_ack_prefix: bool = False
    install_path_prefix: str | None = None

    def __post_init__(self) -> None:
        if self.template_params_json_flag and self.template_params_file_flag:
            raise ValueError(
                "InfraOpWrapper: template_params_json_flag and "
                "template_params_file_flag are mutually exclusive."
            )


def _build_infra_wrapper_re(wrapper: InfraOpWrapper, *, ack_env_var: str | None) -> re.Pattern[str]:
    """Build the whole-string (`^...$`) admission regex for one
    `InfraOpWrapper` (reference `_infra_ops_wrapper_pattern`).

    Anchored on BOTH ends so no extra flag, positional argument, or
    trailing shell content can be appended past the last required value —
    the structural property this module's docstring "WHY THE INFRA POSTURE
    GENUINELY DIFFERS" section explains in full.
    """
    flag_parts = []
    for flag in wrapper.required_flags:
        if flag == wrapper.template_params_json_flag:
            value_grammar = _INFRA_TEMPLATE_PARAMS_JSON_VALUE
        else:
            value_grammar = _INFRA_VALUE
        flag_parts.append(re.escape(flag) + r"\s+" + value_grammar)
    body = r"\s+".join(flag_parts)

    if wrapper.install_path_prefix is not None:
        verb_alt = (
            r"(?:"
            + re.escape(wrapper.install_path_prefix.rstrip("/") + "/" + wrapper.verb_name)
            + r"|" + re.escape(wrapper.verb_name) + r")"
        )
    else:
        verb_alt = re.escape(wrapper.verb_name)

    if wrapper.admits_ack_prefix and ack_env_var:
        ack_prefix = (
            r"(?:" + re.escape(ack_env_var) + r"=" + _INFRA_ACK_VALUE_RE + r"\s+)?"
        )
    else:
        ack_prefix = ""

    return re.compile(r"^" + ack_prefix + verb_alt + r"\s+" + body + r"$")


@dataclass(frozen=True)
class InfraOpsConfig:
    """Caller-supplied configuration for `check_infra_op_wrapper`
    (`BashRole.INFRA`).

    op_wrappers: the enumerated set of admitted op-wrapper shapes — a
        caller supplies its own installed verb names/flag sequences (see
        `InfraOpWrapper`); no default (an INFRA role with zero configured
        ops admits nothing beyond the fixed `lore` read/audit-write
        subset, matching a caller that has not yet wired any op registry
        in — a config/sequencing state, not a permissive fallback).
    ack_env_var: the exact env-VAR NAME an optional leading
        `<ack_env_var>=<value> ` prefix admits on op wrappers with
        `admits_ack_prefix=True` (reference: `INFRA_OPS_APPROVAL_ACKED_BY`).
        `None` (the default) means no op wrapper admits any such prefix at
        all, regardless of its own `admits_ack_prefix` flag — a caller not
        using an operator-ack propagation mechanism need not configure
        this.
    extra_verb_patterns: additional admitted command-prefix regexes beyond
        the enumerated op-wrapper set and the fixed `lore` subset (e.g. a
        caller's own additional narrow read-only op) — matched with
        `re.match` against the raw command, same contract as every other
        role checker's `extra_verb_patterns` field in this package.
    """

    op_wrappers: tuple[InfraOpWrapper, ...] = ()
    ack_env_var: str | None = None
    extra_verb_patterns: tuple[re.Pattern[str], ...] = field(default_factory=tuple)


#: Fixed `lore` read/audit-write subset (reference lr-b3de6c comment: "a
#: narrow, enumerated read set only -- no `lore *` wildcard, and no lore
#: write/mutation verb beyond the existing observe/task-comment"). See
#: module docstring FLAGGED LOSSY-COLLAPSE / SCOPE POINTS for why this is
#: NOT generalized to caller config.
_INFRA_LORE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^lore\s+observe(\s|$)"),
    re.compile(r"^lore\s+task\s+comment(\s|$)"),
    re.compile(r"^lore\s+task\s+(show|list)(\s|$)"),
    re.compile(r"^lore\s+search(\s|$)"),
)

#: Extracts a `--template-params-file <value>` flag's value out of an
#: already argv-shape-matched command (reference: the regex inside
#: `_infra_ops_params_file_path_contained`).
_TEMPLATE_PARAMS_FILE_VALUE_RE = re.compile(
    r"--template-params-file\s+(" + _INFRA_VALUE + r")"
)


def is_infra_params_file_path_contained(
    command: str, *, env: dict[str, str] | None = None
) -> bool:
    """Return True iff *command* does not use `--template-params-file` at
    all, OR uses it with a value that resolves (realpath, symlink-and-`..`-
    safe) under a configured `$TMPDIR` scratch boundary (reference
    `_infra_ops_params_file_path_contained`, lr-c88fba).

    lr-f8649f: narrowed from `("HOME", "TMPDIR")` to `TMPDIR`-only — `$HOME`
    is no longer a sanctioned scratch-staging boundary anywhere in this
    package (see `guard.scratch_policy`'s own "TMPDIR-ONLY NARROWING"); a
    `--template-params-file` value resolving under `$HOME` alone is no
    longer contained and must fall through to the deny below, exactly like
    a value resolving to any other out-of-scratch location.

    Reuses `guard.scratch_policy.resolve_scratch_boundary` directly (never
    a second, forked realpath/symlink-escape implementation — module
    docstring FLAGGED LOSSY-COLLAPSE / SCOPE POINTS) — the SAME boundary
    model every other scratch-containment check in this package already
    uses. Checked in ADDITION to the op wrapper's own whole-string argv
    pattern (which only proves the flag SHAPE is well-formed, not that the
    path is safe) — `check_infra_op_wrapper` calls this AFTER a
    `--template-params-file`-shaped wrapper pattern has already matched.
    """
    match = _TEMPLATE_PARAMS_FILE_VALUE_RE.search(command)
    if not match:
        return True  # no such flag present; nothing to contain
    raw_target = match.group(1)
    boundary = resolve_scratch_boundary("TMPDIR", env=env)
    if boundary is None:
        return False
    resolved = os.path.realpath(os.path.expandvars(raw_target))
    return resolved == boundary.resolved_path or resolved.startswith(
        boundary.resolved_path + os.sep
    )


def check_infra_op_wrapper(
    command: str, *, config: InfraOpsConfig | None = None
) -> tuple[bool, str]:
    """Return (ok, reason) for `BashRole.INFRA` — port of the reference
    deployment's mutating-infrastructure/host-operator Bash-command checker
    (reference `_is_allowed_infra_ops`, guard-bash.py ll.5095-5553).

    Admission pipeline (mirrors the reference's exact composition order):
      1. `guard.shell_parsing.compound_check` — hard-deny on any compound/
         piped/chained/backgrounded shell expression (reference:
         `_compound_check`, run BEFORE the op-wrapper pattern list, exactly
         as every other role checker in this package runs it first).
      2. `config.op_wrappers` — the enumerated, whole-string-anchored
         op-wrapper shapes (reference: the five `_INFRA_OPS_*_RE` patterns).
         A `--template-params-file`-shaped wrapper match additionally
         requires `is_infra_params_file_path_contained` before admitting
         (reference lr-c88fba follow-up).
      3. The fixed `lore` read/audit-write subset
         (`_INFRA_LORE_PATTERNS`) — see module docstring FLAGGED
         LOSSY-COLLAPSE / SCOPE POINTS for why this is a fixed part of this
         checker, not caller-configurable.
      4. `config.extra_verb_patterns` — caller-supplied additional admitted
         command prefixes.

    MANDATORY ANSI-C ANALYSIS (this module's own MANDATORY review per
    `guard-policy.md`'s Slice-5 "Post-landing hardening" section, applied
    here explicitly since — per this task's dispatch instruction — this
    checker's shape differs from every prior bare-verb-grant checker that
    section targets, and the reason must be documented rather than the gate
    silently omitted or an inapplicable gate bolted on): the mandatory
    normalize-then-hard-deny pattern exists specifically to close an
    evasion where an ANSI-C-quote-fragmented FORBIDDEN operation still
    matches an unrelated BARE-VERB leading-token affirmative grant (e.g.
    `git $'push' --force` still matching a bare `^git(\\s|$)` grant while
    evading a raw substring scan for `push --force`). This checker has NO
    such shape anywhere in its admission pipeline:

      - Every op-wrapper pattern is a WHOLE-STRING (`^...$`) anchor over an
        EXACT, ordered `--flag <value>` sequence with a closed
        no-shell-metacharacter value grammar (`_INFRA_VALUE`/
        `_INFRA_TEMPLATE_PARAMS_JSON_VALUE`) — there is no free-form argv
        tail past a leading verb token for a fragmented forbidden operation
        to hide inside. An ANSI-C span appearing ANYWHERE in the value
        position would need to decode to a value STILL matching that flag's
        own closed grammar (no whitespace, no shell metacharacter) to have
        any chance of matching at all — and EVERY value grammar this module
        admits (`_INFRA_VALUE`, `_INFRA_TEMPLATE_PARAMS_JSON_VALUE`, and
        `_INFRA_ACK_VALUE_RE`) excludes `$` and backtick, so a
        `$'...'`/`$"..."` ANSI-C-quote OPENER — and, not incidentally, a
        `$(...)`/backtick command-substitution opener or a bare `$VAR`
        expansion — cannot even appear inside an admitted value without
        failing the argv-shape match outright, independent of whether it
        would decode cleanly. This is a REQUIRED-EXACT-SHAPE check, not a
        forbidden-substring-scan-feeding-a-bare-verb-grant — the shape class
        the mandatory gate targets does not exist here.
      - The fixed `lore` subset (`_INFRA_LORE_PATTERNS`) is a
        leading-token-anchored grant, matching the shape the mandatory
        gate targets MORE closely than the op-wrapper patterns above — but
        this role has NO forbidden-substring deny check of its own for
        these four patterns to evade in the first place (unlike
        BUILDER/MERGER's bare `^git(\\s|$)` grant, which coexists with
        `check_forbidden_git_patterns`'s `git push --force`-shaped deny
        list that a fragmented `git $'push' --force` could otherwise
        evade). `lore observe`/`lore task comment`/`lore task show|list`/
        `lore search` admit ONLY those exact subcommand shapes; there is no
        WIDER `lore` grant (no bare `lore` wildcard, per the reference's
        own comment) an ANSI-C-fragmented subcommand token could smuggle a
        DIFFERENT, otherwise-forbidden `lore` subcommand past — an
        ANSI-C-wrapped subcommand token (`lore $'task'... `) that failed to
        decode would simply fail to match any of the four patterns and fall
        through to the fail-closed deny at the bottom of this function,
        exactly as a garbled subcommand of any other kind would.

      Per this task's dispatch instruction ("if the infra role has a
      required-presence-only check shape ... where the ANSI-C gate is
      genuinely N/A, document WHY rather than omit silently OR bolt on an
      inapplicable gate"): this documents that analysis explicitly, rather
      than either silently omitting the gate or calling
      `role_allowlist.check_ansi_c_quote_denied` here where it would have
      no evasion class to close (mirroring
      `check_director_identity_discipline`'s own precedent, SE3 PR1 —
      "every branch below is a REQUIRED-PRESENCE check ... this checker's
      shape differs from every prior bare-verb-grant checker").

    Fails closed for anything matching none of the above.
    """
    cfg = config if config is not None else InfraOpsConfig()

    ok, reason = compound_check(command)
    if not ok:
        return False, reason

    for wrapper in cfg.op_wrappers:
        pattern = _build_infra_wrapper_re(wrapper, ack_env_var=cfg.ack_env_var)
        if pattern.match(command):
            if wrapper.template_params_file_flag is not None:
                if not is_infra_params_file_path_contained(command):
                    return False, (
                        f"--template-params-file must resolve (after "
                        f"symlink/`..` canonicalization) under "
                        f"$TMPDIR: {command[:160]!r}"
                    )
            return True, ""

    for pat in _INFRA_LORE_PATTERNS:
        if pat.match(command):
            return True, ""

    for pat in cfg.extra_verb_patterns:
        if pat.match(command):
            return True, ""

    return False, (
        f"command not in infra-role allowlist: {command[:160]!r}. INFRA's "
        f"Bash surface is exactly the caller-configured op-wrapper shapes "
        f"(each in its narrow, whole-string flag shape only -- no raw "
        f"command string, no git, no push/PR-transport verb, no Write/Edit) "
        f"plus lore observe, lore task comment, lore task show|list, lore "
        f"search (narrow read/audit-write subset -- no bare lore wildcard, "
        f"no lore task close|create|update), plus any caller-configured "
        f"extra_verb_patterns."
    )


__all__ = [
    "InfraOpsConfig",
    "InfraOpWrapper",
    "check_infra_op_wrapper",
    "is_infra_params_file_path_contained",
]
