# Agent provisioning

`clagentic: loadout` installs its verbs to PATH, but PATH visibility alone does not let a
spawned agent actually CALL them — most harnesses gate every `Bash` invocation
behind a permission allowlist, and a verb that isn't on it hits an interactive
permission-prompt wall (or is refused outright in a non-interactive spawn).

This is the missing piece: how an integrator declares which `clagentic: loadout` verbs a
given agent ROLE invokes, generates that role's permission-allowlist fragment,
and lands it in their harness's settings.

This document covers the ALLOWLIST side (which verbs a role may invoke). See
[docs/integration.md](integration.md) for the CREDENTIALS side: what a spawned
agent's runtime environment must actually supply (env vars, config-file
tier, precedence, defaults) for those allowed verbs to succeed once invoked.

**Roles, never agent names.** Every piece of this contract is keyed on a bare
role string (`builder`, `reviewer`, `merger`, `lead`, or any role name an
integrator invents) — never on a specific agent's identity. A deployment with
ten differently-named agents that all act as "builder" shares one role
declaration and one generated fragment.

**Per-role, never global.** There is no single flat allowlist covering every
`clagentic: loadout` verb. Each role gets its own fragment, containing only the verbs that
role declares. A deployment with a `builder` role and a `merger` role
generates and lands two separate fragments — merging both is the
integrator's own harness-config decision, not something this tooling does for
them.

## Step 1 — declare the role's verb set

Add a `roles:` section to the target repo's `.clagentic/loadout/config.yaml`
(the same sectioned config file `wait:` and `credentials:` already use — one
file, one section per verb/feature; legacy `.loadout/config.yaml` is read as
a transitional fallback with a deprecation warning):

```yaml
roles:
  builder:
    - push
  reviewer:
    - git-host-api
    - review-post
    - stage-body
  merger:
    - merge
    - push
    - release-dispatch
    - release-detect
  lead:
    - git-host-api
```

Verb labels are the same names used throughout [docs/verbs.md](verbs.md) and
the `clagentic-loadout <verb>` umbrella dispatch (`push`, `review-post`,
`merge`, `git-host-api`, `stage-body`, `release-dispatch`, `release-detect`,
`poll-wait`, `scoped-test-wait`).

A repo with **no** repo-local config file, or one with no `roles:` section,
falls back to a built-in reference mapping covering the four roles above —
this is *a* default, not *the* role taxonomy. A repo that declares its own
`roles:` section **replaces** the default mapping entirely; a role you omit
from your own config is simply not provisioned, rather than silently
inheriting a default entry you never asked for.

An unknown role or an unknown verb label in `roles:` is a hard, resolved-values
error at config-load time (never a silently-empty fragment) — the error names
the bad value and the full known-good set (known roles, or known verbs).

## Step 2 — generate the role's allowlist fragment

```sh
clagentic-loadout provision-allowlist --role builder --repo-root /path/to/repo
```

or the standalone console script:

```sh
loadout-provision-allowlist --role builder --repo-root /path/to/repo
```

Default behavior **prints** the fragment as a JSON array to stdout — safe,
side-effect-free, copy-pasteable:

```json
[
  "Bash(loadout-push *)",
  "Bash(loadout-push:*)"
]
```

Every verb the role owns contributes both forms (`Bash(<verb>:*)` and
`Bash(<verb> *)`) — different harnesses record a Bash tool call in either
shape, so both are needed for reliable matching.

`--repo-root` is optional; omit it to generate against the built-in reference
mapping only (no repo-local config lookup).

## Step 3 — land the fragment in the harness's settings

The fragment is a list of entries for whatever key your harness's permission
allowlist lives under (e.g. Claude Code's `permissions.allow` in
`settings.json`). Two ways to land it:

**Copy-paste (always safe):** take the printed JSON array and merge it by hand
into your settings file's allowlist.

**`--write` (opt-in, idempotent):**

```sh
loadout-provision-allowlist --role builder --write --settings-file /path/to/settings.json
```

`--write` merges the fragment into `<settings-file>`'s `permissions.allow`
array in place: existing entries are never duplicated, reordered, or removed,
and a missing file is created with the minimal shape needed. Running it again
for the same role (or landing a second role's fragment into the same file
afterward) is safe — nothing already present is disturbed.

### Where the settings file lives

`--settings-file` resolution, highest precedence first:

1. `--settings-file PATH` (explicit flag)
2. `CLAGENTIC_LOADOUT_SETTINGS_FILE` (env var override)
3. A `HOME`-derived default: `~/.config/clagentic/loadout/settings.json`

An environment with `HOME` empty or unset (a real, expected shape for many
agent-spawn environments) and neither override set is refused outright, with
every resolved input named in the error — the same empty-`HOME` fail-fast
discipline `scripts/install.sh` already applies, rather than silently writing
to a root-relative path that succeeds while landing nowhere useful.

## Step 4 — verify no prompt

Spawn the agent under its role's normal harness invocation and run one of its
declared verbs (e.g. `loadout-push --help`). No permission prompt should
appear; the verb should behave exactly as it does from an interactive shell.
If a prompt still appears, confirm the fragment actually landed in the
settings file path the harness itself reads from — `--settings-file` /
`CLAGENTIC_LOADOUT_SETTINGS_FILE` must point at the same file the harness
loads, not merely *a* file this tooling can write to.

## Trade-offs named

- **Print-by-default, `--write` opt-in.** A prior attempt built this as an
  installer-driven global fragment write; that shape was rejected outright by
  the operator (global allowlists conflate every role's blast radius into
  one). Print-only is the safe default here for the same reason a mutation
  should never be a tool's default behavior when a copy-pasteable
  side-effect-free alternative exists; `--write`'s idempotent merge exists for
  integrators who have already decided their settings file is
  machine-managed.
- **Config lives at `.clagentic/loadout/config.yaml`, not a new file.**
  Reuses the one-file/one-section-per-verb convention `wait.config`
  established, rather than inventing a second per-repo config file for one
  more feature.
- **Role taxonomy is a default, not a schema.** The four seed roles
  (`builder`/`reviewer`/`merger`/`lead`) are what today's known integrations
  need; nothing in the config format, the generator, or the CLI hardcodes
  that set as exhaustive — see the conformance note below.

## Scope-based model routing

A role's escalation policy — which model chain a role's agent should run
under, given how large the diff under review/build is — is a separate,
optional declaration from the verb allowlist above:
`clagentic_loadout.provisioning.model_routing`. It follows the exact same
repo-local config replace-not-merge convention as `roles:` (Step 1
above), under its own `model_routing:` top-level section:

```yaml
model_routing:
  reviewer:
    - max_loc: 10000
      model_chain: ["reviewer-standard"]
    - max_loc: null
      model_chain: ["reviewer-architectural", "reviewer-standard"]
  builder:
    - max_loc: null
      model_chain: ["builder-standard"]
```

Tiers are evaluated in list order; the first tier whose `max_loc` is greater
than or equal to the caller-supplied `changed_lines` wins. `max_loc: null`
is the open-ended top tier and must be listed last (config-load time error
otherwise — a tier placed after it could never be reached). Model ids are
opaque tokens this module never interprets, defaults, or hardcodes to a real
provider/model literal (CLAUDE.md rule 1) — a deployment wiring a real
escalation (e.g. a reviewer role that reaches for a stronger model on
architectural diffs over 10k changed lines, the reference shape this task
was filed against) declares its own `model_routing:` section entirely in
config; nothing in this module's code names a specific model.

`resolve_model_chain(role, changed_lines, repo_root=...)` is the read API —
it does not compute `changed_lines` itself (that diff-scope fact is the
caller's own job, the same "caller fetches, this module is a pure policy
check" split `merge.diff_scope` already uses) and raises with the resolved
role, changed_lines, and known-good role set when the role is not declared
anywhere. A repo with no repo-local config `model_routing:` section
gets `DEFAULT_MODEL_ROUTING` — a single, open-ended, no-escalation tier per
seed role, each pointing at one opaque placeholder model id; this is *a*
default, not *the* routing policy.

There is no console-script verb for this seam yet — it is a library API a
consuming dispatch/lead layer calls directly (see this repo's CLAUDE.md
"Task boundary" section: agent-contract/cast-registry wiring that consumes
this stays in the deploying project, not here).

## Merge-gate config homes

A repo migrating onto loadout-native config also needs its **working merge
gates** — pre-merge checks, merge requirements, required-reviewer roles,
merge authority — to survive the move. Before these sections landed,
`loadout-merge`'s own gate chain (see [docs/verbs.md](verbs.md)'s
`loadout-merge` section) was
entirely CLI-flag-driven: `--authorized-role`, `--required-reviewer`,
`--max-changed-files` all had to be re-supplied on every invocation, with no
repo-local config home. A repo with no dispatcher re-deriving those flags
from its own external config format LOST its merge gates the instant it
switched to `.clagentic/loadout/config.yaml` alone.

These sections close that gap — **schema and doctor validation only** (this
task's scope); wiring `merge.verb`'s CLI to read these as its own flag
DEFAULTS (so a bare `loadout-merge` invocation with no flags still enforces
a repo's declared policy) is a named follow-up, not built into this slice.

**`required_reviewer_roles` absence is not symmetric with `authorized_roles`
absence.** Once a repo's `merge:` section exists at all, omitting
`required_reviewer_roles` from it is a config-load error, not a silent "no
reviewer gate" — a repo must say, one way or the other, whether a reviewer
verdict is required before a merge, either by declaring the real role(s) or
by declaring an explicit `required_reviewer_roles: []` opt-out.
`authorized_roles`' own absence-means-fail-closed behavior is unchanged. See
`clagentic_loadout.merge.gate_config`'s module docstring ("ABSENCE
SEMANTICS") for the full rationale, and
[docs/merge-authority.md](merge-authority.md#3-configuring-your-own-attestation-source)'s
"Absence semantics" note for the consumer-facing summary.

**Role vocabulary stays open-ended; `loadout-doctor` WARNs on a mismatch
instead.** Neither `required_reviewer_roles` nor `authorized_roles` is
validated against a fixed role allowlist at config-load time —
`provisioning.roles.DEFAULT_ROLE_VERBS` is documented as *a* reference
default, not *the* taxonomy, and a repo's own `roles:` section can declare
any role name it invents. Hardcoding an allowlist here would reject
legitimate deployments with invented roles. Instead, `loadout-doctor`'s
`repo_loadout_schema` check cross-references every gate role against that
same repo's own `roles:` declaration (falling back to `DEFAULT_ROLE_VERBS`
when `roles:` is absent) and WARNs — `ok` stays `True` — on any gate role
matching nothing there. This targets the shape where a value naming a
specific identity/account, rather than a bare role token, ends up sitting in
a role slot: it validates syntactically as a non-empty string but can never
match an emitted reviewer/authority verdict, so a config that reads as a
gate is not actually an enforceable one.

### Design calls

1. **Repo-tier vs. deployment-tier split.** Every key below that is a POLICY
   value (how many reviewers, how wide a diff, which pre-merge commands, which
   ROLES) is REPO-TIER: committed, public-safe, lives in the same
   `.clagentic/loadout/config.yaml` `merge:` section `post_merge_steps`
   already occupies. Every key that is an IDENTITY value (a login, an email,
   a display name) is DEPLOYMENT-TIER: the USER-LEVEL
   `~/.config/clagentic/loadout/config.yaml`, mirroring
   `transport.provider_config`'s `credentials:` tier and
   `transport.github_app_config`'s `github_app:` tier — a cloned repo's own
   committed config must never be able to name an identity a gate trusts
   (the same security direction already applied to the credentials
   tier).
2. **Replace-not-merge.** Every list-valued key (`pre_checks`,
   `required_reviewer_roles`, `authorized_roles`) REPLACES the (empty)
   default entirely when declared — mirrors `roles:`'s own "a role you omit
   is simply not provisioned" contract, consistent with
   `provisioning.roles.load_role_verbs`. `merge_requirements` is a mapping of
   independent keys; each declared key replaces only that key's own default.
3. **Role vocabulary only.** Every role string read by these sections
   (`required_reviewer_roles`, `authorized_roles`) is a bare role token —
   `builder`/`reviewer`/`security`/`merger`/`lead`, or any role name an
   integrator invents — never an agent name, consistent with
   `merge.verdict`'s reviewer-role fence and this document's own
   role-vocabulary rule above.
4. **The Forgejo vs. GitHub review-login story.** `required_reviewer_roles`
   declares WHICH roles are required (repo-tier); it does **not** need to
   declare each role's platform LOGIN. `merge.reviewer_login.resolve_reviewer_login`
   already derives that, platform-aware, with **no new config** in the common
   case: on Forgejo the bare role name **is** the login; on GitHub it resolves
   `github_app.slugs.<role>` (an EXISTING deployment-tier seam,
   `transport.github_app_config`) + `[bot]`. `review.login_config`'s
   `review.reviewer_logins.<role>` (deployment-tier, see below) exists **only**
   for the residual case: a Forgejo bot account whose actual login differs
   from its role name.

### `merge:` section — repo-tier (`.clagentic/loadout/config.yaml`)

```yaml
merge:
  pre_checks:
    - cmd: make lint
      description: "lint pass, no CI runner wired up"
      on_failure: fail
  merge_requirements:
    tests_pass: true
    ci_pass: true
    max_changed_files: 30
  required_reviewer_roles:
    - reviewer
  authorized_roles:
    - merger
```

`enforce_single_verdict_fence` is omitted from this example deliberately: it
defaults to `true` (enforced), so a repo with no legacy multi-fence
comments never needs to declare it at all. See its own bullet below for the
`false` opt-out shape.

- **`pre_checks`** (`clagentic_loadout.merge.pre_checks_config`) — an ordered
  list of read-only-by-convention commands the merge gate runs BEFORE
  authorizing a merge. Same shape as `post_merge_steps` (`cmd`,
  `description`, `on_failure`, `detaches`), reusing `merge.post_merge`'s
  validator/executor verbatim — this is the same primitive applied at the
  opposite end of the gate's lifecycle (before the merge call, not after).
  Absent means "no pre-merge validation commands," a legitimate shape for a
  repo gated purely by CI + reviewer verdicts. `detaches: true`
  is unusual here — a pre-merge check is normally read-only-by-convention and
  its own outcome matters to the gate — but is validated/executed
  identically since this reuses `merge.post_merge` verbatim; `detaches: true`
  combined with `on_failure: fail` is rejected the same way it is for
  `post_merge_steps`.
- **`merge_requirements`** (`clagentic_loadout.merge.gate_config`) —
  `tests_pass` / `ci_pass` (bool) declare whether those gates apply at all;
  `max_changed_files` (positive int) is the diff-scope cap
  (`merge.diff_scope.check_diff_scope`'s existing parameter, same value
  `--max-changed-files` supplies today). Each key is independent; a partial
  mapping keeps its own defaults for the keys it omits.
- **`required_reviewer_roles`** — the release-gate reviewer-verdict
  roster (role names, feeds `merge.verb`'s existing `--required-reviewer`
  mechanism via `merge.reviewer_login.resolve_reviewer_login`).
- **`authorized_roles`** — the merge-authority roster (role names, feeds
  `merge.authority.StaticRoleAuthorityProvider` exactly like the repeated
  `--authorized-role` flag does today). Absent means no role holds merge
  authority — fail-closed by construction, matching
  `StaticRoleAuthorityProvider`'s own empty-set contract.
- **`enforce_single_verdict_fence`** (bool, default `true`) — hard refusal,
  ENFORCED BY DEFAULT, when a reviewer's verdict comment carries more than
  one fenced ` ```review-result``` ` block. Set to `false` as an ESCAPE
  HATCH for a repo carrying legacy multi-fence comments it cannot
  immediately clean up — omitting the key entirely gets the safe (enforced)
  behavior. Deliberately the INVERSE of `merge: enforce_merge_shape`'s
  WARN-BY-DEFAULT trade-off, not the same shape: see
  [docs/verbs.md](verbs.md)'s "Multi-fence verdict bodies" section for the
  full defect this pairs with and why this one key enforces by default.

### Deployment-tier identity sections (`~/.config/clagentic/loadout/config.yaml`)

```yaml
builder_identity:
  name: "clagentic-builder[bot]"
  email: "123456+clagentic-builder[bot]@users.noreply.example.com"

review:
  reviewer_logins:
    reviewer: clagentic-reviewer-bot
```

- **`builder_identity`** (`clagentic_loadout.push.identity_config`) — the
  `name`/`email` pair `push.identity.pin_commits_to_bot_identity` re-authors
  commits to before push. Identity-bearing, so deployment-tier only — this
  module has no `repo_root` parameter anywhere in its API, mirroring
  `transport.github_app_config`'s own no-repo-local-tier choice. Absent
  means commit re-authoring is not configured; the caller's own
  `fail_closed_on_missing` decides whether that is acceptable.
- **`review.reviewer_logins.<role>`** (`clagentic_loadout.review.login_config`)
  — the residual Forgejo login-override case described in design call #4
  above. Malformed values degrade to "no override" (additive tier, never a
  hard failure) rather than raising, mirroring
  `transport.github_app_config`'s own per-caller `slugs` map contract.

### `github_app:` section — deployment-tier, GitHub App slug (`~/.config/clagentic/loadout/config.yaml`)

`design call #4` above already names this seam by reference
(`github_app.slugs.<role>` + `[bot]`); this is the schema itself, since
every login and commit-identity derivation on GitHub in this package —
`review.github_backend.resolve_own_login`, `merge.reviewer_login.resolve_reviewer_login`,
`merge.verb`'s required-reviewer resolution, and `push.crew_identity`'s
derived commit identity (see "Derived commit identity for a recognized
crew caller" below) — reads from this ONE section:

```yaml
github_app:
  slug: some-default-app-slug        # single-global fallback tier
  slugs:
    builder: some-builder-app-slug   # per-caller override, one entry per role
    reviewer: some-reviewer-app-slug
  callers:
    - builder
    - reviewer
```

- **`github_app.slug`** — the single-global GitHub App slug, used when no
  per-caller entry matches (or no caller was supplied at all). Sufficient
  for a deployment running exactly one GitHub App across every role.
- **`github_app.slugs.<caller>`** — per-caller override (`transport.github_app_config`,
  design call #4). A role-scoped deployment running multiple GitHub Apps
  (a builder App, a reviewer App, ...) sets one entry per caller here;
  each caller's own slug wins over the single-global fallback above.
- **`github_app.callers`** — the deployment's declared caller registry: the
  exact `--caller`/`--role` strings this deployment's harness actually
  passes at runtime. This is the key-space `slugs` is keyed by, and is also
  the ONLY gate deciding whether `push.crew_identity` may derive a commit
  identity unconditionally for a caller (see below) — a caller absent from
  this list, or a deployment with no `github_app:` section at all, is
  unaffected by that derivation.
- Selection precedence (highest first): env var
  `CLAGENTIC_LOADOUT_GITHUB_APP_SLUG` (wins regardless of caller) >
  `github_app.slugs.<caller>` > `github_app.slug` > unconfigured (fails
  closed — every consumer of this seam reports which two config seams
  would resolve it, rather than guessing or falling back to a live API
  lookup that cannot work for a GitHub App installation token; see
  `transport.github_app_config.resolve_github_app_slug`'s own docstring).

**Why this is user-level ONLY, and structurally cannot become repo-local.**
This is the same identity-bearing-value rule stated in design call #1
above, made concrete for this one seam: `transport.github_app_config`
accepts **no `repo_root` parameter anywhere in its public API** — there is
no code path here that could even be tempted to add a repo-local tier
later without a deliberate signature change, not merely "we chose not to
read one." The reason is a security property, not a convenience default: a
cloned or forked repo's own **committed** config is attacker-influenced
input the moment that repo is untrusted — a fork opened as a PR against
your repo ships its own `.clagentic/loadout/config.yaml` alongside the
diff. If `github_app:` could be set there, that untrusted config would
choose which App slug this package treats as "the caller's own identity"
for every downstream check that trusts it (a reviewer-verdict comparison,
a required-reviewer login, a derived commit author) — silently redirecting
those checks to attacker-controlled criteria. Keeping this seam user-level
only, with no repo-root parameter to exploit, closes that entire class
before it can be reached. **A deployment configures this exactly once,
globally** (the same user-level `~/.config/clagentic/loadout/config.yaml`
every other identity-bearing tier in this document uses) — nothing
per-repo is needed, or possible.

### Derived commit identity for a recognized crew caller (GitHub only)

`push.crew_identity` derives a bot **commit** identity (not just a login)
directly from the `github_app:` section above, for a caller pushing to
GitHub that is present in `github_app.callers` — **no `builder_identity:`
section required**. This is a deliberate design choice, not a placeholder
for future work; read the trade-off below before treating the result as
incomplete.

**What happens.** For a recognized crew caller (present in
`github_app.callers`) pushing to GitHub, `loadout-push` re-authors every
commit on the branch to:

- **name:** `<slug>[bot]` — GitHub's own documented App-bot login
  convention, the SAME derivation `resolve_own_login`/
  `resolve_reviewer_login` already use for the login string.
- **email:** `<slug>[bot]@users.noreply.github.com`

applied in-flight at push time (the existing re-authoring path, which
rebuilds each commit's git object directly with the new identity rather
than checking anything out) and persisted nowhere — no local git config
is written, on any host or in any repo, to produce this.

**Where `<slug>` comes from — a credential provider's VERIFIED slug now
outranks this config.** Precedence, most-specific first:
`--bot-name`/`--bot-email` (explicit CLI) > a credential-minting
provider's own broker-verified `app_slug` (see
[docs/integration.md](integration.md)'s "Optional: a minting provider MAY
report a verified identity alongside the token" section) > this section's
`github_app.slugs.<caller>`/`github_app.slug` > `builder_identity:` >
fail closed. The provider tier activates ONLY when the resolved
`TokenProvider` for this push actually supplies a non-empty `app_slug` on
this call, and OVERRIDES this section's slug for a caller that is
ALREADY recognized-and-resolvable here — it is a slug-SOURCE upgrade
within the existing gate above, not a way to become a "recognized crew
caller" without `github_app.callers`, and not a way to rescue a caller
this section's config alone could not resolve at all (see
`push.verb._resolve_effective_bot_identity`'s own docstring for the exact
call-ordering rationale: identity is still resolved once, cheaply, before
any token mint, to fail fast on a genuinely unresolvable caller without
spending a mint on a push that config alone already proves must be
refused; the provider's verified slug is folded in as an override
immediately after the token has actually been minted). A deployment with
no such provider integration (every deployment before this task, and any
`CommandTokenProvider` that never opted into
`token_command_emits_json_forgejo`/`_github`) sees **zero behavior
change**: `github_app.slugs.<caller>`/`github_app.slug` remain the sole
slug source, exactly as described below.

**The deliberate limitation, stated plainly.** This form omits the
numeric-id-prefixed noreply address GitHub actually uses to bind a
commit to an App's own bot account: `<numeric-bot-user-id>+<slug>
[bot]@users.noreply.github.com`. Without that numeric id, the commit will
**not** carry GitHub's "Bot" badge and will **not** link to the App's
profile page — GitHub will show it as an unlinked/unverified email rather
than crediting the App account. **This limitation is UNCHANGED by the
provider-verified tier above:** a reference minting provider's structured
mint output carries a verified `app_slug`, not the numeric App-bot user
id — see the next bullet for why that id is not obtainable from either
source. A deployment on the provider-verified tier gets the
SAME slug-only email shape as the config-only tier; only the SOURCE of
`<slug>` differs (broker-verified vs. operator-typed), not the email
format's own completeness.

**Why, and why this is not a gap to be filled later:**

- Every OTHER identity surface this package derives from `github_app:`
  binds by **login**, where `<slug>[bot]` is complete and authoritative —
  it is GitHub's own documented App-bot-login convention, and every
  consumer of `resolve_github_app_slug` (`review.github_backend`,
  `merge.reviewer_login`, `merge.verb`, and this derived-commit-identity
  path itself) uses exactly that string with no further input needed.
- Git has no login field. A commit's author is `Name <email>` — GitHub
  reverse-maps that email to an account to decide badge/link/attribution.
  The numeric-id prefix exists **only** to make that reverse mapping
  resolve; it is a git-author-format constraint GitHub's UI imposes on
  top of the login identity, not a second identity this package's own
  model is missing.
- The numeric bot **user** id is not derivable from any value this
  package configures, INCLUDING the provider-verified `app_slug` above. It
  is not the App ID and not the Installation ID (both of which this
  package does use, for token minting), and it is not the App slug
  either — it is a third, distinct number with no configured source on
  either the config tier or the provider tier. Obtaining it would require
  a live, unauthenticated API lookup at push time: a second
  identity-resolution mechanism, network-dependent, existing for exactly
  one cosmetic field on exactly one platform. This package declines to
  add it.
- **What this DOES guarantee, and is the actual property it exists for:**
  for a recognized crew caller pushing to GitHub, the commit author is
  never an ambient or personal identity. That holds unconditionally, with
  no config beyond `github_app:` above (optionally sharpened by a
  provider-verified slug), and is unaffected by the bot-badge limitation
  described here.

A deployment that wants full bot-badge binding configures `--bot-name`/
`--bot-email` explicitly (always wins over both the config- and
provider-derivation above) or the `builder_identity:` section above with
the full id-prefixed email it has obtained out of band — both existing
seams already accept any email string this package does not itself
validate against GitHub's noreply convention.

**Forgejo is unaffected.** Forgejo has no GitHub-style App-bot-login
convention in this package's contract at all (see design call #4 above) —
there is no slug-to-identity derivation to perform on that platform, so a
recognized crew caller pushing to Forgejo sees no change from this
section: no re-authoring beyond whatever `builder_identity:`/`--bot-name`/
`--bot-email` already provide. This is unaffected by the provider-verified
tier too — a credential provider's `app_slug` is only ever consulted on
the GitHub platform code path, mirroring `is_recognized_crew_caller`'s own
platform gate above.

### Conformance

Exercised with **synthetic role names, invented repo/config paths, and no
LORE present** — same posture as every other section in this document. See
`tests/test_merge_pre_checks_config.py`, `tests/test_merge_gate_config.py`,
`tests/test_push_identity_config.py`, `tests/test_review_login_config.py`,
and the `check_repo_loadout_schema` / `check_builder_identity_config`
coverage in `tests/test_doctor_checks.py`. The provider-verified slug tier
is covered in `tests/test_transport_credential_provider.py`
(`ResolvedToken`/`resolve_token_result`, `CommandTokenProvider`'s
`emit_structured_output` opt-in), `tests/test_transport_provider_config.py`
(the `token_command_emits_json_forgejo`/`_github` config/env wiring), and
`tests/test_push_crew_identity.py` /
`tests/test_push_verb.py::TestProviderVerifiedIdentityTier` (the actual
slug-source precedence, non-vacuously proving the provider slug wins only
when it DIFFERS from the config slug, and that a bare-token/empty-slug
provider falls through to config unchanged).

## Attestation source config (`attestation:`)

`transport.attestation.resolve_identity` (see that module's own docstring for
the full three-layer resolution order) resolves WHAT identity a process is
running as, before `bind_caller` compares it against `--caller`/`--role`.
Layer 2 of that chain — the sidecar adapter — has THREE sources, all
DEPLOYMENT-TIER (`~/.config/clagentic/loadout/config.yaml`, never repo-local,
same identity-bearing-value split as `builder_identity` above):

```yaml
attestation:
  identity_env: MY_SPAWN_IDENTITY_VAR       # layer 1, single env-var name
  identity_sidecar_path: /var/run/my-agent-identity  # layer 2, single literal path
  sidecars:                                  # layer 2, NEW: ordered adapter list
    - dir: /tmp
      file_prefix: my-harness-agent-name-
      session_id_env: MY_HARNESS_SESSION_ID
    - dir: /tmp
      file_prefix: my-harness-spawn-
      session_id_env: MY_HARNESS_SPAWN_ID
```

**`sidecars`** exists for a harness that runs many concurrently-live
sessions, each with its own session-scoped identity file — a single literal
path (`identity_sidecar_path`) can only ever name ONE file per process, so it
cannot distinguish between sessions without per-command env-stamping. Each
list entry is `{dir, file_prefix, session_id_env}`:

- `dir` — the directory the external harness writes its identity file into.
- `file_prefix` — the filename prefix before the session id.
- `session_id_env` — the name of the env var holding the CURRENT session id;
  the composed file is `<dir>/<file_prefix><value of that env var>`.

Adapters are walked in DECLARED ORDER; the first adapter whose
`session_id_env` resolves to a non-empty value AND whose composed file
exists wins. An adapter that has nothing to offer (unset/empty session id,
or the composed file is genuinely absent) is SKIPPED, not an error — the
chain falls through to the next adapter, then to the next attestation
source, then eventually to the built-in OS-user fallback exactly as it does
today. A session id value that is not a safe single path component (contains
a path separator, or is `.`/`..`) is REFUSED for that adapter — rejected
outright, never sanitized, so an attacker who controls the session-id env
var cannot redirect the composed read outside `dir`. Every composed path is
opened with the same atomic `O_NOFOLLOW`+`fstat` read every sidecar source in
this layer uses — a symlink or other non-regular directory entry
is a hard failure, never silently followed.

**Precedence within layer 2** (highest first): `CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH`
(env) > `attestation.identity_sidecar_path` (config) > `attestation.sidecars`
(config, this section, first adapter to resolve). An empty or absent
`attestation:` block is byte-identical to today's behavior — this section is
entirely additive.

**Worked example (not a dependency):** clagentic-gatekeeper's own deployed
config uses this exact adapter shape to distinguish a top-level session's
identity sidecar from a spawned sub-agent's own sidecar within the same
session — e.g. an adapter list ordering a spawn-scoped adapter (narrower
scope, checked first) ahead of a session-scoped adapter (broader scope,
checked second), so a sub-agent spawn's own stamped identity is preferred
over its parent session's when both are present. This document cites that
deployment only as an EXAMPLE of the generalized shape above — `clagentic: loadout`
itself has no dependency on, import of, or hardcoded reference to that (or
any other) specific harness; see `transport.attestation`'s own module
docstring for the full agnosticism contract (CLAUDE.md rule 1 / rule 6a).

`loadout-doctor` (`doctor.checks.check_attestation_source_configured`)
WARNs when `github_app.callers` is declared but none of `identity_env` /
`identity_sidecar_path` / `sidecars` (nor their env-var equivalents) is
configured — every invocation would otherwise fall through silently to the
built-in OS-user layer, which `bind_caller` then compares against the
declared caller names (the structural root cause of a real chat-agent
`root`-attribution failure this check was added to catch).

## Conformance

This entire contract is exercised in the test suite with **synthetic role
names, invented verb/model-id combinations, and no LORE present** — the
pipeline never assumes the seed role taxonomy, any specific agent name, or
any external task-tracking system. See `tests/test_provisioning_roles.py`,
`tests/test_provisioning_allowlist.py`,
`tests/test_provisioning_settings_path.py`, `tests/test_provisioning_writer.py`,
`tests/test_provisioning_cli.py`, `tests/test_provisioning_model_routing.py`,
`tests/test_transport_attestation.py`, and `tests/test_doctor_checks.py`.
