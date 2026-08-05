# Verbs

This describes the CLI verbs that are actually landed on `main` today. `clagentic:
loadout` is **pre-release** (no tag, no packaged installer yet) — every verb below is
invoked from a source checkout via its `project.scripts` console-script name (see
`pyproject.toml`); see "Live-cast cut-over" below for which verbs are already gating real
deployments vs. still pending cut-over.

## Landed verbs

### `loadout-git-host-api` — authenticated Forgejo REST call

`clagentic_loadout.transport.git_host_api`. Makes one authenticated call
against a Forgejo instance's REST API. `--body-stdin` or `--body-env` is
the body path for every write method (POST/PATCH/PUT/DELETE) — exactly one
is required; no `--body-file` staging, no second free-form content source.
A comment POST requires `--verify-comment` +
`--pr-sha`: a mandatory post-and-verify readback (resolve the caller's own
login, re-fetch the comment thread, confirm a freshly-created comment
matching login + body exists) before the call is considered to have
succeeded. Token is always resolved through the credential-provider seam
(see "Credentials" below) — never read from an inherited environment
variable. Base URL resolution (the `--git-host-base-url` flag, its env
vars, and the config-file tier) is the runtime contract documented in
[docs/integration.md](integration.md).

**`--body-env` — body-off-argv-and-pipe:**
see the dedicated section below (shared by `loadout-git-host-api` and
`loadout-review-post`) for the full mechanism and the harness-side staging
contract.

**`--expect-verdict-block <reviewer>` — the reviewer route:**
a reviewer role's merge-gate verdict is a fenced ` ```review-result``` `
block (`merge.verdict`, hard requirement — see `loadout-merge` below). A
shell-argv guard that forbids a backtick in any `--body-stdin` producer
argument cannot tell a markdown fence apart from command substitution — it
has to refuse both, which otherwise leaves a reviewer with no way to post
the fence except hand-authoring it through a shell producer. This flag
moves fence construction INTO the tool: `--body-stdin`'s JSON carries
`{"body": "<prose>", "review_status": "clean"|"blocking"}` — zero
backticks anywhere in the argv or the piped bytes — and this verb appends
the fence itself via `merge.verdict.build_verdict_block` (the same
function `loadout-merge`'s own re-parse uses, so the posting side and the
merge-gate side never author or accept two different fence shapes).
`head_sha` comes from `--pr-sha` (the same value `--verify-comment`'s
pre-check already confirmed against the PR's live head — never a second
SHA to keep in sync) and `pr_number` comes from the POST path itself, so
the fenced `pr_number` can never disagree with the PR actually posted to.
Requires `--body-stdin`, `--verify-comment`, and `--pr-sha`; after the
ordinary `--verify-comment` readback confirms the comment landed, the
verified comment's own body is re-parsed and checked field-for-field
against what was requested — a fence that landed truncated or mangled in
transit is caught here, not later as an opaque merge-gate refusal.
`--body-stdin`'s `'body'` field must NOT already contain a fenced
` ```review-result``` ` block — this verb constructs the one fence itself,
so a pre-embedded fence is refused (`EXIT_VERDICT_BLOCK_USAGE`, before any
network call) rather than silently landing a comment with two. See
"Multi-fence verdict bodies" under `loadout-merge` below for the full
defect this closes and the paired merge-gate consumer-side behavior.

**`--caller-tracking-id <id>` — the general body-transport route:**
the counterpart to `--expect-verdict-block` for any caller (not just a
reviewer) that wants to carry an opaque work-item tracking reference
alongside a comment body. Before this flag, a caller stamping a comment
with its own tracking id (e.g. a task id) had no shell-safe way to do it —
either a hand-authored fence (the same backtick-in-argv trap
`--expect-verdict-block` fixes) or staging a state note via a
`cat >> $TMPDIR/... << EOF` heredoc with a `$VAR`-substituted redirect
target, neither of which a static shell-argv guard can analyze (it fails
closed on both, producing false blocks and manual approval prompts). This
flag moves note construction INTO the tool
exactly like `--expect-verdict-block` moves fence construction in:
`transport.note_compose.build_composed_body` appends a fenced
` ```loadout-note``` ` block (a distinct fence-language tag from
`review-result`, carrying no merge-gate enforcement semantics) to the
`--body-stdin` `body` prose, entirely in-process. Requires a POST to
`issues/<pr>/comments` and `--verify-comment`; composes on top of
`--expect-verdict-block`'s fence when both flags are supplied together —
the tracking-id note always trails the verdict fence. The tracking id
itself is caller-opaque: this tool never interprets it (a lore task id for
a LORE-integrated deployment, or any other deployment's own tracking-id
shape — CLAUDE.md rule 6a).

### `--body-env` — body-off-argv-and-pipe

Shared by `loadout-git-host-api` (mutually exclusive with `--body-stdin`,
exactly one required for a write method) and `loadout-review-post`
(alternative to the default stdin body path).

**The gap this closes:** `--body-stdin` fixed the backtick/heredoc class of
guard-hostile shell (see `--expect-verdict-block` / `--caller-tracking-id`
above), but it did not remove body data from the shell command line
entirely. A caller still has to get JSON bytes into the process's stdin
somehow — `echo '{"body": "..."}' | some-verb ...` puts the varying body
string directly on the shell command line as `echo`'s own argument. The
**Claude Code harness's own static Bash analyzer** — a gate distinct from
this repo's `guard-bash.py`, and one no allowlist entry can ever admit
because the body payload (and therefore the exact brace/quote sequence) is
different on every single call — flags an inline `{`/`"` combination as
"expansion obfuscation" and raises a manual operator Allow/Deny prompt on
every invocation. A `< body.json` redirect or heredoc has the identical
problem whenever the source file name itself varies per call.

**The mechanism:** `--body-env` is a bare, zero-argument switch — supplying
it never changes the invoking argv by even one character across calls. When
set, the verb reads the body from a FIXED, CALLER-NAMESPACED path
(`transport.body_env.resolve_caller_body_path`:
`$TMPDIR/clagentic-loadout/body.<caller>.json`, keyed off the verb's own
`--caller` value, TMPDIR-scoped per CLAUDE.md rule 7) instead of stdin.
Validation of the read bytes is byte-identical to the `--body-stdin` path —
same `validate_body_stdin_content` / `validate_review_body_stdin_content`
functions, same downstream composition with `--expect-verdict-block`,
`--verdict-review-status`, and `--caller-tracking-id`. See
`transport.body_env`'s module docstring for the full design, including the
explicit trade-off analysis against an envelope-delivered body (the other
candidate this task's scope named) and why this is NOT a revival of the
previously-rejected `--body-file` staging flag (a caller-supplied arbitrary
path, as opposed to a fixed, tool-computed one) — enforced suite-wide by
`tests/test_reviewer_no_disk_staging_or_hand_authored_fence.py`'s
`_DISK_STAGING_FLAG_MARKERS` guard, which `--body-env` does not trip.

**Concurrent same-TMPDIR callers (gate-integrity fix):** the path
is namespaced per `--caller`, not shared — two callers dispatched
concurrently onto the SAME `TMPDIR` (the common case once a deployment sets
`TMPDIR` to one fixed root for every spawn) resolve to two DIFFERENT
physical files and can never collide, structurally, regardless of write/read
timing. A caller whose harness never staged a body under ITS OWN `--caller`
namespace fails closed (`BodyEnvError` / `EXIT_BODY_ENV_UNREADABLE`) rather
than risking a read of a different caller's staged content. The now-legacy
single-shared-path behavior (`resolve_body_path`, `body.json` with no
caller component) is still available at the `transport.body_env` API level
for a caller with no identity to namespace by, but every write-method verb
in this package always resolves and passes `--caller` before reading, so
this is the path every production `--body-env` invocation actually uses.

**Sequential stale-read across invocations (read-and-consume +
identity stamp):** the concurrent-caller fix above closes collision between
two DIFFERENT concurrent callers, but did nothing for a LATER, unrelated
invocation of the SAME caller silently re-reading a leftover staged body
from a prior PR/review (a real foreign-body incident: a stale review body
got re-POSTed onto an unrelated PR). The staged body is now
consumed (unlinked) after a successful read, and bound to an identity
stamp (`target_pr`, optional `head_sha`, `staged_at`) the read must match —
`read_body_bytes(caller=...)` requires `expect_target_pr`; a mismatch or a
second read with no re-staging fails closed (`BodyEnvError`,
`EXIT_BODY_ENV_UNREADABLE`) rather than ever silently posting stale
content. See `transport.body_env`'s module docstring and
`stage_caller_body` for the write-side contract.

**Harness-side local process (the consumer's responsibility):** a
deployment wiring an agent harness at this route must, before invoking a
verb with `--body-env`, stage BOTH the body file and its identity-stamp
sidecar. There is exactly one sanctioned way to do this, for EVERY calling
role (builder or reviewer — no role-specific staging path):

**`loadout-stage-body`, the dedicated stage verb.**
A single console-script invocation writes both files
atomically, through the SAME `transport.body_env.stage_caller_body` API —
so an agent's own Bash permission allowlist only ever needs an entry for
`loadout-stage-body` itself, never a `*.stamp.json` write pattern. This is
the fix for the exact defect that motivated this verb: an agent's allowlist
commonly admits `body.<caller>.json` for raw staging but categorically
denies `body.<caller>.stamp.json` (a stamp is a platform-computed
provenance value, not something an agent should be able to hand-author
with an arbitrary `target_pr`/`head_sha`), which left `--body-env` reads
failing closed with "no identity stamp staged" for any caller whose
harness only had a raw-printf staging step.

```
echo '{"body":"...", "review_status":"clean"}' | \
  loadout-stage-body --caller <role> --target-pr <pr-number> \
    [--head-sha <sha>]
```

or, from a non-shell harness (e.g. a Write-tool-equivalent that does not go
through a Bash command analyzer at all), redirect a file already staged at
a constant local path — this is still ingest through `loadout-stage-body`'s
own stdin, never a hand-authored stamp:

```
loadout-stage-body --caller <role> --target-pr <pr-number> \
  --head-sha <sha> < /constant/local/path/to/body.json
```

`--caller` and `--target-pr` are mandatory; `--head-sha` is optional (omit
for an ordinary, non-verdict comment with no SHA to bind against). stdin
content is validated identically to `--body-stdin` on every other verb in
this package — empty/malformed content is refused BEFORE anything is
staged to disk. See `transport.stage_body_verb`'s module docstring for the
full contract, including why this is a thin wrapper around
`stage_caller_body` rather than a second implementation of the
stamp shape.

**Retired: hand-writing the staged pair.** A prior revision of this
document also described a "hand-write both files directly" alternative —
composing `body.<caller>.json` and its `.stamp.json` sidecar via a raw
shell redirect, for a harness whose allowlist could not yet admit
`loadout-stage-body` for its own calling role. That alternative is
retired: once a deployment's own guard/allowlist layer admits
`loadout-stage-body` for every calling role, there is no longer a role
left for the hand-write path to serve, and it was the exact raw
`printf`/`echo`/redirect improvisation
that produced a recurring family of guard-denial and doc-drift tasks each
time an allowlist and this document's own description of the sanctioned
path came apart. A harness that hits a guard denial invoking
`loadout-stage-body` has an allowlist gap for its role to fix (see
[docs/provisioning.md](provisioning.md)), not a reason to fall back to a
hand-composed stamp.

**Then, for either staging invocation above:** invoke the reading verb
with a CONSTANT argv that includes `--body-env`, the SAME `--caller` value
used to stage above, and no body data of any kind — this is the step that
is genuinely allowlistable, because the same argv shape is issued on every
call regardless of what the body actually contains (the caller identity
itself is not new per-invocation variance: it is the SAME acting role
across a given harness's calls, already present in every `--body-env`
invocation's argv). The staged body and its stamp ARE
consumed (unlinked) by the verb after a successful, provenance-matched read
— a harness never needs its own cleanup step for a body that
was actually read. A RETRIED invocation must RE-STAGE a fresh body+stamp
pair via `loadout-stage-body` again; the staged path is no longer safely
re-readable across invocations (a prior version of this doc said
otherwise — that assumption is exactly what a real foreign-body incident,
a stale leftover body silently re-posted under a later invocation's
identity, proved unsafe). A read whose target PR/SHA does not match the
staged stamp fails closed without consuming anything.

**Abandonment:** the consume-on-read guarantee above only covers a stage
that is later actually read. A stage followed by a crash/kill/guard-denial
before the matching read, or a stamp-mismatch read that correctly left
its pair in place for a different, still-pending invocation that then
never arrives, leaves files behind with no reader left to consume them.
`stage_caller_body` and `read_caller_body_bytes` both opportunistically
sweep the `$TMPDIR/clagentic-loadout/` staging directory on every call,
removing any regular file directly inside it older than one hour — this
runs on ordinary stage/read traffic, not as a separate cron or daemon,
and never fails or changes the calling verb's exit code on a sweep error
(warn-only). This closes the abandonment case as long as SOME
`--body-env`/`loadout-stage-body` traffic (for any caller) continues on
the same `TMPDIR`; a host where no loadout verb in this staging path ever
runs again is not covered — there is nothing outside a verb invocation
that ever sweeps this directory.

A harness whose allowlist genuinely cannot admit `loadout-stage-body` at
all (rather than a role-gap that should be closed per
[docs/provisioning.md](provisioning.md)) should continue to use
`--body-stdin`, piped from a NON-shell-command-line delivery mechanism
where the delivering process is not itself a Bash tool call subject to the
harness's own static analyzer.

**Channel parity note:** the guard/allowlist wiring this section assumes is
shell-command-scoped (`guard.scratch_policy`/`role_allowlist`, see
[docs/guard-policy.md](guard-policy.md)'s "Coverage boundary" section) — a
harness that also grants a non-shell `Write`/`Edit`-equivalent tool must
consult the same containment predicate set on that channel too, or the
staging path documented here is guarded while a sibling channel is not. See
[docs/integration.md](integration.md)'s "Channel parity" section for the
full requirement and its motivating failure shape.

### `loadout-stage-body` — the sanctioned WRITE side for `--body-env`

`clagentic_loadout.transport.stage_body_verb`. Stages a caller-namespaced
body file AND its identity-stamp sidecar atomically, via the SAME
`transport.body_env.stage_caller_body` API `--body-env`'s read side
(`read_caller_body_bytes`) already verifies against. Makes no
network call and mints no credential — this is a pure local filesystem
staging step, host-agnostic by construction (the SAME staged pair is read
by `loadout-git-host-api`'s Forgejo-only `--body-env` route,
`loadout-review-post`'s shared Forgejo+GitHub route, and `loadout-push`'s
own `--body-env` route — see below).

Exactly one of `--target-pr` (existing-PR update/comment path) or
`--create-branch` (push's PR-creation path) is required:

```
echo '{"body":"...", "review_status":"clean"}' | \
  loadout-stage-body --caller <role> --target-pr <pr-number> \
    [--head-sha <sha>]

echo '{"body":"plain PR description text"}' | \
  loadout-stage-body --caller <role> --create-branch <branch-name>
```

Flags: `--caller` (required, the SAME value the later `--body-env` read
invocation's own `--caller` will carry), `--target-pr` (positive int — the
PR this body is staged for) OR `--create-branch` (the git branch that will
open a NEW PR — mutually exclusive with `--target-pr`, exactly one
required), `--head-sha` (optional — bind a verdict post's evaluated SHA).
`--body-stdin` (explicit marker for the default stdin-body behavior, kept
only to mirror every other body-ingesting verb's own flag name) — stdin is
the SOLE content-input path; **no verb in this package accepts a
caller-supplied filesystem path for PR-body content** (a prior
`--body-file <path>` flag on this verb and on `loadout-push` was removed
after a security audit found the analogous flag on `loadout-push` bypassed
the identity-stamp mechanism entirely — see that verb's own section below).
Body content is validated with the same `validate_body_stdin_content` every
other body-ingesting verb in this package uses, BEFORE anything is staged —
malformed content is refused here, not later at `--body-env` read time.

### `loadout-review-post` — post-and-verify a review comment

`clagentic_loadout.review.verb`. Posts exactly one review comment and
verifies it landed, on either Forgejo or GitHub, behind one contract
(`review.contract.ReviewBackend`). `--platform` (`forgejo` | `github`) is
mandatory — resolved by the caller (e.g. from a dispatch envelope's PR
URL), never auto-detected from ambiguous input. The platform guard
(`assert_platform_is_forgejo` / `assert_platform_is_github`) always runs
**before** any credential is minted or any network call is made, in both
directions. stdin is the default body path; `--body-env` (see below) reads
the FIXED staged path instead. Outside the verdict
route below, this verb does not touch the fenced ` ```review-result``` `
block that the merge gate reads — see `review.contract`'s module
docstring.

**`--verdict-review-status <clean|blocking>` — MANDATORY, fail-closed
emit-and-verify verdict route, Forgejo AND GitHub parity:**
the sibling of `loadout-git-host-api`'s Forgejo-only
`--expect-verdict-block` — that flag is unchanged and still the
canonical route for a Forgejo-only caller. This route closes the two gaps
that flag leaves open: GitHub had no tool-owned verdict-fence emit-and-verify
at all, and a reviewer role reaching `review-post` directly (rather than
`git-host-api`) had no fence enforcement — a plain `--body-stdin` post with
hand-typed prose was indistinguishable from a real verdict. Requires
`--verdict-head-sha` alongside it (no partial/optional lane). This verb (1)
CONSTRUCTS the fenced block internally via `merge.verdict.build_verdict_block`
— the same fence-authoring source `--expect-verdict-block` and the merge
gate's own re-parse both use — from `--caller` (the `reviewer` field),
`--verdict-review-status`, `--verdict-head-sha`, and the `pr_number`
positional (never re-declared in stdin, so it can never disagree with the PR
actually posted to); (2) posts through the ordinary
`ReviewBackend.post_and_verify()` path for the resolved platform — no second
transport; (3) re-fetches the landed body from the VERIFIED comment/review
(`VerifiedReview.body`, sourced from the backend's own mandatory readback,
never the locally-constructed string) and re-parses it via
`merge.verdict.parse_verdict_block`; (4) asserts every field matches; (5)
returns `EXIT_OK` only on a verified, byte-identical landed fence — any
mismatch, or no fence found at all, fails closed with
`EXIT_VERDICT_BLOCK_MISMATCH`. No model-side retry (locked). Like
`--expect-verdict-block`, `--body-stdin`'s `'body'` field must NOT already
contain a fenced ` ```review-result``` ` block — this route constructs the
fence itself, so a pre-embedded fence is refused (`EXIT_VERDICT_BLOCK_USAGE`)
rather than silently doubled. See "Multi-fence verdict bodies" under
`loadout-merge` below.

**`--verdict-findings` — PRIMARY structured-body-construction route,
Forgejo AND GitHub parity:** supersedes `--verdict-review-status`'s
caller-supplied-prose shape for a reviewer that wants the strongest
guarantee. **The reviewer NEVER hands this tool a free-form body.**
`--body-stdin`'s JSON carries only `{"review_status": "clean"|"blocking",
"findings": [{"file", "line", "rule_id", "message"}, ...]}` — there is no
`body`/prose field on this route at all. This verb constructs the **entire**
comment body — a header line, one bullet per finding, then the tool-owned
fenced block — via `merge.verdict.build_findings_verdict_body` (which itself
calls `build_verdict_block`, not a re-implementation). A foreign reviewer's
narrative cannot appear in the posted body because there is no prose input
for one to hide inside: the good path is the only path (operator reframe —
"enforce good behavior over blocking bad behavior" — removing
the ability to do the wrong thing rather than instructing against it).
Requires `--verdict-head-sha`; mutually exclusive with
`--verdict-review-status` (usage error if both are given).
After the ordinary `post_and_verify` readback, the same per-field re-parse
`--verdict-review-status` performs runs here too.

**Foreign-block backstop, applies to BOTH verdict routes:** the
SECONDARY, fail-closed guard beneath the primary structured-body mechanism.
The per-field re-parse above only inspects `parse_verdict_block`'s
LAST-match result — a body carrying a correctly-tagged block AND a foreign
reviewer's block riding along earlier in the text would still pass that
check alone (a structural self-verify pass — "a correctly-tagged block is
present" — that a foreign reviewer's block could ride along inside
unnoticed). After the per-field re-parse, both routes now also call
`merge.verdict.assert_single_own_verdict_block` on the landed body: it must
carry **exactly one** fenced `review-result` block, tagged with the calling
`--caller`'s own reviewer id. Zero blocks, more than one block, or a block
tagged with a different reviewer all fail closed with
`EXIT_VERDICT_BLOCK_MISMATCH`. This backstop is intentionally secondary — it
exists for `--verdict-review-status`'s still-free-form-prose body shape, and
should shrink/retire as `--verdict-findings` becomes the sole route.

**`--delete-own-comment <COMMENT_ID>` — platform-aware self-delete:**
belt-and-suspenders self-delete of ONE already-posted
comment, routed to the resolved `--platform`'s own backend instead of
posting — `review.github_backend.delete_own_comment` for GitHub,
`review.forgejo_backend` (a thin adapter delegating to `transport.
git_host_api.delete_own_comment` verbatim) for Forgejo. Before this fix,
GitHub had NO self-delete CLI path reachable from `review-post` at all —
only `loadout-git-host-api`'s own `--delete-own-comment` existed, and that
verb is Forgejo-shaped only (its `_ISSUE_COMMENT_ID_RE` and base-URL
resolution assume the Forgejo REST API). A caller on a GitHub PR that needed
to clean up a duplicate/stub comment had no tool-owned route to do so. This
flag closes that gap with ONE entry point for either platform: refuses
unless (a) the comment's author matches the caller's own resolved identity
(cross-author delete is refused unconditionally — no override; human-comment
removal stays an operator action outside this tool entirely) and (b) the
comment body carries no fenced ` ```review-result``` ` verdict block (even
the caller's own — deleting a landed verdict could game the merge gate's
re-read: post clean, get read, delete, repost). Not PR-scoped: takes a bare
comment id, so the `pr_number` positional is optional when this flag is
supplied. Platform guard ordering is identical to the ordinary post route —
`assert_platform_is_github` / `assert_platform_is_forgejo` still run before
any credential is minted.

### `loadout-acquire` — platform-agnostic PR diff/content acquisition

`clagentic_loadout.acquire.verb`. Fetches a PR's diff and changed-file list
from the HOST API, on either Forgejo or GitHub, behind one contract
(`acquire.contract.AcquireBackend`) — **never a local working tree.**
`--platform` (`forgejo` | `github`) is mandatory, resolved the same way
`loadout-review-post` resolves it (e.g. via `platform_detect.resolve_platform`
against the target PR's own remote URL); the platform guard
(`assert_platform_is_forgejo` / `assert_platform_is_github`) always runs
**before** any credential is minted or any network call is made.

This exists because the read-only code-review gate and the security-review
gate each had to source a PR's diff themselves before either was landed —
and two independent failure modes resulted: a
reviewer defaulting to `git diff main..head` against a STALE local `main` ref
produced a bogus, far-too-wide diff; another reviewer's working directory was
a project wrapper (not a git repo itself), and its sandboxed `git` invocation
had no way to reach the actual checkout. Both are symptoms of one gap: diff
**acquisition** had no first-class, platform-agnostic `clagentic: loadout` entrypoint, so
every caller improvised its own (sometimes wrong) fetch. `loadout-acquire`
resolves `base_sha`/`head_sha` from the PR's own metadata as read from the
host API at fetch time — never a caller-supplied guess, never a
locally-resolved git ref — which is what makes wrapper-cwd and
stale-local-`main` non-issues **by construction**, not by convention: there is
no code path here that could reach a local git ref even if one happened to be
present and stale.

Without `--stage-scratch`, the JSON result on stdout carries `base_sha`,
`head_sha`, `changed_files` (a plain filename list — the same shape
`merge.diff_scope.check_diff_scope` already expects), and `diff_text` (the
whole-PR unified diff, suitable for a reviewer to read directly).

**`--stage-scratch` — scannable artifacts without a local checkout:** a
security scanner (gitleaks, semgrep, osv-scanner) does not operate on a diff
blob — it walks a directory tree. Without a writable scratch path reachable
from its own sandbox, a security-review caller had previously fallen back to
manual-only review whenever local repo access was blocked (the same
wrapper-cwd/stale-ref class of gap, one layer further downstream). This flag
additionally fetches each changed file's POST-CHANGE content from the host
API's own contents endpoint and stages it — together with the whole-PR diff
text — into a per-spawn `TMPDIR` scratch directory
(`acquire.scratch.write_scratch_content`) a scanner can be pointed at
directly; **never** anywhere in the repo tree (repo `CLAUDE.md` rule 7). The
JSON result additionally carries `scratch_root`, `scratch_diff_path`,
`scratch_files_dir`, and `scratch_written_files` (the repo-relative paths
actually written — a deleted file, a binary file the backend could not
decode, or an acquisition run without this flag contributes nothing here).
**Trade-off named:** scanning only the fetched changed files is narrower than
a real full-tree checkout scan (it misses cross-file context, e.g. a secret
pattern that only resolves against an untouched file elsewhere in the repo) —
accepted because the alternative, when a local checkout is genuinely
unreachable, is no scannable artifact at all. A caller that needs full-tree
scanning and DOES have local repo access should use a real checkout instead;
`repo_config.find_git_top_level_down_hop` is a bounded,
one-level DOWN-hop mirror of the existing UP-hop wrapper-config discovery
(`resolve_repo_config_root`) for a caller that genuinely needs a
LOCAL repo root resolved from a wrapper directory with no `.git` of its own —
lower priority than this verb, since API-based acquisition avoids local git
entirely.

### `loadout-push` — bot-attributed commit push + PR open/update

`clagentic_loadout.push.verb`. Pushes a branch's commits (re-authored to
a configured bot identity via `push.identity`), links an issue trailer
(`push.issue_link`, fails closed before any network call if the trailer
is missing/malformed when `--issue` is supplied), and opens or updates a
PR (`--update-pr`). Never merges, never pushes to a protected branch
(`HEAD`/`main`/`master`) — merge authority lives entirely in the merge
verb. Platform is auto-detected from the git remote
(`clagentic_loadout.platform_detect`) or supplied explicitly. Credential
injection uses `GIT_ASKPASS` with an isolated `HOME`; no token ever
appears in an argv, a log line, or a shell string.

**Supplying the PR body:** two mutually-exclusive paths,
exactly one required to create a PR:

- **`--body-env`** (RECOMMENDED) — a bare switch, no caller-supplied
  filesystem path anywhere. Reads a body already staged via
  `loadout-stage-body`'s identity-stamped API
  (`transport.body_env.stage_caller_body` / `read_caller_body_bytes`).
  On the create path, stage first with `--create-branch`, binding to the
  branch that will open the new PR:
  ```
  echo '{"body":"plain PR description text"}' | \
    loadout-stage-body --caller builder --create-branch $(git rev-parse --abbrev-ref HEAD)
  push --caller builder --title 'feat: add x' --body-env
  ```
  This verb resolves its OWN current branch (`push.git_coords.
  current_branch`) and requires the staged stamp's `create_branch` to
  match — a caller never types a branch name into this verb's own argv.
  On `--update-pr`, stage via `--target-pr <n>` instead (the same binding
  `loadout-git-host-api`/`loadout-review-post` already use); this verb then
  checks the stamp against `--pr`. A missing stage or a binding mismatch
  exits `EXIT_BODY_ENV_UNAVAILABLE`. **On `--update-pr`, supplying a body
  ALSO requires an explicit body mode — see `--replace-body`/`--append-body`
  below.**

  **The staged content is the `{"body": "..."}` JSON envelope, and this
  verb unwraps it — the PR body is the caller's original prose, never the
  wrapper.** `loadout-stage-body` always stages that envelope (the same
  shape `--body-stdin` requires); `--body-env`'s read side parses it and
  extracts the `body` field before opening/updating the PR, exactly as
  `--body-stdin` does, with no content-sniffing (malformed staged content
  fails closed with the same `BodyEmptyError` refusal `--body-stdin` uses,
  never a silent pass-through of unparseable bytes). A prior revision of
  this verb read the staged bytes verbatim instead — the resulting PR body
  rendered as the literal JSON wrapper (escaped newlines, braces and all);
  fixed to match every other `--body-env` reader's contract.

  **Why not a caller-supplied `--body-file <path>`:** an earlier version of
  this fix added exactly that flag (plain-text content, validated against
  a `$TMPDIR` scratch-boundary allowlist at read time). A security
  audit and an explicit operator correction rejected it: a validated
  arbitrary path still ACCEPTS a location parameter, and every containment
  check is one canonicalization edge case, one symlink race, one future
  refactor away from a bypass. The fix that shipped instead extends the
  ALREADY-SANCTIONED `--body-env` mechanism (which reviewer roles already
  use for the update/comment path) to also cover PR creation — the caller
  supplies body CONTENT (via `loadout-stage-body`'s own stdin), never a
  filesystem LOCATION, in either mode.
- **`--body-stdin`** — JSON (`{"body": "<PR description>"}`) read from
  stdin, for a caller that already has the body JSON-wrapped. An invalid-
  JSON `--body-stdin` error names `--body-env` directly, with the
  `loadout-stage-body --create-branch` invocation spelled out, as the
  RECOMMENDED next step — a caller that still prefers stdin directly is
  pointed at re-checking its own JSON shape, never at a raw
  `printf`/`echo`-redirect staging alternative (retired; see
  [docs/integration.md](integration.md)'s "Retired: hand-writing the
  staged pair" section for why).

`loadout-stage-body --create-branch` is now the sanctioned
mechanism for this verb's create path too: a PR-open call has no
`--target-pr` yet by definition, so it binds to the git branch that will
open the new PR instead — the SAME identity-stamped write API
(`--target-pr`) reviewer roles already use for the update/comment path,
extended rather than duplicated.

**`--update-pr` never carries a `head_sha` field — the create path is the
sole source of that value.** `--update-pr` is metadata-only by
construction (title/body PATCH; no push, no re-authoring) and its success
envelope now reports `pr_number`, `pr_url`, `owner`, `repo`, and an
explicit `"pushed": false` — no SHA field of any kind. A prior version of
this verb also emitted `head_sha` on the `--update-pr` path, computed via a
LOCAL `git rev-parse HEAD` in the caller's own working tree — formatted
identically to the create path's genuinely-pushed value, even though
`--update-pr` never touches the remote's git state at all. A caller
reading that field reasonably believed a SHA had landed on the remote when
this call never pushed anything (observed live: a PR's title/body updated
successfully while an unrelated local commit sat entirely unpushed, and the
envelope gave no signal). The fix removes the field rather than performing
a remote readback of a value this call did not itself produce — a readback
here would still misleadingly imply this call caused that remote state.
As a best-effort diagnostic, `--update-pr` also warns on stderr when the
local branch is ahead of its own remote-tracking ref — the exact situation
a caller is most likely to misread as "the metadata call also pushed my
commits."

**`--update-pr` with a body now requires an explicit mode — there is no
default.** Supplying `--body-stdin`/`--body-env` on `--update-pr` without
ALSO supplying exactly one of `--replace-body` / `--append-body` is a usage
error, refused before any token resolution or network call. A prior
version of this verb replaced the PR's ENTIRE existing body unconditionally
whenever a body was supplied — the only semantic it had — which silently
destroyed an existing PR body when a caller intended to append a short
follow-up note (observed live: a full measurement writeup replaced by a
two-sentence update). Per an explicit operator directive, the fix does not
pick a safer default (e.g. "append by default") either — only the caller
knows whether an edit is a deliberate revision or an addition, so any
default is the tool guessing at intent it does not have.

- **`--replace-body`** — REPLACE the PR's existing body wholesale with the
  supplied content. Destructive: the prior body is gone. This is the
  ORIGINAL (and, before this fix, only) behavior, now opt-in rather than
  automatic.
- **`--append-body`** — GET the PR's CURRENT body immediately before the
  update PATCH (`push.forgejo_backend.get_pr_body` /
  `push.github_backend.get_pr_body`) and concatenate the supplied content
  onto it with a blank-line separator, rather than replacing it.
  `update_pr()` itself on both backends is unchanged — still a single
  unambiguous whole-field PATCH; append is composed at the call site, not
  threaded into either backend as a mode parameter.

Omitting a body entirely on `--update-pr` (a title-only update) is
unchanged: the existing body is left untouched, and neither body-mode flag
is required — there is nothing ambiguous about a call that supplies no
body at all.

**Authoritative post-push remote readback (ADDITIVE — see
[docs/integration.md](integration.md#authoritative-post-push-remote-state)
for the full design writeup):** on the create-PR path (never `--update-pr`,
which performs no push), the success envelope now ALSO carries:

- `remote_head_sha` — the branch's HEAD SHA read back FROM THE REMOTE via
  `git ls-remote`, immediately after `git push` returns success. This is a
  genuine round-trip to the remote, never a re-read of the local working
  tree — unlike the pre-existing `head_sha` field (still present, unchanged
  on the create path, computed via a LOCAL `git rev-parse HEAD`), a caller
  cannot get a `remote_head_sha` value for a push that was never actually
  invoked; there is nothing to read back.
- `remote_head_sha_source` — always the literal `"git_ls_remote"` when the
  readback succeeded, `null` when it could not be performed (see below).
  A downstream consumer keys provenance validation off this field's
  PRESENCE, not off `remote_head_sha` alone — a bare SHA-shaped string is
  not enough to prove where it came from.
- `authorship_checked` / `authorship_matches` — present ONLY when a bot
  identity was in effect for this push (`--bot-name`/`--bot-email`, or the
  `builder_identity:` config below). A readback that only confirms the ref
  advanced would pass cleanly even if the landed commit carries the WRONG
  author (re-authoring is flag-contingent — see `push.identity`'s own
  module docstring); this checks the actual author of the commit the
  remote just confirmed it holds.

**Failure is never a new hard failure for an existing caller:** a
transient failure of this diagnostic re-read (e.g. a network blip in the
seconds after a successful push) does NOT fail an otherwise-successful
push+PR-open — `remote_head_sha`/`remote_head_sha_source` are `null` in
the envelope and a `WARNING` line is printed to stderr, but the process
still exits `EXIT_OK`. This is deliberate, additive-only scope: shipping a
verb that FAILS on a readback mismatch is a separate, config-gated
follow-up, so no existing external consumer of this verb sees a new
failure mode from this change alone.

**`builder_identity:` config wiring (closes a previously dead config
surface):** `push.identity_config.load_builder_identity` (a
USER-LEVEL `~/.config/clagentic/loadout/config.yaml` `builder_identity:`
section — see that module's own docstring) was, before this change,
validated ONLY by `loadout-doctor` and never actually consulted by this
verb — an operator could configure it, have doctor report the deployment
healthy, and still get operator-attributed commits, because nothing
downstream ever read the config doctor was checking. This verb now
consults it as a FALLBACK when `--bot-name`/`--bot-email` are both
omitted from the invocation's own argv (an explicit per-call flag pair
still always wins). OPT-IN: a deployment with no `builder_identity:`
section configured sees no change in behavior. A PRESENT-but-malformed
section fails closed (`EXIT_AUTHOR_MISMATCH`) rather than silently
falling back to no identity — a malformed config an operator believes is
active must fail loud, not produce false assurance.

**Caller-derived commit identity, unconditional for a recognized crew
caller on GitHub (no `builder_identity:` needed):** ahead of the
`builder_identity:` fallback above, this verb derives a bot commit
identity directly from the deployment's existing `github_app:` section
(`push.crew_identity`) for a `--caller` present in `github_app.callers`,
pushing to GitHub — see
[docs/provisioning.md](provisioning.md#derived-commit-identity-for-a-recognized-crew-caller-github-only)
for the full contract, the precedence order (`--bot-name`/`--bot-email` >
caller-derived > `builder_identity:` config > none), the deliberate
bot-badge-binding limitation and why it is a settled trade-off rather than
a gap, and why a recognized caller with an unresolvable identity FAILS
CLOSED (`EXIT_AUTHOR_MISMATCH`) rather than falling back to ambient git
config. Forgejo is unaffected by this tier — see that same section for
why.

**Known trap — a redundant PR-create can 409/422 even after a successful
push (documented, exit code unchanged):** if a PR for this branch's
head/base pair already exists, the PR-create call can return HTTP
409/422 even though the `git push` immediately before it already landed
successfully. This verb still exits `EXIT_PR_FAILED` in that case — the
exit code contract is unchanged, since silently returning `EXIT_OK` for a
call that did not confirm or return a PR number would itself be a new
failure mode for a caller relying on the current contract — but the error
message is unambiguous about WHICH part failed: when the post-push
readback above already confirmed a `remote_head_sha`, the message states
the push landed and names that SHA, plus a note that HTTP 409/422 likely
means a redundant create rather than a failed push. A caller that has
learned to distrust this verb's exit code (the documented origin of that
distrust) can now read the message instead of falling back to its own
local `git rev-parse` guess.

**`--force-with-lease` / `--no-force-with-lease` — explicit lease control, always
printed:** an explicit flag always wins over the auto-derived default (whether
bot-identity re-authoring rewrote this branch's commits — see "Caller-derived commit
identity" above). Before this control existed, `force_with_lease` was derived SOLELY from
that re-authoring signal with no override, and this verb never fetched the remote-tracking
ref first — silently forcing a lease evaluation against a **stale** local copy of the
remote's state on essentially every re-authored push, which can surface as a `(stale
info)` rejection classified `unknown` (see
[push-failure-reporting.md](push-failure-reporting.md#troubleshooting-stale-info)).
Whenever the resolved decision is to force, this verb now fetches the target branch from
the remote immediately before the push (best-effort: a fetch failure degrades to a printed
warning, never a hard refusal). The resolved `force_with_lease` value and its origin
(`cli-flag(--force-with-lease)`, `cli-flag(--no-force-with-lease)`,
`history-rewritten(auto)`, or `default-false(no-rewrite)`) are **always printed to stderr
before the push runs** — never inferred silently. See
[push-failure-reporting.md](push-failure-reporting.md) for the full failure-reporting
contract this pairs with, including the reject-reason parser that replaced the earlier
substring classifier and the `--verbose`/`--trace` flag below (the discoverable form of the
opt-in `CLAGENTIC_LOADOUT_PUSH_GIT_TRACE` passthrough).

**On a `git push` failure (`EXIT_PUSH_FAILED`), a caller gets the same evidence on TWO
channels, both on stderr — never only the coarse classification:** the
human-readable `push: git push failed (exit N, <sub_cause>): ...` line (which already folds
in the extracted `remote: `-prefixed / local-hook lines verbatim), immediately followed by a
single JSON line — the SAME structured fields documented in
[push-failure-reporting.md](push-failure-reporting.md) (`sub_cause`, `exit_code`,
`reached_transport`, `reject_reason`, `remote_lines`, `local_hook_lines`) — for a caller that
parses JSON instead of scraping text. Both channels are redacted through the identical
choke point (see [push-failure-reporting.md](push-failure-reporting.md#the-redaction-guarantee)).
This JSON line is stderr-only, never stdout — stdout carries JSON only on a successful push
(the `_run_create_pr`/`_run_update_pr` envelopes below), so a caller reading stdout-as-JSON
on success is never confused by a failure-path line appearing there too.

**`--dry-run` — a sanctioned diagnostic affordance, through the SAME minted credential
path:** performs a read-only `git push --dry-run` through the identical minted per-caller
credential resolution, the identical hermeticity pre-flight (`check_git_version`,
`check_repo_local_config_hazards`), and the SAME single git-push call site
(`push.git_push.git_push_with_token`, test-locked at
`tests/test_push_shared_git_push_entrypoint.py`) a real push uses — a dry-run that used a
second call site or skipped pre-flight could report success where a real push would refuse,
which is a misleading affordance, worse than none. **No ref is updated on the remote.** The
full transcript (stdout and stderr, including any `remote: `-prefixed sideband a real push
would also receive) is printed to stderr under the caller's own identity — this is the
sanctioned substitute for an agent shelling out to raw git under an ambient credential when
a push fails opaquely and the classified failure message alone isn't enough. `--dry-run`
skips PR creation/update and the post-push remote readback (nothing was actually pushed to
read back) and exits `EXIT_OK` once the dry-run attempt itself completes; a dry-run push
that would ITSELF be rejected still raises `GitPushError`/exits `EXIT_PUSH_FAILED` exactly
like a real push would, after the transcript is printed — proving what a real push would do
is the point. Ignored (has no effect) on `--update-pr`, which never pushes at all.

**`--verbose` / `--trace` — the discoverable form of `GIT_TRACE`:** enables git's own
verbose push output (`git push -v`) plus the GIT_TRACE packet/hook/transport trace, so a
failed push's phase — local hook / transport / remote negotiation / server hook — is
distinguishable without server-side log access. `--trace` is a synonym for `--verbose` (the
same flag, same destination), not a second mechanism. The `CLAGENTIC_LOADOUT_PUSH_GIT_TRACE`
environment variable keeps working as a compat alias — either one enables the identical
passthrough — but was, before this flag existed, absent from `--help` entirely, which was
the actual usability defect: an agent with a failing push and no discoverable verbosity
control had no sanctioned way to get phase-level detail short of reaching around this verb.
Every byte either flag surfaces passes through the SAME redaction choke point
(`push.push_redaction.redact_push_secrets`) every other push-failure field already uses —
see [push-failure-reporting.md](push-failure-reporting.md#the-redaction-guarantee) for
exactly what is and is not redacted.

**Hermeticity pre-flight — fails closed, exit `EXIT_HERMETICITY_FAILED` (32):** before any
credentialed git subprocess runs, this verb (via `push.git_hermeticity`) verifies the
resolved `git` version meets a minimum floor and inspects the target repository's
**local** `.git/config` for a hermeticity hazard (a repo-local `credential.*` entry, an
`http.*.extraheader` entry, an `includeIf.*` directive, or a `url.*.insteadOf`/
`pushInsteadOf` redirect rule) that ambient-credential neutralization cannot suppress by
environment isolation alone. Either condition refuses the push before any credential is
resolved or network call attempted, with no override flag — see
[push-hermeticity.md](push-hermeticity.md) for the full security contract: what ambient
credential machinery is neutralized on every credentialed call (including the
`$XDG_CONFIG_HOME/git/config` fallback global-config path), what cannot be neutralized and
is validated instead, and why.

### `loadout-merge` — the merge gate

`clagentic_loadout.merge.verb`. **This is the load-bearing release gate**
— the code that decides whether a PR actually lands. `--platform`
(`forgejo` | `github`, mandatory) selects only which backend fetches gate
facts and executes the merge call; the gate chain itself runs identically
on both platforms:

1. Namespace guard (config-driven allowed-namespace set; refuses before
   any credential mint or network call).
2. Merge-authority check (`merge.authority` — a fail-closed
   `AuthorityProvider` seam; see
   [docs/merge-authority.md](merge-authority.md) for the full
   identity-binding model, the fail-closed guarantee, how to configure your
   own attestation source, what the built-in fallback grants, and the
   git-host attestation mark posted after a successful merge).
3. Platform guard (both directions, fail-closed) → credential resolution.
4. Stale-head-SHA refusal (`--expected-head-sha` vs. the PR's live current
   head, read fresh from the resolved platform's own API).
5. Reviewer-verdict fences (`merge.verdict`) — for each
   `--required-reviewer name:login`, locate that login's latest PR
   comment, parse the fenced ` ```review-result``` ` block, refuse on
   missing/malformed/stale/blocking. **Multi-fence refusal, enforced by
   default:** see "Multi-fence verdict bodies" below.
6. Diff-scope cap (`merge.diff_scope`) — refuse a PR whose changed-file
   count exceeds `--max-changed-files`.
7. PR-title gate (`merge.title_gate`, Conventional Commits grammar; a
   logged `--skip-title-check` bypass exists).
8. **CI-status gate (`merge.ci_status`).**
   Reads CI evidence at the PR's HEAD from the resolved platform (Forgejo:
   combined commit status + `/actions/tasks` runner-activity evidence;
   GitHub: combined commit status + `check-runs`). **An EMPTY result (zero
   commit-status entries AND zero check/workflow runs) is an EXPLICIT
   PASS, not a missing gate** — many repos, this one included, have no CI
   runner wired up **by design** ("runner explicitly out of scope
   -> deletion not stub"), and a gate that failed closed on "no CI data"
   would falsely refuse every merge in such a repo. Observed directly on
   this repo's own history at a real PR's head commit: the combined-status
   endpoint returned an empty state and `/actions/tasks` reported
   `total_count: 0` — expected, not a red. A **non-empty** result gates on
   the real combined state: `"success"` passes; `"failure"` / `"error"` /
   `"pending"` / any other non-empty value refuses, reporting the actual
   state and evidence counts seen. Unreachable/non-200 from either endpoint
   is a `GateFactUnavailableError` exactly like every other gate-fact fetch
   here — a read failure is never conflated with the genuine empty-CI pass
   case. See `merge.ci_status`'s module docstring for the full decision.
9. Only if every gate above passes: execute the merge via the resolved
   platform's `merge_pr` (Forgejo or GitHub, both through the
   redirect-hardened shared transport).

Any gate failure refuses and exits non-zero; the merge is never attempted
on a partial pass. Both the Forgejo backend (`merge.forgejo_backend`) and
the GitHub backend (`merge.github_backend`) are landed and CLI-reachable
via `--platform`.

**`--merge-method` actually controls the merge:**
`--merge-method` (default `"merge"`) is forwarded verbatim to the resolved
backend's `merge_pr` — GitHub's own `merge_method` field, or Forgejo's `Do`
field. The flag also gates the branch commit-subject check (step 7b above):
that check's no-op-on-non-merge logic depends on the SHAPE that was actually
requested, so both the merge-method forwarding and the commit-subject gate's
own condition are derived from the same resolved value, never allowed to
silently diverge from one another.

Forgejo's `Do` field accepts `merge` / `squash` / `rebase` (the same three
caller-facing tokens GitHub's `merge_method` accepts — requesting the same
`--merge-method` value produces IDENTICAL semantics on either platform) plus
two Forgejo-only shapes with no GitHub equivalent: `rebase-merge` (a rebase
that keeps a trailing merge commit) and `manually-merged` (an out-of-band
"someone already merged this, just record it" marker). An unrecognized
value is refused BEFORE any HTTP call, naming the value and the full valid
set (`merge.forgejo_backend.VALID_DO_VALUES`).

**Requested-vs-actual merge-shape check (`merge.merge_shape`):** without
this check, a requested squash/rebase that silently landed as a real merge
commit (or vice versa) would be undetectable from inside the tool — the only
way to find out would be a manual `git merge-base` run out-of-band, after
the fact. Whenever `--repo-path` is given (see "Working-tree
sync after merge" below), immediately after step 10's tree-sync advances the
local tree onto the verified merged SHA, `loadout-merge` reads back that
landed commit's ACTUAL parent count (`git log -1 --format=%P`, against the
already-fetched local object database — no extra network round-trip) and
compares it against what the requested `--merge-method` predicts: `merge` /
`rebase-merge` predict >= 2 parents (a genuine merge commit); `squash` /
`rebase` predict exactly 1. `manually-merged` and any unrecognized value
carry no shape prediction at all and are silently skipped (never a false
mismatch report).

A mismatch is **logged to stderr by default, never a hard failure** — see
the trade-off named explicitly in `merge.merge_shape`'s own module
docstring: hard-failing by default, in the SAME release that finally makes
`--merge-method` actually work, would turn "the flag now finally works" into
"the flag now finally works, or your merge starts refusing," for every
existing caller that has (unknowingly, per this exact defect) been getting a
different shape than requested. A repo opts into the stricter behavior via
`merge: enforce_merge_shape: true` in its own
`.clagentic/loadout/config.yaml` (default `false`) — see
`merge.post_merge_config.resolve_enforce_merge_shape`. When enforcement is
on, a mismatch is `EXIT_MERGE_SHAPE_MISMATCH`. The check itself never runs
at all for a bare API-only merge (`--no-post-merge-tree` / `--skip-post-merge`
with no `--repo-path`) — there is no local object database to read a parent
count from without a synced tree, and this check deliberately does not add a
second platform-API round trip to cover that case (see `merge.merge_shape`'s
own docstring, "SCOPE").

**Multi-fence verdict bodies (`merge.verdict`, `merge.post_merge_config`):**
a reviewer-verdict comment body is expected to carry exactly one fenced
` ```review-result``` ` block. Both TOOL-OWNED emitters now REFUSE to
construct a body that would carry two: `transport.git_host_api`'s
`--expect-verdict-block` (`build_expected_verdict_body`) and
`review.verb`'s `--verdict-review-status` route both reject a `--body-stdin`
`'body'` field that already contains a pre-embedded fence, rather than
silently appending a second one on top — a caller-input-shape usage error
(`EXIT_VERDICT_BLOCK_USAGE`), fired before any network call. This closes the
PRODUCER side of a defect where a caller that had already hand-embedded its
own fence in `'body'` ended up with a comment carrying two identical fences,
which the pre-existing last-fence-wins parse (`merge.verdict.
parse_verdict_block`) validated cleanly — including via `--verify-comment`'s
own self-check.

On the CONSUMER side — `loadout-merge`'s own reviewer-verdict gate
(`merge.verdict.read_reviewer_verdict`) — a multi-fence body is now **a
hard refusal by default**: a selected reviewer comment carrying more than
one fenced block raises `VerdictMalformedError` → `EXIT_GATE_RESULT_BLOCKED`,
via `merge.verdict.assert_verdict_block_count_at_most_one`, checked BEFORE
the ordinary parse. THE EVIDENCE FOR THIS DEFAULT: under the pre-existing
last-fence-wins parse, a comment body carrying a `blocking` fence followed
by a `clean` fence resolves to `clean` — a gate-bypass primitive, not a
benign ambiguity, sitting at the gate that decides what lands on `main`.
This is deliberately the INVERSE of `enforce_merge_shape`'s
WARN-BY-DEFAULT trade-off, not a copy of it: `enforce_merge_shape` guards a
flag every existing caller had been silently getting wrong regardless of
what they requested, so hard-failing by default would have turned "the
flag now finally works" into "the flag now finally works, or your merge
starts refusing," for callers with no way to have seen it coming. Here,
both known tool-owned producers of a reviewer-verdict body
(`--expect-verdict-block` and `--verdict-review-status`, above) now REFUSE
to construct a multi-fence body in the first place — there is no known-good
caller left for a permissive default to protect, and a caller who is
*still* somehow producing one has been getting a silently mis-parsed
verdict every time, which is strictly worse than a loud failure with a
one-line documented opt-out.

A repo carrying legacy multi-fence comments it cannot immediately clean up
sets `merge: enforce_single_verdict_fence: false` in its own
`.clagentic/loadout/config.yaml` to opt OUT and restore the unconditional
last-fence-wins parse — see
`merge.post_merge_config.resolve_enforce_single_verdict_fence`. This key is
an **escape hatch for legacy comments**, not a safety toggle a caller must
remember to switch on: omitting it entirely gets the safe (enforced)
behavior, exactly like every repo that has never heard of this key.

`merge.verdict.assert_single_own_verdict_block` (the pre-existing
foreign-block backstop `review.verb`'s `--verdict-findings` /
`--verdict-review-status` emit-and-verify routes already call on their OWN
freshly-posted comment — see "Foreign-block backstop" above) now delegates
its count check to the same `assert_verdict_block_count_at_most_one`
primitive — one count implementation, two call sites (the emit-and-verify
backstop, and the enforced-by-default merge-gate consumer check above).

**Second-order: no self-correction path for an already-malformed
comment.** An agent cannot delete a verdict-bearing PR comment
(`--delete-own-comment` refuses unconditionally whenever the target body
carries a fenced block, by design — see "Self-delete-own-comment" below);
a comment that already landed with two fences before this fix shipped stays
on the PR permanently, superseded by a later, well-formed re-post but not
removable. This task's scope is the producer refusal and the enforced-by-
default consumer refusal above — a recovery mechanism for an ALREADY-malformed
comment (e.g. a same-author/same-SHA replacement exception, or preferring
the latest well-formed fence from a given reviewer over the first) is
explicitly OUT OF SCOPE here: it is a distinct, security-sensitive design
fork in its own right (any relaxation of the delete-refusal or the
comment-selection rule touches the exact anti-merge-gate-gaming property
`--delete-own-comment` and `read_reviewer_verdict`'s created_at-ordering
both exist to protect) and deserves its own dedicated review rather than
being folded in as a second concern alongside the fence-count fix. With the
producer refusal now shipped, the malformed shape is unreachable from either
tool-owned posting path going forward, which narrows this gap to
already-landed comments and any non-loadout-owned posting path — not
eliminated, but significantly smaller than before this task.

**Repo-config homes for the gate DECLARATION (schema + doctor
only — see [docs/provisioning.md](provisioning.md)'s "Merge-gate config
homes" section):** `--authorized-role`, `--required-reviewer`, and
`--max-changed-files` above are CLI flags today with no repo-config
default — a caller (a dispatch/lead layer) re-supplies them on every
invocation. `merge.pre_checks_config` and `merge.gate_config` now give a
repo a `.clagentic/loadout/config.yaml` `merge:` section home for that
same declaration (`pre_checks`, `merge_requirements`,
`required_reviewer_roles`, `authorized_roles`) plus a NEW pre-merge-check
gate link (`pre_checks`, run before step 9's merge call — not yet wired
into this CLI's own gate chain). `push.identity_config` and
`review.login_config` add matching deployment-tier homes for the identity
half (`builder_identity`, `review.reviewer_logins`). Wiring `merge.verb`'s
own flag DEFAULTS to read these values, and adding `pre_checks` as an
actual gate-chain link, are named follow-ups — this slice is the schema +
`loadout-doctor` validation only.

**Merge-completion attestation (`merge.attestation`):**
immediately after step 9 actually merges the PR — before any post-merge
step below — `loadout-merge` posts exactly one git-host-visible comment (PR
comment on Forgejo, PR review on GitHub) attesting that IT executed the
merge, via the SAME POST-and-verify comment transport `loadout-review-post`
already carries for each platform (`review.forgejo_backend.
post_and_verify_comment` / `review.github_backend.post_and_verify_review` —
reused, not a third implementation). The body is PURE git-host/product data,
lore-free (CLAUDE.md rule 6a): tool identity + version (`Merged via
clagentic-loadout vX.Y.Z`), the gated HEAD SHA and the SHA that landed, the
required-reviewer logins whose clean verdicts gated the merge (or `(none
required)`), and the CI-status gate's own already-computed disposition. No
lore references, no `LORE_*` env, no crew vocabulary — roles (reviewer
logins), never agent names. **Fail-open by design:** the merge above has
already succeeded by this point, so a failed attestation POST (network
error, non-2xx) is logged to stderr and swallowed — it never changes
`loadout-merge`'s exit code or blocks the post-merge steps below. See
[docs/merge-authority.md](merge-authority.md)'s "The git-host attestation
mark" section for the full field-by-field content and the rationale for
this being the one intentionally fail-open step in the gate chain.

**Post-merge steps (`merge.post_merge` / `merge.post_merge_config`):**
after step 9 above actually merges the PR — never on any refusal path —
`--repo-path DIR` opts into running an ordered list of steps in that local
working tree. Steps are read from `<repo-path>/.loadout/config.yaml`'s
`merge:` section, `post_merge_steps:` key (a list of `{cmd, description,
on_failure, detaches}` mappings). `--skip-post-merge` is an explicit, logged
opt-out even when `--repo-path` and a configured step list are both
present.

**`--repo-path` is an OPTIONAL override, never a silent skip:**
`--repo-path` is a caller-supplied working-tree root — a dispatcher with its
own project/task registry is expected to derive and pass it whenever a local
tree exists for the repo being merged; `loadout-merge` itself has no project
registry to derive one from (this package is orchestration-agnostic, CLAUDE.md
rule 2/6a). Before this fix, simply omitting `--repo-path` silently
downgraded to "no post-merge run attempted", exit `0`, with **no warning at
all** — even for a repo whose own committed config declares
`post_merge_steps`. That exact shape recurred multiple times across
deployments: a caller who forgot to type the
flag got a clean exit with zero steps run, indistinguishable from a
deliberate no-tree invocation. Omitting `--repo-path` now REQUIRES one of two
explicit acknowledgments, checked before any credential mint or network call:
`--no-post-merge-tree` (this invocation genuinely has no local tree — a bare
API-only merge) or `--skip-post-merge` (skip regardless of tree). Omitting
`--repo-path` with **neither** flag is `MergeUsageError` → `EXIT_USAGE` — a
caller must now say what it means, never rely on what a missing flag happens
to default to.

**Working-tree sync after merge, and landing on the base branch
(`sync_tree_after_merge`):** whenever `--repo-path` is given,
`merge.tree_sync.advance_repo_to_merged_sha` now runs **unconditionally** —
no longer gated on whether `post_merge_steps` are configured at all.
Immediately after this sync, the requested-vs-actual
merge-shape check described above runs against the now-verified landed SHA,
BEFORE any configured `post_merge_steps` entry — see "`--merge-method`
actually controls the merge" above for the full contract. Before
this, a repo with no `post_merge_steps` got no local sync whatsoever after a
merge: the tree stayed wherever the caller last left it (typically the
feature-branch HEAD that opened the PR), which repeatedly caused the NEXT
dispatch into that repo to branch from a stale base. `--skip-post-merge`
now **only** skips the configured steps — it no longer skips the sync too
(that conflation of one flag meaning two independent things was itself part
of the gap). After any configured steps run (or are skipped) against the
detached, verified tree, `merge.tree_sync.land_on_base_branch` moves the
tree **off** the detached HEAD onto the PR's base branch via
`git checkout -B <base_branch> <landed_sha>` — a ref repoint, never a
merge/rebase, pointed at the exact SHA `advance_repo_to_merged_sha` already
verified, so it can never diverge from the server-side merge result. The end
state is a local base branch updated to the merged tip, not a detached HEAD
and not a stale feature branch — exactly where the next dispatch into this
repo needs it.

A per-repo `merge:` section key, `sync_tree_after_merge` (boolean, default
`true`), is the only way to turn this phase off entirely — set it to `false`
to leave `--repo-path` exactly where the caller left it, restoring the
prior no-sync behavior. Read via
`merge.post_merge_config.resolve_sync_tree_after_merge`, the same `merge:`
section `git_working_tree` and `post_merge_steps` already read (no new
YAML-reading path) — absence at any level (no repo root, no config file, no
`merge:` section, or the key simply omitted) resolves to the default `true`;
a non-bool value or a malformed `merge:` section raises
`PostMergeConfigError` at load time.

Each step's `cmd` is either a shell-quoted **string** (`shlex.split`, always
executed `shell=False`) or an explicit **list of argv strings** — never a
shell. A plain cmd string containing a shell-operator token (`&&`, `||`,
`|`, `;`, `>`, `>>`, `<`) is **rejected at config-validation time** with a
clear error, rather than silently misparsed into a bogus literal argv token
(the defect the reference implementation's `shlex.split` + `shell=False`
handling had — fixed at port time, not carried across). A caller needing a
genuine multi-command sequence expresses it as **separate ordered
`post_merge_steps` entries**, or as a list-form `cmd`. A leading `VAR=VALUE`
token (or several) in either form is stripped as an env-assignment prefix
and applied via `env={**os.environ, **assignments}` — the same behavior the
reference implementation had, kept unchanged at port time.

`on_failure: warn` (default) logs a non-zero step and continues; `on_failure:
fail` raises immediately, and `loadout-merge` exits `EXIT_POST_MERGE_FAILED`
(28) — the merge itself already succeeded by this point, but a step-failure
this loud is deliberate: a broken post-merge install must surface, not fail
silently while an old binary stays on PATH.

**`detaches: true` — fire-and-forget steps (defense-in-depth):**
every step above normally runs via `subprocess.run(..., capture_output=True)`,
which waits for the child's stdout/stderr pipe to reach EOF. A step whose
command spawns a long-lived daemon that inherits fds 1/2 and holds them open
(a double-forked daemon that never closes/redirects its own inherited stdio)
never delivers that EOF — the wait blocks forever, AFTER the merge itself
already succeeded (observed: `loadout-merge` stuck 20+ minutes post-merge on a
daemon-spawning step). `detaches: true` (default `false`) opts a step out of
that capture-and-wait path entirely: it is launched via `subprocess.Popen`
with `stdin`/`stdout`/`stderr` redirected to `DEVNULL` and
`start_new_session=True`, and its exit is never awaited. Because a detached
step's exit code is never observed, `on_failure: fail` combined with
`detaches: true` is rejected at config-validation time
(`PostMergeConfigError`) rather than silently ignored — the two are a direct
contradiction ("fail the merge on this step's exit code" vs. "never wait for
this step's exit code"). Use `on_failure: warn` (the default) on a detached
step.

**`timeout_seconds` — bounded per-step timeout:** `detaches:
true` closes the "block forever" hang only for a step that flags itself
correctly. A step that spawns a daemon WITHOUT setting `detaches: true` (a
config-authoring mistake, or a step whose author did not anticipate its own
command forking) still hits the exact `communicate()`-blocks-forever failure
mode `detaches` exists to fix, and `loadout-merge` (and a standalone
`loadout-post-merge` re-run) would hang indefinitely with no way out. An
ORDINARY (non-`detaches`) step may set `timeout_seconds` (an int/float count
of seconds) to bound its own `subprocess.run` wait; on expiry,
`PostMergeStepTimeoutError` is raised — **always terminal**, regardless of
the step's own `on_failure` (an unbounded hang is not the kind of ordinary,
plannable non-zero exit `on_failure: warn` exists to tolerate) — and
`loadout-merge` exits `EXIT_POST_MERGE_FAILED` (28), the same code a
`on_failure: fail` step failure uses. A per-repo `merge:` section key,
`post_merge_step_timeout_seconds`, sets a **repo-wide fallback** applied to
any step that does not set its own `timeout_seconds`; a step's own value
always wins when both are set. **Both are OPTIONAL and default to no bound
at all** (`subprocess.run`'s own unbounded wait, byte-identical to before
this feature) — a shipped tool with external users whose steps legitimately
run long must not have an already-passing, merely slow step start failing
the moment this ships for a default it never chose; see
`merge.post_merge_config`'s own module docstring
("POST_MERGE_STEP_TIMEOUT_SECONDS") for the full trade-off. `timeout_seconds`
is rejected at config-validation time when combined with `detaches: true` —
a detached step is never awaited, so there is nothing for a timeout to
bound; use `liveness_probe` (below) instead.

**`liveness_probe` — verified liveness for `detaches: true` steps:**
`detaches: true` alone proves only that this executor's own
`Popen` call returned — never that the daemon the step intended to
(re)start is actually up. A `detaches: true` step may declare an OPTIONAL
`liveness_probe` mapping:

```yaml
liveness_probe:
  cmd: ["cat", "/var/run/myservice/heartbeat"]   # generic argv, same
                                                   # shell-operator-free
                                                   # resolution as a step's
                                                   # own cmd
  poll_interval_seconds: 5   # default 5
  max_polls: 6               # default 6; must be >= 2
```

After the detached `Popen` call returns, `probe.cmd` is polled every
`poll_interval_seconds`, and liveness is confirmed the moment two
CONSECUTIVE samples are both non-empty and DIFFER — i.e. the probe's own
reported value **advanced across one poll interval**, not merely that time
passed. This is the task's own endorsed formulation over a fixed wall-clock
wait: a fixed wait either flakes on a slow restart or wastes time waiting
past an already-live daemon, while an observed advance is genuine evidence
the daemon did something between the two samples. If no advancing pair is
observed within `max_polls` samples, `PostMergeLivenessError` is raised —
**always terminal** (same reasoning as `PostMergeStepTimeoutError`) — and
`loadout-merge` exits `EXIT_POST_MERGE_FAILED` (28).

`merge.post_merge` has **no built-in notion** of what a "heartbeat" is, what
a "sentinel" is, or where either lives — `liveness_probe.cmd` is an
arbitrary, consumer-supplied argv: reading a heartbeat file's contents,
checking a `systemctl show` timestamp field, or curling a health endpoint
are all expressible as an ordinary argv command a caller already knows how
to write for its own daemon; loadout only supplies the generic
poll-and-compare mechanism. `liveness_probe` is rejected at
config-validation time on a step that does not also set `detaches: true` —
an awaited step's own exit code already answers the success/failure
question a liveness probe exists to answer for a fire-and-forget step.
Absent `liveness_probe` on a `detaches: true` step (the default): no
verification at all, byte-identical to before this feature.

**Environment inheritance:** a step with no leading `VAR=VALUE`
prefix runs with `env=None`, which means `subprocess.run` inherits the
CURRENT process's environment (`os.environ`) unchanged — including `HOME`.
`.clagentic/loadout/config.yaml` is REPO-LOCAL COMMITTED config (see
`merge.post_merge_config`'s module docstring); it can never carry a
machine-specific `HOME` value (CLAUDE.md rule 1), so a step like this repo's
own `scripts/install.sh` (which fails fast with exit 1 rather than silently
resolving a root-relative path when `HOME` is empty/unset — see that
script's own fail-fast guard) depends on either the INVOKING process
already having a real `HOME` set, or the deployment env-override seam
below.

**Deployment env-override seam:** the supported, released
mechanism for a deployment to inject an env var (e.g. `HOME` in an
isolated-HOME spawn harness) into every configured step WITHOUT
hand-editing that machine value into this repo's own committed
`.clagentic/loadout/config.yaml`. `merge.post_merge_config.resolve_env_overrides()`
(called automatically by `loadout-merge` before running any configured
steps) resolves a flat `{VAR: value}` mapping from two OPTIONAL,
deployment-owned sources — mirroring the EXACT precedent
`transport.provider_config`'s credentials tier and `transport.git_host_api`'s
git-host base-URL tier already established (same user-level file, same
loader, same env-wins-over-file precedence):

1. **`CLAGENTIC_LOADOUT_POST_MERGE_ENV_<NAME>`** env vars — e.g.
   `CLAGENTIC_LOADOUT_POST_MERGE_ENV_HOME=/root` injects `HOME=/root` into
   every step. Read directly from the invoking process's own environment —
   a spawn harness supplies this the same way it supplies any other env
   var, no file write required.
2. **The user-level config file's `post_merge_env:` section** —
   `~/.config/clagentic/loadout/config.yaml` (the SAME file the git-host
   base-URL and credentials tiers already use), a flat mapping:

   ```yaml
   post_merge_env:
     HOME: /root
   ```

   This file lives entirely OUTSIDE any repo checkout — it is never
   committed and therefore categorically excluded from both this repo's own
   tracked `.clagentic/loadout/config.yaml` (rule 1) and this repo's
   published tree (which only ever carries tracked repo content, CLAUDE.md
   rule 8).

Tier 1 wins over tier 2 per-variable. Resolved overrides are layered UNDER
each step's own inline `VAR=VALUE` prefix — a step's explicit, repo-local,
committed assignment always wins for the same name; the deployment seam
only fills gaps the invoking process's own environment does not already
cover. Both tiers are OPTIONAL and independent of `post_merge_steps` itself:
a deployment that configures neither sees `env=None` passthrough, exactly
as before this feature existed. See
[docs/integration.md](integration.md)'s spawn-env checklist for the
worked HOME example, and `merge.post_merge_config`'s module docstring for
the full design and the trade-offs considered (a per-step `env:` config
field, and a `.loadout-local` overlay file, were both weighed against this
and rejected — see that docstring).

This repo's own `.clagentic/loadout/config.yaml` (moved from
`.loadout/config.yaml` as part of the path-migration cutover) configures
exactly one step: `scripts/install.sh`, `on_failure: fail`, so a merge into this repo
self-installs the new build (`loadout-git-host-api` on PATH, `config.yaml`
`git_host.base_url` seeded) into whatever environment invokes
`loadout-merge --repo-path <this checkout>` next.

### `loadout-close-pr` — close a PR WITHOUT merging it

`clagentic_loadout.merge.close_verb`. Abandons a superseded/dead PR: issues
a platform-aware `state=closed` PATCH via either the Forgejo or GitHub API.
**Never merges, never lands a diff** — this is the tool-owned counterpart to
clicking "Close" on a PR a release process has decided not to land, not an
alternate merge path.

```
loadout-close-pr --role merger --platform forgejo \
    --repo some-owner/some-repo --pr 42 \
    --authorized-role merger
```

**Gate chain — deliberately NARROWER than `loadout-merge`'s full chain**
(closing abandons a PR; it never lands code on the target branch, so the
stale-SHA, reviewer-verdict, diff-scope, PR-title, and CI-status gates that
exist to protect what lands on `main` do not apply here). What DOES carry
over unchanged, because an abandonment action still needs the same
authorization posture a landing action does — both are PR-terminal actions:

1. Namespace guard (`push.namespace_guard`, reused verbatim) — runs first,
   before any credential or network call.
2. Merge-authority check (`merge.authority`) — the SAME `AuthorityProvider`
   seam `loadout-merge` consumes; a close is refused for the identical
   reason a merge would be, if the calling role has no configured authority
   over this owner/repo/PR.
3. Platform guard (both directions, fail-closed) → credential resolution —
   the SAME credential seam every other `clagentic: loadout` verb resolves a git-host
   token through.
4. Execute the close via the resolved backend's `close_pr` (Forgejo or
   GitHub) — `MergeExecutionError` on any non-2xx response or network
   failure, never a silent partial success.

**Flags:** `--platform` (`forgejo` | `github`, required), `--repo`
(`owner/repo`, required), `--pr` (PR number, required), `--role` (whose
authority is checked and whose token is resolved; defaults to the
credential-provider's default role), `--authorized-role` (repeatable — a
role permitted to hold close authority when no external
`AuthorityProvider` is injected), `--git-host-base-url` (Forgejo API base
URL override; ignored for GitHub), `--allowed-namespace` (repeatable —
restrict the close target owner to this namespace).

**No `--body-stdin`/`--body-env` of any kind:** the `{"state": "closed"}`
body is constructed by the two backend `close_pr` functions themselves,
in-process — there is no shell-visible body payload anywhere in this
verb's own invocation.

Exit codes: `0` OK, `1` usage error, `2` token-fetch failed, `4` wrong
platform, `20` namespace denied, `21` authority denied, `26` close failed.

### `loadout-post-merge` — standalone post-merge re-run for an already-merged PR

`clagentic_loadout.merge.post_merge_verb`. Closes a gap `loadout-merge`
itself cannot: `loadout-merge`'s own `post_merge_steps` tail (see above) only
ever runs inside the SAME invocation that just executed the merge. When a
merge succeeds but its post-merge deploy step fails, hangs, or was never
attempted (`--no-post-merge-tree` / `--skip-post-merge`, or a
`--repo-path`-less bare API-only merge), there was previously no way to
re-fire `post_merge_steps` for that PR afterward without re-running the
full merge gate chain — and `loadout-merge` re-invoked against an
already-merged PR correctly no-ops the merge itself, but still requires
re-satisfying every upstream gate (stale-SHA, reviewer verdicts, diff-scope,
title, CI-status) that made sense before the merge decision, not after one
was already made and recorded.

```
loadout-post-merge --role merger --platform forgejo \
    --repo some-owner/some-repo --pr 42 \
    --repo-path /path/to/checkout --authorized-role merger
```

Runs only the two gate links that matter for a re-run — namespace guard and
merge-authority (the SAME `merge.authority` seam and `--authorized-role`
flag `loadout-merge`/`loadout-close-pr` both already consume: a role
authorized to merge/close a PR is authorized to re-run its post-merge
deploy) — then reads the target PR's LIVE state from the resolved
platform's own API and **positively confirms it is actually merged**
(`merged: true`) before doing anything else. A PR that is still open, or was
closed without merging, is refused (`EXIT_PR_NOT_MERGED`) — never silently
treated as an already-satisfied no-op. Only then does it advance
`--repo-path` to the merged SHA (the SAME `merge.tree_sync.
advance_repo_to_merged_sha` `loadout-merge`'s own post-merge step already
calls) and run `post_merge_steps` (the SAME `merge.post_merge.
run_post_merge_steps` + `merge.post_merge_config.load_post_merge_steps`
`loadout-merge` already calls) — no execution-path logic is duplicated,
only re-sequenced around a narrower gate set. `--repo-path` is **required**
on this verb (unlike `loadout-merge`'s optional, acknowledgment-gated flag)
— there is no "skip" shape for a verb whose entire purpose is running
`post_merge_steps` against a local tree; skipping is simply not invoking it.
The SAME `post_merge_step_timeout_seconds` repo-tier default and per-step
`timeout_seconds`/`liveness_probe` keys (see "`timeout_seconds` —
bounded per-step timeout" above) apply identically here — a standalone
re-run gets the exact same bounded-timeout/verified-liveness behavior a
merge-embedded run would have, via the SAME
`merge.post_merge_config.resolve_post_merge_step_timeout_seconds` call
`loadout-merge`'s own post-merge tail makes.

Never calls a merge or comment endpoint — only a PR-info read, then local
git/subprocess operations. Mirrors `loadout-close-pr`'s own precedent for a
PR-terminal-adjacent action that intentionally runs a narrower gate subset
than a full merge (see that verb's own module docstring, "SCOPE —
deliberately NARROWER than merge.verb's full gate chain") rather than adding
another flag to `loadout-merge`'s own 700+-line gate chain.

### `loadout-release-detect` / `loadout-release-dispatch`

`clagentic_loadout.release.detector` / `clagentic_loadout.release.dispatch`.
Tag-triggered release detection and an HMAC-signed "task shipped" event
dispatcher. Endpoint, secret, and dispatcher name are all caller-supplied
configuration — this module never bakes in a specific host or dispatcher
literal, and treats an unrecognized `task_id` as a safe no-op rather than a
failure. `loadout-release-dispatch`'s same-repo case (deriving `task_id`
from a merged PR body's `Task: <id>` trailer) takes the body text via
`--merged-pr-body-stdin` (stdin content) — never a caller-named file path.
This module is deliberately git-host-agnostic and never fetches a PR body
itself; a caller wanting the same-repo case fetches the body from the git
host first (e.g. `loadout-git-host-api GET .../pulls/<pr>`, reading its
`.body` field) and pipes it in.

### `loadout-provision-allowlist` — per-role permission-allowlist generator

`clagentic_loadout.provisioning.cli`. Given `--role` (and, optionally,
`--repo-root` to read that repo's `.clagentic/loadout/config.yaml` `roles:`
section),
emits that role's Bash permission-allowlist fragment: both `Bash(<verb>:*)`
and `Bash(<verb> *)` for every verb the role declares. PER-ROLE only — there
is no global, all-verbs fragment. Default behavior prints the fragment
(safe, copy-pasteable); `--write --settings-file PATH` opts into an
idempotent in-place JSON merge instead. See
[docs/provisioning.md](provisioning.md) for the full agent-provisioning
workflow this verb is one step of.

### `loadout-poll-wait` / `loadout-scoped-test-wait`

`clagentic_loadout.wait.cli`. File-poll and long-running-test-wait
primitives used by other verbs' callers; not gate logic themselves.

### `loadout-doctor` — deployment-conformance check suite

`clagentic_loadout.doctor.cli`. **Read-only**: never mutates config, never
mints or consumes a real credential — safe to run repeatedly, including in
CI. Motivated by a real incident: the `github_app.slugs`
config seam shipped in code but was never provisioned on disk in a live
deployment, and nothing checked that gap until it produced a deterministic
identity-resolution failure in production. `loadout-doctor` is the
conformance suite that would have caught it, inspecting a deployment's
ACTUAL on-disk/env state against the config seams
`transport.provider_config` and `transport.github_app_config` already
define, always reporting RESOLVED values rather than a guess:

1. **Credentials** — for each platform (Forgejo, GitHub), resolves the
   configured `token_command_forgejo` / `token_command_github` helper (same
   precedence `transport.provider_config.resolve_platform_provider` uses),
   confirms the executable exists, is executable, and is not
   world-writable, then runs it with a fixed, non-real probe caller
   (`loadout-doctor-probe` — never a real role name) and classifies the
   outcome. A helper that runs and refuses the probe caller is reported
   `OK` (`downstream-refusal`) — that is the expected shape for a correctly
   configured helper, not a defect. A platform left at the `static`
   default (no command helper configured) is reported `OK`
   (`not-configured`) — there is nothing to validate.
2. **`github_app.slugs` coverage** — the exact gap the incident above exposed: every caller the
   deployment's own role taxonomy declares (`provisioning.roles.
   load_role_verbs`) must have a `github_app.slugs.<caller>` entry once ANY
   per-caller slugs map is configured at all. Flags both directions: a
   declared caller with no slugs entry (the failure), and a slugs entry
   with no matching declared caller (reported for visibility, not a
   failure — a slugs map may legitimately serve a wider caller set than one
   repo's own loaded role taxonomy). A deployment that never configures a
   per-caller map (single global `slug`, or nothing) is a no-op pass. This
   is `clagentic: loadout`'s half of the App-vs-agent coverage question: it confirms
   every caller this DEPLOYMENT's own
   config declares has a slug to resolve its bot login from. The paired
   half — that the slug a broker actually issues for a role MATCHES that
   role's CONFIGURED `app_slug` at credential-mint time, so a wrong-App
   fallback can never silently mint a token under the wrong App's identity
   — is a mint-time enforcement point in this package's reference
   deployment (a gatekeeper-style minting service), outside `clagentic: loadout`'s own
   task boundary; `check_github_app_slugs_coverage` here validates the
   CONFIG SHAPE this package's own `github_app_config.resolve_github_app_slug`
   reads, it does not (and cannot, being a local, read-only conformance
   check) verify what a broker issues at mint time.
3. **Per-repo `.clagentic/loadout/config.yaml` schema** (`--repo-root`) —
   validates each known top-level section (`wait:`, `roles:`, `merge:`)
   through the SAME loader/validator the verb that actually reads that
   section at runtime uses (`wait.config.load_scoped_test_patterns`,
   `provisioning.roles.load_role_verbs`,
   `merge.post_merge_config.load_post_merge_steps`,
   `merge.pre_checks_config.load_pre_checks`, and `merge.gate_config`'s three
   loaders — the last two cover `merge.pre_checks`,
   `merge_requirements`/`required_reviewer_roles`/`authorized_roles`, all
   under the existing `merge:` section, see
   [docs/provisioning.md](provisioning.md)'s "Merge-gate config homes"
   section) — never a second, doctor-only schema that could drift from what
   "valid" means to the real consumer. Also flags a `credentials:` section
   (repo-local credentials config is never honored) as a
   conformance finding rather than only a runtime stderr warning. A legacy
   `.loadout/` marker dir (no config at the new path, or a config.yaml only
   present there) is a WARN, not a failure — migration-incomplete signal,
   removed once every repo finishes migrating onto the new path.
4. **`builder_identity` / `review.reviewer_logins` deployment-tier config**
   (no `--repo-root` needed — always runs) — validates the
   USER-LEVEL config.yaml's `builder_identity:` section
   (`push.identity_config`) and `review.reviewer_logins:` map
   (`review.login_config`) through the SAME loaders `push.verb`'s commit
   re-authoring and a `--required-reviewer` login-override resolution would
   use. Both sections are OPTIONAL; a deployment that configures neither is
   a no-op pass. See [docs/provisioning.md](provisioning.md)'s "Merge-gate
   config homes" section for the full schema and the repo-tier vs.
   deployment-tier design calls.

Exits `2` (`EXIT_CHECKS_FAILED`) if any check's outcome is a failure, `0`
if every check passes. `--repo-root` opts into the schema check and scopes
the slugs-coverage check's role taxonomy to that repo; omitting it skips
the schema check and falls back to the built-in reference role mapping for
slugs coverage. The deployment-tier identity check (item 4 above) always
runs, with or without `--repo-root`.

## Not yet landed

- `clagentic-loadout` (the bare top-level CLI, `clagentic_loadout.cli`) is
  still a stub — verb subcommand dispatch under one binary has not been
  wired up yet. Today each verb is its own console-script entry point.
- No installer, no packaged release, no version tag.

## Live-cast cut-over

Every verb above is **built and proven inside this package** (ported from
its pre-existing reference implementation, with its own test coverage).
Wiring a verb to a live, running agent cast — the step where a real
deployment's agents actually invoke these console scripts instead of their
pre-existing reference scripts — is tracked per verb.

`loadout-merge`: SHADOW-PARITY proven — replaying a real,
already-merged PR's gate facts (SHA-stamp, both reviewer verdict fences,
diff-scope, title) through `merge.verb`'s own gate chain reached the
identical decision the deployment's reference merge tool reached on that
same PR, with zero deltas (see `tests/test_merge_shadow_parity_pr42.py`).
The CI-status gate (added after the original
shadow-parity proof landed) is proven against that same PR's real,
empty CI-status evidence — the gate's explicit no-runner-by-design pass
case — plus runner-wired failure/pending negative controls, in the same
test module (`TestCiStatusGateAgainstPR42Facts`).
`.clagentic/loadout/config.yaml`'s `merge.post_merge_steps` is
already configured so a `loadout-merge --repo-path <this-checkout>` invocation
self-installs the merged build. The remaining step — pointing the live
merger role's dispatch at `loadout-merge` instead of its pre-existing
reference merge tool as the PRIMARY entrypoint — is agent-contract
configuration (which command the merger's own operating contract names, and
the `Bash` allowlist regex that admits it) that lives outside this package's
task boundary; see this repo's CLAUDE.md "Task boundary" section. That
config already allowlists `loadout-merge` additively; flipping it to
primary and retiring the reference tool from the merger role's dispatch is a
follow-up in the deployment's own agent-contract repo, evidence-gated per
the RETIRE discipline referenced above.

## Credentials

Every verb above resolves its git-host token through one seam:
`clagentic_loadout.transport.credential_provider.resolve_token(role,
provider)`. See the README's "Credential provider seam" section for the
standalone-vs-minted distinction between the Forgejo and GitHub paths, and
its "Per-platform provider selection" section for how a deployment points
each platform at its own provider via config. See
[docs/integration.md](integration.md) for the consumer-facing runtime
contract: exactly what a spawn environment must supply (env vars,
config-file tier, precedence, defaults) for these resolutions to succeed.

In production use (no `token_provider` injected — the CLI default), every
verb resolves the provider for the request's platform through
`clagentic_loadout.transport.provider_config.resolve_platform_provider`, the
single factory every verb's token resolution goes through:

- `git-host-api` is Forgejo-only, so it always resolves the Forgejo
  platform's configured provider.
- `review-post` and `merge` resolve the provider for the platform given via
  the (mandatory) `--platform` flag, at the same call site that already
  runs the platform guard before any credential is minted.
- `push` resolves the provider for the platform it has already determined
  (either `--platform` or git-remote auto-detection) before either of its
  token-resolution call sites (create-PR and update-PR).

`resolve_platform_provider` defaults to `StaticTokenProvider` per platform
(unchanged behavior) unless a deployment configures
`CLAGENTIC_LOADOUT_TOKEN_PROVIDER_FORGEJO` / `_GITHUB` (env) or the
`credentials:` section of the **user-level**
`~/.config/clagentic/loadout/config.yaml` (config file) to select `command`
— see the README for the full precedence rule and the `CommandTokenProvider`
command contract (argv shape, role substitution, fail-closed error
reporting). A repo-local `.clagentic/loadout/config.yaml` `credentials:`
section is never consulted for provider selection and is rejected with a
stderr warning when present.

### Optional repo context for repo-scoped minting

`resolve_token(role, provider, *, repo=None)` carries an OPTIONAL `repo`
("owner/repo") string end to end, for a repo-scoped minting provider — a
GitHub App installation-token mint, for example, is scoped to one specific
repo and cannot be minted correctly without knowing which:

- `push`, `review-post`, and `merge` each resolve `owner/repo` before they
  reach their own credential-resolution call site anyway (namespace guards,
  platform guards, the merge gate's authority/stale-SHA checks all need it
  too), and pass it straight through.
- `git-host-api` derives `owner/repo` from the request PATH via the same
  `_REPOS_PATH_RE` already used for its known-bad-owner check, passing
  `None` for a path that is not repo-scoped (e.g. `/api/v1/user`).
- `CommandTokenProvider` opts in via a `{repo}` argv placeholder (e.g.
  `mint --role {role} --repo {repo}`), substituted the same argv-token-only
  way `{role}` already is — never into a shell string. A configured command
  with **no** `{repo}` placeholder is byte-identical to before this
  feature, regardless of whether the calling verb has repo context. A
  command **with** `{repo}` but invoked with no repo context fails closed
  with a resolved-values error, never a literal unsubstituted `{repo}`
  string reaching the exec'd command.
- Backward compatibility: a pre-existing custom `TokenProvider`
  implementing the old `resolve_token(self, role)` signature keeps working
  unmodified — `resolve_token()` inspects the resolved provider's own
  signature (not a fragile try/except retry) to decide whether it can
  accept `repo` at all before ever passing it. See
  `transport.credential_provider`'s module docstring for the full trade-off
  this compatibility mechanism was chosen for.
- Argv-level option-injection guard: `CommandTokenProvider` validates both
  `role` and `repo` against a bare-token grammar (`role` —
  `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$`, mirroring `git_host_api`'s own
  `--caller` validation and `StaticTokenProvider`'s role grammar; `repo` —
  `^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$`) BEFORE
  substituting either into the configured argv. `shell=False` alone rules
  out shell-metacharacter injection, but a leading `-` is a normal argv
  byte a getopt/argparse-style minting CLI would parse as a flag — this is
  enforced at the provider itself (the seam), since not every verb's
  `--caller`/`--role` pre-validates its own input the way `git_host_api` does.

This is also the piece a "both platforms, one mint command" convergence
needs (see the README's per-platform provider selection rationale): once a
minting command can resolve a token for a specific `owner/repo`, pointing
both platforms' `credentials:` config at the same `{role}`/`{repo}`-templated
command is a config-only change, with no code change in `clagentic: loadout` required.

## Guard policy

`clagentic_loadout.guard` — a harness-agnostic policy library, not a
console verb: a Bash command classifier, a Write/Edit scope enforcer, a
credentials guard, a task-dispatch guard, and a git-operation guard, plus
single-source/dual-sink settings-fragment generation, consumed by a
caller's own PreToolUse-style hook and settings/allowlist generator. See
[docs/guard-policy.md](guard-policy.md) for the full policy contract: every
`guard.*` category, its API, its config shape.
