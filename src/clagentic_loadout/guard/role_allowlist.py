"""guard.role_allowlist — role-keyed Bash-command allow-checkers (lr-7feafc,
sub-slice SE1 of the guard-bash per-role checker port; sub-epic lr-19ae42,
grand-epic lr-5a8d Wave C).

PORT PATTERN — see `guard.write_scope`'s module docstring for the full
role-keyed port convention this module mirrors (established there as the
PATTERN-SETTER for the remaining Wave C decomposition, lr-fd279d). The short
version applied here:

1. STRIP THE HOOK SHELL. The reference deployment's `_is_allowed_<agent>`
   functions are called from a PreToolUse stdin-JSON-in/exit-code-out hook
   `main()`, keyed off a fixed `agent_type` field carrying one of a small set
   of internal agent names. None of that harness plumbing is ported — every
   function here is a pure function over an explicit command string plus
   caller-supplied config, with no stdin read, no process exit, no
   hook-contract awareness (CLAUDE.md rule 6a).

2. AGENT NAMES -> ROLES. The reference ports here are the reference
   deployment's per-identity Bash-command checker for its general-purpose
   repo-authoring identity (reference guard-bash.py ll.2564-2731) and its
   per-identity checker for its narrow release-gate identity (reference
   ll.2732-2932) — one checker per fixed agent identity. CLAUDE.md rule 1
   forbids agent names in product code, so this module collapses them into
   `BashRole.BUILDER` (behaviorally: the reference's repo-authoring-identity
   checker — a general-purpose identity with a wide command allowlist
   centered on git/lore/push/build-verify) and `BashRole.MERGER`
   (behaviorally: the reference's release-gate-identity checker — the
   narrow release-gate identity whose allowlist is deliberately smaller:
   merge/close/post-merge verbs, a narrow read-only pre-check path, no
   build/lint commands at all). Both are BEHAVIOR buckets keyed on what the
   reference checker actually admits, not on which identity fired it — a
   caller's own role registry (mirroring `provisioning.roles`'s role ->
   verb-set convention, the SAME config-driven mapping pattern reused
   rather than reinvented) supplies which of its own identities maps to
   which `BashRole`; this module never performs that mapping itself and
   never hardcodes a name anywhere.

3. NO HARDCODED MACHINE/OPERATOR PATHS OR VERB NAMES. Every reference
   allowlist entry that named a fixed absolute script path or a fixed
   transport-verb literal is replaced by a caller-supplied
   `RoleAllowlistConfig` — an explicit list
   of extra admitted verb-prefix regexes (`extra_verb_patterns`), a scoped
   build/test/lint pattern set (defaulting to
   `wait.config.DEFAULT_SCOPED_TEST_PATTERNS` — the already-generalized
   loadout-native equivalent of the reference's `AMOS_BUILD_TEST_LINT`,
   reused rather than re-hardcoded a second time), and the loadout-family
   read/validate grant's `mutating_verb_names`/`sanctioned_bin_dir`
   parameters `bash_admission.is_admitted_loadout_family_readonly` already
   exposes. A caller wires its own installed verb names/paths in; no
   specific binary name or absolute path is hardcoded here.

4. NO PERMISSIVE FALLBACK FOR AN UNMAPPED ROLE. `BashRole` is a closed
   two-member enum for this slice (BUILDER, MERGER) — later sub-slices
   (SE2/SE3/SE4) add their own members to the SAME enum rather than
   inventing a parallel one, so a caller's single role-dispatch call site
   never needs a per-slice enum-type switch. `check_bash_call` raises
   `ValueError` for any role this slice does not yet cover — a caller
   passing an unmapped role is a config/sequencing error (the role's
   checker has not landed yet), never a silent allow.

5. REUSE LANDED PRIMITIVES; NEVER DUPLICATE A BOUNDARY. This module performs
   NO parsing, containment, or forbidden-substring scanning of its own — it
   composes `guard.shell_parsing.compound_check`,
   `guard.scratch_policy.is_scratch_contained` /
   `ScratchContainmentError`, and `guard.bash_admission.
   is_admitted_loadout_family_readonly` exactly as the reference's own
   per-identity checker functions compose their own equivalent internal
   helpers, in the SAME order (forbidden-substring check, compound check,
   scratch-mkdir bridge, loadout-family grant, then the per-role
   verb-pattern list) — see each function's docstring for the exact mirrored
   sequence and any FLAGGED lossy spot in the collapse.

BEHAVIOR-PRESERVING MAPPING TABLE (documented here for SE2/SE3/SE4 to mirror,
per this task's dispatch instruction):

    reference checker (identity role) -> loadout role    -> this module's checker
    ------------------------------------------------------------------
    general-purpose repo-authoring     -> BashRole.BUILDER -> check_builder_command
    narrow release-gate                -> BashRole.MERGER  -> check_merger_command

FLAGGED LOSSY COLLAPSE POINTS (explicit callouts per this task's dispatch
instruction — "flag any place the collapse is lossy"):

  - The reference's forbidden-substring check and the `/proc/*/environ`
    guard apply UNIVERSALLY to BOTH the repo-authoring identity and the
    release-gate identity (identical call, identical forbidden list,
    identical order) in the reference file — this is NOT a per-identity
    behavior at all, just duplicated inline in each function. This module
    ports both as ROLE-INDEPENDENT helpers (`check_forbidden_git_patterns`,
    `check_proc_environ_denied`) that both `check_builder_command` and
    `check_merger_command` call identically — matching
    `guard.credential_paths`'s own precedent for a check the reference
    applies uniformly across every identity (that module's docstring point
    1). This is a faithful, non-lossy collapse: the reference never varied
    this behavior per identity, so extracting it once is a deduplication,
    not a policy change.
  - The reference's env-assignment-prefix stripping and its caller-mismatch
    attestation check run in the reference's `main()`, BEFORE either
    per-identity checker is ever called — genuinely harness-adapter
    plumbing (point 1 above), not part of either identity's own allowlist
    logic, and correctly excluded from this slice's scope entirely (a
    caller's own harness adapter is responsible for any such
    pre-normalization before calling into this module, exactly as
    `guard.bash_admission`'s docstring already establishes for the
    `--body-stdin` pipe carve-out's own caller-composition contract).
  - The reference's test-command escape hatch in its repo-authoring-identity
    checker (an env var admitting one extra caller-named literal command)
    is a TEST-ONLY debugging seam with no analogue needed here — a caller
    wanting to admit one extra literal command for its own test fixtures
    passes it via `extra_verb_patterns` instead; this module does not read
    any environment variable itself (CLAUDE.md rule 6a: no env var reads
    baked into product policy code beyond a caller's own explicit config).
  - The release-gate identity's PR-terminal-action verb literals
    (`loadout-merge`, `loadout-close-pr`, `loadout-post-merge`,
    `loadout-release-detect`, `loadout-release-dispatch`) and the
    forgejo-curl/git-host-api wrapper verb names are, in the reference, FIXED
    bare-basename or absolute-install-path regexes with no per-repo
    override. This module generalizes them to `RoleAllowlistConfig.
    extra_verb_patterns` (a caller-supplied list) rather than hardcoding the
    literal `loadout-*` names as regex constants baked into this file — the
    loadout brand's OWN verb names are legitimate product vocabulary (not an
    operator/agent identity), so a caller integrating THIS package with
    itself can and should pass its own installed verb name set here; this
    module does not assume any specific verb naming convention belongs to
    every possible caller.
  - `check_merger_command`'s narrow GET-only pre-check admission (reference:
    the release-gate identity's narrow git-host-api read-only pre-check
    function, gated on a literal `--caller <identity-name>` flag) is
    generalized to a caller-supplied `caller_role` string via
    `MergerReadOnlyConfig` — the CALLER passes whichever role label its own
    attestation model uses for the merger identity (e.g. "merger"), so the
    admitted flag shape becomes `--caller <caller_role>` rather than a
    hardcoded agent name. This is the ONE place in this slice where a bare
    role-name STRING (not an enum member) flows into a regex — flagged here
    explicitly since a caller supplying an unexpected value
    (e.g. one containing a shell metacharacter) could theoretically widen
    the admitted flag shape; `_build_caller_flag_re` constrains the
    caller_role token to the SAME bare-token grammar
    `provisioning.roles._TOKEN_RE` already enforces for role names
    (alphanumeric/hyphen/underscore, no leading hyphen) precisely to close
    that gap — a caller_role value outside that grammar raises ValueError at
    config-construction time rather than ever reaching the regex.

POST-LANDING HARDENING (security-review finding, BLOCKING — fixed same
task, lr-7feafc): the original landing of this module's
`check_forbidden_git_patterns` was a RAW substring match with no
quote-normalization pre-pass, and BOTH `check_builder_command`'s and
`check_merger_command`'s bare `^git(\s|$)` affirmative grant matched the same
raw, un-normalized command with no ANSI-C gate. An ANSI-C-quote-fragmented
forbidden git operation (`git $'push' --force`, `git $'push --force'`) is not
a literal substring match against `DEFAULT_FORBIDDEN_GIT_PATTERNS` (bash only
produces the joined forbidden word AFTER quote-removal), so it evaded the
deny — and the SAME fragmented command still matched the bare `git` prefix
grant unchanged, since fragmentation touches only the operation's own verb/
flag tokens, not the untouched leading `git` token. TWO-PART FIX: (1)
`check_forbidden_git_patterns` now scans `shell_parsing.
normalize_shell_words(command)` instead of the raw string, so a RESOLVABLE
ANSI-C fragmentation (both examples above decode cleanly) is caught by the
substring scan itself; (2) `check_ansi_c_quote_denied` (mirroring `guard.
bash_admission.is_admitted_loadout_family_readonly`'s own ANSI-C gate) now
runs FIRST in both role checkers, hard-denying the residual case where the
ANSI-C span does NOT decode cleanly at all (an unrecognized escape), which
would otherwise make normalization fail and fall back to a raw scan that
still cannot see through the intact `$'...'` wrapper. **BOTH HALVES ARE
MANDATORY FOR EVERY FUTURE BARE-VERB AFFIRMATIVE GRANT PORTED BY SE2/SE3/
SE4**: any checker that affirmatively admits a command by matching only a
LEADING bare-verb token (`^<verb>(\s|$)`) against a raw/un-normalized command
string is vulnerable to this exact class unless (a) any substring-based deny
check it depends on scans the normalized command, not the raw one, and
(b) an ANSI-C-ambiguity hard-deny gate runs ahead of the grant for the
residual unresolvable case — see `guard-policy.md` for the corresponding
cross-slice callout.

SE2 ADDITION (lr-a64227, sub-epic lr-19ae42 sub-slice SE2) — reviewer/
analysis roles: `BashRole.REVIEWER`, `BashRole.SECURITY`, `BashRole.ANALYSIS`,
`BashRole.RESEARCH`, `BashRole.PLANNING_READER`. Ports the reference's six
remaining non-director per-identity checkers (`_is_allowed_peaches`,
`_is_allowed_bobbie`, `_is_allowed_miller`, `_is_allowed_drummer`,
`_is_allowed_prax`, `_is_allowed_avasarala`, reference ll.2933-3618 and
4690-5094) using the SAME port pattern SE1 established above — see each new
role's own docstring and checker function for the mirrored composition
order and any flagged lossy-collapse point.

THE AGENT-NAME -> ROLE MAPPING FOR SE2 (mirrors the table above):

    reference checker (identity role)          -> loadout role              -> this module's checker
    ------------------------------------------------------------------------------------------------
    read-only PR/commit reviewer               -> BashRole.REVIEWER         -> check_reviewer_command
    read-only pre-merge security-audit gate    -> BashRole.SECURITY         -> check_security_command
    read-only troubleshooting detective        -> BashRole.ANALYSIS         -> check_analysis_command
    read-only platform observer                -> BashRole.ANALYSIS         -> check_analysis_command
    read-only crew researcher                  -> BashRole.RESEARCH         -> check_research_command
    read-only build-planning agent             -> BashRole.PLANNING_READER  -> check_planning_reader_command

COLLAPSE RATIONALE (why five roles, not six, and why the split is where it
is — this task's dispatch instruction requires documenting both the collapse
and the places a real authority difference was preserved rather than
flattened):

  - The reference's troubleshooting-detective checker and its platform-
    observer checker (reference `_is_allowed_miller` / `_is_allowed_drummer`)
    are, behaviorally, THE SAME shape with two different config values: both
    admit `lore` read/observe/task-mutation subcommands, a `git -C <repo>`/
    bare `git` READ-ONLY subcommand set (one wider — adds diff/blame — one
    narrower), `systemctl`/`docker` inspection subcommands (identical set),
    a fixed HTTP-status-code curl health probe, and bare file readers (one
    adds `grep`, the other doesn't). Neither reference function varies its
    STRUCTURE from the other — only which literal subcommand/verb tokens are
    in each pattern list — so collapsing them into one `BashRole.ANALYSIS`
    checker parameterized by an `AnalysisRoleConfig` (git subcommand set,
    extra read verbs, extra curl-probe patterns) is a faithful,
    non-lossy collapse: a caller wanting the reference's narrower or wider
    subset supplies its own config value, rather than this module hardcoding
    two near-identical functions that would drift independently over time
    (exactly the `_GIT_READONLY_FULL`/`_GIT_READONLY_NARROW` shared-grammar
    precedent the reference itself already uses for these two).
  - The reference's crew-researcher checker (`_is_allowed_prax`) is
    DELIBERATELY NOT collapsed into `ANALYSIS` despite being read-only like
    the other two: it has ZERO git/systemctl/docker visibility at all (no
    infra or repo-state read surface whatsoever) and its entire admitted
    surface beyond `lore`/file-readers is external-research-engine
    invocation (`gemini -m <model>`, a `gemini-research` wrapper) — a
    genuinely different AUTHORITY SHAPE, not a narrower/wider config value
    of the same shape. Collapsing it into `ANALYSIS` would either (a) grant
    every `ANALYSIS`-role caller external-research-engine invocation it
    never asked for, or (b) grant every `RESEARCH`-role caller git/docker/
    systemctl visibility it should never have — both are real widenings,
    which the port pattern's "no over-collapse" instruction (module
    docstring point 2 analogue) forbids. `BashRole.RESEARCH` is its own
    role, with `check_research_command` taking a caller-supplied
    `research_engine_patterns` set instead of a hardcoded model-name list
    (CLAUDE.md rule 1: no fixed external-tool invocation string baked in
    here for a caller who wires a different research engine).
  - The reference's build-planning agent checker (`_is_allowed_avasarala`)
    is ALSO not collapsed into `ANALYSIS`: it is the ONLY one of the six
    with (a) a `lore task create`/`update` grant (task-AUTHORING, not
    read-only observation — the same authority class `check_merger_command`
    already treats as narrower-but-still-mutating for lore, not identical to
    a pure read-only `lore task show|list`), and (b) a narrow GET-only
    forge-read pre-check gated on a caller-role flag — structurally the SAME
    shape as `check_merger_command`'s own `is_admitted_merger_read_only`
    (reused directly here, not re-implemented, per module docstring point 5
    "reuse landed primitives; never duplicate a boundary"), never a bare
    forge-read grant. Neither capability belongs on `ANALYSIS` (which has no
    forge-read surface at all in the reference) or `RESEARCH` (which has no
    lore-task-authoring or forge-read surface either) — `BashRole.
    PLANNING_READER` is its own role for exactly this reason.
  - `BashRole.REVIEWER` and `BashRole.SECURITY` remain
    two separate roles, matching the sub-epic dispatch's own naming and the
    reference's real behavioral difference: SECURITY alone gets deterministic
    scanner invocation (`gitleaks`/`trufflehog`/`semgrep`/`osv-scanner`) and
    `git log`, while REVIEWER alone gets the Path-D external-model-carrier
    invocation (`codex exec`, `claude -p`) and a narrower `git show`/`git
    diff`-only read surface (no `git log`). Collapsing these two would widen
    whichever role lost its distinguishing grant — precisely the "mis-collapse
    widens/narrows an agent's power" risk lr-19ae42 comment #2 names as the
    reason this whole surface is authority-defining.

SE3 ADDITION (lr-1cc4df, sub-epic lr-19ae42 sub-slice SE3, PR1 + PR2, PR2 =
THIS PR) — director/lead authority checker: `BashRole.LEAD`. Ports the
reference's `_check_director_clagentic` + `_check_director_lead_mutation`
checker pair (reference ll.3619-4689) — the LARGEST and MOST
AUTHORITY-SENSITIVE remaining slice (lr-19ae42 comment #2: "a mis-collapse
here changes what a lead/director can mutate"). Lands in
`guard.director_mutation` (a SEPARATE module — this file is already >1600
lines; see that module's own docstring for the no-god-file rationale),
composed here via `check_lead_command`.

THE AGENT-NAME -> ROLE MAPPING FOR SE3:

    reference checker (identity role)                    -> loadout role  -> this module's checker
    -------------------------------------------------------------------------------------------
    director/lead identity discipline + mutation deny     -> BashRole.LEAD -> check_lead_command

Every reference identity that fires either `_check_director_clagentic` or
`_check_director_lead_mutation` (a "director" identity, and every "lead"-
suffixed identity sharing the reference's `_DIRECTOR_LEAD_NAMES` /
`_LEAD_SUFFIX` convention) collapses into this ONE role — see
`guard.director_mutation`'s own module docstring for the full port pattern
and each function's own ANSI-C-evasion analysis.

SE3 PR1/PR2 SPLIT (this task's dispatch authorized splitting the ~1071-line
slice across two PRs given its size and authority-sensitivity — see task
lr-1cc4df comment thread): PR1 ported `check_director_identity_discipline`
only. PR2 (THIS PR) extends `check_lead_command` with the mutation-verb-
family deny dispatch (git write / file mutation / package mutation /
systemctl mutation / forge-PR-mutation), completing lr-1cc4df — see
`guard.director_mutation.check_lead_mutation`'s own docstring for the full
mutation-verb surface, and that module's docstring "ATTESTATION SEAM
DESIGN" section for the forgejo-curl acting-subagent carve-out: PR1
scope-trimmed that mechanism out (a strictly-no-wider omission, since PR1
had no mutation deny to carve an exception into); THIS PR owns the
mutation deny the carve-out exists to soften, so it ports the carve-out's
actual DECISION LOGIC as a caller-supplied `ActingSubagentResolver` seam
rather than dropping it silently or hardcoding the reference's
harness-specific sidecar-file/agent-name-dispatch-table lookups.

SE4 ADDITION (lr-6f61aa, sub-epic lr-19ae42 sub-slice SE4, FINAL SUB-SLICE —
THIS SLICE COMPLETES lr-19ae42) — infra/host-operator role: `BashRole.INFRA`.
Ports the reference's `_is_allowed_ashford` (reference guard-bash.py
ll.5095-5553) — the reference deployment's mutating-infrastructure /
host-operator identity checker (SSH + credential rotation, the reference's
highest-blast-radius identity). Lands in `guard.infra_ops` (a SEPARATE
module, mirroring `guard.director_mutation`'s own no-god-file precedent —
this file is already >1800 lines), composed here via `check_infra_command`.

THE AGENT-NAME -> ROLE MAPPING FOR SE4:

    reference checker (identity role)                    -> loadout role   -> this module's checker
    -------------------------------------------------------------------------------------------
    mutating-infrastructure / host-operator identity      -> BashRole.INFRA -> check_infra_command

Only one reference checker fires this posture (unlike SE2's five-way split
or SE3's director/lead collapse) — see `guard.infra_ops`'s own module
docstring for the full port pattern, the fixed enumerated op-wrapper shape
this role is scoped to, and the explicit ANSI-C-applicability analysis this
task's dispatch instruction required (`guard.infra_ops.
check_infra_op_wrapper`'s own docstring "MANDATORY ANSI-C ANALYSIS"
section) — INFRA's admission shapes are whole-string-anchored exact-flag
grants with a closed no-metacharacter value grammar, not
forbidden-substring-scan-feeding-a-bare-verb-grant shapes, so the SE1/SE2
mandatory hard-deny gate has no evasion class to close here; that section
documents why rather than omitting the analysis or bolting on an
inapplicable gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from clagentic_loadout.guard.bash_admission import (
    BodyStdinVerb,
    MethodPathFlagRule,
    is_admitted_body_stdin_pipe,
    is_admitted_loadout_family_readonly,
    requires_admission_flag,
)
from clagentic_loadout.guard.director_mutation import (
    DirectorClagenticConfig,
    LeadMutationConfig,
    check_director_identity_discipline,
    check_lead_mutation,
)
from clagentic_loadout.guard.infra_ops import (
    InfraOpsConfig,
    check_infra_op_wrapper,
)
from clagentic_loadout.guard.scratch_policy import (
    ScratchContainmentError,
    is_scratch_contained,
)
from clagentic_loadout.guard.shell_parsing import (
    cmd_head,
    compound_check,
    has_unresolved_ansi_c_quote,
    normalize_shell_words,
)
from clagentic_loadout.wait.config import DEFAULT_SCOPED_TEST_PATTERNS

#: Role-keyed bucket a caller's identity is mapped to (never an agent name).
#: A closed, GROWING enum: later sub-slices (SE2 reviewer/analysis, SE3
#: director/lead, SE4 infra) add their own members here rather than
#: inventing a parallel enum, so `check_bash_call` stays the single
#: role-dispatch entry point for the whole guard-bash decomposition.
from enum import Enum


class BashRole(Enum):
    """The behavior buckets this slice ports.

    BUILDER — general-purpose repo-authoring identity (reference: the
        reference deployment's repo-authoring Bash-command checker): the
        widest allowlist of the two — git/lore, a push/PR transport verb,
        the loadout read/validate family, scoped build/test/lint commands,
        and $TMPDIR staging writes (lr-f8649f: $HOME dropped as a
        scratch-staging root).
    MERGER — narrow release-gate identity (reference: the reference
        deployment's release-gate Bash-command checker): deliberately
        SMALLER than BUILDER — merge/close/post-merge/release verbs, a
        narrow GET-only pre-check read path, a narrower `lore` subset
        (task/observe/update only, no general `lore search`/etc.), and NO
        build/test/lint commands at all (a release gate does not need to
        run the suite itself; CI/pre_checks own that).
    REVIEWER — read-only PR/commit reviewer identity (reference: the
        reference deployment's PR/commit-review Bash-command checker,
        SE2/lr-a64227): lore, the loadout posting/staging verb family with
        `--verify-comment`/`--delete-own-comment` belt-and-suspenders, a
        narrow `git show`/`git diff` read-only surface (no `git log`), an
        external-model-carrier invocation surface (Path D — a caller-
        configured research-carrier CLI, e.g. `codex exec`/`claude -p`),
        and $TMPDIR staging (lr-f8649f: $HOME dropped). No git write, no
        scanner invocation.
    SECURITY — read-only pre-merge security-audit-gate identity (reference:
        the reference deployment's security-audit Bash-command checker,
        SE2/lr-a64227): the SAME loadout posting/staging/lore surface as
        REVIEWER, plus deterministic scanner invocation (a caller-configured
        scanner-verb set, e.g. gitleaks/trufflehog/semgrep/osv-scanner) and a
        `git show`/`git diff`/`git log` read-only surface (one entry wider
        than REVIEWER's). No git write, no external-model-carrier surface.
    ANALYSIS — read-only troubleshooting/observation identity (reference:
        the reference deployment's troubleshooting-detective AND
        platform-observer Bash-command checkers, SE2/lr-a64227 — a faithful,
        non-lossy collapse of two reference checkers that share one
        structure and differ only in configured verb subsets; see module
        docstring COLLAPSE RATIONALE): lore, a caller-configured read-only
        `git` subcommand set (scoped to `-C <repo>` or bare), systemctl/
        docker inspection subcommands, a fixed HTTP-status-code curl health
        probe, and bare file readers. No git write, no service mutation, no
        forge-read, no external-research-engine surface.
    RESEARCH — read-only external-research identity (reference: the
        reference deployment's crew-researcher Bash-command checker,
        SE2/lr-a64227 — kept SEPARATE from ANALYSIS; see module docstring
        COLLAPSE RATIONALE for why): lore and bare file readers ONLY, plus a
        caller-configured external-research-engine verb set. ZERO git/
        systemctl/docker visibility — a genuinely narrower authority shape
        than ANALYSIS, not a config variant of it.
    PLANNING_READER — read-only build-planning identity (reference: the
        reference deployment's build-planning-agent Bash-command checker,
        SE2/lr-a64227 — kept SEPARATE from ANALYSIS/RESEARCH; see module
        docstring COLLAPSE RATIONALE): a WIDER `lore task` grant than the
        other read-only roles (create/update, not just show/list — task
        AUTHORING), plus a narrow GET-only forge-read pre-check (reusing
        `is_admitted_merger_read_only`'s shape via its own
        `PlanningReaderReadOnlyConfig`), and bare file readers. No git, no
        systemctl/docker, no external-research-engine surface, no forge
        write of any kind.
    LEAD — director/lead authority-surface identity (reference: the
        reference deployment's `_check_director_clagentic` +
        `_check_director_lead_mutation` checker pair, SE3/lr-1cc4df — the
        HIGHEST-AUTHORITY-SENSITIVITY role in this whole decomposition, see
        `guard.director_mutation`'s module docstring): a lead/director
        session dispatches build/review/merge work to OTHER roles rather
        than acting on the codebase directly. `check_lead_command` composes
        `guard.director_mutation.check_director_identity_discipline`
        (caller-identity discipline on a relay-shaped IPC verb's
        open/post/close subcommands) with this slice's own mutation-verb
        deny surface (git write / file mutation / package mutation /
        systemctl mutation / forge-PR-mutation, lands in a follow-up PR to
        the same task per the SE3 dispatch's split authorization — see
        `guard.director_mutation`'s module docstring "WHAT THIS MODULE
        PORTS"). Every reference identity that fires either reference
        checker (a "director" identity and every "lead"-suffixed identity)
        collapses into this ONE role — see `guard.director_mutation`'s
        FLAGGED LOSSY COLLAPSE POINTS / SCOPE TRIM sections for the explicit
        callouts on where that collapse was scrutinized.
    INFRA — mutating-infrastructure / host-operator identity (reference:
        the reference deployment's `_is_allowed_ashford`, SE4/lr-6f61aa —
        the FINAL sub-slice of this decomposition, completing lr-19ae42):
        the reference's highest-blast-radius identity (SSH + credential
        rotation). `check_infra_command` composes `guard.infra_ops.
        check_infra_op_wrapper` — a fixed, enumerated set of narrow,
        flag-based op-wrapper shapes (install a binary, rotate a token,
        restart a service, install a local package, run a scoped
        host-side command), each admitted ONLY as a whole-string-anchored
        exact `--flag <value>` sequence with a closed value grammar, never
        a raw command string, plus a narrow `lore` read/audit-write
        subset. No `git`, no push/PR-transport verb, no Write/Edit — see
        `guard.infra_ops`'s own module docstring for the full port pattern
        and the explicit ANSI-C-applicability analysis this role's
        structurally different (whole-string, not bare-verb-prefix) grant
        shape required.
    """

    BUILDER = "builder"
    MERGER = "merger"
    REVIEWER = "reviewer"
    SECURITY = "security"
    ANALYSIS = "analysis"
    RESEARCH = "research"
    PLANNING_READER = "planning_reader"
    LEAD = "lead"
    INFRA = "infra"


#: The forbidden git-operation substrings both roles deny unconditionally
#: (reference `_GIT_FORBIDDEN`, applied identically by both
#: `_is_allowed_amos` and `_is_allowed_naomi` — see module docstring's
#: FLAGGED LOSSY COLLAPSE POINTS, first bullet: this is a role-INDEPENDENT
#: check the reference merely duplicated inline, not a per-agent policy).
#: Mirrors this project's own CLAUDE.md hard rule 5 forbidden-git-operations
#: list.
DEFAULT_FORBIDDEN_GIT_PATTERNS: tuple[str, ...] = (
    "git push --force",
    "git push -f ",
    "git add -A",
    "git add .",
    "git reset --hard",
    "git checkout .",
    "git clean -fd",
    "git commit --no-verify",
)

#: Verb-anchored /proc/*/environ access guard (reference
#: `_proc_environ_check` / `_PROC_ENVIRON_GUARD`) — also role-independent
#: (both reference checkers call it identically). Fires only when a
#: file-reading verb is immediately followed by a /proc/<pid-or-*>/environ
#: path, so a literal "/proc/1234/environ" string embedded in an
#: allowlisted command's narrative argv (e.g. a lore comment body) does not
#: false-trigger — its verb is not one of the file-reading verbs below.
_PROC_ENVIRON_GUARD_RE = re.compile(
    r"^(cat|head|tail|grep|awk|sed|dd)\s+.*"
    r"/proc/(?:[0-9]+|\*)/environ"
)


def check_ansi_c_quote_denied(command: str) -> tuple[bool, str]:
    """Return (ok, reason). ok=False if *command* cannot be confidently
    shell-word-normalized AND contains an unresolved ANSI-C ($'...'/$"...")
    quote opener (security-review BLOCKING finding, PR #115).

    This is HALF of the fix for the finding — the other half is
    `check_forbidden_git_patterns` itself now scanning
    `shell_parsing.normalize_shell_words(command)` instead of the raw
    string, so a RESOLVABLE ANSI-C-quote-fragmented forbidden operation
    (e.g. `git $'push' --force` or `git $'push --force'` — both decode
    cleanly to the joined literal `git push --force`) is caught by the
    forbidden-pattern substring scan itself, exactly as an equivalent
    plainly-quoted command would be. THIS function's job is the remaining
    gap: a forbidden operation fragmented behind an ANSI-C span that does
    NOT decode cleanly at all (an unrecognized escape, e.g.
    `git $'push \\c --force'`) makes `normalize_shell_words` return `None`
    — `check_forbidden_git_patterns`'s own deny-on-ambiguity fallback then
    scans the RAW string, which (by construction) still cannot see the verb
    hidden inside the intact `$'...'` wrapper, exactly the class
    `shell_parsing.has_unresolved_ansi_c_quote`'s own module-level
    commentary warns a "fall back to raw" posture is NOT actually
    deny-on-ambiguity for. This function is the dedicated hard-deny for
    THAT residual case; the bare `^git(\\s|$)` affirmative grant in both
    `check_builder_command` and `check_merger_command` is only ever reached
    once BOTH this gate and the (now-normalized) forbidden-pattern check
    have already run and passed, so it can no longer wave through either
    the resolvable-and-forbidden shape or the unresolvable-ambiguity shape.

    Mirrors `guard.bash_admission.is_admitted_loadout_family_readonly`'s own
    ANSI-C gate (INHERITED ITEM 2 there, PR #113 comment 15824): normalize
    first; only when normalization FAILS and the failure is caused by an
    unresolved ANSI-C opener does this hard-deny. A command whose ANSI-C
    span resolves cleanly (e.g. `git $'status'`, which decodes to the
    harmless literal `status`) is NEVER denied by this gate — only
    genuinely UNRESOLVABLE ANSI-C escapes (deny-on-ambiguity, `shell_
    parsing`'s core security invariant) trigger it. Callers run this BEFORE
    `check_forbidden_git_patterns` and before any bare-verb affirmative
    grant, per module docstring point 5 and `guard.bash_admission`'s own
    posture: EVERY bare-verb affirmative grant must be preceded by both this
    gate AND a normalized (not raw) forbidden-pattern scan (see this
    module's docstring FLAGGED LOSSY COLLAPSE POINTS addendum) so a future
    ported role checker (SE2/SE3/SE4) does not reintroduce this exact gap.
    """
    head = cmd_head(command)
    if normalize_shell_words(head) is None and has_unresolved_ansi_c_quote(head):
        return False, (
            "command contains an unresolved ANSI-C ($'...'/$\"...\") quote "
            "span that could not be confidently normalized; this shape can "
            "hide a forbidden git operation from substring-based deny "
            "checks while still matching a bare-verb affirmative grant -- "
            "deny-on-ambiguity applies. Issue a plainly-quoted command "
            "instead."
        )
    return True, ""


def check_forbidden_git_patterns(
    command: str, *, forbidden: tuple[str, ...] = DEFAULT_FORBIDDEN_GIT_PATTERNS
) -> tuple[bool, str]:
    """Return (ok, reason). ok=False if a forbidden git-operation substring
    from *forbidden* appears in *command*.

    ANSI-C evasion fix (security-review BLOCKING finding, PR #115): this
    originally scanned the RAW command with no quote-normalization pre-pass,
    which an ANSI-C-quote-fragmented forbidden operation evades outright —
    `git $'push' --force` and `git $'push --force'` both decode (bash
    quote-removal) to the literal joined word sequence `git push --force`,
    but neither raw string contains that literal SUBSTRING, so a plain
    `in`-scan never sees it. This now scans
    `shell_parsing.normalize_shell_words(command)` — the SAME shell-word
    normalization `compound_check`/`bash_admission`'s verb-matchers already
    use — which correctly joins a RESOLVABLE ANSI-C span into its decoded
    literal content before the substring scan runs, so the fragmented forms
    above normalize to the exact literal text `forbidden`'s patterns already
    match. On normalization failure (unbalanced quoting, unresolved ANSI-C
    escape, quoted command substitution) this falls back to scanning the RAW
    command — deny-on-ambiguity's usual posture — matching every other
    raw-fallback verb-matcher in this codebase; a caller pairing this with
    `check_ansi_c_quote_denied` (called FIRST by both role checkers) still
    gets a hard deny for the genuinely-unresolvable case rather than relying
    on this fallback's raw scan alone.

    Role-independent (see module docstring's FLAGGED LOSSY COLLAPSE POINTS):
    the reference applies this identical check inside BOTH
    `_is_allowed_amos` and `_is_allowed_naomi`.
    """
    scan_target = normalize_shell_words(command)
    if scan_target is None:
        scan_target = command  # deny-on-ambiguity: unparseable quoting, scan raw
    for pattern in forbidden:
        if pattern in scan_target:
            return False, f"forbidden pattern: {pattern!r} (CLAUDE.md hard rule 5)"
    return True, ""


def check_proc_environ_denied(command: str) -> tuple[bool, str]:
    """Return (ok, reason). ok=False if *command* directly reads
    `/proc/<pid>/environ`, which may expose process secrets.

    Role-independent (see module docstring's FLAGGED LOSSY COLLAPSE POINTS):
    the reference applies this identical check inside BOTH per-agent
    checkers. Verb-anchored so a `/proc/.../environ` string appearing only
    as narrative text in an allowlisted command's own argv (e.g. a `lore
    observe "... /proc/1234/environ ..."` call) is unaffected — `lore` is
    not one of the file-reading verbs this pattern matches on.
    """
    if _PROC_ENVIRON_GUARD_RE.match(command):
        return False, (
            "command would access /proc/*/environ, which may expose process "
            "secrets; if this path appeared in a narrative argv body rather "
            "than as a direct read target, use a non-/proc representation "
            "in that text instead"
        )
    return True, ""


def _is_admitted_scratch(command: str) -> bool:
    """Adapter over `guard.scratch_policy.is_scratch_contained` for this
    module's admission pipeline: a `ScratchContainmentError` (compound
    command, unsafe verb, empty command, unparseable quoting) means "not
    THIS admission path," not "deny everything" — the caller's own
    allowlist fallthrough (or `compound_check`, called earlier in the same
    pipeline) still applies. Never treated as a bypass of `compound_check`:
    this module calls `compound_check` FIRST in both role checkers below,
    exactly mirroring the reference's own `_is_admitted_scratch_mkdir`
    call order (after `_compound_check`, before the per-role pattern list).
    """
    try:
        return is_scratch_contained(command)
    except ScratchContainmentError:
        return False


@dataclass(frozen=True)
class RoleAllowlistConfig:
    """Caller-supplied configuration for a `BashRole` admission check.

    extra_verb_patterns: additional admitted command-prefix regexes beyond
        this module's own role-appropriate base set (e.g. a caller's own
        installed push/PR-transport verb, or an authenticated-API wrapper
        verb) — matched with `re.match` against the RAW command, same
        contract as every other pattern in this module.
    scoped_test_patterns: the scoped build/test/lint pattern set BUILDER
        commands are checked against. Defaults to
        `wait.config.DEFAULT_SCOPED_TEST_PATTERNS` — the SAME already-landed
        generalized pattern set `wait.scoped_test.scoped_test_wait` uses, so
        a caller never maintains two independently-drifting copies of "what
        counts as a scoped verification command." MERGER does not consult
        this field at all (see `check_merger_command` — a release gate does
        not run build/test/lint itself).
    mutating_loadout_verb_names: verb names EXCLUDED from the generic
        loadout-family read/validate grant
        (`bash_admission.is_admitted_loadout_family_readonly`) — see that
        function's own docstring. Both roles pass this through identically;
        MERGER's own mutating verbs (loadout-merge, etc.) should be included
        here too, so the read/validate family grant never accidentally
        admits them a second, laxer way alongside their own explicit
        `extra_verb_patterns` entry.
    sanctioned_bin_dir: optional absolute install directory for the
        loadout-family grant's install-path form (see
        `is_admitted_loadout_family_readonly`'s own docstring) — no default,
        since a caller with only PATH-based bare-basename dispatch has no
        such directory to supply.
    """

    extra_verb_patterns: tuple[re.Pattern[str], ...] = ()
    scoped_test_patterns: tuple[re.Pattern[str], ...] = field(
        default_factory=lambda: tuple(
            re.compile(p) for p in DEFAULT_SCOPED_TEST_PATTERNS
        )
    )
    mutating_loadout_verb_names: frozenset[str] = frozenset()
    sanctioned_bin_dir: str | None = None


#: Read-only, no-write-redirect-concern verbs (reference: `ls|pwd|head|tail|
#: wc|find`) — identical set admitted by BOTH roles in the reference file.
_READONLY_VERBS_RE = re.compile(r"^(ls|pwd|head|tail|wc|find)(\s|$)")

#: A bare `cat <path>` with no `>` redirect to any absolute path or a
#: non-$TMPDIR `$var` (reference: both checkers' identical `cat` read-side
#: pattern). A `cat > <path>` falls through to the staging-write patterns
#: below instead.
#:
#: lr-f8649f: the negative-lookahead exemption narrowed from `$(?!HOME|
#: TMPDIR)` to `$(?!TMPDIR)` — a `cat ... > $HOME/x` redirect must NOT be
#: silently classified as "read-only, no redirect of concern" now that
#: `$HOME` is no longer a sanctioned staging root; it must instead fall
#: through and be denied for lack of a matching write-staging grant
#: (exactly the deny-loop trap this task's dispatch calls out: an exemption
#: regex and a staging-pattern regex must narrow together, or the exemption
#: silently re-admits the very shape the staging patterns now deny).
_CAT_READ_ONLY_RE = re.compile(r"^cat\s+(?!.*>\s*/)(?!.*>\s*\$(?!TMPDIR))")

#: $TMPDIR/ write-redirect staging patterns (reference: both checkers admit
#: this identically) — /tmp/ writes are never admitted here (shared across
#: spawns; a caller's own harness is responsible for that distinction, this
#: module only recognizes the one sanctioned staging root by name).
#:
#: lr-f8649f: narrowed from `$HOME/` + `$TMPDIR/` to `$TMPDIR/`-only
#: (operator sign-off dropping `$HOME` as a scratch-staging root — see
#: `guard.scratch_policy`'s own "TMPDIR-ONLY NARROWING"). This is the SAME
#: predicate narrowing that must land in the same commit as every denial
#: hint naming a staging root below (the deny-loop trap this task's dispatch
#: explicitly calls out: a hint recommending `$HOME` staging while this
#: pattern list denies it would be an unrecoverable recommend-then-deny
#: loop) — every `f"...cat/echo/printf/mkdir[-p] > $HOME/$TMPDIR/..."` reason
#: string in this module is updated in this same change.
_STAGING_WRITE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(cat|echo|printf)\s.*>\s*\$TMPDIR/"),
    re.compile(r"^cat\s*>\s*\$TMPDIR/"),
)

#: BUILDER-only: bare `mkdir [-p] $TMPDIR/...` staging (the reference's
#: `_is_admitted_scratch_mkdir` pre-port bridge is superseded here by the
#: general `is_scratch_contained` category grant, which already covers
#: `mkdir` among its `SCRATCH_SAFE_VERBS` — see `_is_admitted_scratch` above;
#: no separate mkdir-only pattern is needed in this module).


def check_builder_command(
    command: str, *, config: RoleAllowlistConfig | None = None
) -> tuple[bool, str]:
    """Return (ok, reason) for `BashRole.BUILDER` — port of the reference
    deployment's repo-authoring-identity Bash-command checker (reference
    guard-bash.py ll.2564-2731).

    Admission pipeline (mirrors the reference's exact composition order):
      1. `check_ansi_c_quote_denied` — hard-deny on unresolved ANSI-C
         quoting (role-independent; security-review finding, PR #115 — must run
         BEFORE `check_forbidden_git_patterns` and before the bare `git`
         grant below, since both are vulnerable to the same fragmentation
         evasion).
      2. `check_forbidden_git_patterns` — hard-deny (role-independent).
      3. `check_proc_environ_denied` — hard-deny (role-independent).
      4. `guard.shell_parsing.compound_check` — hard-deny on any compound/
         piped/chained/backgrounded shell expression.
      5. `guard.scratch_policy.is_scratch_contained` category grant — ANY
         verb in `SCRATCH_SAFE_VERBS` (not just `mkdir`, a strict superset of
         the reference's narrower pre-port bridge — see
         `guard.bash_admission`'s own "RECONCILIATION" docstring section for
         why this is the correct, already-landed replacement) whose targets
         resolve under `$TMPDIR` (lr-f8649f: `$HOME` dropped).
      6. `guard.bash_admission.is_admitted_loadout_family_readonly` — the
         generic loadout `<verb>` read/validate family grant.
      7. The role-appropriate base pattern list: read-only verbs
         (ls/pwd/head/tail/wc/find/cat), `$TMPDIR` staging write
         redirects, and `config.scoped_test_patterns` (defaults to the
         shared `wait.config.DEFAULT_SCOPED_TEST_PATTERNS` set — the
         loadout-native equivalent of the reference's
         `AMOS_BUILD_TEST_LINT`).
      8. `config.extra_verb_patterns` — caller-supplied additional admitted
         command prefixes (e.g. an installed push/PR-transport verb, an
         authenticated-API wrapper verb, a `/crew-deploy`-shaped script
         trio) — the reference's fixed absolute-path literals for these,
         generalized to caller config per module docstring point 3.

    Fails closed for anything matching none of the above.
    """
    cfg = config if config is not None else RoleAllowlistConfig()

    ok, reason = check_ansi_c_quote_denied(command)
    if not ok:
        return False, reason
    ok, reason = check_forbidden_git_patterns(command)
    if not ok:
        return False, reason
    ok, reason = check_proc_environ_denied(command)
    if not ok:
        return False, reason
    ok, reason = compound_check(command)
    if not ok:
        return False, reason

    if _is_admitted_scratch(command):
        return True, ""

    if is_admitted_loadout_family_readonly(
        command,
        mutating_verb_names=cfg.mutating_loadout_verb_names,
        sanctioned_bin_dir=cfg.sanctioned_bin_dir,
    ):
        return True, ""

    if re.compile(r"^git(\s|$)").match(command):
        return True, ""
    if re.compile(r"^lore(\s|$)").match(command):
        return True, ""
    if _READONLY_VERBS_RE.match(command):
        return True, ""
    if _CAT_READ_ONLY_RE.match(command):
        return True, ""
    for pat in _STAGING_WRITE_PATTERNS:
        if pat.match(command):
            return True, ""
    for pat in cfg.scoped_test_patterns:
        if pat.match(command):
            return True, ""
    for pat in cfg.extra_verb_patterns:
        if pat.match(command):
            return True, ""

    return False, (
        f"command not in builder-role allowlist: {command[:80]!r}. Key "
        f"allowed prefixes: git, lore, scoped build/test/lint commands, "
        f"cat/echo/printf/mkdir[-p] > $TMPDIR/, ls/pwd/cat/head/tail/"
        f"wc/find, plus any caller-configured extra_verb_patterns."
    )


#: Bare-token grammar for a `caller_role` value flowing into
#: `_build_caller_flag_re` below — mirrors `provisioning.roles._TOKEN_RE`
#: (alphanumeric/hyphen/underscore, 1-64 chars, no leading hyphen). See
#: module docstring's FLAGGED LOSSY COLLAPSE POINTS, final bullet.
_ROLE_TOKEN_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


@dataclass(frozen=True)
class MergerReadOnlyConfig:
    """Config for `check_merger_command`'s narrow GET-only pre-check read
    path (reference: the release-gate identity's narrow git-host-api
    read-only pre-check function).

    verb_pattern: matches the read-capable API-wrapper verb invocation
        itself (bare basename and/or absolute install path — caller-
        composed, no hardcoded verb name here).
    caller_role: the bare role-name token that must appear as
        `--caller <caller_role>` for this narrow path to admit. Validated
        against `_ROLE_TOKEN_RE` at construction time (raises ValueError
        immediately on a malformed value) rather than letting a
        metacharacter-bearing string ever reach a compiled regex.
    """

    verb_pattern: re.Pattern[str]
    caller_role: str

    def __post_init__(self) -> None:
        if not _ROLE_TOKEN_RE.match(self.caller_role):
            raise ValueError(
                f"caller_role {self.caller_role!r} is not a bare token "
                f"(expected alphanumeric/hyphen/underscore, 1-64 chars, no "
                f"leading hyphen)."
            )


_GET_METHOD_RE = re.compile(r"(?:^|\s)GET(?:\s|$)", re.IGNORECASE)
_WRITE_METHOD_RE = re.compile(r"(?:^|\s)(?:POST|PATCH|PUT|DELETE)(?:\s|$)", re.IGNORECASE)


def _build_caller_flag_re(caller_role: str) -> re.Pattern[str]:
    return re.compile(rf"(?:^|\s)--caller\s+{re.escape(caller_role)}(?:\s|$)")


def is_admitted_merger_read_only(
    command: str, *, config: MergerReadOnlyConfig
) -> bool:
    """Return True iff *command* is admitted as the merger role's narrow
    read-only pre-check shape (reference: the release-gate identity's narrow
    git-host-api read-only pre-check function): matches `config.verb_pattern`,
    carries `--caller <config.caller_role>` as a genuine shell word, and
    carries no write-method token (POST/PATCH/PUT/DELETE). GET is the
    default when no method token is present at all (mirrors the reference's
    own "METHOD defaults to GET" contract) and is also accepted when spelled
    out explicitly.

    ANSI-C evasion (security-review NIT finding, PR #115, same class and same
    two-part fix as `check_forbidden_git_patterns`/`check_ansi_c_quote_
    denied`): the write-method exclusion was originally a raw-text
    `_WRITE_METHOD_RE.search(command)` scan with no normalization pre-pass —
    an ANSI-C-obscured write-method token (e.g. `$'POST'`, which decodes
    cleanly to the literal `POST`) could defeat it while still being decoded
    by bash into a genuine POST at execution time; the raw string never
    contains the bare word `POST` the regex is anchored on. This now scans
    `shell_parsing.normalize_shell_words(command)` instead, so a resolvable
    ANSI-C-obscured method token normalizes to its plain literal form and is
    caught exactly as an unquoted one would be. On normalization failure
    this hard-DENIES outright (stricter than a generic raw-fallback) when
    caused by an unresolved ANSI-C opener — this is a security-boundary
    exclusion check (mirrors `bash_admission.requires_admission_flag`'s own
    fail-closed-on-ambiguity posture for the same reason: resolving
    ambiguity permissively here could hide a smuggled write-method token).
    `config.verb_pattern.match` and `_build_caller_flag_re` are unaffected —
    only the write-method exclusion (the check the finding named) needed the
    normalized scan target.
    """
    if not config.verb_pattern.match(command):
        return False

    scan_target = normalize_shell_words(command)
    if scan_target is None:
        if has_unresolved_ansi_c_quote(command):
            return False  # deny-on-ambiguity: unresolved ANSI-C could hide a write method
        scan_target = command  # non-ANSI-C ambiguity: fall back to raw, matching module posture

    if _WRITE_METHOD_RE.search(scan_target):
        return False
    if not _build_caller_flag_re(config.caller_role).search(scan_target):
        return False
    return True


def check_merger_command(
    command: str,
    *,
    config: RoleAllowlistConfig | None = None,
    read_only_config: MergerReadOnlyConfig | None = None,
) -> tuple[bool, str]:
    """Return (ok, reason) for `BashRole.MERGER` — port of the reference
    deployment's release-gate-identity Bash-command checker (reference
    guard-bash.py ll.2732-2932).

    Deliberately NARROWER than `check_builder_command` (see `BashRole.MERGER`
    docstring): no `config.scoped_test_patterns` consultation at all (a
    release gate runs pre_checks/CI, not the suite itself), and the base
    `lore` allowance is left to `config.extra_verb_patterns` — the
    reference's own narrower `lore task`/`lore observe`/`lore update
    [--force]`/`lore sentinel status|restart` subset (never a bare `lore`
    wildcard) is release-gate-specific policy this module does not hardcode,
    exactly as it does not hardcode any other verb literal; a caller wires
    its own narrower `lore` subset via `extra_verb_patterns` rather than
    this module assuming the reference's exact subcommand list is universal.

    Admission pipeline (mirrors the reference's exact composition order):
      1. `check_ansi_c_quote_denied` — hard-deny on unresolved ANSI-C
         quoting (role-independent; security-review finding, PR #115 — must run
         BEFORE `check_forbidden_git_patterns` and before the bare `git`
         grant below, since both are reachable from the merger role via this
         shared checker and both are vulnerable to the same fragmentation
         evasion).
      2. `check_forbidden_git_patterns` — hard-deny (role-independent).
      3. `check_proc_environ_denied` — hard-deny (role-independent).
      4. `guard.shell_parsing.compound_check` — hard-deny.
      5. `guard.scratch_policy.is_scratch_contained` category grant.
      6. `is_admitted_merger_read_only` (when *read_only_config* is given) —
         the narrow GET-only pre-check path.
      7. The role-appropriate base pattern list: read-only verbs, `$TMPDIR`
         staging write redirects (same shared set as BUILDER,
         reference identical for both).
      8. `config.extra_verb_patterns` — caller-supplied additional admitted
         command prefixes (merge/close/post-merge/release-detect/
         release-dispatch verbs, the narrower `lore` subset, etc.).

    Fails closed for anything matching none of the above.
    """
    cfg = config if config is not None else RoleAllowlistConfig()

    ok, reason = check_ansi_c_quote_denied(command)
    if not ok:
        return False, reason
    ok, reason = check_forbidden_git_patterns(command)
    if not ok:
        return False, reason
    ok, reason = check_proc_environ_denied(command)
    if not ok:
        return False, reason
    ok, reason = compound_check(command)
    if not ok:
        return False, reason

    if _is_admitted_scratch(command):
        return True, ""

    if read_only_config is not None and is_admitted_merger_read_only(
        command, config=read_only_config
    ):
        return True, ""

    if re.compile(r"^git(\s|$)").match(command):
        return True, ""
    if _READONLY_VERBS_RE.match(command):
        return True, ""
    if _CAT_READ_ONLY_RE.match(command):
        return True, ""
    for pat in _STAGING_WRITE_PATTERNS:
        if pat.match(command):
            return True, ""
    for pat in cfg.extra_verb_patterns:
        if pat.match(command):
            return True, ""

    return False, (
        f"command not in merger-role allowlist: {command[:120]!r}. Note "
        f"merger's allowlist is intentionally narrower than builder's — "
        f"release-gate scope only (no build/test/lint verbs). Key allowed "
        f"prefixes: git, ls/pwd/cat/head/tail/wc/find, cat/echo/printf > "
        f"$TMPDIR/, plus any caller-configured extra_verb_patterns "
        f"(merge/close/post-merge/release verbs, a narrower lore subset) "
        f"and read_only_config's GET-only pre-check shape."
    )


# ---------------------------------------------------------------------------
# SE2 (lr-a64227): reviewer/analysis role checkers.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewGateConfig:
    """Caller-supplied configuration shared by `check_reviewer_command` and
    `check_security_command` — both roles admit the SAME loadout posting/
    staging verb family, the same belt-and-suspenders comment-endpoint flag
    requirements, and the same body-stdin pipe carve-out (reference:
    identical inline logic duplicated in both `_is_allowed_peaches` and
    `_is_allowed_bobbie`) — parameterized once here rather than as two
    independently-drifting config dataclasses.

    post_verb_patterns: verb-prefix regexes for the loadout posting/staging
        family (e.g. a caller's own installed `loadout-git-host-api`,
        `loadout-stage-body`, `loadout-review-post`, `resolve-platform`
        verbs) — matched with `re.match` against the raw command, same
        contract as `RoleAllowlistConfig.extra_verb_patterns`.
    comments_post_rule: an optional `bash_admission.MethodPathFlagRule`
        requiring a flag (e.g. `--verify-comment`) on a POST to a
        comments-shaped API path — reuses `bash_admission.
        requires_admission_flag` rather than re-implementing the belt-and-
        suspenders check a third time in this module.
    comments_post_verb_pattern: the verb-shape `requires_admission_flag`
        checks *comments_post_rule* against (typically the same pattern as
        one entry in *post_verb_patterns* — passed separately since
        `requires_admission_flag` needs a single pattern, not a tuple).
    delete_own_comment_rule: the DELETE-side sibling of
        *comments_post_rule* (e.g. requiring `--delete-own-comment` on a
        single-comment-resource DELETE) — optional, same reuse rationale.
    body_stdin_verbs: the `bash_admission.BodyStdinVerb` registry for the
        `echo|printf|cat | loadout-<verb> [--body-stdin]` pipe carve-out
        (reused via `bash_admission.is_admitted_body_stdin_pipe`, never
        re-implemented here).
    extra_verb_patterns: additional admitted command-prefix regexes beyond
        the shared review-gate surface (e.g. a role-specific script path).
    mutating_loadout_verb_names / sanctioned_bin_dir: passed through to
        `bash_admission.is_admitted_loadout_family_readonly` exactly as
        `RoleAllowlistConfig`'s own fields are for BUILDER/MERGER.
    """

    post_verb_patterns: tuple[re.Pattern[str], ...] = ()
    comments_post_rule: MethodPathFlagRule | None = None
    comments_post_verb_pattern: re.Pattern[str] | None = None
    delete_own_comment_rule: MethodPathFlagRule | None = None
    body_stdin_verbs: tuple[BodyStdinVerb, ...] = ()
    extra_verb_patterns: tuple[re.Pattern[str], ...] = ()
    mutating_loadout_verb_names: frozenset[str] = frozenset()
    sanctioned_bin_dir: str | None = None


def _check_review_gate_flags(command: str, config: ReviewGateConfig) -> tuple[bool, str]:
    """Shared belt-and-suspenders flag checks for `check_reviewer_command`/
    `check_security_command` — reuses `bash_admission.requires_admission_flag`
    (never re-implemented inline) for both the comments-POST and the
    single-comment-DELETE rule, when the caller supplies them.
    """
    if config.comments_post_rule is not None and config.comments_post_verb_pattern is not None:
        ok, reason = requires_admission_flag(
            command,
            verb_pattern=config.comments_post_verb_pattern,
            rule=config.comments_post_rule,
        )
        if not ok:
            return False, reason
    if config.delete_own_comment_rule is not None and config.comments_post_verb_pattern is not None:
        ok, reason = requires_admission_flag(
            command,
            verb_pattern=config.comments_post_verb_pattern,
            rule=config.delete_own_comment_rule,
        )
        if not ok:
            return False, reason
    return True, ""


def _check_review_gate_body_stdin_pipe(command: str, config: ReviewGateConfig) -> bool:
    """Return True iff *command* is admitted by the shared body-stdin pipe
    carve-out (reused directly, never re-implemented — see `ReviewGateConfig.
    body_stdin_verbs` docstring)."""
    if not config.body_stdin_verbs:
        return False
    return is_admitted_body_stdin_pipe(cmd_head(command), verbs=config.body_stdin_verbs)


def check_reviewer_command(
    command: str, *, config: ReviewGateConfig | None = None
) -> tuple[bool, str]:
    """Return (ok, reason) for `BashRole.REVIEWER` — port of the reference
    deployment's read-only PR/commit-review Bash-command checker (reference
    `_is_allowed_peaches`, ll.2933-3121).

    Admission pipeline (mirrors the reference's exact composition order):
      1. `check_forbidden_git_patterns` — hard-deny (role-independent).
      2. `check_proc_environ_denied` — hard-deny (role-independent).
      3. The shared body-stdin pipe carve-out (`config.body_stdin_verbs`) —
         reuses `bash_admission.is_admitted_body_stdin_pipe`, checked BEFORE
         `compound_check` (mirrors the reference's own `_compound_check`
         composition order, which checks this carve-out internally before
         its generic pipe deny — `guard.shell_parsing.compound_check`
         deliberately excludes the carve-out itself, per that module's
         docstring point 2, so a caller composing this checker must
         replicate the same ordering rather than letting the generic
         structural gate deny the one narrow pipe shape this role admits).
      4. `guard.shell_parsing.compound_check` — hard-deny (every OTHER
         pipe/chain/background shape).
      5. The shared review-gate belt-and-suspenders flag checks
         (`config.comments_post_rule`/`config.delete_own_comment_rule`, when
         supplied) — reuses `bash_admission.requires_admission_flag`.
      6. `guard.scratch_policy.is_scratch_contained` category grant.
      7. `guard.bash_admission.is_admitted_loadout_family_readonly`.
      8. The role-appropriate base pattern list: `lore` task/tome/search/
         observe (read + comment/close/create/update, matching the
         reference — a review-comment/task-close identity, not a bare `lore`
         wildcard), `config.post_verb_patterns` (the loadout posting/staging
         family), a narrow `git show`/`git diff` read-only surface (NO `git
         log` — narrower than `BashRole.SECURITY`, see module docstring
         COLLAPSE RATIONALE), read-only file readers, and $TMPDIR staging
         writes (lr-f8649f: $HOME dropped).
      9. `config.extra_verb_patterns` — caller-supplied additions (e.g. a
         Path-D external-model-carrier invocation pattern, or a back-compat
         review-read script path).

    Deliberately NO `check_ansi_c_quote_denied` grant-gating is needed ahead
    of a bare-verb git grant here, because REVIEWER's only admitted `git`
    forms are the narrow `git show(\\s|$)`/`git diff(\\s|$)` subcommand-
    anchored patterns below — never a bare `^git(\\s|$)` affirmative grant —
    so the ANSI-C-fragmentation class this module's POST-LANDING HARDENING
    section describes (a bare-verb grant matching underneath a forbidden-
    pattern fragmentation) does not apply to this checker's git surface.
    `check_forbidden_git_patterns` (step 1) still normalizes before scanning,
    matching every other checker in this module.

    Fails closed for anything matching none of the above.
    """
    cfg = config if config is not None else ReviewGateConfig()

    ok, reason = check_forbidden_git_patterns(command)
    if not ok:
        return False, reason
    ok, reason = check_proc_environ_denied(command)
    if not ok:
        return False, reason

    if _check_review_gate_body_stdin_pipe(command, cfg):
        return True, ""

    ok, reason = compound_check(command)
    if not ok:
        return False, reason
    ok, reason = _check_review_gate_flags(command, cfg)
    if not ok:
        return False, reason

    if _is_admitted_scratch(command):
        return True, ""
    if is_admitted_loadout_family_readonly(
        command,
        mutating_verb_names=cfg.mutating_loadout_verb_names,
        sanctioned_bin_dir=cfg.sanctioned_bin_dir,
    ):
        return True, ""

    patterns = [
        re.compile(r"^lore\s+task\s+(show|list|comment|close|create|update)(\s|$)"),
        re.compile(r"^lore\s+tome\s+(show|list)(\s|$)"),
        re.compile(r"^lore\s+search(\s|$)"),
        re.compile(r"^lore\s+observe(\s|$)"),
        *cfg.post_verb_patterns,
        re.compile(r"^git\s+show(\s|$)"),
        re.compile(r"^git\s+diff(\s|$)"),
        _READONLY_VERBS_RE,
        _CAT_READ_ONLY_RE,
        *_STAGING_WRITE_PATTERNS,
        *cfg.extra_verb_patterns,
    ]

    for pat in patterns:
        if pat.match(command):
            return True, ""

    return False, (
        f"command not in reviewer-role allowlist: {command[:120]!r}. Key "
        f"allowed prefixes: lore task/tome/search/observe, "
        f"caller-configured loadout posting/staging verbs, "
        f"git show|diff (read-only, no git log), "
        f"ls/pwd/cat/head/tail/wc/find, cat/echo/printf > $TMPDIR/, "
        f"plus any caller-configured extra_verb_patterns. REVIEWER is "
        f"read-only by design; no git write, no scanner invocation "
        f"(that is SECURITY's surface)."
    )


def check_security_command(
    command: str, *, config: ReviewGateConfig | None = None, scanner_patterns: tuple[re.Pattern[str], ...] = ()
) -> tuple[bool, str]:
    """Return (ok, reason) for `BashRole.SECURITY` — port of the reference
    deployment's read-only pre-merge security-audit-gate Bash-command
    checker (reference `_is_allowed_bobbie`, ll.4690-4879).

    Admission pipeline mirrors `check_reviewer_command` exactly (both roles
    share `ReviewGateConfig` — see that function's docstring for the shared
    steps 1-7), with two SECURITY-only differences in the base pattern list
    (step 8): `git log` is additionally admitted (one entry wider than
    REVIEWER — reference difference, not narrowed here), and
    *scanner_patterns* (a caller-supplied deterministic-scanner verb set,
    e.g. `gitleaks`/`trufflehog`/`semgrep`/`osv-scanner` — CLAUDE.md rule 1:
    no fixed scanner binary name hardcoded in this module) is consulted
    instead of a Path-D external-model-carrier surface (that is REVIEWER's,
    not SECURITY's — see module docstring COLLAPSE RATIONALE).

    Fails closed for anything matching none of the above.
    """
    cfg = config if config is not None else ReviewGateConfig()

    ok, reason = check_forbidden_git_patterns(command)
    if not ok:
        return False, reason
    ok, reason = check_proc_environ_denied(command)
    if not ok:
        return False, reason

    if _check_review_gate_body_stdin_pipe(command, cfg):
        return True, ""

    ok, reason = compound_check(command)
    if not ok:
        return False, reason
    ok, reason = _check_review_gate_flags(command, cfg)
    if not ok:
        return False, reason

    if _is_admitted_scratch(command):
        return True, ""
    if is_admitted_loadout_family_readonly(
        command,
        mutating_verb_names=cfg.mutating_loadout_verb_names,
        sanctioned_bin_dir=cfg.sanctioned_bin_dir,
    ):
        return True, ""

    patterns = [
        re.compile(r"^lore\s+task\s+(show|list|comment|close|create|update)(\s|$)"),
        re.compile(r"^lore\s+tome\s+(show|list)(\s|$)"),
        re.compile(r"^lore\s+search(\s|$)"),
        re.compile(r"^lore\s+observe(\s|$)"),
        *cfg.post_verb_patterns,
        *scanner_patterns,
        re.compile(r"^git\s+show(\s|$)"),
        re.compile(r"^git\s+diff(\s|$)"),
        re.compile(r"^git\s+log(\s|$)"),
        _READONLY_VERBS_RE,
        _CAT_READ_ONLY_RE,
        *_STAGING_WRITE_PATTERNS,
        *cfg.extra_verb_patterns,
    ]

    for pat in patterns:
        if pat.match(command):
            return True, ""

    return False, (
        f"command not in security-role allowlist: {command[:120]!r}. Key "
        f"allowed prefixes: lore task/tome/search/observe, "
        f"caller-configured loadout posting/staging verbs, "
        f"caller-configured scanner_patterns (e.g. gitleaks/trufflehog/"
        f"semgrep/osv-scanner), git show|diff|log (read-only), "
        f"ls/pwd/cat/head/tail/wc/find, cat/echo/printf > $TMPDIR/, "
        f"plus any caller-configured extra_verb_patterns. SECURITY is "
        f"read-only by design; no git write, no push, no edit."
    )


@dataclass(frozen=True)
class AnalysisRoleConfig:
    """Caller-supplied configuration for `check_analysis_command`
    (`BashRole.ANALYSIS`) — parameterizes the ONE structural shape the
    reference's troubleshooting-detective and platform-observer checkers
    both share (see module docstring COLLAPSE RATIONALE): a read-only `git`
    subcommand set, optional extra bare file-reader verbs, and extra curl
    health-probe patterns.

    git_readonly_subcommands: the read-only git subcommand set admitted in
        both `git -C <repo> <sub>` and bare `git <sub>` form (reference:
        `_GIT_READONLY_FULL` for the wider identity — adds diff/blame — or
        `_GIT_READONLY_NARROW` for the narrower one). No default: a caller
        must choose which subset its own identity gets, mirroring the
        reference's own two genuinely different configured subsets rather
        than this module silently picking one.
    extra_reader_verbs: additional bare read-only verb names beyond the
        shared `cat|tail|head` base (reference: the wider identity adds
        `grep|less`; the narrower one does not) — a tuple of bare verb
        names, not full patterns, so this module still owns the anchoring
        regex shape.
    extra_probe_patterns: additional admitted curl-probe / narrow-endpoint
        regexes beyond the shared HTTP-status-code probe (reference: the
        narrower identity additionally admits two fixed task-tracking-
        service endpoint probes) — a caller supplies its own endpoint
        shapes; no specific hostname is hardcoded in this module
        (CLAUDE.md rule 1).
    extra_verb_patterns: additional admitted command-prefix regexes beyond
        the shared analysis surface.

    HARDENING (security-review advisory, PR #116 comment 15875 item 3):
    `git_readonly_subcommands` and `extra_reader_verbs` are bare tokens this
    module itself joins with `"|".join(...)` and interpolates into a
    compiled regex alternation (see `check_analysis_command`) — unlike every
    OTHER caller-supplied tuple in this module (`extra_verb_patterns`,
    `extra_probe_patterns`, `ReviewGateConfig.post_verb_patterns`, etc.),
    which are already-compiled `re.Pattern` values a caller builds and owns
    the safety of. A malformed token here (one containing a regex
    metacharacter, e.g. a stray `|` or `.*`) could widen the alternation's
    admitted match shape at the SAME authority-defining boundary
    `MergerReadOnlyConfig.caller_role`/`PlanningReaderReadOnlyConfig.
    caller_role` already close via `_ROLE_TOKEN_RE`-validated construction —
    this is the same defense, applied to the two fields this dataclass is
    the one place in the module that left it open. Validated at
    construction time (`__post_init__`) against the SAME `_ROLE_TOKEN_RE`
    bare-token grammar (alphanumeric/hyphen/underscore, no leading hyphen)
    those fields already use, rather than a new, divergent validator — a
    malformed token raises `ValueError` immediately, before it can ever
    reach `check_analysis_command`'s `"|".join(...)` + `re.compile(...)`.
    Advisory, not blocking: this requires a config-authoring mistake (a
    caller wiring a bad literal into its own `AnalysisRoleConfig`), not
    exploitation via an untrusted command string — but the SAME module
    already treats an equivalent shape as authority-defining, so the same
    fail-closed posture applies here too, and closing it here prevents the
    unvalidated pattern from propagating into SE3/SE4 role configs that
    would otherwise copy this dataclass as their template.
    """

    git_readonly_subcommands: tuple[str, ...] = ()
    extra_reader_verbs: tuple[str, ...] = ()
    extra_probe_patterns: tuple[re.Pattern[str], ...] = ()
    extra_verb_patterns: tuple[re.Pattern[str], ...] = ()

    def __post_init__(self) -> None:
        for token in (*self.git_readonly_subcommands, *self.extra_reader_verbs):
            if not _ROLE_TOKEN_RE.match(token):
                raise ValueError(
                    f"AnalysisRoleConfig subcommand/verb token {token!r} is "
                    f"not a bare token (expected alphanumeric/hyphen/"
                    f"underscore, 1-64 chars, no leading hyphen) -- this "
                    f"value flows into a regex alternation "
                    f"(check_analysis_command's git_dash_c_re/git_bare_re/"
                    f"reader_re) and a metacharacter-bearing token could "
                    f"widen the admitted match shape."
                )


_ANALYSIS_MUTATION_FORBIDDEN: tuple[str, ...] = DEFAULT_FORBIDDEN_GIT_PATTERNS + (
    "git push",
    "git commit",
    "git checkout",
    "git merge",
    "git rebase",
    "git reset",
    "git add",
    "git clean",
    "git stash drop",
    "systemctl start",
    "systemctl stop",
    "systemctl restart",
    "systemctl reload",
    "docker start",
    "docker stop",
    "docker restart",
    "docker rm",
    "docker kill",
    "docker compose up",
    "docker compose down",
    "docker-compose up",
    "docker-compose down",
)

_SYSTEMCTL_READONLY_RE = re.compile(r"^systemctl\s+(status|is-active|is-enabled|is-failed)(\s|$)")
_DOCKER_READONLY_RE = re.compile(r"^docker\s+(ps|logs|inspect)(\s|$)")
_CURL_STATUS_PROBE_RE = re.compile(
    r'^curl\s+-sS\s+-o\s+/dev/null\s+-w\s+"%\{http_code\}"\s+'
)


def check_analysis_command(
    command: str, *, config: AnalysisRoleConfig
) -> tuple[bool, str]:
    """Return (ok, reason) for `BashRole.ANALYSIS` — port of the reference
    deployment's read-only troubleshooting-detective AND platform-observer
    Bash-command checkers (reference `_is_allowed_miller` ll.3180-3227,
    `_is_allowed_drummer` ll.3230-3300 — a faithful, non-lossy collapse of
    two identically-structured reference functions; see module docstring
    COLLAPSE RATIONALE).

    *config* has no default (unlike `RoleAllowlistConfig`) — a caller MUST
    choose its own `git_readonly_subcommands` subset, since the reference's
    two source identities genuinely differ there and this module must not
    silently pick one as a hidden default.

    Uses `_ANALYSIS_MUTATION_FORBIDDEN` (mirrors the reference's
    `_MUTATION_FORBIDDEN` — a wider forbidden-substring list than
    `DEFAULT_FORBIDDEN_GIT_PATTERNS` alone, since an observer role must also
    be denied service-mutation verbs, not only git-mutation ones) instead of
    the role-independent `check_forbidden_git_patterns` helper — this is
    role-appropriate, not a bug: BUILDER/MERGER are git-authoring identities
    whose only forbidden substrings are the CLAUDE.md hard-rule-5 git
    patterns, while ANALYSIS is a read-only OBSERVER identity that must also
    never mutate a service it is merely inspecting.

    Admission pipeline (mirrors the reference's exact composition order):
      1. Forbidden-substring scan against `_ANALYSIS_MUTATION_FORBIDDEN`,
         normalized via `shell_parsing.normalize_shell_words` with the same
         ANSI-C hard-deny-on-ambiguity gate `check_forbidden_git_patterns`/
         `check_ansi_c_quote_denied` use (POST-LANDING HARDENING posture,
         mandatory for every bare-verb grant this checker admits below).
      2. `check_proc_environ_denied` — hard-deny.
      3. `guard.shell_parsing.compound_check` — hard-deny.
      4. The role-appropriate base pattern list: `lore` (read + observe +
         task-mutation, matching the reference), a `git -C <repo>`/bare
         `git` read-only subcommand set from `config.git_readonly_
         subcommands`, systemctl/docker inspection subcommands, the shared
         curl status-code probe plus `config.extra_probe_patterns`, and bare
         file readers (`cat|tail|head` plus `config.extra_reader_verbs`).
      5. `config.extra_verb_patterns` — caller-supplied additions.

    Fails closed for anything matching none of the above.
    """
    ok, reason = _check_analysis_forbidden(command)
    if not ok:
        return False, reason
    ok, reason = check_proc_environ_denied(command)
    if not ok:
        return False, reason
    ok, reason = compound_check(command)
    if not ok:
        return False, reason

    git_subcommand_alt = "|".join(config.git_readonly_subcommands)
    git_dash_c_re = re.compile(
        r"^git\s+-C\s+\S+\s+(" + git_subcommand_alt + r")(\s|$)"
    ) if git_subcommand_alt else None
    git_bare_re = re.compile(
        r"^git\s+(" + git_subcommand_alt + r")(\s|$)"
    ) if git_subcommand_alt else None

    reader_verb_alt = "|".join(("cat", "tail", "head", *config.extra_reader_verbs))
    reader_re = re.compile(r"^(" + reader_verb_alt + r")(\s|$)")

    patterns: list[re.Pattern[str]] = [
        re.compile(r"^lore\s+task\s+(show|list|comment|close|create|update)(\s|$)"),
        re.compile(r"^lore\s+tome\s+(show|list)(\s|$)"),
        re.compile(r"^lore\s+search(\s|$)"),
        re.compile(r"^lore\s+retro\s+list(\s|$)"),
        re.compile(r"^lore\s+observe(\s|$)"),
        _SYSTEMCTL_READONLY_RE,
        _DOCKER_READONLY_RE,
        _CURL_STATUS_PROBE_RE,
        reader_re,
        _READONLY_VERBS_RE,
        *config.extra_probe_patterns,
        *config.extra_verb_patterns,
    ]

    for pat in patterns:
        if pat.match(command):
            return True, ""

    # The git read-only subcommand grant is SUBCOMMAND-scoped (unlike
    # BUILDER/MERGER's bare `^git(\s|$)` grant, which admits every
    # subcommand and so never needs to see past the leading token): an
    # ANSI-C-wrapped but RESOLVABLE subcommand (`git $'status'`) must still
    # match here, so this scans `normalize_shell_words(command)` rather than
    # the raw string. On normalization failure this falls back to the raw
    # command -- deny-on-ambiguity, matching every other raw-fallback
    # verb-matcher in this module; `_check_analysis_forbidden` above has
    # already hard-denied the genuinely UNRESOLVABLE ANSI-C case before this
    # point is ever reached (POST-LANDING HARDENING posture).
    if git_dash_c_re is not None or git_bare_re is not None:
        git_scan_target = normalize_shell_words(command)
        if git_scan_target is None:
            git_scan_target = command  # deny-on-ambiguity: unparseable quoting, scan raw
        if git_dash_c_re is not None and git_dash_c_re.match(git_scan_target):
            return True, ""
        if git_bare_re is not None and git_bare_re.match(git_scan_target):
            return True, ""

    return False, (
        f"command not in analysis-role allowlist: {command[:120]!r}. Key "
        f"allowed prefixes: lore task/tome/search/observe/retro-list, "
        f"git -C <repo>|bare git read-only subcommands (caller-configured), "
        f"systemctl status|is-active|is-enabled|is-failed, "
        f"docker ps|logs|inspect, a curl HTTP-status-code health probe, "
        f"cat/tail/head (plus any caller-configured extra_reader_verbs), "
        f"ls/pwd/wc/find, plus any caller-configured extra_verb_patterns. "
        f"ANALYSIS is read-only by design; no git mutation, no service "
        f"mutation, no forge-read, no external-research-engine surface."
    )


def _check_analysis_forbidden(command: str) -> tuple[bool, str]:
    """ANALYSIS-role forbidden-substring scan against
    `_ANALYSIS_MUTATION_FORBIDDEN` — same normalize-then-scan,
    hard-deny-on-unresolved-ANSI-C posture as `check_forbidden_git_patterns`/
    `check_ansi_c_quote_denied` (POST-LANDING HARDENING, mandatory for every
    bare-verb grant this checker composes)."""
    head = cmd_head(command)
    if normalize_shell_words(head) is None and has_unresolved_ansi_c_quote(head):
        return False, (
            "command contains an unresolved ANSI-C ($'...'/$\"...\") quote "
            "span that could not be confidently normalized -- deny-on-"
            "ambiguity applies. Issue a plainly-quoted command instead."
        )
    scan_target = normalize_shell_words(command)
    if scan_target is None:
        scan_target = command  # deny-on-ambiguity: unparseable quoting, scan raw
    for pattern in _ANALYSIS_MUTATION_FORBIDDEN:
        if pattern in scan_target:
            return False, (
                f"forbidden mutation pattern: {pattern!r} -- ANALYSIS is "
                f"read-only on every working tree and every running service"
            )
    return True, ""


@dataclass(frozen=True)
class ResearchRoleConfig:
    """Caller-supplied configuration for `check_research_command`
    (`BashRole.RESEARCH`).

    research_engine_patterns: verb-prefix regexes for the external-research-
        engine invocation surface this role's authority is defined by (e.g.
        a caller's configured `gemini -m <model>` forms and/or a
        research-prompt wrapper script) — no model name or script path is
        hardcoded here (CLAUDE.md rule 1); a caller wires its own installed
        research tooling in.
    extra_verb_patterns: additional admitted command-prefix regexes beyond
        the shared read-only/research surface.
    """

    research_engine_patterns: tuple[re.Pattern[str], ...] = ()
    extra_verb_patterns: tuple[re.Pattern[str], ...] = ()


def check_research_command(
    command: str, *, config: ResearchRoleConfig | None = None
) -> tuple[bool, str]:
    """Return (ok, reason) for `BashRole.RESEARCH` — port of the reference
    deployment's read-only crew-researcher Bash-command checker (reference
    `_is_allowed_prax`, ll.3303-3348).

    Deliberately has NO git/systemctl/docker surface at all (see module
    docstring COLLAPSE RATIONALE for why this is not collapsed into
    `BashRole.ANALYSIS`) — this checker's forbidden-substring scan is
    `check_forbidden_git_patterns`'s narrower git-only list (role-
    independent, reused directly), not `ANALYSIS`'s wider service-mutation
    list, since RESEARCH has no service-mutation surface to guard against in
    the first place.

    Admission pipeline (mirrors the reference's exact composition order):
      1. `check_ansi_c_quote_denied` — hard-deny on unresolved ANSI-C
         quoting (this checker's base grants below include the bare-verb-
         adjacent `config.research_engine_patterns`; a caller supplying a
         bare-verb-shaped pattern there inherits the same POST-LANDING
         HARDENING obligation via this gate, run ahead of it here).
      2. `check_forbidden_git_patterns` — hard-deny (role-independent; git
         is not otherwise admitted by this role at all, but the reference
         still applies this check identically before its own pattern list).
      3. `check_proc_environ_denied` — hard-deny (role-independent).
      4. `guard.shell_parsing.compound_check` — hard-deny.
      5. The role-appropriate base pattern list: `lore` (read + observe +
         task-mutation, matching the reference), `config.
         research_engine_patterns`, and bare file readers.
      6. `config.extra_verb_patterns` — caller-supplied additions.

    Fails closed for anything matching none of the above.
    """
    cfg = config if config is not None else ResearchRoleConfig()

    ok, reason = check_ansi_c_quote_denied(command)
    if not ok:
        return False, reason
    ok, reason = check_forbidden_git_patterns(command)
    if not ok:
        return False, reason
    ok, reason = check_proc_environ_denied(command)
    if not ok:
        return False, reason
    ok, reason = compound_check(command)
    if not ok:
        return False, reason

    patterns = [
        re.compile(r"^lore\s+task\s+(show|list|comment|close|create|update)(\s|$)"),
        re.compile(r"^lore\s+tome\s+(show|list)(\s|$)"),
        re.compile(r"^lore\s+search(\s|$)"),
        re.compile(r"^lore\s+observe(\s|$)"),
        *cfg.research_engine_patterns,
        _READONLY_VERBS_RE,
        _CAT_READ_ONLY_RE,
        *cfg.extra_verb_patterns,
    ]

    for pat in patterns:
        if pat.match(command):
            return True, ""

    return False, (
        f"command not in research-role allowlist: {command[:120]!r}. Key "
        f"allowed prefixes: lore task/tome/search/observe, "
        f"caller-configured research_engine_patterns, "
        f"ls/pwd/cat/head/tail/wc/find. RESEARCH is read-only by design; "
        f"no git, no systemctl/docker, no forge-read."
    )


@dataclass(frozen=True)
class PlanningReaderReadOnlyConfig:
    """Config for `check_planning_reader_command`'s narrow GET-only
    forge-read pre-check path (reference: the build-planning identity's
    narrow reader-cred GET-only admission helpers,
    `_planning_reader_forgejo_curl_get_read`/
    `_planning_reader_loadout_git_host_api_get_read`, ll.3378-3430).

    Deliberately reuses `MergerReadOnlyConfig`'s exact shape (a
    `verb_pattern` plus a validated `caller_role` token) rather than
    inventing a parallel dataclass — module docstring point 5 ("reuse landed
    primitives; never duplicate a boundary"). A caller composes a
    `MergerReadOnlyConfig` instance directly (e.g.
    `MergerReadOnlyConfig(verb_pattern=..., caller_role="planning-reader")`)
    and passes it here; this type alias exists only to give the parameter a
    role-appropriate name at PLANNING_READER's own call site.
    """

    verb_pattern: re.Pattern[str]
    caller_role: str

    def __post_init__(self) -> None:
        if not _ROLE_TOKEN_RE.match(self.caller_role):
            raise ValueError(
                f"caller_role {self.caller_role!r} is not a bare token "
                f"(expected alphanumeric/hyphen/underscore, 1-64 chars, no "
                f"leading hyphen)."
            )


def _planning_reader_read_only_admitted(
    command: str, *, configs: tuple[PlanningReaderReadOnlyConfig, ...]
) -> bool:
    """Return True iff *command* is admitted by ANY of *configs*'s narrow
    GET-only pre-check shapes (a caller typically supplies two — one for a
    forgejo-curl-shaped verb, one for a loadout-git-host-api-shaped verb,
    mirroring the reference's two parallel helpers) — reuses
    `is_admitted_merger_read_only` directly (never re-implemented) by
    constructing an equivalent `MergerReadOnlyConfig` per entry.
    """
    for cfg in configs:
        merger_shaped = MergerReadOnlyConfig(
            verb_pattern=cfg.verb_pattern, caller_role=cfg.caller_role
        )
        if is_admitted_merger_read_only(command, config=merger_shaped):
            return True
    return False


def check_planning_reader_command(
    command: str,
    *,
    config: RoleAllowlistConfig | None = None,
    read_only_configs: tuple[PlanningReaderReadOnlyConfig, ...] = (),
) -> tuple[bool, str]:
    """Return (ok, reason) for `BashRole.PLANNING_READER` — port of the
    reference deployment's read-only build-planning-agent Bash-command
    checker (reference `_is_allowed_avasarala`, ll.3432-3480).

    Admission pipeline (mirrors the reference's exact composition order):
      1. `check_forbidden_git_patterns` — hard-deny (role-independent; git
         is not otherwise admitted by this role at all).
      2. `check_proc_environ_denied` — hard-deny (role-independent).
      3. `guard.shell_parsing.compound_check` — hard-deny.
      4. `read_only_configs` — the narrow GET-only forge-read pre-check
         (reuses `is_admitted_merger_read_only` via
         `_planning_reader_read_only_admitted`).
      5. The role-appropriate base pattern list: `lore` (task
         show/list/comment/close/CREATE/UPDATE — task AUTHORING, wider than
         the other read-only roles' `lore task` grant, matching the
         reference), and bare file readers.
      6. `config.extra_verb_patterns` — caller-supplied additions.

    Fails closed for anything matching none of the above.
    """
    cfg = config if config is not None else RoleAllowlistConfig()

    ok, reason = check_forbidden_git_patterns(command)
    if not ok:
        return False, reason
    ok, reason = check_proc_environ_denied(command)
    if not ok:
        return False, reason
    ok, reason = compound_check(command)
    if not ok:
        return False, reason

    if _planning_reader_read_only_admitted(command, configs=read_only_configs):
        return True, ""

    patterns = [
        re.compile(r"^lore\s+task\s+(show|list|comment|close|create|update)(\s|$)"),
        re.compile(r"^lore\s+tome\s+(show|list)(\s|$)"),
        re.compile(r"^lore\s+search(\s|$)"),
        re.compile(r"^lore\s+observe(\s|$)"),
        _READONLY_VERBS_RE,
        _CAT_READ_ONLY_RE,
        *cfg.extra_verb_patterns,
    ]

    for pat in patterns:
        if pat.match(command):
            return True, ""

    return False, (
        f"command not in planning-reader-role allowlist: {command[:120]!r}. "
        f"Key allowed prefixes: lore task show|list|comment|close|create|"
        f"update, lore tome show|list, lore search, lore observe, "
        f"caller-configured read_only_configs GET-only forge-read pre-check, "
        f"ls/pwd/cat/head/tail/wc/find, plus any caller-configured "
        f"extra_verb_patterns. PLANNING_READER is read-only by design; no "
        f"git, no python3, no write verbs on any forge-read wrapper."
    )


# ---------------------------------------------------------------------------
# SE3 (lr-1cc4df): director/lead authority-surface checker. PR1 landed the
# caller-identity discipline half (`check_director_identity_discipline`).
# PR2 (THIS PR) extends `check_lead_command` with the mutation-verb-family
# deny surface (`guard.director_mutation.check_lead_mutation`) rather than
# adding a second LEAD entry point — see that module's docstring for the
# full port scope and its harness-attestation seam design.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeadRoleConfig:
    """Caller-supplied configuration for `check_lead_command`
    (`BashRole.LEAD`).

    director_clagentic_config: passed through to
        `guard.director_mutation.check_director_identity_discipline` when
        supplied — a caller not using a relay-shaped IPC verb at all may
        omit this entirely (the identity-discipline check is then simply
        never consulted, matching the reference's own posture that this
        check only ever fires for that one specific IPC verb shape).
    mutation_config: passed through to `guard.director_mutation.
        check_lead_mutation` when supplied (SE3 PR2, lr-1cc4df) — a caller
        may omit this to get PR1's own INCOMPLETE-by-itself posture (no
        mutation-verb deny at all), but per that PR's own docstring this is
        no longer the intended steady state: a caller integrating
        `BashRole.LEAD` should supply a `LeadMutationConfig` so the
        mutation-verb-family deny (the surface that actually restricts what
        a lead/director session may do) is active.
    """

    director_clagentic_config: DirectorClagenticConfig | None = None
    mutation_config: LeadMutationConfig | None = None


def check_lead_command(
    command: str,
    *,
    identity_label: str,
    config: LeadRoleConfig | None = None,
) -> tuple[bool, str]:
    """Return (ok, reason) for `BashRole.LEAD` — full SE3 port (PR1 +
    PR2, see `guard.director_mutation`'s module docstring for the complete
    scope split and, for PR2, the harness-attestation seam design).

    Composition order mirrors the reference's own `main()` dispatch
    EXACTLY (reference: `_check_director_lead_mutation` runs BEFORE
    `_check_director_clagentic` for a director/lead session — see the
    reference's `main()` director/lead branch): the mutation-verb-family
    deny (`guard.director_mutation.check_lead_mutation`, PR2) is checked
    FIRST, since it is the SOLE enforcement surface restricting general
    command execution for this role (reference: "no allowlist fallback — a
    missed verb here falls through to the final `return True`"); only once
    a command clears that gate does the narrower caller-identity-discipline
    check (`guard.director_mutation.check_director_identity_discipline`,
    PR1 — which has no opinion on any command that isn't the configured
    relay-shaped IPC verb's conversation subcommand shape) get consulted.

    Both `config.mutation_config` and `config.director_clagentic_config`
    are independently optional (see `LeadRoleConfig`'s own docstring for
    what omitting each means) — a caller integrating this role should
    supply both to reach the reference's full authority boundary; PR1's own
    "intentionally incomplete on its own" posture (no config at all) is
    preserved as a degenerate case, not removed, since some caller might
    have legitimate reason to run only the identity-discipline half (e.g.
    a caller enforcing the mutation deny via a wholly separate mechanism).

    *identity_label* is an opaque display string for denial messages only
    (see `guard.director_mutation.check_director_identity_discipline`'s own
    docstring, module docstring point 2) — never branched on.
    """
    cfg = config if config is not None else LeadRoleConfig()

    if cfg.mutation_config is not None:
        ok, reason = check_lead_mutation(
            command,
            identity_label=identity_label,
            config=cfg.mutation_config,
        )
        if not ok:
            return False, reason

    if cfg.director_clagentic_config is not None:
        ok, reason = check_director_identity_discipline(
            command,
            identity_label=identity_label,
            config=cfg.director_clagentic_config,
        )
        if not ok:
            return False, reason

    return True, ""


# ---------------------------------------------------------------------------
# SE4 (lr-6f61aa, FINAL sub-slice of lr-19ae42): infra/host-operator role.
# ---------------------------------------------------------------------------


def check_infra_command(
    command: str, *, config: InfraOpsConfig | None = None
) -> tuple[bool, str]:
    """Return (ok, reason) for `BashRole.INFRA` — thin composition wrapper
    over `guard.infra_ops.check_infra_op_wrapper` (see that module's
    docstring for the full port pattern, port of the reference's
    `_is_allowed_ashford`, and the MANDATORY ANSI-C ANALYSIS section
    explaining why this role's whole-string-anchored exact-flag grant
    shapes have no evasion class for the SE1/SE2 bare-verb-grant hard-deny
    gate to close).

    This module composes `guard.infra_ops` exactly as it already composes
    `guard.director_mutation` for `BashRole.LEAD` — no parsing or
    containment logic of its own beyond what `infra_ops` itself delegates
    to `guard.shell_parsing`/`guard.scratch_policy`.
    """
    return check_infra_op_wrapper(command, config=config)


def check_bash_call(
    role: BashRole,
    command: str,
    *,
    config: RoleAllowlistConfig | None = None,
    read_only_config: MergerReadOnlyConfig | None = None,
    review_config: ReviewGateConfig | None = None,
    scanner_patterns: tuple[re.Pattern[str], ...] = (),
    analysis_config: AnalysisRoleConfig | None = None,
    research_config: ResearchRoleConfig | None = None,
    planning_reader_read_only_configs: tuple[PlanningReaderReadOnlyConfig, ...] = (),
    lead_config: LeadRoleConfig | None = None,
    lead_identity_label: str = "lead",
    infra_config: InfraOpsConfig | None = None,
) -> tuple[bool, str]:
    """Top-level role-dispatch entry point: is *command* permitted for
    *role*?

    `read_only_config` is consulted only for `BashRole.MERGER`;
    `review_config`/`scanner_patterns` only for `BashRole.REVIEWER`/
    `BashRole.SECURITY`; `analysis_config` only for `BashRole.ANALYSIS` (and
    is REQUIRED for that role — see `check_analysis_command`'s docstring for
    why this module does not silently default it); `research_config` only
    for `BashRole.RESEARCH`; `planning_reader_read_only_configs` only for
    `BashRole.PLANNING_READER`; `lead_config`/`lead_identity_label` only for
    `BashRole.LEAD` (see `check_lead_command`'s docstring for this PR's
    scope — the mutation-verb deny half of LEAD lands in a follow-up PR);
    `infra_config` only for `BashRole.INFRA` (SE4/lr-6f61aa, the FINAL
    sub-slice of lr-19ae42 — see `check_infra_command`'s docstring).
    Every role-specific parameter is ignored, without error, when passed
    alongside a different role — a caller composing a single dispatch call
    site for every role need not conditionally omit any of them.

    Raises ValueError for any `BashRole` member this module does not yet
    implement a checker for (no permissive fallback — see module docstring
    point 4), or if `BashRole.ANALYSIS` is dispatched with no
    `analysis_config` supplied.
    """
    if role is BashRole.BUILDER:
        return check_builder_command(command, config=config)
    if role is BashRole.MERGER:
        return check_merger_command(
            command, config=config, read_only_config=read_only_config
        )
    if role is BashRole.REVIEWER:
        return check_reviewer_command(command, config=review_config)
    if role is BashRole.SECURITY:
        return check_security_command(
            command, config=review_config, scanner_patterns=scanner_patterns
        )
    if role is BashRole.ANALYSIS:
        if analysis_config is None:
            raise ValueError(
                "BashRole.ANALYSIS requires an explicit analysis_config "
                "(git_readonly_subcommands has no safe default -- see "
                "check_analysis_command's docstring)."
            )
        return check_analysis_command(command, config=analysis_config)
    if role is BashRole.RESEARCH:
        return check_research_command(command, config=research_config)
    if role is BashRole.PLANNING_READER:
        return check_planning_reader_command(
            command,
            config=config,
            read_only_configs=planning_reader_read_only_configs,
        )
    if role is BashRole.LEAD:
        return check_lead_command(
            command, identity_label=lead_identity_label, config=lead_config
        )
    if role is BashRole.INFRA:
        return check_infra_command(command, config=infra_config)
    raise ValueError(
        f"BashRole {role!r} has no registered checker in "
        f"guard.role_allowlist."
    )


__all__ = [
    "DEFAULT_FORBIDDEN_GIT_PATTERNS",
    "AnalysisRoleConfig",
    "BashRole",
    "LeadRoleConfig",
    "MergerReadOnlyConfig",
    "PlanningReaderReadOnlyConfig",
    "ResearchRoleConfig",
    "ReviewGateConfig",
    "RoleAllowlistConfig",
    "check_analysis_command",
    "check_ansi_c_quote_denied",
    "check_bash_call",
    "check_builder_command",
    "check_forbidden_git_patterns",
    "check_infra_command",
    "check_lead_command",
    "check_merger_command",
    "check_planning_reader_command",
    "check_proc_environ_denied",
    "check_research_command",
    "check_reviewer_command",
    "check_security_command",
    "is_admitted_merger_read_only",
]
