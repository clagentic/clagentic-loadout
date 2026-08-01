# Guard policy

`clagentic_loadout.guard` is a harness-agnostic policy library: a Bash
command classifier, a Write/Edit scope enforcer, a credentials guard, a
task-dispatch guard, and a git-operation guard, plus the settings/allowlist
generation that keeps a harness's own outer permission classifier in sync
with what the guard layer actually admits. It replaces the guard surface an
earlier, internal deployment carried as harness-coupled hook scripts —
every module here is pure policy: explicit typed inputs in, an admit/deny
(or warn) decision out, with no dependency on any specific harness's hook
contract, agent-name roster, or unreleased internal tool (CLAUDE.md rules 1
and 6a).

A caller's own harness-specific adapter does the stdin/exit-code
translation and maps its own identity model onto this library's role enums
(`guard.role_allowlist.BashRole`, `guard.write_scope.WriteRole`) — none of
that adapter plumbing lives in this package.

## `guard.env_prefix` — exactly-anchored env-prefix admission

`strip_allowed_env_prefix(command)` admits EXACTLY one leading
env-assignment shape:

```
CLAGENTIC_LOADOUT_GITHUB_APP_SLUG=<safe-token-value>
```

end-anchored on both the variable name (`ALLOWED_ENV_PREFIX_VAR` — no other
name is ever admitted) and the value's character class (alphanumeric,
hyphen, underscore, 1-128 chars, no leading hyphen — no shell metacharacter
of any kind is representable). A near-miss (wrong variable name, or a value
outside the safe-token class) is never partially stripped — the function
returns `(None, command)` unchanged, so a caller's own downstream verb
classifier evaluates the ENTIRE original command line, including the bogus
prefix, and correctly fails to recognize any known verb at its head.

## `guard.scratch_policy` — category-grant spawn-scratch containment

`is_scratch_contained(command)` is a CATEGORY grant by target-path
CONTAINMENT, not a verb-by-verb enumeration. Any command whose argv[0] is
in `SCRATCH_SAFE_VERBS` (`mkdir`, `touch`, `mv`, `cp`, `rm`, `mktemp`,
`rmdir`, `ln`, `chmod`) AND whose every filesystem-shaped argument resolves
— after `$TMPDIR` expansion and `os.path.realpath` canonicalization — under
a configured scratch boundary, is admitted. The instant any single argument
resolves outside that boundary, the WHOLE command is denied (never a
partial grant): `mv $TMPDIR/x /some/repo-tree/y` is refused even though one
of its two arguments is contained, because the command's real effect —
moving into the repo tree — is not.

**TMPDIR-only:** `SCRATCH_ROOT_ENV_VARS` previously admitted
both `$TMPDIR` and `$HOME` as sanctioned scratch-staging roots. `$HOME` is
now DROPPED — the earlier justification (a `--body-file` design needing an
addressable location outside `$TMPDIR`) no longer holds once `--create-
branch` staging closed that gap. An unset/empty `$TMPDIR` falls back to the
process's real uid-home directory (`resolve_scratch_boundary`'s own
docstring) rather than yielding zero boundaries — a deliberate design
decision aligned with the identical posture landed on the sibling
deployment-automation project this platform composes with. `$HOME` remains
the per-spawn process-identity directory; it is simply no longer a
scratch-staging boundary or redirect target.

Both the scratch boundary itself and the expanded target are run through
`os.path.realpath` before the containment comparison, so a symlink planted
inside `$TMPDIR` pointing outside it, or a `..`-traversal token, both
resolve to their true target and correctly fail containment.

What is deliberately NOT in this category grant — a caller's own narrow
enumerated allowlist covers these separately, scratch is explicitly not it:

- git operations of any kind
- push / merge / release verbs
- any network call
- any write targeting the repo tree (project root)

`is_scratch_contained` raises `ScratchContainmentError` (rather than
returning `False`) for a compound/piped/redirected shell expression, or a
verb outside `SCRATCH_SAFE_VERBS` — this module classifies exactly one
simple, unpiped, uncompounded command. A command with no scratch boundary
configured at all (`$TMPDIR` empty/unresolvable AND no uid-home fallback
available) fails closed — never permissive by default.

## `guard.settings_export` — single source, both sinks

`guard.scratch_policy.SCRATCH_SAFE_VERBS` and
`guard.write_scope.WriteScopeConfig.allowed_paths` are each the ONE policy
source both of the following sinks read from, so a guard-layer admission
decision and a harness's outer permission classifier can never disagree:

- **Sink 1 (guard layer)**: a caller's own PreToolUse-style hook calls
  `is_scratch_contained` / `check_write_call` directly against the literal
  command at hook-fire time — the real containment/scope decision, since
  only the guard layer has the actual argv/file_path and can resolve
  `$TMPDIR` against the live process environment.
- **Sink 2 (harness settings fragment)**: `guard.settings_export.
  scratch_permission_fragment()` / `write_scope_permission_fragment()` emit
  the coarse `Bash(<verb> *)` / `Edit(<glob>)` permission-allowlist entries
  for a harness's outer allowlist, from the SAME source list, so the outer
  classifier never blocks a call the guard layer already correctly
  contains-checks per invocation.

The outer harness classifier is coarser than the guard layer by
construction (a permission allowlist matches only on argv[0]/argv-prefix or
glob shape — it cannot itself resolve `$TMPDIR` or canonicalize a
target path), so Sink 2 never bypasses Sink 1's enforcement — it only
removes an outer-layer prompt for a call the guard layer is already
prepared to correctly admit-or-deny on its own.

Not every guard module is dual-sink; see "Coverage map" under Conformance
below for which are and why.

## `guard.write_scope` — Write/Edit scope enforcement

`WriteRole` has four members: `SCOPED` (the only role whose calls are
conditionally admitted, checked against a `WriteScopeConfig` — `allow_all`
/ `allowed_paths` / `blocked_paths`, with `blocked_paths` always
deny-winning and an undeclared scope failing closed), `MERGE_GATE` and
`LEAD` (unconditional hard-deny — a release-gate or lead/director identity
never authors files), and `READ_ONLY` (unconditional hard-deny —
defense-in-depth for any role with no Write/Edit capability at all).
`check_write_call` is the single entry point a caller's harness adapter
invokes.

## `guard.credential_paths` — credential-path denial

Applies identically to every role — there is no role parameter.
`DEFAULT_CREDENTIAL_DENY_LIST` (`.netrc`, `.git-credentials`,
`inject_credentials`, etc.) is a universal default, since it names
credential FILE SHAPES rather than machine paths. `check_read_path` /
`check_glob_call` take caller-supplied `protected_home_prefixes` /
`allowed_exact_paths` / `allowed_bak_prefixes`. `check_bash_command` uses a
path-anchored regex approach so `curl --netrc` — a legitimate auth flag —
is never confused with a credential-file-path reference. `is_valid_bak_path`
hardens `.bak-` suffix handling (rejects directory-as-prefix and traversal
suffixes) as a base-path-parameterized helper.

## `guard.shell_parsing` — shell-word normalization core

Pure text-processing primitives with no policy content: `decode_for_match`,
`mask_quoted_spans`, `decode_ansi_c_escapes`, `quote_delimited_spans`,
`normalize_shell_words`, `compound_check`, `split_glued_redirect_operators`,
`detect_tmp_redirect_target`, and their tightly-coupled helpers
(`unquoted_spans`, `split_segments`, `cmd_head`, `is_safe_redirect_only`,
`has_background_operator`, `has_unresolved_ansi_c_quote`,
`is_staging_redirect_target`). `compound_check` is the pure structural gate
(does this command contain a compound/piped/chained/backgrounded shell
operator) — verb-specific pipe carve-outs are a policy decision layered on
top by `guard.bash_admission`, not part of this module.

**Fail-closed posture**: every normalization function that can fail to
confidently parse a command (unbalanced quoting, an unresolvable ANSI-C
escape, a quoted command-substitution opener) returns `None` rather than
guessing — `None` means "cannot confidently normalize," never "permitted."

## `guard.bash_admission` — building-block admission predicates

`detect_project_tree_write_targets` / `is_fd_safe_target` (enumerate every
non-staging write-redirect target so a caller can apply
`guard.write_scope.check_write_scope` to a Bash command's redirect targets,
not only a Write/Edit call's `file_path`), `is_admitted_loadout_family_
readonly` (the generic `loadout-<verb>` read/validate family grant,
mutating-verb-name excluded), `requires_admission_flag` (the generic
"METHOD to PATH shape requires FLAG" admission, parameterized via
`MethodPathFlagRule`), and `is_admitted_body_stdin_pipe` /
`classify_body_stdin_pipe_ambiguity` (the loadout-verb `--body-stdin` pipe
carve-out, built on `shell_parsing.split_segments` / `normalize_shell_
words` / `cmd_head`, generalized to a caller-supplied `BodyStdinVerb`
registry).

Every admission-deciding raw-fallback verb-matcher here hard-denies on an
unresolved ANSI-C opener rather than falling through to a raw scan that
cannot see a verb hidden inside an intact `$'...'` wrapper.

## `guard.role_allowlist` — per-role Bash allow-checkers

`BashRole` is a single, growing enum: `BUILDER`, `MERGER`, `REVIEWER`,
`SECURITY`, `ANALYSIS`, `RESEARCH`, `PLANNING_READER`, plus `LEAD` and
`INFRA` (composed from the separate `guard.director_mutation` /
`guard.infra_ops` modules below). `check_bash_call` is the single
role-dispatch entry point once every role checker is wired.

- **`BUILDER`** — general-purpose repo-authoring: git/task-tracking, a
  push/PR transport verb via caller config, the `clagentic: loadout` read/validate
  family, scoped build/test/lint commands, `$TMPDIR` staging writes.
- **`MERGER`** — deliberately NARROWER: release-gate scope only
  (merge/close/post-merge/release verbs via caller config), a narrow
  GET-only pre-check read path, no build/test/lint verbs, no bare
  task-tracking-CLI grant.
- **`REVIEWER`** / **`SECURITY`** — read-only review/audit roles; `SECURITY`
  alone gets deterministic scanner invocation and `git log`, `REVIEWER`
  alone gets an external-model-carrier invocation surface and a narrower
  `git show`/`git diff`-only read surface.
- **`ANALYSIS`** — read-only observation (troubleshooting/platform-health
  shape): a configurable `git -C <repo>`/bare `git` read-only subcommand
  set, systemctl/docker inspection subcommands, a curl health probe, bare
  file readers.
- **`RESEARCH`** — read-only, zero git/systemctl/docker visibility;
  external-research-engine invocation only.
- **`PLANNING_READER`** — read-only plus task-authoring (creating/updating
  work-tracking items), plus a narrow GET-only forge-read pre-check.

Every role checker composes `check_forbidden_git_patterns` /
`check_proc_environ_denied` (role-independent helpers),
`shell_parsing.compound_check`, `scratch_policy.is_scratch_contained`, and
`bash_admission.is_admitted_loadout_family_readonly` — no parsing,
containment, or forbidden-substring scanning is reimplemented per role. No
machine/operator path or fixed transport-verb literal is hardcoded — a
caller-supplied `RoleAllowlistConfig.extra_verb_patterns` (and
role-specific config fields, e.g. `AnalysisRoleConfig.
git_readonly_subcommands`) supplies the caller's own installed-verb
vocabulary and path shapes.

**MANDATORY bare-verb-grant hardening**: any checker that affirmatively
admits a command by matching only a leading bare-verb token
(`^<verb>(\s|$)`) against a raw/un-normalized command string must (a) scan
`shell_parsing.normalize_shell_words(command)`, not the raw string, for any
substring-based deny check the grant depends on, and (b)
`check_ansi_c_quote_denied` must run ahead of the grant for the residual
unresolvable-ANSI-C case. Every bare-verb grant in this module follows both
parts. Any caller-supplied config field whose value flows into a guard
regex must be grammar-validated (`_ROLE_TOKEN_RE`, or `re.escape()`d where
the field's contract needs non-token characters) at construction time,
never left to reach `re.compile()` unchecked.

## `guard.director_mutation` — director/lead identity discipline and
mutation deny

Composed by `role_allowlist.check_lead_command` (`BashRole.LEAD`), in a
separate module since folding it into `role_allowlist.py` would make that
module a god file.

`check_lead_command` runs the mutation-verb deny FIRST — `check_lead_
mutation` classifies `git_write` / `file_mutation` / `package_mutation` /
`systemctl_mutation` / raw curl-wget and forge PR-mutation patterns / shell
write redirection / credentials-file write denial, all ROLE-INDEPENDENT
shell-command SHAPE matchers, via `LeadMutationConfig` (caller-supplied
verb/host/path patterns — no operator hostname or script path is
hardcoded) — then `check_director_identity_discipline` (caller-identity
discipline on a relay-shaped IPC verb's `open`/`post`/`close` conversation
subcommands: a lead/director session must make its own identity EXPLICIT
via `--opener`/`--from`, or the `close` subcommand's `--reason` text must
embed it).

`ActingSubagentResolver` is an OPTIONAL harness-attestation seam (a frozen
dataclass of caller-supplied callables) that lets a caller defer a
forgejo-curl invocation carrying a trustworthy, attested `--caller <name>`
to that role's own narrower allowlist instead of denying it as the lead's
own direct PR-mutation attempt. `config.acting_subagent_resolver = None`
(the default) means the carve-out is unreachable and every matching
forge-PR-mutation invocation denies unconditionally — the seam only ever
ADDS admission when a caller explicitly wires a resolver in.

`check_lead_mutation` is the SOLE enforcement surface for this role (no
allowlist fallback), so it hard-denies before the mutation-family dispatch
runs whenever normalization fails to resolve the command AND an ANSI-C
opener is present.

## `guard.infra_ops` — INFRA (host-operator) role

Composed by `role_allowlist.check_infra_command` (`BashRole.INFRA`, the
mutating-infrastructure / host-operator identity — SSH + credential
rotation, the highest-blast-radius role), in its own module for the same
god-file reason as `director_mutation`.

Structurally different admission shape from every other role: each of the
five fixed-path op wrappers is admitted ONLY as a flag-based invocation
whose argv carries EXACTLY the typed fields that op's own input-schema
already validates — never a raw command string. Every admission pattern in
`check_infra_op_wrapper` is a WHOLE-STRING (`^...$`) anchor over an exact,
ordered `--flag <value>` sequence with a closed value grammar (excludes
whitespace and every shell metacharacter, including `$`) — there is no
free-form argv tail for an ANSI-C-quote-fragmented forbidden operation to
hide inside, so the mandatory bare-verb-grant ANSI-C gate does not apply
here (documented explicitly in the function's own docstring rather than
omitted silently or bolted on where it doesn't fit).

The task-tracking surface for this role is a FIXED subset (observe/comment/
show/list/search only, no wildcard, no write/mutation verb beyond that) —
not caller-configurable, a deliberate role-defining security boundary. No
`git *`, no push/PR-transport verb, no Write/Edit admission of any kind is
in the admitted-pattern list at all.

## Conformance

Exercised with **synthetic paths, invented commands/roles, and no LORE
present** (CLAUDE.md rule 6a) — see `tests/test_guard_scratch_policy.py`,
`tests/test_guard_env_prefix.py`, `tests/test_guard_settings_export.py`,
`tests/test_guard_write_scope.py`, `tests/test_guard_credential_paths.py`,
`tests/test_guard_shell_parsing.py`, `tests/test_guard_bash_admission.py`,
`tests/test_guard_role_allowlist.py`,
`tests/test_guard_role_allowlist_se2.py`,
`tests/test_guard_role_allowlist_se3.py`,
`tests/test_guard_role_allowlist_se3_pr2.py`,
`tests/test_guard_role_allowlist_se4.py`,
`tests/test_guard_director_mutation.py`,
`tests/test_guard_director_mutation_se3_pr2.py`,
`tests/test_guard_infra_ops.py`,
`tests/test_guard_task_dispatch.py`,
`tests/test_guard_dispatch_discipline.py`, and
`tests/test_guard_git_write_guard.py`.
No agent name, task-tracking system, or specific harness is imported or
hardcoded anywhere in `clagentic_loadout.guard`.

## Coverage boundary — what the shipped predicates see, and what they don't

Every guard predicate in this package classifies a **shell command string**
(or a `Write`/`Edit` `file_path` string passed explicitly to
`guard.write_scope.check_write_call`). Concretely, the channels the shipped
predicates evaluate are:

- **Shell redirects** — `>`, `>>`, `<` targets, recognized via
  `shell_parsing.detect_tmp_redirect_target` /
  `is_staging_redirect_target` / `bash_admission.
  detect_project_tree_write_targets`, and the write-side containment check
  in `scratch_policy.is_scratch_contained`.
- **Heredocs** — `shell_parsing.cmd_head` strips heredoc body content so a
  quoted pipeline or verb inside a heredoc's own text is never mistaken for
  a live shell operator; the heredoc's *opening* command line is still
  scanned normally.
- **Recognized verb spellings** — a fixed, per-role set of admitted command
  prefixes/whole-string shapes (`guard.role_allowlist.BashRole`'s
  `check_builder_command` / `check_merger_command` / etc., and
  `guard.infra_ops.check_infra_op_wrapper`'s five whole-string-anchored op
  wrappers), plus the compound/pipe/background structural gate
  (`shell_parsing.compound_check`) and the ANSI-C-quote-evasion hard-deny
  (`role_allowlist.check_ansi_c_quote_denied`) that every bare-verb grant in
  this package composes ahead of its own affirmative match.
- **The one explicit non-shell channel this package DOES cover**:
  `guard.write_scope.check_write_call`, when a caller's harness actually
  routes its own `Write`/`Edit`-equivalent tool calls through it. This is
  opt-in composition, not automatic interception — see below.

**What is NOT covered, structurally, by any predicate in this package: any
write channel that never becomes a shell command string or an explicit
`check_write_call` argument at all.** A harness that grants an agent a
direct file-write tool — a `Write`/`Edit`-shaped capability distinct from
`Bash`, wired into the harness's own tool-dispatch layer rather than shelled
out through `subprocess`/`bash -c` — produces file mutations this package's
Bash-command classifiers never see, because there is no command string for
`shell_parsing`/`role_allowlist`/`infra_ops` to parse in the first place.
`guard.write_scope.check_write_call` CAN classify such a call correctly —
its contract is an explicit `WriteRole` + `file_path`, not a shell string —
but only if the harness's own tool-dispatch layer actually calls it before
admitting the write. A harness that grants a file-write tool without wiring
that tool's admission through `check_write_call` (or an equivalent
containment predicate applied at the same boundary) has a write channel
with no containment check on it at all, regardless of how tightly every
shell-facing channel is guarded.

**This is the integrator's responsibility, not something this package can
discharge for a harness it does not own** (CLAUDE.md hard rule 2 — loadout
does not own agent spawning or tool dispatch). Cover a granted file-write
channel with the SAME boundary this package already enforces on the shell
side:

- **Scratch writes are TMPDIR-only.** Reuse
  `guard.scratch_policy.is_scratch_contained` (or, for a single already-
  resolved path rather than a full command line,
  `guard.scratch_policy.resolve_scratch_boundary` /
  `resolve_all_scratch_boundaries` directly) as the containment check a
  harness applies to a file-write tool's target path before admitting the
  call — the identical `$TMPDIR`-realpath-containment boundary this package
  already applies to every `mkdir`/`touch`/`mv`/`cp`/`rm`/`mktemp`/`rmdir`/
  `ln`/`chmod` shell invocation. These are the reusable, importable public
  predicates; do not re-derive a second realpath/symlink-escape
  implementation for the file-write-tool channel.
- **Repo governance/config directories are config-only.** Reuse
  `guard.write_scope.check_write_call` (or the underlying
  `WriteScopeConfig`/`check_write_scope` containment logic it composes) as
  the SAME scope boundary a repo's `allow_all`/`allowed_paths`/
  `blocked_paths` declaration already applies to a `SCOPED`-role Bash-driven
  write — a harness-granted file-write tool targeting a repo's own
  governance or config directory (e.g. wherever a repo keeps its own
  per-project agent policy file) must consult that same boundary, not a
  channel-specific carve-out invented independently.

**No channel is ambiguous once this boundary is applied consistently**: a
harness integrator enumerates every write-capable tool it grants an agent,
and for each one, either (a) it is a shell-spelled Bash invocation, already
covered by the predicates above, or (b) it is a non-shell tool, and the
integrator is responsible for calling the same containment predicate set
against that tool's own admission path before the write is allowed to
proceed. See [docs/integration.md](integration.md)'s "Channel parity"
section for the concrete failure shape this boundary exists to prevent, and
for the wiring pattern a harness integrator should follow.

### Coverage map — which modules are dual-sink

| guard module | dual-sink? | settings_export function | source of truth |
|---|---|---|---|
| `env_prefix` | no (guard-layer-only) | — | prefix-stripping classifier, not a verb grant |
| `scratch_policy` | **yes** | `scratch_permission_fragment` | `SCRATCH_SAFE_VERBS` |
| `write_scope` | **yes** (`WriteRole.SCOPED` only) | `write_scope_permission_fragment` | `WriteScopeConfig.allowed_paths` |
| `credential_paths` | no (guard-layer-only) | — | pure denylist, no positive grant |
| `shell_parsing` | no (guard-layer-only) | — | pure parsing primitives, no admission decision |
| `bash_admission` | no (guard-layer-only) | — | building-block predicates consumed by `role_allowlist`; its one literal-verb grant shape is already covered by the separate `provisioning.allowlist` generator |
| `role_allowlist` (BUILDER/MERGER/REVIEWER/SECURITY/ANALYSIS/RESEARCH/PLANNING_READER) | no (guard-layer-only) | — | admission keyed on caller-supplied `re.Pattern` objects, no mechanical glob translation |
| `director_mutation` (`BashRole.LEAD`) | no (guard-layer-only) | — | same regex-pattern config shape as `role_allowlist`, plus majority deny/mutation-family classification, not affirmative grants |
| `infra_ops` (`BashRole.INFRA`) | no (guard-layer-only) | — | same regex-pattern config shape as `role_allowlist` |
| `task_dispatch` | no (guard-layer-only) | — | Task-tool `subagent_type` roster, not a `Bash`/`Edit`/`Write` permission-allowlist shape |
| `dispatch_discipline` | no (guard-layer-only) | — | warn-only, never denies; no admission decision to mirror |
| `git_write_guard` | no (guard-layer-only) | — | git/PR write hard-deny classifier, not an affirmative grant |

`tests/test_guard_settings_export.py::TestClosingCoverageConformance`
asserts every module above is either a real dual-sink function or named in
`guard.settings_export`'s own module docstring as guard-layer-only — so a
future guard module cannot land without an explicit, test-enforced
coverage decision either way.
