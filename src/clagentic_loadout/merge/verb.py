"""merge.verb — the merge-gate CLI: the full gate chain, then merge.

Wave B slice 4 (lr-885f, tome #688) + slice 4b's CLI wiring (lr-9c69). Ported
from the reference merge gate; the source module stays primary until its
separate CUT OVER + RETIRE + VERIFY-GONE task per the migration plan.

THIS IS THE LOAD-BEARING RELEASE GATE — the code that decides whether
anything lands on main. Every gate link below is ALL FAIL-CLOSED; do not
weaken any of them. The platform choice (--platform, mandatory) affects
ONLY which backend fetches gate facts and executes the merge — the gate
chain itself (steps 1-7 below) runs IDENTICALLY regardless of platform.

THE GATE CHAIN (in enforcement order, mirroring the reference module's own
documented step ordering):
  0. Repo-path/slug consistency (merge.repo_path_consistency, lr-4522a3) —
     when --repo-path points at a real, parseable git tree, its OWN origin
     remote must name the same owner/repo as --repo, or the merge refuses
     naming both values. Runs before step 1, so a caller-side argument
     defect (the wrong local tree for the requested slug) never reaches a
     credential mint and surfaces as a confusing platform-API rejection.
     Compares against the tree's remote, never its directory name — see
     that module's own docstring for why the '.github' org-profile shape
     (directory basename diverges from slug by design) is correctly never
     flagged.
  1. Namespace guard (merge.verb, reusing push.namespace_guard verbatim —
     the same config-driven allowed-namespace seam, no second
     implementation). Runs first: a refusal here must never mint a
     credential or make a network call.
  2. Merge-authority check (merge.authority) — FAIL-CLOSED provider seam.
     Runs before any credential is minted for an out-of-scope/unauthorized
     request.
  3. Platform guard + credential resolution (transport.credential_provider)
     — the platform guard (assert_platform_is_forgejo /
     assert_platform_is_github, BOTH directions, fail-closed) runs FIRST,
     then the SAME credential seam every other loadout verb resolves a
     git-host token through. Runs only after 1-2 pass, so an unauthorized or
     wrong-platform request never causes a token resolution attempt.
  4. Stale-SHA refusal (merge.stale_sha) — compares --expected-head-sha
     against the PR's LIVE current head, read fresh from the resolved
     backend's own platform API.
  5. Reviewer-verdict fences (merge.verdict) — for each required reviewer:
     locate their latest PR comment (authorship verified by user.login,
     never comment body text), parse the fenced ```review-result``` block,
     refuse on missing/malformed/stale/blocking/role-mismatched. The
     role-mismatch check (lr-23fe19) is defense-in-depth ON TOP OF the
     user.login binding: it additionally asserts the block's own
     self-declared 'reviewer' field matches the required-reviewer name this
     login slot is for, catching a right-App/wrong-content verdict (see
     merge.verdict's module docstring and merge.errors.
     VerdictRoleMismatchError). Identical on both platforms — see
     merge.github_backend's module docstring. ENFORCED BY DEFAULT (lr-5260f9
     — merge.post_merge_config.resolve_enforce_single_verdict_fence): a
     selected comment body carrying MORE THAN ONE fenced block is ALSO a
     refusal (VerdictMalformedError) rather than silently parsing the last
     one. A repo with legacy multi-fence comments opts OUT explicitly via
     `merge: enforce_single_verdict_fence: false` — see that resolver's own
     docstring for the full trade-off. The roles required are the UNION of
     --required-reviewer and the repo's declared
     `merge: required_reviewer_roles` (a floor; merge.repo_gate_runtime),
     read from the tracked gate file at the PR's BASE commit, never the
     working tree, so the PR under review cannot relax its own gate. If
     required_reviewer_roles or required_scanners cannot be loaded, that
     pair (only) falls back to flags-only with a stderr warning; a loadable but unsatisfiable floor refuses, overridable
     only by --ignore-repo-gate (logged, attested). Findings state: a
     finding an earlier fence held open that the current verdict neither
     clears at this head nor re-raises refuses (merge.verdict.
     assert_prior_findings_resolved), and a clean verdict reporting a
     `merge: required_scanners` scanner as failed refuses
     (merge.verdict.check_required_scanners).
  5b. Model attestation (merge.model_attestation, lr-95543d) — OPT-IN
     (`merge: require_model_attestation: true`, default off): when enabled,
     a required reviewer's `clean` verdict must ALSO carry a genuine
     `model_attested` declaration, refused with the SAME disposition as a
     blocking verdict otherwise. THIS IS A SELF-REPORTED FIELD, NOT A
     TOOL-VERIFIED FACT — see merge.model_attestation's module docstring
     for the full trust model this check does and does not provide before
     relying on it.
  6. Diff-scope cap (merge.diff_scope) — refuse a PR whose changed-file
     count exceeds the configured limit.
  7. PR-title gate (merge.title_gate) — Conventional Commits grammar, with a
     --skip-title-check bypass (logged when used).
  7b. Branch commit-subject gate (merge.commit_subjects, lr-835c57) — on a
     RESOLVED --merge-method='merge' (real, non-squash) repo, validates
     EACH branch commit subject (base..head) against the SAME Conventional
     Commits grammar step 7 already applies to the PR title, since
     semantic-release parses individual branch commit subjects (not the
     promoted title) on a real merge. A no-op on any other --merge-method
     value (squash/rebase rewrite the resulting commit subject FROM the
     already-gated PR title). BLOCKS, never rewrites history — a
     --skip-commit-check bypass mirrors --skip-title-check exactly (logged
     when used). See that module's docstring for the full rationale.
  8. CI-status gate (merge.ci_status, lr-afba CI-status-gate slice) — reads
     CI evidence at the PR's HEAD from the resolved backend. AN EMPTY
     RESULT (zero commit-status entries AND zero check/workflow runs) IS AN
     EXPLICIT PASS, not a fall-through: many repos (this one included, by
     design — lr-368c, "runner explicitly out of scope") have no CI runner
     wired up at all, and a gate that fails closed on "no CI data" would
     falsely refuse every merge in such a repo. A NON-EMPTY result gates on
     the real combined state: "success" passes; any other non-empty state
     (failure, error, pending, or an unrecognized value) refuses, reporting
     the actual state and evidence counts seen — never a collapsed guess.
     See merge.ci_status's module docstring for the full decision and
     merge.forgejo_backend.fetch_ci_status / merge.github_backend.
     fetch_ci_status for the fail-closed fetch contract (unreachable/non-200
     still raises GateFactUnavailableError exactly like every other
     gate-fact fetch below).
  8b. Pre-merge checks gate (merge.pre_checks_config, lr-843900) — an
     OPTIONAL, repo-declared `merge: pre_checks:` list (same step shape as
     post_merge_steps, reusing merge.post_merge.run_post_merge_steps
     VERBATIM — same on_failure semantics, same PASS/FAIL/exit-code stderr
     record on every step). DECLARED by the tracked gate file at the PR's
     BASE commit (the same read as `required_reviewer_roles`, see step 5) and
     EXECUTED in --repo-path, as always; only their configuration comes from
     base. A repo with no local tree (--no-post-merge-tree/--skip-post-merge)
     declares none, the same pre-existing "no tree, no repo-local config
     readable at all" boundary every other repo-tier key in this chain
     already has, not a new gap this step introduces. See "PRE-MERGE
     CHECKS" further down for the full history/contract this closes.
  9. Only after ALL of the above pass: execute the merge via the resolved
     backend's merge_pr (Forgejo or GitHub, both via a redirect-hardened
     transport), passing args.merge_method THROUGH to the backend (lr-14f704
     — before this fix, --merge-method was parsed and gated step 7b above but
     was NEVER forwarded to either backend's merge_pr, so a caller requesting
     --merge-method squash silently got a real merge commit anyway; see
     merge.forgejo_backend's module docstring, "MERGE_METHOD THREADING", for
     the full defect history). On ANY gate failure above: refuse and exit
     non-zero — never merge. The GitHub path's 200+merged:false
     disambiguation (never trust the status code alone — see
     merge.github_backend.merge_pr) is reached unchanged through this same
     call site. When --repo-path is given, step 10 below FIRST fetches the
     merged commit into that tree's local object database (merge.tree_sync,
     lr-7c5540 — a CHECKOUT only when post_merge_steps will actually run
     this invocation, see lr-173768), THEN (lr-14f704 item 3) reads back
     that landed commit's ACTUAL parent count and compares it against what
     the requested --merge-method predicts — a mismatch is logged loudly
     (WARN by default; a hard refusal, EXIT_MERGE_SHAPE_MISMATCH, only for a
     repo that opts in via `merge: enforce_merge_shape: true` — see
     merge.merge_shape's own docstring for the full trade-off) before any
     post_merge_steps entry runs — see "WORKING-TREE SYNC BEFORE POST-MERGE
     STEPS" further down.

PLATFORM DISPATCH (lr-9c69): mirrors review.verb's platform-parameterized
dispatch shape exactly (see that module's docstring and its build_backend).
_resolve_backend below runs the platform guard BEFORE constructing either
backend or resolving any credential — there is no call path that reaches
token resolution before the platform has been confirmed to match the
resolved backend, for either direction.

SCOPE (task lr-885f, extended by lr-5375 + lr-9c69): the Forgejo backend
(lr-885f) and GitHub backend (lr-5375, merge.github_backend) both existed
before this CLI wiring landed; lr-9c69 is the completion that makes the
GitHub path CLI-reachable. GitHub's review-gate mechanism was a materially
different port (GitHub review *state* is NOT used — the same fenced
```review-result``` comment contract is reused unchanged, see
merge.github_backend's docstring) from a separate migration slice, not a
mechanical restatement of the Forgejo slice.

IDENTITY / SEAM STRIP FROM THE SOURCE MODULE (full inventory in the PR body):
  1. The hardcoded 'clagentic' namespace check is now push.namespace_guard's
     config-driven allowed-namespace seam (reused, not reimplemented).
  2. The hardcoded release-gate-role caller default and the directory-
     service-specific merge-authority check are now merge.authority's
     provider seam: which ROLE may authorize a merge is config (--role / an
     AuthorityProvider), never a baked identity.
  3. Credential mint (an OpenBao self-fetch + a gatekeeper-CLI subprocess
     call) is now the SAME transport.credential_provider seam every other
     loadout verb uses — a TokenProvider resolves a token for a
     caller-supplied role, never a hardcoded broker client or fixed
     binary/config path.
  4. An operator-specific Forgejo host is never baked in — the API base is
     CLI input (--git-host-base-url) or an env var, matching
     transport.git_host_api's own resolution.
  5. The gate-note post-merge comment and the LORE task-signal comment (both
     lore-coupled, best-effort audit trail features in the reference module)
     are out of this package's task boundary (CLAUDE.md hard rule 6a: lore
     never appears in product code) and are not ported.
  6. (SUPERSEDED by lr-843900 — see "PRE-MERGE CHECKS" below.) The reference
     module's per-repo pre_checks config loading was NOT ported at the time
     this inventory was first written; merge.pre_checks_config existed as an
     unwired module for a full release cycle before lr-843900 wired it into
     the gate chain as step 8b above. Left here, struck through in prose
     rather than deleted, as the documented history of the gap — see
     "PRE-MERGE CHECKS" for the defect this caused and the fix.

PRE-MERGE CHECKS (lr-843900): merge.pre_checks_config's `load_pre_checks`
existed as a fully-implemented, fully-tested module (repo-local `merge:
pre_checks:` config, same step shape/validator as post_merge_steps) for a
full release cycle WITHOUT ever being imported or called from this module —
item 6 above documented the omission as deliberate, but the module was never
actually gate-chain-dead by design; it was a completed port that was never
wired in. Consequence: a repo could declare `pre_checks` with
`on_failure: fail`, believe it had a deterministic merge gate (particularly a
repo with no CI runner wired up, using pre_checks AS its gate), and get zero
signal that the check never ran at all — the merge proceeded silently, gate
config accepted and inert. Step 8b above closes this: `pre_checks` are now a
REAL gate step, executed via the SAME merge.post_merge.run_post_merge_steps
executor post_merge_steps already uses (same cmd shape, same shell-operator-
token rejection, same on_failure semantics) — a repo's `on_failure: fail`
pre_check that exits non-zero now REFUSES the merge (EXIT_PRE_CHECKS_FAILED)
BEFORE step 9's merge_pr call is ever reached. A pre_checks declaration at
base that is malformed or unreadable REFUSES the merge the same way (no
fallback; only the reviewer-roles/scanners pair falls back, see
merge.repo_gate_runtime). `--skip-pre-checks` is the explicit, logged bypass (mirroring
--skip-post-merge exactly); the gate is enforced by default. Every step,
success or failure, now emits an explicit PASS/FAIL line carrying the raw
exit code and the RESOLVED cwd it executed in (see merge.post_merge.
run_post_merge_steps' own docstring) — silent-on-success was the specific
defect that let this stay undetected: a gate that says nothing when it
passes is indistinguishable from a gate that says nothing because it never
ran.

POST-MERGE STEPS (lr-77d6, added after the identity-strip inventory above
was written): the reference module's post_merge_steps MECHANISM (not its
config file, not its identity) IS ported, as merge.post_merge — an ordered,
config-driven list of commands run in the repo's own working tree ONLY after
step 8 below has ACTUALLY merged the PR, never on any refusal path. Wired
into this module via --repo-path (an OPTIONAL override the caller supplies
whenever a local tree exists — loadout has no project registry of its own to
derive one from, see the "ABSENT --repo-path IS NEVER A SILENT SKIP"
paragraph below) and --skip-post-merge (explicit opt-out, logged when used).
Steps are read from the repo's OWN `.clagentic/loadout/config.yaml` `merge:`
section (merge.post_merge_config) — see that module's docstring for why this
one config surface is repo-local by design, unlike the credentials tier.

ABSENT --repo-path IS NEVER A SILENT SKIP (lr-ac5c8a): before lr-ac5c8a,
omitting --repo-path silently downgraded a merge to "no post-merge run
attempted", exit 0, with no warning — even for a repo whose OWN committed
config declares post_merge_steps. That recurred three times across three
repos (lr-5854ff, lr-4e6f31, clagentic-console PR #365/#366) precisely
because a missing flag and an intentional skip were indistinguishable.
_run now REQUIRES one of three explicit shapes whenever --repo-path is
omitted, checked BEFORE any credential mint or network call (same fail-fast
placement as the owner/repo parse): --repo-path itself (a tree exists, steps
may run), --no-post-merge-tree (an explicit acknowledgment that this
invocation genuinely has no local tree, e.g. a bare API-only merge), or
--skip-post-merge (skip regardless of tree). Omitting --repo-path with
NEITHER of the other two flags is now MergeUsageError -> EXIT_USAGE. Which
of these three a caller should pass is NOT resolved here: deriving a
project's working-tree root from a project/task registry is a DISPATCHER
concern (this package is orchestration-agnostic — see this module's own
docstring point 2 above and this repo's CLAUDE.md rule 2/6a); loadout's
contract is only that the choice must be explicit, never implicit.

WORKING-TREE SYNC BEFORE POST-MERGE STEPS, AND LANDING ON THE BASE BRANCH
AFTER (lr-7c5540, extended lr-d95cdb, re-scoped lr-173768): step 9's
`backend.merge_pr` is a server-side API merge — it never advances the local
`--repo-path` tree. Whenever `--repo-path` is given and the repo's own
`merge.sync_tree_after_merge` config key (default True) has not opted out,
step 10 below fetches the merged commit into that tree's local object
database — but a `git checkout` (detached or otherwise) is performed ONLY
when at least one `post_merge_steps` entry will actually run this
invocation (a non-empty, non-`--skip-post-merge`-bypassed list): a step that
packages/installs the repo (e.g. `scripts/install.sh` reading
pyproject.toml/package source off disk) has no way to see "what merged"
other than a real, populated checkout, via
merge.tree_sync.advance_repo_to_merged_sha (fetch + detached checkout, with
a post-checkout `git rev-parse HEAD` readback verified against the merge
API's own reported SHA when one was returned — see that module's docstring).
When NO steps will run (none configured, or `--skip-post-merge`), step 10
instead calls merge.tree_sync.fetch_merged_sha_object -- the SAME fetch and
an equally independent local `git cat-file -e <sha>^{commit}` readback (see
that function's own docstring, "VERIFICATION IS THIS FUNCTION'S OWN, NOT
DELEGATED TO THE CALLER" — added after a security-review finding on this
PR), but with NO checkout at all: the working tree, index, and
HEAD are left exactly as the caller had them. This still keeps the merge-
shape readback (below) and the merge-completion attestation's SHA claim
independently confirmed against a real fetched object in EVERY case, while
eliminating the unsignaled working-tree mutation on every merge whose repo
either has no post_merge_steps configured or is invoked with
--skip-post-merge — the documented cross-agent contention source on a host
where multiple agents share one on-disk checkout (see merge.tree_sync's own
module docstring, "NO CHECKOUT UNLESS SOMETHING WILL ACTUALLY READ THE
TREE", for the full rationale). See that module's docstring for the
per-backend SHA-resolution trade-off. Any failure to verify the tree/object
landed on the merged commit is EXIT_POST_MERGE_FAILED, never a silent run
against the stale ref.

AFTER post_merge_steps run (or are skipped because the merged commit's own
config resolved to zero steps -- see the drift-authoritative paragraph
below), merge.tree_sync.land_on_base_branch moves the tree OFF the detached
HEAD onto the PR's base branch, repointed (`git checkout -B`, never a merge/
rebase) at the SAME already-verified landed SHA -- so the tree is left
positioned exactly where the NEXT dispatch into this repo needs it: on an
updated local base branch, not detached. GATED ON `tree_checked_out`, NOT
`steps_will_run` (lr-cd3644 fold-in #4): `steps_will_run` can be corrected
DOWN to False by the drift check below AFTER a real checkout already
happened (the merged commit's own tracked config declares zero steps, a
legitimate result, not an error) -- `tree_checked_out` tracks whether a
verified checkout actually occurred, independent of the final step count,
and is the only reliable "is there a detached HEAD to land" signal once
drift correction can change `steps_will_run` after the fact. When
`tree_checked_out` is False (no checkout ever happened -- only a fetch, via
fetch_merged_sha_object), land_on_base_branch is skipped entirely too --
there is no detached HEAD to move off of, and repointing the caller's branch
ref with nothing having read the tree would be exactly the same class of
unsignaled mutation this task removes. `--skip-post-merge` therefore skips
BOTH the configured steps AND any checkout that would otherwise exist only
to serve them (`tree_checked_out` stays False on that path -- the drift
check's own promotion is itself gated on `not args.skip_post_merge`) — it no
longer forces a sync-then-do-nothing checkout the way it did between
lr-d95cdb and lr-173768; a repo that wants to suppress even the FETCH (not
just the checkout) sets `merge.sync_tree_after_merge: false` in its own
config instead.

STALE PRE-SYNC CONFIG DRIFT CHECK (lr-cd3644): `steps` (and therefore
`steps_will_run`, the checkout-vs-fetch-only decision above) is resolved from
`--repo-path`'s WORKING TREE exactly as the caller left it checked out --
BEFORE this step ever advances that tree to the merged commit. A caller whose
`--repo-path` was left on a STALE local ref (e.g. still on an EARLIER merge's
SHA, itself lacking a `post_merge_steps` key a LATER merge to the same base
branch added) therefore resolves `steps` against config that does not match
what is actually being merged THIS invocation -- silently running fewer (or
different) steps than the repo's own live config declares, with no signal at
all (the observed incident this closes: a configured `on_failure: fail`
deploy step skipped entirely because the caller's `--repo-path` was still on
the PRIOR merge's commit -- confirmed by a real `git diff --stat <old> <new>
-- .clagentic/loadout/config.yaml` showing the file differs between the two
commits in that repo). Immediately after `landed_sha` is resolved (either
branch above), `merge.post_merge_config.load_post_merge_steps_from_git_sha`
re-reads the SAME `post_merge_steps` key directly from the git object
database AT `landed_sha` (a `git show`, no checkout -- safe to call
regardless of which branch above ran) and compares it against the pre-sync
`steps` value. THE MERGED COMMIT IS AUTHORITATIVE ON DISAGREEMENT (lr-cd3644
followup, folded into this same original PR): `steps`/`steps_will_run` are
REASSIGNED to the merged commit's own resolution and a checkout is promoted
if needed (see below) -- FAIL LOUD (EXIT_POST_MERGE_FAILED) is preserved only
for the one case the check cannot self-correct, the merged commit's own
config failing to READ at all (malformed YAML at that exact commit). This is
the task's own explicitly named alternative to resolving `post_merge_steps`
post-sync from the start, chosen because resolving it post-sync FIRST would
need to know `landed_sha` before deciding whether a checkout happens at all,
which is exactly the chicken-and-egg lr-173768's checkout-vs-fetch-only
gating was designed to avoid.

NOT EVERY REPO COMMITS THIS FILE -- THE CHECK ONLY FIRES WHEN IT CAN COMPARE
SOMETHING REAL: `.clagentic/loadout/config.yaml` is, by design, sometimes
GITIGNORED and never committed at all (this very package's own dogfooding
config is -- see this repo's own `.gitignore`) -- a valid, common deployment
shape where the file lives purely on disk and, because `git checkout` never
touches an untracked file, literally cannot drift relative to a commit.
`load_post_merge_steps_from_git_sha` returns `None` (never a fabricated `[]`)
when the config path is absent from `landed_sha`'s tree entirely, and the
comparison above is skipped whenever that happens -- the check fires ONLY
for a repo that actually commits this file (like the observed incident's own
repo), never for the gitignored-config shape, where every merge would
otherwise look like a spurious mismatch against a `[]` that was never a real
resolution of anything.

CONFIG-ROOT VS GIT-TREE-ROOT (lr-93d718): --repo-path is not always a git
working tree. A wrapper-layout repo keeps `.clagentic/loadout/config.yaml`
at a wrapper directory (alongside other non-git tooling state) while its
actual `.git` lives at a subdirectory of that wrapper -- no single
--repo-path value satisfied BOTH tree_sync (requires a git working tree) and
config discovery (requires the config-bearing root) for that layout before
this task; passing the wrapper failed tree_sync outright, passing the
subdirectory silently loaded zero post_merge_steps. Step 10 now resolves an
OPTIONAL `merge.git_working_tree` key (merge.post_merge_config.
resolve_git_working_tree) from the repo's own config -- when present, a path
relative to the config root naming the actual git tree, which becomes
tree_sync's target while config discovery (load_post_merge_steps) keeps
reading from --repo-path exactly as always. Absent (the common, flat-layout
case): tree_sync targets --repo-path unchanged, identical to pre-lr-93d718
behavior. See merge.post_merge_config's module docstring for the full
"CONFIG-ROOT VS GIT-TREE-ROOT" rationale and the upward-.git-search
alternative that was considered and rejected in favor of this explicit knob.

THE STALE PRE-SYNC DRIFT CHECK NEEDS THE SAME SPLIT APPLIED TO ITS OWN
GIT-OBJECT READ (lr-cd3644 fold-in #3): `load_post_merge_steps_from_git_sha`
below runs `git show <sha>:<path>` with `cwd=git_tree_path` -- the ACTUAL git
tree, not necessarily the config root. Passing that function's own bare
`config_relative_path`/`legacy_relative_path` defaults unchanged (as if
`git_tree_path` and the config root were the same directory) silently
mis-resolved for exactly the wrapper-layout case this knob exists to serve:
the committed config lives at `<config_root>/.clagentic/loadout/config.yaml`,
which sits OUTSIDE `<config_root>/<git_working_tree>`'s own git tree
entirely, so `git show` there always reported the path absent -- permanently
disabling the drift check whenever a repo declares `git_working_tree`, never
comparable regardless of any real drift.
`merge.post_merge_config.resolve_git_tree_relative_config_paths` re-expresses
the SAME config root's candidate paths relative to `git_tree_path` before
they are handed to `load_post_merge_steps_from_git_sha`, so both the pre-sync
read and the post-sync git-object read agree on which file they mean.

DEPLOYMENT ENV-OVERRIDE SEAM (lr-52d7): a step's subprocess environment is
also layered with merge.post_merge_config.resolve_env_overrides() — a
deployment-owned, non-repo-local tier (env vars named
CLAGENTIC_LOADOUT_POST_MERGE_ENV_<NAME>, plus the user-level config file's
post_merge_env: section) for injecting a machine-specific value (e.g. HOME
in an isolated-HOME spawn harness) into every step without ever hardcoding
it into this repo's own committed .clagentic/loadout/config.yaml. A step's
own inline VAR=VALUE prefix still wins over this tier for the same name. See
that module's docstring for the full design.

DEAD .crew/<role>.yaml post_merge_steps CROSS-CHECK (lr-f9a01b, followup to
doctor.checks.check_dead_crew_post_merge_config): that doctor check flags a
repo whose .crew/<role>.yaml declares post_merge_steps while this repo's own
.clagentic/loadout/config.yaml never declares the key — but a doctor check
only fires when someone explicitly runs `loadout-doctor`, and the reported
failure this class produced was an UNATTENDED merge reporting exit 0 with
steps_run=0: nobody was running doctor. Step 10 now surfaces the SAME
cross-check (via merge.post_merge_config.
find_crew_yaml_files_declaring_post_merge_steps, the identical scan doctor's
own check calls — one scan, two surfaces, never divergent) as a loud,
non-blocking `merge: WARNING --` line on stderr whenever steps_will_run is
False AND at least one .crew/*.yaml file mentions post_merge_steps AND this
repo's own live config never explicitly declares the key
(merge.post_merge_config.post_merge_steps_key_declared — deliberately NOT
`bool(steps)`, so a repo that explicitly wrote `post_merge_steps: []` in its
own config, an informed choice at the correct file, is never warned about an
unrelated stale .crew/*.yaml mention).

WARN, NEVER REFUSE (deliberate, operator-directed disposition): this is a
diagnostic surfaced louder, not a new gate. A repo with a stale .crew/*.yaml
comment must never become unmergeable over it — turning this into a hard
refusal would be a materially larger behavioral change than closing the
"nobody saw the diagnosis" gap requires, and doctor.checks.
check_repo_loadout_schema's own "BLAST RADIUS" precedent (merge.gate_config's
module docstring) already establishes that wiring a diagnostic-only check
into a write/merge path as an ENFORCED gate is an explicit operator decision
with bootstrap implications, not a mechanical follow-up. The warning never
blocks step 9's merge_pr call, never changes steps_will_run, never changes
the exit code — a merge with this shape still exits 0, exactly as before,
just with the silent no-op named loudly in its own output instead of only
discoverable by a later, separate doctor run.

READ-ONLY, NEVER A NEW EXECUTION SURFACE: this cross-check only asks WHICH
.crew/*.yaml files mention the post_merge_steps KEY (a filename and a static
key-name string) — it never parses, returns, or executes that key's VALUE.
.crew/*.yaml was not, and remains not, part of any executable
step-loading path; only this repo's own .clagentic/loadout/config.yaml
(load_post_merge_steps) ever supplies commands run_post_merge_steps
executes.

PRESERVED (load-bearing, not identity — see each gate module's own
docstring for its individual fail-closed contract):
  - Namespace guard, merge-authority check, stale-SHA refusal, verdict-fence
    parse+assert (including the same-line-tag fence requirement and the
    authorship-by-user.login rule), diff-scope cap, PR-title gate.
  - The Forgejo merge_pr HTTP-level fidelity (200/204 success, 405
    three-case disambiguation, any-other-non-2xx refusal) — see
    merge.forgejo_backend.merge_pr.
  - Token never touches os.environ of this process, never appears in logs.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error

from clagentic_loadout._version import get_version
from clagentic_loadout.merge import (
    ci_status,
    commit_subjects,
    diff_scope,
    forgejo_backend,
    github_backend,
    title_gate,
    verdict,
)
from clagentic_loadout.merge.attestation import build_attestation_body
from clagentic_loadout.merge.authority import (
    AuthorityProvider,
    StaticRoleAuthorityProvider,
    check_authority,
)
from clagentic_loadout.merge.errors import (
    AuthorityDeniedError,
    CiStatusFailedError,
    CommitSubjectInvalidError,
    DiffScopeExceededError,
    GateFactUnavailableError,
    MergeExecutionError,
    MergeUsageError,
    PlatformMismatchError,
    StaleHeadShaError,
    TitleInvalidError,
    VerdictBlockingError,
    VerdictMalformedError,
    VerdictMissingError,
    VerdictPriorFindingsOpenError,
    VerdictRoleMismatchError,
    VerdictScannerFailedError,
    VerdictStaleError,
)
from clagentic_loadout.merge.merge_readback import verify_merge_landed
from clagentic_loadout.merge.model_attestation import (
    ModelAttestationInvalidError,
    ModelAttestationMissingError,
    assert_model_attested,
)
from clagentic_loadout.merge.merge_shape import (
    MergeShapeCheckError,
    check_merge_shape,
    format_mismatch_message,
)
from clagentic_loadout.merge.post_merge import (
    EXIT_POST_MERGE_FAILED as _EXIT_POST_MERGE_FAILED,
    PostMergeConfigError,
    PostMergeLivenessError,
    PostMergeStepFailedError,
    PostMergeStepTimeoutError,
    run_post_merge_steps,
)
from clagentic_loadout.merge.post_merge_config import (
    DEFAULT_CONFIG_RELATIVE_PATH as DEFAULT_POST_MERGE_CONFIG_RELATIVE_PATH,
    config_path_tracked_at_git_sha,
    find_crew_yaml_files_declaring_post_merge_steps,
    load_post_merge_steps,
    load_post_merge_steps_from_git_sha,
    post_merge_steps_key_declared,
    resolve_enforce_merge_shape,
    resolve_enforce_single_verdict_fence,
    resolve_env_overrides,
    resolve_git_tree_relative_config_paths,
    resolve_git_working_tree,
    resolve_model_attestation_denylist,
    resolve_post_merge_step_timeout_seconds,
    resolve_require_model_attestation,
    resolve_sync_tree_after_merge,
)
from clagentic_loadout.merge.repo_gate_runtime import (
    load_repo_gate_at_base,
    with_resolvable_reviewer_roles,
)
from clagentic_loadout.merge.repo_path_consistency import assert_repo_path_consistent
from clagentic_loadout.merge.reviewer_login import (
    ReviewerLoginNotConfiguredError,
    resolve_reviewer_login,
)
from clagentic_loadout.merge.stale_sha import check_stale_head_sha
from clagentic_loadout.merge.tree_sync import (
    TreeSyncError,
    advance_repo_to_merged_sha,
    fetch_merged_sha_object,
    land_on_base_branch,
    resolve_base_branch,
    resolve_base_sha,
)
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.repo_config import TRACKED_GATE_RELATIVE_PATH
from clagentic_loadout.task_id_guard import (
    TaskIdGuardViolation,
    load_task_id_guard_config,
)
from clagentic_loadout.push.errors import NamespaceDeniedError, RemoteResolutionError
from clagentic_loadout.push.git_coords import parse_owner_repo
from clagentic_loadout.push.issue_link import parse_closes_issue_number
from clagentic_loadout.push.namespace_guard import (
    ALLOWED_NAMESPACES_ENV_VAR,
    check_namespace_allowed,
    resolve_allowed_namespaces,
)
from clagentic_loadout.review.errors import ReviewPostError, ReviewVerifyError
from clagentic_loadout.review.forgejo_backend import post_and_verify_comment as _forgejo_post_and_verify_comment
from clagentic_loadout.review.github_backend import post_and_verify_review as _github_post_and_verify_review
from clagentic_loadout.transport.attestation import (
    AttestationError,
    resolve_bound_identity as _resolve_identity,
)
from clagentic_loadout.transport.caller_binding import (
    CallerBindingError,
    bind_caller,
    describe_omitted_caller_behavior as _describe_omitted_caller,
    resolve_for_binding as _resolve_for_binding,
)
from clagentic_loadout.transport.credential_provider import (
    CredentialProviderError,
    DEFAULT_ROLE,
    TokenProvider,
    resolve_token as _resolve_token,
)
from clagentic_loadout.transport.git_host_api import (
    DEFAULT_GIT_HOST_BASE_URL,
    GIT_HOST_BASE_URL_ENV_VAR,
    _resolve_git_host_base,
)
from clagentic_loadout.transport.provider_config import resolve_platform_provider
from clagentic_loadout.transport.readback_envelope import READBACK_ENVELOPE_KEY

# ---------------------------------------------------------------------------
# Exit codes — one reserved range for the merge verb.
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_TOKEN_FETCH_FAILED = 2
EXIT_WRONG_PLATFORM = 4
EXIT_NAMESPACE_DENIED = 20
EXIT_AUTHORITY_DENIED = 21
EXIT_STALE_HEAD_SHA = 23
EXIT_GATE_RESULT_BLOCKED = 24
EXIT_PR_TITLE_INVALID = 25
EXIT_MERGE_FAILED = 26
EXIT_GATE_FACT_UNAVAILABLE = 27
EXIT_POST_MERGE_FAILED = _EXIT_POST_MERGE_FAILED
EXIT_CI_STATUS_FAILED = 29
EXIT_COMMIT_SUBJECT_INVALID = 30
EXIT_MERGE_SHAPE_MISMATCH = 31
#: A fresh post-merge readback (merge.merge_readback.verify_merge_landed) did
#: NOT confirm the merge landed (lr-361de3): the mutating merge_pr call
#: itself returned success, but a SEPARATE GET re-reading the PR afterward
#: did not find merged=True with a non-empty merge_commit_sha. Distinct from
#: EXIT_MERGE_FAILED (the merge_pr call itself failed) so a caller can tell
#: "the merge API call succeeded but could not be independently confirmed"
#: apart from "the merge API call itself refused." FAIL-CLOSED, matching
#: --verify-comment's EXIT_VERIFY_FAILED precedent: a caller MUST NOT report
#: success when this fires.
EXIT_MERGE_READBACK_FAILED = 32
#: An EXPLICIT --role value does not match the ATTESTED invoking identity
#: this process's own attestation-provider chain resolved
#: (transport.caller_binding.bind_caller, lr-c75c9a -- the same fail-closed
#: binding transport.git_host_api's EXIT_CALLER_INVOKER_MISMATCH already
#: enforced; this verb now enforces it too). FAILS CLOSED BEFORE ANY I/O --
#: no token mint, no authority check, no merge is ever attempted. An
#: OMITTED --role is ALSO bound to the attested identity now (lr-620837
#: operator ruling): this code also fires when NO attested identity can be
#: resolved at all for an omitted --role (transport.attestation.
#: AttestationError / BoundAttestationError propagating through
#: resolve_for_binding), not only on an explicit mismatch -- see
#: transport.caller_binding's own module docstring for the full behavior
#: change.
EXIT_CALLER_INVOKER_MISMATCH = 33
#: A branch commit subject introduced by the PR matched the deployment's own
#: configured task_id_guard_pattern in mode="block"
#: (task_id_guard.TaskIdGuardViolation, lr-4005f5) -- ONLY reachable on a
#: RESOLVED --merge-method='merge' repo (mirrors EXIT_COMMIT_SUBJECT_INVALID's
#: own merge_method scoping exactly -- see merge.commit_subjects' own
#: docstring) AND only once a repo has configured
#: `push: task_id_guard_pattern:` (no configured pattern is a strict no-op;
#: default mode once a pattern IS set is "block"). See docs/verbs.md's
#: `loadout-merge` section for the full contract.
EXIT_TASK_ID_GUARD_VIOLATION = 36
#: A repo-declared `merge: pre_checks:` step (merge.pre_checks_config,
#: lr-843900) failed -- either an `on_failure: fail` step exited non-zero/
#: timed out, or the repo's own config could not be loaded/validated at
#: all. FAIL-CLOSED in BOTH cases: a pre_check a caller cannot be shown to
#: have actually run is never treated as a pass (see merge.verb's own
#: module docstring, "PRE-MERGE CHECKS", for the full contract this closes
#: -- a declared `on_failure: fail` pre_check was previously accepted by
#: config and silently never executed at all).
EXIT_PRE_CHECKS_FAILED = 37

#: Reviewers required to post a clean verdict before a merge is authorized.
#: A caller wanting a different reviewer roster passes --required-reviewer
#: (repeatable), overriding this default entirely — this is not a partial
#: override, matching push.namespace_guard's own "explicit always wins"
#: precedence rule.
DEFAULT_REQUIRED_REVIEWERS: tuple[str, ...] = ()


class MergeVerbError(Exception):
    """Raised for any merge-gate failure that should terminate the process
    with a specific exit code. Carries the intended exit code as `.code`."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _fail(message: str, code: int) -> None:
    raise MergeVerbError(message, code)


# ---------------------------------------------------------------------------
# Platform-parameterized backend dispatch (lr-9c69)
#
# _ForgejoMergeBackend / _GithubMergeBackend adapt the two platform backend
# modules' differing gate-fact/merge_pr signatures (forgejo_backend takes an
# explicit api_base on every call; github_backend's endpoints are pinned to
# GITHUB_API_BASE and take none) onto ONE uniform shape, so _run's gate chain
# below drives either platform through the exact same call sites. Neither
# adapter re-implements any HTTP or gate logic -- both are thin pass-throughs
# to their respective backend module's own functions.
# ---------------------------------------------------------------------------


class _ForgejoMergeBackend:
    """Uniform merge-backend adapter over merge.forgejo_backend."""

    def __init__(self, *, git_host_base: str, token: str, opener) -> None:
        self._git_host_base = git_host_base
        self._token = token
        self._opener = opener

    def post_merge_attestation(self, owner: str, repo: str, pr_number: int, *, body: str) -> "str | int":
        """Post the merge-completion attestation via the SAME POST-and-verify
        comment transport review.forgejo_backend already carries (reused, not
        reimplemented -- see that module's post_and_verify_comment). Returns
        the verified comment id. Raises ReviewPostError/ReviewVerifyError on
        any failure -- the caller (merge.verb._run) wraps this call fail-open,
        since the merge itself has already succeeded by the time this fires.
        """
        verified = _forgejo_post_and_verify_comment(
            self._git_host_base, self._token, owner, repo, pr_number, body, opener=self._opener,
        )
        return verified.id

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> dict:
        return forgejo_backend.get_pr_info(
            self._git_host_base, owner, repo, pr_number, token=self._token, opener=self._opener
        )

    def fetch_comments(self, owner: str, repo: str, pr_number: int) -> list:
        return forgejo_backend.fetch_comments(
            self._git_host_base, owner, repo, pr_number, token=self._token, opener=self._opener
        )

    def fetch_changed_files(self, owner: str, repo: str, pr_number: int) -> list:
        return forgejo_backend.fetch_changed_files(
            self._git_host_base, owner, repo, pr_number, token=self._token, opener=self._opener
        )

    def fetch_ci_status(self, owner: str, repo: str, head_sha: str) -> ci_status.CiStatusResult:
        return forgejo_backend.fetch_ci_status(
            self._git_host_base, owner, repo, head_sha, token=self._token, opener=self._opener
        )

    def fetch_branch_commit_subjects(
        self, owner: str, repo: str, base_branch: str, head_sha: str
    ) -> list[tuple[str, str]]:
        return forgejo_backend.fetch_branch_commit_subjects(
            self._git_host_base, owner, repo, base_branch, head_sha,
            token=self._token, opener=self._opener,
        )

    def merge_pr(
        self, owner: str, repo: str, pr_number: int, *,
        merge_message: str, merge_title: str, merge_method: str,
    ) -> str | None:
        return forgejo_backend.merge_pr(
            self._git_host_base, owner, repo, pr_number,
            token=self._token, merge_message=merge_message, merge_title=merge_title,
            merge_method=merge_method, opener=self._opener,
        )


class _GithubMergeBackend:
    """Uniform merge-backend adapter over merge.github_backend."""

    def __init__(self, *, token: str, opener, caller: str | None = None) -> None:
        self._token = token
        self._opener = opener
        self._caller = caller

    def post_merge_attestation(self, owner: str, repo: str, pr_number: int, *, body: str) -> "str | int":
        """Post the merge-completion attestation via the SAME POST-and-verify
        review transport review.github_backend already carries (reused, not
        reimplemented -- see that module's post_and_verify_review). `caller`
        (the merge role resolved by _resolve_backend) is forwarded for the
        SAME app-slug identity-resolution seam review.github_backend.
        resolve_own_login already consults for every other GitHub post in
        this package. Raises ReviewPostError/ReviewVerifyError on any
        failure -- the caller (merge.verb._run) wraps this call fail-open,
        since the merge itself has already succeeded by the time this fires.
        """
        verified = _github_post_and_verify_review(
            owner, repo, pr_number, body, self._token, caller=self._caller, opener=self._opener,
        )
        return verified.id

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> dict:
        return github_backend.get_pr_info(owner, repo, pr_number, token=self._token, opener=self._opener)

    def fetch_comments(self, owner: str, repo: str, pr_number: int) -> list:
        return github_backend.fetch_comments(owner, repo, pr_number, token=self._token, opener=self._opener)

    def fetch_changed_files(self, owner: str, repo: str, pr_number: int) -> list:
        return github_backend.fetch_changed_files(owner, repo, pr_number, token=self._token, opener=self._opener)

    def fetch_ci_status(self, owner: str, repo: str, head_sha: str) -> ci_status.CiStatusResult:
        return github_backend.fetch_ci_status(owner, repo, head_sha, token=self._token, opener=self._opener)

    def fetch_branch_commit_subjects(
        self, owner: str, repo: str, base_branch: str, head_sha: str
    ) -> list[tuple[str, str]]:
        return github_backend.fetch_branch_commit_subjects(
            owner, repo, base_branch, head_sha, token=self._token, opener=self._opener,
        )

    def merge_pr(
        self, owner: str, repo: str, pr_number: int, *,
        merge_message: str, merge_title: str, merge_method: str,
    ) -> str | None:
        return github_backend.merge_pr(
            owner, repo, pr_number, token=self._token, merge_message=merge_message,
            merge_title=merge_title, merge_method=merge_method, opener=self._opener,
        )


def _resolve_backend(
    platform: str,
    *,
    owner: str,
    repo: str,
    role: str,
    git_host_base: str,
    token_provider: TokenProvider | None,
    opener,
):
    """Resolve platform guard -> mint/resolve token -> construct the matching
    merge backend adapter. Mirrors review.verb.build_backend exactly (lr-9c69):
    the platform guard ALWAYS runs before token resolution, for both
    platforms -- there is no call path here that reaches _resolve_token
    before the platform has been confirmed to match the selected backend.

    Raises MergeVerbError(code=EXIT_USAGE) for an unrecognized --platform
    value, PlatformMismatchError for a recognized-but-wrong platform (the
    caller translates that to EXIT_WRONG_PLATFORM), and MergeVerbError(code=
    EXIT_TOKEN_FETCH_FAILED) on credential resolution failure.
    """
    if platform == PLATFORM_GITHUB:
        github_backend.assert_platform_is_github(owner, repo, explicit_platform=platform)
    elif platform == PLATFORM_FORGEJO:
        forgejo_backend.assert_platform_is_forgejo(owner, repo, explicit_platform=platform)
    else:
        _fail(
            f"--platform {platform!r} not recognized. Expected "
            f"{PLATFORM_GITHUB!r} or {PLATFORM_FORGEJO!r}.",
            code=EXIT_USAGE,
        )

    print(f"merge: resolving token for role={role!r}", file=sys.stderr)
    active_provider = (
        token_provider if token_provider is not None else resolve_platform_provider(platform)
    )
    try:
        token = _resolve_token(role, active_provider, repo=f"{owner}/{repo}")
    except CredentialProviderError as exc:
        _fail(f"token resolution FAILED -- {exc}", code=EXIT_TOKEN_FETCH_FAILED)

    if platform == PLATFORM_GITHUB:
        return _GithubMergeBackend(token=token, opener=opener, caller=role)
    return _ForgejoMergeBackend(git_host_base=git_host_base, token=token, opener=opener)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merge",
        description=(
            "merge -- the full gate chain (namespace, authority, stale-SHA, "
            "reviewer verdicts, diff-scope, PR title), then merge via either "
            "the Forgejo or GitHub API. Refuses and exits non-zero on ANY "
            "gate failure; never merges on a partial pass."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  merge --role merger --platform forgejo \\\n"
            "      --repo some-owner/some-repo --pr 42 \\\n"
            "      --expected-head-sha <sha40> --required-reviewer some-reviewer\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"merge {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    parser.add_argument(
        "--platform",
        required=True,
        choices=(PLATFORM_GITHUB, PLATFORM_FORGEJO),
        help="Target platform for the PR (mandatory -- resolved "
        "independently, e.g. from a dispatch envelope's pr_url). Affects "
        "only which backend fetches gate facts and executes the merge; "
        "the gate chain itself runs identically on both platforms.",
    )
    parser.add_argument(
        "--role",
        default=None,
        help=f"Role whose merge authority is checked and whose token is "
        f"resolved via the credential provider. Which role may authorize a "
        f"merge is config (see --authorized-role / an AuthorityProvider), "
        f"never a hardcoded identity. Already-attested, opaque config key "
        f"downstream (the credential provider, the authority check never "
        f"re-authenticate it themselves -- see merge.authority's module "
        f"docstring). It must match this process's own already attested "
        f"invoking identity (transport.attestation.resolve_bound_identity) "
        f"or the call is refused fail-closed before any I/O (transport."
        f"caller_binding.bind_caller). "
        f"{_describe_omitted_caller(flag_name='--role')}",
    )
    parser.add_argument(
        "--authorized-role",
        action="append",
        dest="authorized_roles",
        default=None,
        help="A role permitted to hold merge authority (repeatable). Used "
        "to build the standalone StaticRoleAuthorityProvider when no "
        "external AuthorityProvider is injected. Required for the merge-"
        "authority gate to ever pass in the standalone configuration.",
    )
    parser.add_argument("--repo", required=True, help="owner/repo to merge in.")
    parser.add_argument("--pr", type=int, required=True, dest="pr_number", help="PR number to merge.")
    parser.add_argument(
        "--git-host-base-url",
        default=None,
        help=f"Forgejo API base URL (default: ${GIT_HOST_BASE_URL_ENV_VAR} env "
        f"var, falling back to a configurable compat-alias env var if that "
        f"is unset, or {DEFAULT_GIT_HOST_BASE_URL!r} if neither is set -- see "
        f"transport.git_host_api._resolve_git_host_base). Ignored for the "
        f"GitHub platform.",
    )
    parser.add_argument(
        "--expected-head-sha",
        default="",
        dest="expected_head_sha",
        help="Expected PR head SHA at gate time. When supplied, the merge "
        "is refused if the current head differs. When absent, the "
        "stale-SHA check is a no-op — never invent a SHA.",
    )
    parser.add_argument(
        "--required-reviewer",
        action="append",
        dest="required_reviewers",
        default=None,
        help="A reviewer required to have posted a clean verdict comment "
        "(repeatable). Either a BARE reviewer name (e.g. 'some-reviewer'), "
        "whose expected platform login is DERIVED platform-aware -- "
        "the bare name on --platform forgejo, or "
        "'<resolve_github_app_slug(caller=name)>[bot]' on --platform github, "
        "reusing the SAME github_app.slugs config review.github_backend."
        "resolve_own_login already consults -- or an explicit "
        "'reviewer_name:git_host_login' pair to pin a login directly (override/"
        "back-compat path). A required reviewer with no clean, current-SHA "
        "verdict refuses the merge. Omit entirely to run with no "
        "reviewer-verdict gate (e.g. a deployment gating solely on CI + "
        "authority). Roles declared in the repo's merge.required_reviewer_roles "
        "(read via --repo-path) are required as well: the roles enforced are "
        "the union of the two.",
    )
    parser.add_argument(
        "--ignore-repo-gate",
        action="store_true",
        default=False,
        dest="ignore_repo_gate",
        help="Lift exactly two repo-declared gates: the reviewer roles in "
        "merge.required_reviewer_roles and the scanners in "
        "merge.required_scanners; only --required-reviewer applies. Every "
        "other gate, including model attestation and the single-fence "
        "requirement, stays enforced. For a repo whose declared gate cannot be satisfied (for "
        "example while landing the config that fixes it). Logged to stderr "
        "and recorded in the merge-completion attestation. Reviewer roles "
        "and scanners that cannot be loaded at all already fall back to "
        "flags-only with a warning and do not need this flag. It does not "
        "lift merge.pre_checks (use --skip-pre-checks).",
    )
    parser.add_argument(
        "--max-changed-files",
        type=int,
        default=diff_scope.DEFAULT_MAX_CHANGED_FILES,
        dest="max_changed_files",
        help=f"Maximum changed-file count allowed in the PR diff (default: "
        f"{diff_scope.DEFAULT_MAX_CHANGED_FILES}).",
    )
    parser.add_argument(
        "--skip-title-check",
        action="store_true",
        default=False,
        dest="skip_title_check",
        help="Bypass the Conventional Commits PR title gate. Default: "
        "enforced. Use of this flag is logged to stderr for audit.",
    )
    parser.add_argument(
        "--merge-method",
        default=commit_subjects.REAL_MERGE_METHOD,
        dest="merge_method",
        help="The RESOLVED merge method for this repo/PR (e.g. from repo/"
        f"gate config's allow_squash vs merge_style='merge') -- default: "
        f"{commit_subjects.REAL_MERGE_METHOD!r} (a real, non-squash merge). "
        f"ACTUALLY EXECUTES the requested method: forwarded "
        f"verbatim to the resolved backend's merge_pr as GitHub's "
        f"merge_method / Forgejo's Do field. Gates the branch commit-subject "
        f"check (see --skip-commit-check): the check only fires "
        f"when this resolves to {commit_subjects.REAL_MERGE_METHOD!r} -- on "
        f"any other value (squash, rebase) the check is a no-op, since a "
        f"squash/rebase merge rewrites the resulting commit subject FROM the "
        f"PR title, which the PR-title gate already validated. A "
        f"requested-vs-actual shape mismatch is detected and reported when "
        f"--repo-path is given (see EXIT_MERGE_SHAPE_MISMATCH / merge.merge_shape). "
        f"On the SAME merge_method='merge' condition, an OPTIONAL, "
        f"deployment-configured task-id guard "
        f"(.clagentic/loadout/config.yaml push.task_id_guard_pattern) is also "
        f"checked against each branch commit subject -- a strict no-op with "
        f"no pattern configured; once configured, default mode is BLOCK, "
        f"exit {EXIT_TASK_ID_GUARD_VIOLATION}. See docs/verbs.md's "
        f"`loadout-merge` section for the full contract.",
    )
    parser.add_argument(
        "--skip-commit-check",
        action="store_true",
        default=False,
        dest="skip_commit_check",
        help="Bypass the branch commit-subject Conventional Commits gate. "
        "Default: enforced on --merge-method=merge repos. Use "
        "of this flag is logged to stderr for audit -- intended for an "
        "automation PR whose commits cannot be changed after the fact.",
    )
    parser.add_argument(
        "--merge-message",
        default="",
        dest="merge_message",
        help="Optional merge commit message suffix.",
    )
    parser.add_argument(
        "--task-id",
        default=None,
        dest="task_id",
        help="Opaque work-item ref for this invocation's dispatch envelope "
        "(schemas/common.json's task_id fragment -- an opaque, deployment-"
        "defined string; loadout does not assume a specific tracker or ID "
        "pattern). Rendered as a 'task_id' line on the merge-completion "
        "attestation when supplied; omitted entirely when absent. Never "
        "resolved or validated here -- passed through verbatim.",
    )
    parser.add_argument(
        "--allowed-namespace",
        action="append",
        dest="allowed_namespaces",
        default=None,
        help="Restrict the merge target owner to this namespace (repeatable). "
        f"When omitted, falls back to {ALLOWED_NAMESPACES_ENV_VAR} "
        f"(comma-separated); when neither is set, no namespace restriction "
        f"is enforced.",
    )
    parser.add_argument(
        "--repo-path",
        default=None,
        dest="repo_path",
        help="Local working-tree root the merged repo lives in. When given, "
        "the repo's merge gate (merge.required_reviewer_roles, "
        "merge.required_scanners, merge.pre_checks) is read from "
        f"{TRACKED_GATE_RELATIVE_PATH} as it exists at the PR's BASE commit, "
        "never from the working tree. pre_checks "
        "(declared at base) run in this directory BEFORE the merge is authorized -- "
        "an on_failure: fail pre_check refuses the merge (see "
        "--skip-pre-checks). post_merge_steps are read from "
        f"<repo-path>/{DEFAULT_POST_MERGE_CONFIG_RELATIVE_PATH} "
        "and, if present, run in this directory ONLY after the merge in "
        "step 9 actually succeeds. This is an OPTIONAL override, not a "
        "required input -- the caller (a dispatcher with its own project "
        "registry) is expected to supply it whenever a local tree exists; "
        "loadout itself has no project registry to derive one from (see "
        "merge.verb's module docstring). Omitting it entirely "
        "is ONLY a no-op when paired with --no-post-merge-tree; omitting "
        "both is a usage error (EXIT_USAGE) -- see that flag's help.",
    )
    parser.add_argument(
        "--no-post-merge-tree",
        action="store_true",
        default=False,
        dest="no_post_merge_tree",
        help="Explicitly acknowledge that this invocation has NO local "
        "working tree at all (a bare API-only merge) and that skipping "
        "post_merge_steps as a result is intentional. Required whenever "
        "--repo-path is omitted -- omitting --repo-path with neither this "
        "flag nor --skip-post-merge is a USAGE ERROR (EXIT_USAGE), not a "
        "silent skip (omitting a flag must never downgrade a "
        "repo that declares post_merge_steps to a silent no-run exit 0). "
        "Logged to stderr for audit, exactly like --skip-post-merge.",
    )
    parser.add_argument(
        "--skip-post-merge",
        action="store_true",
        default=False,
        dest="skip_post_merge",
        help="Skip post_merge_steps even when --repo-path is given and the "
        "repo's config declares steps. Logged to stderr for audit. Also "
        "satisfies the --repo-path/--no-post-merge-tree usage requirement "
        "when --repo-path is omitted (an explicit blanket skip covers the "
        "no-tree case too).",
    )
    parser.add_argument(
        "--skip-pre-checks",
        action="store_true",
        default=False,
        dest="skip_pre_checks",
        help="Skip the merge: pre_checks: gate (see merge.pre_checks_config) "
        "even when --repo-path is given and the repo's config declares "
        "checks. Logged to stderr for audit, exactly like --skip-post-merge. "
        "Default: enforced -- a repo-declared on_failure: fail pre_check "
        "refuses the merge (EXIT_PRE_CHECKS_FAILED) unless this flag is "
        "passed.",
    )
    return parser


def _describe_ci_disposition(ci_result: ci_status.CiStatusResult) -> str:
    """Render the CI-status gate's ALREADY-COMPUTED disposition as a short,
    git-host-safe string for both the stderr log line and the merge-completion
    attestation body (merge.attestation.build_attestation_body's
    `ci_disposition` field) -- one rendering, reused, so the two can never
    drift apart from restating the same CiStatusResult two different ways.
    """
    if ci_result.is_empty:
        return "no-runner-by-design (0 commit-status entries at HEAD)"
    return (
        f"combined_state={ci_result.combined_state!r} "
        f"({ci_result.status_count} status(es), {ci_result.run_count} run(s))"
    )


def _parse_required_reviewers(raw: list[str] | None, platform: str) -> dict[str, str]:
    """Parse repeated --required-reviewer values into a {name: login} dict.

    Each entry is either:
      - an explicit 'reviewer_name:git_host_login' pair (override/back-compat
        path) — the login is used verbatim, exactly as before lr-2f1378.
      - a BARE reviewer name (no ':' separator) — its expected login is
        DERIVED platform-aware via merge.reviewer_login.resolve_reviewer_login
        (lr-2f1378): the bare name itself on Forgejo, or the deployment's
        configured GitHub App slug + '[bot]' on GitHub. The derived login
        stays TOOL-AUTHORITATIVE (resolved from config), never trusted from
        a PR comment's claimed identity — see that module's docstring for
        the anti-spoof invariant this preserves (lr-2b3f).

    Raises MergeUsageError on a malformed explicit entry (empty name/login
    around a ':' separator) or when a bare name has no derivable login on
    the target platform (e.g. no github_app.slugs entry configured) — both
    checked BEFORE any network call.
    """
    if not raw:
        return {}
    result: dict[str, str] = {}
    for entry in raw:
        if ":" in entry:
            name, login = entry.split(":", 1)
            name, login = name.strip(), login.strip()
            if not name or not login:
                raise MergeUsageError(
                    f"--required-reviewer {entry!r} must be 'reviewer_name:git_host_login' "
                    f"with both parts non-empty."
                )
            result[name] = login
            continue
        name = entry.strip()
        if not name:
            raise MergeUsageError(
                f"--required-reviewer {entry!r} must be a non-empty reviewer "
                f"name, or 'reviewer_name:git_host_login'."
            )
        try:
            result[name] = resolve_reviewer_login(name, platform)
        except ReviewerLoginNotConfiguredError as exc:
            raise MergeUsageError(str(exc)) from exc
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(
    argv: list[str] | None = None,
    *,
    token_provider: TokenProvider | None = None,
    authority_provider: AuthorityProvider | None = None,
    opener=None,
    identity_provider=None,
) -> int:
    """CLI entrypoint. Returns the process exit code (does not call
    sys.exit itself so it stays testable).

    `token_provider`, `authority_provider`, and `opener` are injection
    points for tests and for a deployment wiring its own reference
    providers; all default to the standalone/real path in production use.

    `identity_provider` (lr-c75c9a): a zero-arg callable returning a
    `transport.attestation.Identity` (defaults to
    `transport.attestation.resolve_bound_identity`, the caller-BOUND
    resolver -- see that function's own docstring for the discriminator and
    the `attestation.bound_identity` policy governing whether an
    undiscriminated miss falls through to the built-in OS-user layer) --
    the injection point for the fail-closed --role/attested-invoker binding
    (transport.caller_binding.bind_caller), mirroring the identical
    parameter transport.git_host_api.main already carries for the same
    purpose. Called on the omitted-role path CONTINGENT on the effective
    `attestation.bound_identity` policy (lr-620837 fold-in #4): under
    `required`, `transport.caller_binding.resolve_for_binding` calls this
    resolver even on an omitted --role, deriving the effective role from
    the resolved identity's own subject, so a process with no attested
    identity at all can refuse here too; under `builtin-fallback` (the
    default), an omitted --role never reaches this resolver at all and
    derives `DEFAULT_ROLE` directly -- see that function's own docstring
    for the full policy-gated rule.
    """
    if argv is None:
        argv = sys.argv[1:]

    if any(arg in ("--help", "-h") for arg in argv):
        _build_arg_parser().print_help()
        return EXIT_OK

    parser = _build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    try:
        return _run(
            args,
            token_provider=token_provider,
            authority_provider=authority_provider,
            opener=opener,
            identity_provider=identity_provider,
        )
    except MergeVerbError as exc:
        print(f"merge: {exc}", file=sys.stderr)
        return exc.code
    except MergeUsageError as exc:
        print(f"merge: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except CallerBindingError as exc:
        print(f"merge: {exc}", file=sys.stderr)
        return EXIT_CALLER_INVOKER_MISMATCH


def _run(
    args: argparse.Namespace,
    *,
    token_provider: TokenProvider | None,
    authority_provider: AuthorityProvider | None,
    opener,
    identity_provider=None,
) -> int:
    try:
        owner, repo = parse_owner_repo(args.repo)
    except RemoteResolutionError as exc:
        raise MergeUsageError(str(exc)) from exc

    # lr-ac5c8a: an absent --repo-path must never silently downgrade to "no
    # post-merge run attempted" -- that is the recurring silent-skip defect
    # class (lr-5854ff, lr-4e6f31, clagentic-console PR #365/#366). A caller
    # with genuinely no local tree must say so explicitly (--no-post-merge-tree)
    # or explicitly skip altogether (--skip-post-merge, which already covers
    # the "skip regardless of tree" case). Omitting --repo-path with NEITHER
    # flag set is a usage error, checked BEFORE any credential/network call --
    # same fail-fast placement as the owner/repo parse above.
    if not args.repo_path and not args.no_post_merge_tree and not args.skip_post_merge:
        raise MergeUsageError(
            "--repo-path was omitted but neither --no-post-merge-tree nor "
            "--skip-post-merge was given. A repo may declare post_merge_steps "
            "that would silently never run in this shape -- pass --repo-path "
            "(when a local tree exists), or --no-post-merge-tree (to "
            "explicitly acknowledge a bare API-only merge with no tree to "
            "check), or --skip-post-merge (to skip regardless)."
        )

    # lr-4522a3: when --repo-path points at a real, parseable git tree,
    # refuse a --repo slug that does not match that tree's OWN origin
    # remote -- before any credential mint, so a caller-side argument
    # defect never becomes an opaque platform-API 422 that misleadingly
    # blames the App installation. Compares against the remote, never the
    # directory name (the '.github' org-profile shape is correct and never
    # flagged) -- see merge.repo_path_consistency's module docstring.
    assert_repo_path_consistent(args.repo, args.repo_path)

    # --role/attested-invoker fail-closed binding (lr-c75c9a, mirrors
    # transport.git_host_api's identical check; OMITTED-CALLER FIX,
    # lr-620837 fold-in #4 -- see caller_binding.resolve_for_binding's own
    # docstring, "OMITTED --caller/--role IS POLICY-GATED"): checked
    # BEFORE any I/O -- before the namespace guard (step 1), before the
    # merge-authority check (step 2), before any token mint. Whether an
    # omitted --role requires attestation is now the effective
    # attestation.bound_identity policy: under the default
    # "builtin-fallback", an omitted --role derives DEFAULT_ROLE with no
    # resolution attempted and no refusal possible (this package's
    # originally-released behavior); under "required", an omitted --role
    # derives the resolved attested identity's own subject, and a process
    # with no attested identity at all is refused here exactly like an
    # explicit mismatched --role always was.
    resolve_identity_fn = identity_provider if identity_provider is not None else _resolve_identity
    try:
        attested_identity = _resolve_for_binding(
            caller_explicit=args.role is not None,
            caller=args.role or DEFAULT_ROLE,
            resolve_identity_fn=resolve_identity_fn,
        )
    except AttestationError as exc:
        _fail(f"attested-identity resolution FAILED -- {exc}", code=EXIT_CALLER_INVOKER_MISMATCH)
    role = args.role if args.role is not None else attested_identity.subject
    bind_caller(role, caller_explicit=True, identity=attested_identity)

    required_reviewers = _parse_required_reviewers(args.required_reviewers, args.platform)
    git_host_base = _resolve_git_host_base(args.git_host_base_url)

    # 1. Namespace guard — runs FIRST, before any credential or network call.
    allowed_namespaces = resolve_allowed_namespaces(
        frozenset(args.allowed_namespaces) if args.allowed_namespaces else None
    )
    try:
        check_namespace_allowed(owner, repo, allowed_namespaces=allowed_namespaces)
    except NamespaceDeniedError as exc:
        _fail(str(exc), code=EXIT_NAMESPACE_DENIED)

    # 2. Merge-authority check — FAIL-CLOSED provider seam. Runs before any
    # credential is minted for an out-of-scope/unauthorized request.
    provider = authority_provider or StaticRoleAuthorityProvider(
        frozenset(args.authorized_roles) if args.authorized_roles else frozenset()
    )
    try:
        check_authority(role, owner, repo, args.pr_number, provider)
    except AuthorityDeniedError as exc:
        _fail(str(exc), code=EXIT_AUTHORITY_DENIED)

    # 3. Platform guard (BOTH directions, fail-closed, BEFORE any credential
    # mint or API call) -> credential resolution -> resolved backend.
    try:
        backend = _resolve_backend(
            args.platform,
            owner=owner,
            repo=repo,
            role=role,
            git_host_base=git_host_base,
            token_provider=token_provider,
            opener=opener,
        )
    except PlatformMismatchError as exc:
        _fail(str(exc), code=EXIT_WRONG_PLATFORM)

    # Read the PR's LIVE current state once, reused by every remaining gate
    # (stale-SHA, verdict SHA-stamp comparison, title gate).
    try:
        pr_info = backend.get_pr_info(owner, repo, args.pr_number)
    except GateFactUnavailableError as exc:
        _fail(str(exc), code=EXIT_GATE_FACT_UNAVAILABLE)
    current_head_sha = forgejo_backend.get_pr_head_sha(pr_info)
    pr_title = forgejo_backend.get_pr_title(pr_info)

    # 4. Stale-SHA refusal.
    try:
        check_stale_head_sha(
            args.expected_head_sha, current_head_sha, args.pr_number, owner, repo
        )
    except StaleHeadShaError as exc:
        _fail(str(exc), code=EXIT_STALE_HEAD_SHA)

    # The repo's gate is read from the PR's BASE commit (merge.repo_gate_runtime),
    # which is why it can only be loaded now that the PR payload is in hand. Its
    # declared reviewer roles are a floor beneath --required-reviewer; a role
    # already named by a flag keeps the flag's login binding.
    repo_gate = load_repo_gate_at_base(
        args.repo_path,
        base_sha=resolve_base_sha(pr_info),
        base_branch=resolve_base_branch(pr_info),
    )
    repo_gate = with_resolvable_reviewer_roles(
        repo_gate, args.platform, flagged_roles=required_reviewers
    )
    for warning in repo_gate.warnings:
        print(f"merge: WARNING -- {warning}", file=sys.stderr)
    floor_only_reviewers: dict[str, str] = {}
    if args.ignore_repo_gate:
        print(
            f"merge: merge.required_reviewer_roles and merge.required_scanners IGNORED "
            f"via --ignore-repo-gate (declared reviewer "
            f"roles: {list(repo_gate.reviewer_roles)!r}, declared required scanners: "
            f"{ {r: list(s) for r, s in (repo_gate.required_scanners or {}).items()}!r}); "
            f"only --required-reviewer applies",
            file=sys.stderr,
        )
    else:
        undeclared = [r for r in repo_gate.reviewer_roles if r not in required_reviewers]
        # Every declared role resolves here: an unresolvable one already took
        # the pair fallback in with_resolvable_reviewer_roles.
        floor_only_reviewers = _parse_required_reviewers(undeclared, args.platform)
        required_reviewers = {**required_reviewers, **floor_only_reviewers}
        unreachable = repo_gate.unreachable_scanner_roles(required_reviewers)
        if unreachable:
            raise MergeUsageError(
                f"merge.required_scanners declares scanners for role(s) {unreachable!r}, "
                f"which are not required reviewers (required: {sorted(required_reviewers)!r}). "
                f"Scanners are checked on a required reviewer's verdict, so this "
                f"declaration could never gate anything. Add the role to "
                f"merge.required_reviewer_roles or --required-reviewer, or remove it from "
                f"merge.required_scanners; --ignore-repo-gate overrides it (logged and "
                f"attested)."
            )

    # 5. Reviewer-verdict fences — for each required reviewer.
    if required_reviewers:
        if not current_head_sha:
            _fail(
                f"reviewer verdict checks FAILED -- current PR head SHA is "
                f"unknown for PR #{args.pr_number} in {owner}/{repo}; cannot "
                f"verify reviewer SHA-stamps without it.",
                code=EXIT_GATE_RESULT_BLOCKED,
            )
        try:
            comments = backend.fetch_comments(owner, repo, args.pr_number)
        except GateFactUnavailableError as exc:
            _fail(str(exc), code=EXIT_GATE_FACT_UNAVAILABLE)

        # lr-5260f9: multi-fence refusal for the reviewer-verdict comment
        # body, ENFORCED BY DEFAULT -- see merge.post_merge_config.
        # resolve_enforce_single_verdict_fence's own docstring for the
        # ENFORCE-BY-DEFAULT / CONFIG-GATED OPT-OUT trade-off (the inverse
        # shape of resolve_enforce_merge_shape's warn-by-default precedent;
        # lives in the SAME module as that resolver). merge.gate_config is
        # reached only through merge.repo_gate_runtime, which owns the
        # unloadable-config fallback; see gate_config's "BLAST RADIUS"
        # docstring section. Resolved once per invocation, from the same
        # --repo-path config root every other repo-tier gate key here reads.
        try:
            enforce_single_verdict_fence = resolve_enforce_single_verdict_fence(
                args.repo_path
            )
            # lr-95543d: OPT-IN, defaults False -- see
            # merge.post_merge_config.resolve_require_model_attestation's
            # own docstring for the rollout-hazard rationale, and
            # merge.model_attestation's module docstring for what this
            # check does and does not prove. Resolved once per invocation,
            # same config root as every other repo-tier gate key here.
            require_model_attestation = resolve_require_model_attestation(args.repo_path)
            model_attestation_denylist = resolve_model_attestation_denylist(args.repo_path)
        except PostMergeConfigError as exc:
            _fail(
                f"post-merge config FAILED to load -- {exc}",
                code=EXIT_POST_MERGE_FAILED,
            )

        for reviewer_name, bot_login in required_reviewers.items():
            try:
                verdict_obj = verdict.read_reviewer_verdict(
                    comments,
                    expected_login=bot_login,
                    current_head_sha=current_head_sha,
                    pr_number=args.pr_number,
                    owner=owner,
                    repo=repo,
                    expected_reviewer_name=reviewer_name,
                    enforce_single_fence=enforce_single_verdict_fence,
                )
                for ignored_id, ignored_reason in verdict_obj.ignored_earlier_fences:
                    print(
                        f"merge: WARNING -- earlier comment #{ignored_id} from {reviewer_name!r} "
                        f"is ignored: {ignored_reason}; it holds no findings open for this "
                        f"reviewer on this PR and clears nothing",
                        file=sys.stderr,
                    )
                verdict.assert_clean_verdict(verdict_obj, reviewer_name)
                verdict.assert_prior_findings_resolved(verdict_obj, reviewer_name)
                required_scanners = (
                    () if args.ignore_repo_gate else repo_gate.scanners_for(reviewer_name)
                )
                for scanner_warning in verdict.check_required_scanners(
                    verdict_obj, reviewer_name, required_scanners
                ):
                    print(f"merge: WARNING -- {scanner_warning}", file=sys.stderr)
                # lr-95543d: mirrors assert_clean_verdict's disposition --
                # a clean verdict lacking genuine attestation refuses the
                # merge exactly like a blocking one. No-op when
                # require_model_attestation is False (the default) or the
                # verdict itself is 'blocking' (already refused above).
                if require_model_attestation:
                    assert_model_attested(
                        verdict_obj, reviewer_name, denylist=model_attestation_denylist
                    )
            except (
                VerdictMissingError,
                VerdictMalformedError,
                VerdictRoleMismatchError,
                VerdictStaleError,
                VerdictBlockingError,
                VerdictPriorFindingsOpenError,
                VerdictScannerFailedError,
                ModelAttestationMissingError,
                ModelAttestationInvalidError,
            ) as exc:
                from_repo_gate = reviewer_name in floor_only_reviewers or (
                    isinstance(exc, VerdictScannerFailedError)
                )
                hint = (
                    " This requirement comes from the repo's merge gate config "
                    "(required_reviewer_roles / required_scanners); --ignore-repo-gate "
                    "overrides it (logged and attested)."
                    if from_repo_gate
                    else ""
                )
                _fail(f"{exc}{hint}", code=EXIT_GATE_RESULT_BLOCKED)
            print(
                f"merge: {reviewer_name!r} verdict PASSED -- "
                f"review_status={verdict_obj.review_status!r}, "
                f"head_sha={verdict_obj.head_sha!r}, "
                f"comment_id={verdict_obj.comment_id}",
                file=sys.stderr,
            )

    # 6. Diff-scope cap.
    try:
        changed_files = backend.fetch_changed_files(owner, repo, args.pr_number)
    except GateFactUnavailableError as exc:
        _fail(str(exc), code=EXIT_GATE_FACT_UNAVAILABLE)
    print(
        f"merge: diff scope -- PR #{args.pr_number} touches "
        f"{len(changed_files)} file(s) (limit={args.max_changed_files})",
        file=sys.stderr,
    )
    try:
        diff_scope.check_diff_scope(
            changed_files, args.pr_number, owner, repo,
            max_changed_files=args.max_changed_files,
        )
    except DiffScopeExceededError as exc:
        _fail(str(exc), code=EXIT_GATE_RESULT_BLOCKED)

    # 7. PR-title gate.
    if args.skip_title_check:
        print(
            f"merge: PR title gate BYPASSED via --skip-title-check for "
            f"PR #{args.pr_number} in {owner}/{repo} (title={pr_title!r})",
            file=sys.stderr,
        )
    try:
        title_gate.check_pr_title(
            pr_title, args.pr_number, owner, repo, skip=args.skip_title_check
        )
    except TitleInvalidError as exc:
        _fail(str(exc), code=EXIT_PR_TITLE_INVALID)

    # 7b. Branch commit-subject gate (lr-835c57) -- fires ONLY on a resolved
    # merge_method='merge' (real, non-squash) repo: semantic-release reads
    # EACH branch commit's own subject on a real merge, not the PR title
    # (already gated by step 7 above), so a non-conformant 'lr-XXXX: <desc>'
    # commit subject would otherwise silently stop beta cuts even with a
    # clean title. A no-op on any other --merge-method value (squash/rebase
    # rewrite the resulting commit subject FROM the already-gated PR title).
    # See merge.commit_subjects' module docstring for the full rationale and
    # the BLOCK-never-rewrite contract.
    if args.skip_commit_check:
        print(
            f"merge: branch commit-subject gate BYPASSED via "
            f"--skip-commit-check for PR #{args.pr_number} in {owner}/{repo}",
            file=sys.stderr,
        )
    if args.merge_method == commit_subjects.REAL_MERGE_METHOD and not args.skip_commit_check:
        base_branch_for_commits = resolve_base_branch(pr_info)
        try:
            branch_commit_subjects = backend.fetch_branch_commit_subjects(
                owner, repo, base_branch_for_commits, current_head_sha
            )
        except GateFactUnavailableError as exc:
            _fail(str(exc), code=EXIT_GATE_FACT_UNAVAILABLE)
        print(
            f"merge: branch commit-subject gate -- PR #{args.pr_number} in "
            f"{owner}/{repo} introduces {len(branch_commit_subjects)} "
            f"commit(s) (merge_method={args.merge_method!r})",
            file=sys.stderr,
        )
    else:
        branch_commit_subjects = []
    # TASK-ID GUARD (lr-4005f5, task_id_guard) -- an independent,
    # deployment-config-gated check layered on the SAME branch commit
    # subjects step 7b already fetched: no configured
    # `push: task_id_guard_pattern:` is a strict no-op (see
    # task_id_guard's own module docstring, "NO-OP BY DEFAULT"); once
    # configured, the default mode is "block" (operator-pinned, see that
    # module's own docstring "DEFAULT MODE IS BLOCK"). --repo-path is the
    # SAME config root every other repo-tier gate key here resolves
    # through; None when absent (--no-post-merge-tree/--skip-post-merge
    # paths), which resolves to the disabled default (no file lookup).
    task_id_guard_config = load_task_id_guard_config(args.repo_path)
    try:
        guard_warnings = commit_subjects.check_branch_commit_subjects(
            branch_commit_subjects, args.pr_number, owner, repo,
            merge_method=args.merge_method, skip=args.skip_commit_check,
            task_id_guard_pattern=task_id_guard_config.pattern,
            task_id_guard_mode=task_id_guard_config.mode,
        )
    except CommitSubjectInvalidError as exc:
        _fail(str(exc), code=EXIT_COMMIT_SUBJECT_INVALID)
    except TaskIdGuardViolation as exc:
        _fail(str(exc), code=EXIT_TASK_ID_GUARD_VIOLATION)
    for warning in guard_warnings:
        print(f"merge: WARNING -- {warning}", file=sys.stderr)

    # 8. CI-status gate. An empty result (zero HEAD-scoped commit-status
    # entries) is an EXPLICIT PASS -- no-runner-by-design is a legitimate
    # repo shape (lr-368c), not a missing gate. Emptiness is HEAD-scoped
    # commit-status absence ONLY -- a repo-global signal (e.g. Forgejo's
    # mirror-sync/historical Actions tasks) is explicitly NOT CI evidence
    # and never overrides this (lr-2d2293). See merge.ci_status's module
    # docstring for the full decision.
    try:
        ci_result = backend.fetch_ci_status(owner, repo, current_head_sha)
    except GateFactUnavailableError as exc:
        _fail(str(exc), code=EXIT_GATE_FACT_UNAVAILABLE)
    ci_disposition = _describe_ci_disposition(ci_result)
    if ci_result.is_empty:
        print(
            f"merge: CI-status gate -- no HEAD-scoped CI evidence for PR "
            f"#{args.pr_number} in {owner}/{repo} (0 commit-status entries "
            f"at HEAD); treating as PASS (no-runner-by-design).",
            file=sys.stderr,
        )
    else:
        print(
            f"merge: CI-status gate -- PR #{args.pr_number} in {owner}/{repo} "
            f"{ci_disposition}",
            file=sys.stderr,
        )
    try:
        ci_status.check_ci_status(ci_result, args.pr_number, owner, repo)
    except CiStatusFailedError as exc:
        _fail(str(exc), code=EXIT_CI_STATUS_FAILED)

    # 8b. Pre-merge checks gate (merge.pre_checks_config, lr-843900) -- see
    # this module's docstring, "PRE-MERGE CHECKS", for the full contract.
    # The checks are DECLARED by the tracked gate file at the PR's base commit
    # (repo_gate, loaded above) and EXECUTED in --repo-path, where they always
    # ran; only their configuration comes from base. No local tree
    # (--no-post-merge-tree/--skip-post-merge) declares no checks, the same
    # pre-existing boundary every other repo-tier key has.
    if args.skip_pre_checks:
        print(
            f"merge: pre_checks gate BYPASSED via --skip-pre-checks for "
            f"PR #{args.pr_number} in {owner}/{repo}",
            file=sys.stderr,
        )
    elif repo_gate.pre_checks_error:
        # Never a fallback: a declaration the verb cannot be shown to have read
        # is not treated as "no checks". --ignore-repo-gate does not lift this.
        _fail(
            f"pre_checks config FAILED to load -- {repo_gate.pre_checks_error}",
            code=EXIT_PRE_CHECKS_FAILED,
        )
    elif repo_gate.pre_checks:
        pre_checks = list(repo_gate.pre_checks)
        print(
            f"merge: pre_checks gate -- running {len(pre_checks)} declared "
            f"check(s) in {args.repo_path!r} before authorizing PR "
            f"#{args.pr_number} in {owner}/{repo} (declared at base "
            f"{resolve_base_sha(pr_info)!r})",
            file=sys.stderr,
        )
        try:
            run_post_merge_steps(pre_checks, args.repo_path)
        except (
            PostMergeStepFailedError,
            PostMergeStepTimeoutError,
            PostMergeLivenessError,
        ) as exc:
            _fail(
                f"pre_checks gate FAILED -- {exc} -- refusing to merge "
                f"PR #{args.pr_number} in {owner}/{repo}.",
                code=EXIT_PRE_CHECKS_FAILED,
            )
        print(
            f"merge: pre_checks gate -- all {len(pre_checks)} check(s) "
            f"PASSED for PR #{args.pr_number} in {owner}/{repo}",
            file=sys.stderr,
        )

    # 9. All gates passed -- execute the merge.
    print(
        f"merge: all gates PASSED -- merging PR #{args.pr_number} in "
        f"{owner}/{repo}",
        file=sys.stderr,
    )
    # lr-1953a8: merge_title=pr_title composes the merge commit's SUBJECT
    # from the PR's own (already step-7-gated Conventional Commits) title,
    # rather than each backend's own default (GitHub: "Merge pull request
    # #N from <owner>/<branch>"; Forgejo: an equivalent branch-ref-bearing
    # default) -- a <type>/<task-id>-<slug> branch name would otherwise put
    # the task id straight into the subject with nobody typing it. Pure
    # readability: pr_title is passed through UNMODIFIED, no task-id
    # stripping/matching of any kind -- see merge.github_backend.merge_pr /
    # merge.forgejo_backend.merge_pr's own merge_title docstrings.
    try:
        merged_sha = backend.merge_pr(
            owner, repo, args.pr_number,
            merge_message=args.merge_message, merge_title=pr_title,
            merge_method=args.merge_method,
        )
    except MergeExecutionError as exc:
        _fail(str(exc), code=EXIT_MERGE_FAILED)

    print(f"merge: PR #{args.pr_number} in {owner}/{repo} merged")

    # Post-merge authoritative readback (lr-361de3): merge_pr's own response
    # is NOT a reliable carrier of the merged state (Forgejo returns 200/204
    # with an EMPTY body on success -- see merge.forgejo_backend.merge_pr's
    # docstring; GitHub's response IS checked already, but only at the
    # moment of the call). A FRESH GET, issued now, re-reads the PR and
    # confirms merged==true with a resolvable merge_commit_sha -- the same
    # predicate seq 2 of this task's research pass specified. FAIL-CLOSED:
    # unlike push's own additive-only readback (push.remote_readback), a
    # merge readback failure DOES fail this verb -- a merge gate's whole
    # purpose is deciding what's authorized to land, so reporting success
    # for a mutation this verb cannot independently confirm landed would
    # undermine the gate itself.
    merge_readback = verify_merge_landed(
        lambda: backend.get_pr_info(owner, repo, args.pr_number)
    )
    if not merge_readback.verified:
        _fail(
            f"post-merge readback FAILED for PR #{args.pr_number} in "
            f"{owner}/{repo} -- {merge_readback.detail.get('reason', '')} "
            f"The merge_pr call itself reported success; this independent "
            f"re-read could not confirm it landed. Gate-pass REFUSED.",
            code=EXIT_MERGE_READBACK_FAILED,
        )
    print(
        f"merge: post-merge readback CONFIRMED -- merged_commit_sha="
        f"{merge_readback.detail.get('merged_commit_sha')!r}",
        file=sys.stderr,
    )

    # Merge-completion attestation (lr-20e866): the ONLY git-host-visible mark
    # that THIS tool (as opposed to a human, or an earlier internal merge
    # tool) executed the merge -- see this module's docstring point 5 for
    # why the LORE-COUPLED half of the reference gate-note was never ported, and
    # merge.attestation's own docstring for why this half is pure git-host/
    # product data. FAIL-OPEN BY DESIGN: the merge above already succeeded --
    # a failed attestation POST must never fail this verb or change its exit
    # code, so any failure here is logged to stderr and swallowed, never
    # re-raised. Posted via the SAME POST-and-verify comment transport each
    # backend already carries (review.forgejo_backend.post_and_verify_comment
    # / review.github_backend.post_and_verify_review), not a third
    # implementation.
    # Both work-item IDs (lr-eb22f3): task_id is passed through verbatim from
    # --task-id (the caller's own opaque envelope value, never resolved or
    # validated here); issue_number is parsed back out of the PR body's own
    # `Closes #NN` trailer (git-host-native, never a lore field) -- reusing
    # push.issue_link's single regex rather than a second implementation.
    pr_body = pr_info.get("body") or ""
    issue_number = parse_closes_issue_number(pr_body)
    attestation_body = build_attestation_body(
        gated_head_sha=current_head_sha,
        merged_sha=current_head_sha,
        required_reviewer_logins=list(required_reviewers.values()),
        repo_gate_ignored=args.ignore_repo_gate,
        ci_disposition=ci_disposition,
        task_id=args.task_id,
        issue_number=issue_number,
    )
    verified_comment_id: "str | int | None" = None
    try:
        verified_comment_id = backend.post_merge_attestation(
            owner, repo, args.pr_number, body=attestation_body
        )
    except (
        ReviewPostError,
        ReviewVerifyError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
    ) as exc:
        # FAIL-OPEN (lr-20e866): the merge above already succeeded -- a
        # transport-level failure (network error, non-2xx the backend
        # translated to ReviewPostError/ReviewVerifyError) posting this
        # best-effort attestation must never fail the verb or change its
        # exit code. Named exception types only, never a bare except: a
        # genuine programming error in this call path (e.g. a TypeError from
        # a malformed call) is NOT swallowed here.
        print(
            f"merge: merge-completion attestation POST FAILED (non-fatal, "
            f"merge already succeeded) -- {exc}",
            file=sys.stderr,
        )
    else:
        print(
            f"merge: merge-completion attestation posted -- "
            f"verified_comment_id={verified_comment_id!r}",
            file=sys.stderr,
        )

    print(json.dumps({
        "pr_number": args.pr_number, "owner": owner, "repo": repo,
        READBACK_ENVELOPE_KEY: merge_readback.to_dict(),
        **({"repo_gate_ignored": list(repo_gate.reviewer_roles)} if args.ignore_repo_gate else {}),
    }))

    # 10. Working-tree sync + post-merge steps -- ONLY reached after the
    # merge above actually succeeded (any earlier _fail() call already
    # returned out of this function). Never attempted when --repo-path is
    # absent: a caller with no local working tree (a bare API-only merge) has
    # nowhere to sync a tree or run steps in. lr-ac5c8a: an absent
    # --repo-path reaching this point has ALREADY been required (by the
    # usage-error check earlier in this function) to carry an explicit
    # --no-post-merge-tree or --skip-post-merge acknowledgment -- there is no
    # remaining silent-skip path where --repo-path is simply missing and
    # nothing is logged.
    #
    # lr-173768: a CHECKOUT (git checkout --detach / git checkout -B) is now
    # performed ONLY when something will actually read the checked-out files
    # this invocation -- i.e. at least one post_merge_steps entry will
    # actually run (non-empty list AND --skip-post-merge not given). When
    # nothing will run, this phase still FETCHES and verifies the merged
    # commit is present locally (merge.tree_sync.fetch_merged_sha_object) --
    # so merge.merge_shape.check_merge_shape's local `git log` readback and
    # the merge-completion attestation's SHA claim keep working
    # unconditionally -- but never checks anything out: the working tree,
    # the index, and HEAD are left exactly where the caller had them. This
    # closes the documented contention source of a shared build-agent
    # checkout being yanked out from under other in-flight work on every
    # merge, regardless of whether that merge's own repo even has
    # post_merge_steps configured (see merge.tree_sync's own module
    # docstring, "NO CHECKOUT UNLESS SOMETHING WILL ACTUALLY READ THE TREE",
    # for the full rationale). A per-repo
    # `merge.sync_tree_after_merge` key (default True,
    # merge.post_merge_config.resolve_sync_tree_after_merge) still turns off
    # even the fetch-only phase entirely, unchanged from before this task.
    if args.repo_path:
        try:
            sync_tree_after_merge = resolve_sync_tree_after_merge(args.repo_path)
        except PostMergeConfigError as exc:
            _fail(
                f"post-merge config FAILED to load -- {exc}",
                code=EXIT_POST_MERGE_FAILED,
            )
        if not sync_tree_after_merge:
            print(
                "merge: working-tree sync SKIPPED -- "
                "merge.sync_tree_after_merge: false for this repo",
                file=sys.stderr,
            )
        else:
            base_branch = resolve_base_branch(pr_info)
            # lr-93d718: config discovery (load_post_merge_steps below) and
            # tree_sync's git-tree target are no longer assumed to be the
            # SAME directory. A wrapper-layout repo (config at the wrapper,
            # git tree at a subdirectory) declares an OPTIONAL
            # `merge.git_working_tree` knob
            # (merge.post_merge_config.resolve_git_working_tree) naming that
            # subdirectory relative to the config root; absent (the common,
            # flat-layout case), tree_sync targets --repo-path exactly as
            # before this task -- see that function's own docstring for the
            # full contract and post_merge_config's module docstring for the
            # "CONFIG-ROOT VS GIT-TREE-ROOT" rationale.
            try:
                declared_working_tree = resolve_git_working_tree(args.repo_path)
            except PostMergeConfigError as exc:
                _fail(
                    f"post-merge config FAILED to load -- {exc}",
                    code=EXIT_POST_MERGE_FAILED,
                )
            git_tree_path = (
                str(declared_working_tree) if declared_working_tree is not None else args.repo_path
            )

            # lr-173768: resolve WHETHER any post_merge_steps will actually
            # run this invocation BEFORE deciding whether to check anything
            # out -- a caller-config-load error here is reported exactly as
            # it always was (EXIT_POST_MERGE_FAILED), just moved earlier so
            # the checkout-vs-fetch-only decision itself can be made off a
            # fully-resolved `steps` value.
            if args.skip_post_merge:
                steps: list[dict] = []
            else:
                try:
                    steps = load_post_merge_steps(args.repo_path)
                except PostMergeConfigError as exc:
                    _fail(
                        f"post-merge config FAILED to load -- {exc}",
                        code=EXIT_POST_MERGE_FAILED,
                    )
            steps_will_run = bool(steps)

            # lr-f9a01b: a doctor-only check for this shape only fires when
            # someone thinks to run `loadout-doctor` -- the reported failure
            # was an UNATTENDED merge reporting exit 0 with steps_run 0,
            # which a doctor check alone cannot catch. Surface the SAME
            # cross-check loudly, right here on the path that actually runs
            # unattended, WITHOUT making a stale .crew/*.yaml comment a
            # merge blocker (see module docstring, "DEAD .crew/<role>.yaml
            # post_merge_steps CROSS-CHECK", for why this warns rather than
            # refuses). `post_merge_steps_key_declared` (not `bool(steps)`)
            # is deliberately used for the live-config half of this check --
            # a repo that explicitly wrote `post_merge_steps: []` in its OWN
            # config has already made an informed choice at the correct
            # file and must never be warned about a stale .crew/*.yaml
            # mention elsewhere.
            if not steps_will_run and not post_merge_steps_key_declared(args.repo_path):
                offending_crew_files = find_crew_yaml_files_declaring_post_merge_steps(
                    args.repo_path
                )
                if offending_crew_files:
                    print(
                        f"merge: WARNING -- {', '.join(offending_crew_files)} "
                        f"declare post_merge_steps, but loadout-merge NEVER "
                        f"reads that key from .crew/*.yaml -- it reads ONLY "
                        f"this repo's own .clagentic/loadout/config.yaml "
                        f"(merge.post_merge_steps), which does not declare "
                        f"it here. This merge is proceeding with 0 "
                        f"post-merge steps; the declared steps will "
                        f"silently never run. Move the post_merge_steps "
                        f"list into .clagentic/loadout/config.yaml under a "
                        f"merge: section to make it live.",
                        file=sys.stderr,
                    )

            # lr-cd3644 fold-in #3 (PR #30 re-review finding B): capture the
            # PRE-SYNC HEAD sha of git_tree_path's working tree, BEFORE
            # anything below advances/checks it out -- this is the commit the
            # `steps` value above was actually resolved from (whatever the
            # caller's tree was on when this invocation started). Used by the
            # drift check further below to distinguish "the config path was
            # never git-tracked at all" (genuinely not comparable, skip) from
            # "the config path WAS tracked here but the merged commit deletes
            # it" (the merged commit is authoritative: zero steps, a real
            # drift-corrected result, not an unresolvable comparison) -- see
            # merge.post_merge_config.config_path_tracked_at_git_sha's own
            # docstring for the full two-case rationale. A tree that cannot
            # even resolve its own HEAD (e.g. an unborn-branch edge case)
            # yields None here, which the tracked-check below treats the same
            # as "not tracked" -- the safe, conservative default this drift
            # check already applies to a genuinely untracked config.
            pre_sync_head_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=git_tree_path,
            )
            pre_sync_head_sha = (
                pre_sync_head_result.stdout.strip()
                if pre_sync_head_result.returncode == 0
                else None
            )

            # lr-cd3644 fold-in #3 (PR #30 re-review finding C): once ANY
            # branch below actually performs a verified checkout
            # (tree_checked_out=True), `land_on_base_branch` must run on
            # EVERY exit path from this point forward -- including a
            # post_merge_steps failure (`run_post_merge_steps` raising below)
            # -- never only on the all-succeeded path. Before this fix, an
            # `_fail()` raised anywhere between the checkout and the
            # `if tree_checked_out:` land call (e.g. a step failure) unwound
            # straight past that call, leaving a real, verified, DETACHED
            # checkout on disk with no landing attempt at all -- the same
            # "no signal to the next dispatch" defect class fold-in #4 (see
            # `land_on_base_branch`'s own docstring below) already fixed for
            # the drift-corrected-to-zero-steps shape, but for the exception
            # path specifically. `tree_checked_out` is initialized False
            # here, ahead of the `try`, so the exception-handling `except`
            # below can safely test it regardless of how early inside the
            # `try` a failure occurs.
            tree_checked_out = False
            try:
                if steps_will_run:
                    # lr-7c5540: advance the --repo-path working tree to the
                    # merged main SHA BEFORE running any post_merge_steps --
                    # backend.merge_pr above was a server-side API merge; it
                    # never touched this local tree. Without this, a post-merge
                    # step that packages/installs the repo (e.g.
                    # `scripts/install.sh` reading pyproject.toml/package source
                    # off disk) would silently package whatever ref the caller
                    # left checked out (the feature branch HEAD), not what
                    # actually landed on main. See merge.tree_sync's module
                    # docstring for the full trade-off on how the merged SHA is
                    # resolved per backend and why. FAIL LOUD on any inability to
                    # verify the tree landed on the merged commit -- never a
                    # silent run against the stale ref.
                    try:
                        landed_sha = advance_repo_to_merged_sha(
                            git_tree_path,
                            base_branch=base_branch,
                            known_merged_sha=merged_sha,
                        )
                    except TreeSyncError as exc:
                        _fail(
                            f"post-merge working-tree sync FAILED -- {exc}",
                            code=EXIT_POST_MERGE_FAILED,
                        )
                    tree_checked_out = True
                    print(
                        f"merge: working tree at {git_tree_path} advanced to "
                        f"merged SHA {landed_sha!r}",
                        file=sys.stderr,
                    )
                else:
                    # lr-173768: nothing will read the checked-out files this
                    # invocation (no post_merge_steps to run) -- fetch and
                    # verify the merged commit is present in the local object
                    # database WITHOUT checking anything out. Never a `git
                    # checkout`, so the working tree/index/HEAD are left exactly
                    # as the caller had them.
                    try:
                        landed_sha = fetch_merged_sha_object(
                            git_tree_path,
                            base_branch=base_branch,
                            known_merged_sha=merged_sha,
                        )
                    except TreeSyncError as exc:
                        _fail(
                            f"post-merge working-tree sync FAILED -- {exc}",
                            code=EXIT_POST_MERGE_FAILED,
                        )
                    # lr-cd3644 followup: tracked so the drift check below can
                    # tell whether a real checkout already happened (the
                    # steps_will_run branch above) or only a fetch (this branch)
                    # -- the drift check promotes fetch-only to a real checkout
                    # if it discovers the merged commit's own config declares
                    # steps the stale pre-sync read never saw.
                    tree_checked_out = False
                    print(
                        f"merge: fetched merged SHA {landed_sha!r} into "
                        f"{git_tree_path} (no post_merge_steps to run -- "
                        f"working tree left untouched, no checkout performed)",
                        file=sys.stderr,
                    )

                # lr-cd3644: STALE PRE-SYNC CONFIG DRIFT CHECK. `steps` above was
                # resolved from `args.repo_path`'s WORKING TREE, BEFORE this
                # function knew whether that tree was actually on the merged
                # commit -- resolving it any later would create a chicken-and-egg
                # with the lr-173768 checkout-vs-fetch-only decision immediately
                # above, which itself depends on knowing `steps_will_run` first.
                # A caller whose --repo-path was left on a STALE local ref (e.g.
                # an earlier merge's commit) can therefore have resolved `steps`
                # against config that does not match what actually landed at
                # `landed_sha` -- silently running too few (or the wrong) steps,
                # exactly the observed incident. Re-resolve the SAME
                # `post_merge_steps` key directly from the git object database at
                # `landed_sha` (load_post_merge_steps_from_git_sha -- a `git
                # show`, no checkout, safe to call regardless of which branch
                # above ran).
                #
                # ONLY COMPARABLE WHEN THE CONFIG FILE IS ACTUALLY GIT-TRACKED AT
                # THAT COMMIT: `load_post_merge_steps_from_git_sha` returns `None`
                # (never `[]`) when the config path is absent from *landed_sha*'s
                # tree entirely -- the common, equally-valid shape where a repo's
                # `.clagentic/loadout/config.yaml` is deliberately GITIGNORED
                # (this package's own dogfooding config included -- see this
                # repo's own .gitignore) and therefore can never drift relative
                # to a commit at all (a `git checkout` never touches an untracked
                # file). Comparing against a fabricated `[]` for that shape would
                # make EVERY merge of an untracked-config repo look like a
                # mismatch. The re-resolution below is skipped entirely when
                # `landed_steps` is `None`.
                #
                # THE MERGED COMMIT IS AUTHORITATIVE, NOT THE PRE-SYNC TREE
                # (lr-cd3644 followup, folded into this same PR): the ORIGINAL
                # version of this check FAILED LOUD on any
                # disagreement (EXIT_POST_MERGE_FAILED) instead of running the
                # merged commit's own steps -- meaning a stale --repo-path whose
                # pre-sync config had ZERO steps while the merged config declares
                # real ones (exactly the acceptance-criterion shape: a repo whose
                # config gained post_merge_steps in the very commit this merge
                # lands) refused the ENTIRE merge's post-merge automation rather
                # than simply running what the merged commit actually declares.
                # That is strictly worse than the incident this check was built
                # to close: the incident was "steps silently never ran"; failing
                # the merge outright over a mismatch means steps STILL never run,
                # now with a hard merge failure attached. Once `landed_sha` is
                # known and `landed_steps` has been read directly from the git
                # object database at that exact commit, there is no remaining
                # reason to prefer the stale pre-sync `steps` value over it --
                # `landed_steps` is REASSIGNED to `steps` below, and
                # `steps_will_run` is RE-DERIVED from that corrected value, so
                # every remaining branch in this function (the log lines, the
                # actual `run_post_merge_steps` call, and the `land_on_base_branch`
                # gate further below) sees the config the merged commit itself
                # declares, not a stale snapshot. FAIL LOUD is preserved for the
                # one case this task's own contract calls out: `landed_steps`
                # itself could not be READ (a malformed/unreadable committed
                # config at `landed_sha` -- `PostMergeConfigError` from
                # `load_post_merge_steps_from_git_sha` above) -- that failure
                # mode is untouched, still EXIT_POST_MERGE_FAILED, since a config
                # this function cannot be shown to have actually resolved must
                # never be silently treated as "no steps." --skip-post-merge is
                # an explicit, logged caller opt-out (steps forced to `[]` above,
                # BEFORE this check ever runs) -- the drift correction below must
                # never override that explicit choice by resurrecting steps the
                # caller asked to skip; the check still fires for its diagnostic
                # value (comparing `[]` against whatever the merged commit
                # declares), but `not args.skip_post_merge` guards the
                # steps-reassignment/promoted-checkout side effects specifically.
                # lr-cd3644 fold-in #3: the pre-sync `steps` read above (and
                # `post_merge_steps_key_declared`/`resolve_git_working_tree`
                # earlier) resolved config from `args.repo_path` (the CONFIG
                # ROOT -- the wrapper, in the lr-93d718 wrapper-layout split),
                # while `load_post_merge_steps_from_git_sha` below runs `git
                # show <sha>:<path>` with cwd=git_tree_path (the ACTUAL GIT
                # TREE, a SUBDIRECTORY of the config root in that same split).
                # Passing this function's own bare defaults there resolved a
                # path relative to the WRONG root: a wrapper-layout repo's
                # committed config lives at `<config_root>/.clagentic/loadout/
                # config.yaml`, entirely OUTSIDE `<config_root>/<git_working_
                # tree>`'s own git tree, so `git show` always reported "path
                # absent" there regardless of any actual drift -- permanently
                # disabling this check for every repo that declares
                # `git_working_tree`. resolve_git_tree_relative_config_paths
                # re-expresses the SAME config root's two candidate paths
                # relative to git_tree_path instead, so both reads agree on
                # which file they mean; a config root that sits genuinely
                # outside the git tree (as in that split) correctly yields
                # (None, None), and load_post_merge_steps_from_git_sha treats
                # that the same as "absent from git" -- None, not comparable,
                # skip -- rather than attempting an outside-the-repository git
                # show call.
                git_relative_config_path, git_relative_legacy_path = (
                    resolve_git_tree_relative_config_paths(
                        args.repo_path, git_tree_path
                    )
                )
                try:
                    landed_steps = load_post_merge_steps_from_git_sha(
                        git_tree_path,
                        landed_sha,
                        config_relative_path=git_relative_config_path,
                        legacy_relative_path=git_relative_legacy_path,
                    )
                except PostMergeConfigError as exc:
                    _fail(
                        f"post-merge config FAILED to load from merged commit "
                        f"{landed_sha!r} -- {exc}",
                        code=EXIT_POST_MERGE_FAILED,
                    )

                # lr-cd3644 fold-in #3 (PR #30 re-review finding B): a `None`
                # landed_steps result is ambiguous between "this config path was
                # NEVER git-tracked at all" (genuinely not comparable -- an
                # untracked file cannot drift relative to a commit) and "this
                # config path WAS tracked at the caller's PRE-SYNC HEAD but the
                # merged commit's own tree no longer tracks it" (a DELETION the
                # merged commit is authoritative for -- zero steps, a real
                # drift-corrected result, not an unresolvable comparison). Only
                # promote None -> [] when the pre-sync HEAD is known AND the
                # config path was genuinely tracked there -- see
                # config_path_tracked_at_git_sha's own docstring for the full
                # two-case rationale this distinguishes.
                if landed_steps is None and pre_sync_head_sha is not None:
                    if config_path_tracked_at_git_sha(
                        git_tree_path,
                        pre_sync_head_sha,
                        config_relative_path=git_relative_config_path,
                        legacy_relative_path=git_relative_legacy_path,
                    ):
                        landed_steps = []

                drifted = (
                    landed_steps is not None
                    and landed_steps != steps
                    and not args.skip_post_merge
                )
                if drifted:
                    print(
                        f"merge: WARNING -- post-merge config DRIFT -- "
                        f"{args.repo_path}'s pre-sync working tree resolved "
                        f"{len(steps)} post_merge_steps "
                        f"entr{'y' if len(steps) == 1 else 'ies'}, but the merged "
                        f"commit {landed_sha!r} actually being merged declares a "
                        f"DIFFERENT, git-tracked post_merge_steps list "
                        f"({len(landed_steps)} entr{'y' if len(landed_steps) == 1 else 'ies'}). "
                        f"This means --repo-path was stale (not yet advanced to "
                        f"the commit this merge landed) at the moment its config "
                        f"was first read. The MERGED COMMIT's own steps are "
                        f"authoritative -- running {len(landed_steps)} "
                        f"post_merge_steps entr{'y' if len(landed_steps) == 1 else 'ies'} "
                        f"from {landed_sha!r} instead of the stale pre-sync list.",
                        file=sys.stderr,
                    )
                    steps = landed_steps
                    steps_will_run = bool(steps)
                    if steps_will_run and not tree_checked_out:
                        # lr-173768's checkout-vs-fetch-only decision (above) ran
                        # off the STALE pre-sync `steps_will_run` and chose the
                        # fetch-only path (fetch_merged_sha_object -- no
                        # checkout). The drift just corrected `steps_will_run`
                        # from False to True: something WILL now read the
                        # checked-out files (the merged commit's own steps), so
                        # the tree must actually be checked out before they run --
                        # a fetch-only tree still has the caller's stale ref
                        # checked out on disk. Promote to a real, verified
                        # checkout now, via the SAME advance_repo_to_merged_sha
                        # call the steps_will_run-from-the-start branch already
                        # uses -- never a second/divergent resolution of
                        # landed_sha, just the checkout that was deferred.
                        # Review finding: known_merged_sha
                        # here MUST be `landed_sha` (the exact commit
                        # fetch_merged_sha_object already fetched and
                        # independently verified above), never `merged_sha` (the
                        # merge backend's own API-reported value, which is `None`
                        # on the Forgejo path -- see merge.tree_sync's module
                        # docstring, "TRADE-OFF NAMED"). Passing `merged_sha`
                        # here meant a Forgejo merge promoted straight to
                        # `advance_repo_to_merged_sha`'s base-branch-FALLBACK
                        # resolution (known_merged_sha=None re-fetches and
                        # re-resolves *base_branch*'s CURRENT remote tip), which
                        # can be a DIFFERENT, later commit than `landed_sha` if
                        # anything else advanced the base branch between the
                        # initial fetch above and this promotion -- checking out
                        # a commit no verification step ever confirmed is the
                        # merge result this task actually landed. Passing
                        # `landed_sha` instead keeps this call on the VERIFIED
                        # known_merged_sha path (fetch + checkout of that exact
                        # SHA, with a post-checkout readback comparison), which
                        # is also what the docstring immediately above already
                        # claimed ("never a second/divergent resolution of
                        # landed_sha") -- this aligns the code with that claim.
                        try:
                            landed_sha = advance_repo_to_merged_sha(
                                git_tree_path,
                                base_branch=base_branch,
                                known_merged_sha=landed_sha,
                            )
                        except TreeSyncError as exc:
                            _fail(
                                f"post-merge working-tree sync FAILED -- {exc}",
                                code=EXIT_POST_MERGE_FAILED,
                            )
                        tree_checked_out = True
                        print(
                            f"merge: working tree at {git_tree_path} advanced to "
                            f"merged SHA {landed_sha!r} (promoted from fetch-only "
                            f"after the drift check found real post_merge_steps "
                            f"at the merged commit)",
                            file=sys.stderr,
                        )

                # lr-14f704 item 3: surface a requested-vs-actual merge-shape
                # mismatch loudly rather than silently -- the exact defect class
                # push.remote_readback (lr-4e8a43) closed one layer down, applied
                # here to the merge call itself. Reads the ALREADY-FETCHED local
                # object (see merge.merge_shape's own docstring, "SCOPE") --
                # this readback needs the commit object present, never a
                # checkout, so it runs unconditionally here regardless of
                # whether steps_will_run checked anything out above. A bare
                # API-only merge with no --repo-path has no local object
                # database to read a parent count from, and is not covered by
                # this check.
                try:
                    shape_check = check_merge_shape(
                        landed_sha, args.merge_method, git_tree_path
                    )
                except MergeShapeCheckError as exc:
                    _fail(
                        f"merge-shape readback FAILED -- {exc}",
                        code=EXIT_MERGE_SHAPE_MISMATCH,
                    )
                if shape_check.verified and not shape_check.matches:
                    mismatch_message = format_mismatch_message(
                        shape_check, pr_number=args.pr_number, owner=owner, repo=repo
                    )
                    try:
                        enforce_merge_shape = resolve_enforce_merge_shape(args.repo_path)
                    except PostMergeConfigError as exc:
                        _fail(
                            f"post-merge config FAILED to load -- {exc}",
                            code=EXIT_POST_MERGE_FAILED,
                        )
                    if enforce_merge_shape:
                        _fail(mismatch_message, code=EXIT_MERGE_SHAPE_MISMATCH)
                    print(f"merge: WARNING -- {mismatch_message}", file=sys.stderr)

                if not steps_will_run:
                    if args.skip_post_merge:
                        print(
                            "merge: post-merge steps SKIPPED via --skip-post-merge",
                            file=sys.stderr,
                        )
                    # else: steps genuinely resolved to an empty list -- nothing
                    # to log beyond the fetch-only message already printed above.
                else:
                    print(
                        f"merge: running {len(steps)} post-merge step(s) in "
                        f"{args.repo_path}",
                        file=sys.stderr,
                    )
                    # Deployment-owned env-override seam (lr-52d7): resolved
                    # from CLAGENTIC_LOADOUT_POST_MERGE_ENV_<NAME> env vars
                    # and the user-level config file's post_merge_env:
                    # section — never from this repo's own (possibly
                    # committed) .clagentic/loadout/config.yaml. See
                    # merge.post_merge_config.resolve_env_overrides for the
                    # full precedence and why this is the correct trust
                    # boundary.
                    deployment_env_overrides = resolve_env_overrides()
                    # lr-d6e52b: repo-tier default bound for any ORDINARY
                    # step that does not set its own timeout_seconds -- see
                    # merge.post_merge_config's own docstring,
                    # "POST_MERGE_STEP_TIMEOUT_SECONDS". None (absent) is a
                    # no-op, matching pre-lr-d6e52b unbounded-wait behavior.
                    try:
                        default_step_timeout = resolve_post_merge_step_timeout_seconds(
                            args.repo_path
                        )
                    except PostMergeConfigError as exc:
                        _fail(
                            f"post-merge config FAILED to load -- {exc}",
                            code=EXIT_POST_MERGE_FAILED,
                        )
                    try:
                        run_post_merge_steps(
                            steps,
                            args.repo_path,
                            deployment_env_overrides=deployment_env_overrides,
                            default_timeout_seconds=default_step_timeout,
                        )
                    except (
                        PostMergeStepFailedError,
                        PostMergeStepTimeoutError,
                        PostMergeLivenessError,
                    ) as exc:
                        _fail(str(exc), code=EXIT_POST_MERGE_FAILED)
            except MergeVerbError:
                # lr-cd3644 fold-in #3 (PR #30 re-review finding C): an
                # `_fail()` raised ANYWHERE in the `try` block above (a step
                # failure, a config-load error, a merge-shape mismatch, ...)
                # must still land a real, verified checkout on *base_branch*
                # before this function's own error propagates -- never leave
                # a genuinely-checked-out tree PERMANENTLY DETACHED just
                # because something failed AFTER the checkout. The ORIGINAL
                # exception (and its ORIGINAL exit code) is always what gets
                # reported: land_on_base_branch runs here as a best-effort
                # cleanup, and if IT ALSO fails, that failure is logged but
                # deliberately swallowed rather than replacing the original
                # error -- a caller must never see "landing failed" mask
                # "why the merge's post-merge automation actually failed."
                if tree_checked_out:
                    try:
                        landed_branch_sha = land_on_base_branch(
                            git_tree_path,
                            base_branch=base_branch,
                            landed_sha=landed_sha,
                        )
                    except TreeSyncError as land_exc:
                        print(
                            f"merge: WARNING -- working tree at "
                            f"{git_tree_path} could NOT be landed on "
                            f"{base_branch!r} after an earlier post-merge "
                            f"failure -- {land_exc}. The tree remains "
                            f"detached at the merged commit; the ORIGINAL "
                            f"post-merge failure (reported below) is still "
                            f"authoritative.",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"merge: working tree at {git_tree_path} landed "
                            f"on {base_branch!r} at {landed_branch_sha!r} "
                            f"despite an earlier post-merge failure "
                            f"(reported below)",
                            file=sys.stderr,
                        )
                raise
            else:
                if tree_checked_out:
                    # lr-d95cdb: only NOW -- after post_merge_steps have run
                    # against the detached, verified tree -- move the tree off
                    # that detached HEAD onto base_branch, pointed at the SAME
                    # landed_sha advance_repo_to_merged_sha already verified. See
                    # merge.tree_sync.land_on_base_branch's own docstring: this
                    # is a ref repoint (git checkout -B), never a merge/rebase,
                    # so it cannot diverge from the server-side merge result.
                    # Runs against git_tree_path (the SAME target
                    # advance_repo_to_merged_sha used above), not necessarily
                    # --repo-path itself (the wrapper-layout split, lr-93d718).
                    # lr-173768: skipped entirely when nothing was ever checked
                    # out (only a fetch happened) -- there is no detached HEAD to
                    # move off of in that case, and re-pointing the caller's
                    # branch ref out from under it with nothing having read the
                    # tree would be exactly the unsignaled-mutation class this
                    # task removes.
                    #
                    # GATED ON `tree_checked_out`, NOT `steps_will_run` (lr-cd3644
                    # fold-in #4): the STALE PRE-SYNC CONFIG DRIFT CHECK above can
                    # re-derive `steps_will_run` from the merged commit's own
                    # config AFTER a real checkout already happened -- including
                    # correcting it DOWN to False when the merged commit's
                    # tracked config declares ZERO steps (a legitimate, fully
                    # comparable drift result, not an error). `steps_will_run`
                    # at this point reflects "did any step actually run," which
                    # is independent of "does a detached checkout need to be
                    # landed" -- `tree_checked_out` tracks the latter directly
                    # and is never reassigned once a checkout has genuinely
                    # happened (see both call sites above: the steps_will_run-
                    # from-the-start branch and the drift-promoted branch both
                    # set it True immediately after their own verified checkout,
                    # and nothing in this function ever sets it back to False).
                    # Gating on `steps_will_run` here left a real, verified,
                    # DETACHED checkout on disk with no signal to the next
                    # dispatch whenever drift corrected a non-empty pre-sync
                    # steps list down to an empty merged-commit list -- the tree
                    # must land on base_branch whenever the sync actually
                    # advanced/checked it out, regardless of the final step
                    # count. lr-cd3644 fold-in #3 (finding C): this `else`
                    # clause is the ALL-SUCCEEDED landing path; the exception
                    # path's OWN landing attempt lives in the `except
                    # MergeVerbError` clause above, so a checkout is landed
                    # exactly once regardless of which path this invocation
                    # takes.
                    try:
                        landed_branch_sha = land_on_base_branch(
                            git_tree_path,
                            base_branch=base_branch,
                            landed_sha=landed_sha,
                        )
                    except TreeSyncError as exc:
                        _fail(
                            f"post-merge working-tree sync FAILED -- {exc}",
                            code=EXIT_POST_MERGE_FAILED,
                        )
                    print(
                        f"merge: working tree at {git_tree_path} landed on "
                        f"{base_branch!r} at {landed_branch_sha!r}",
                        file=sys.stderr,
                    )
    elif args.skip_post_merge:
        print("merge: post-merge steps SKIPPED via --skip-post-merge", file=sys.stderr)
    elif args.no_post_merge_tree:
        print(
            "merge: post-merge steps SKIPPED -- --no-post-merge-tree "
            "explicitly acknowledged no local working tree for this "
            "invocation",
            file=sys.stderr,
        )

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
