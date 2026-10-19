# Merge authority: identity binding, fail-closed guarantee, and attestation

`loadout-merge` is the load-bearing release gate (see
[docs/verbs.md](verbs.md)'s `loadout-merge` section for the full gate chain).
This document is the consumer-facing security contract for one link in that
chain — **merge authority**: how a caller's role is bound to the power to
land a PR, what happens when that binding cannot be confirmed, how a
consumer points `clagentic: loadout` at their own attestation source, and what the
built-in fallback actually grants if they don't.

Read this before wiring `loadout-merge` into a live deployment. Getting the
authority source wrong is a security-relevant misconfiguration, not a
cosmetic one — see "The built-in fallback" below for the failure mode this
document exists to prevent.

## 1. The identity-binding model

A merge is authorized for a **role**, never for a specific agent's name or
account. `clagentic_loadout.merge.authority` defines the seam:

```python
def authority_allows(role: str, owner: str, repo: str, pr_number: int) -> bool: ...
```

`merge.verb` resolves the caller's role from its own `--caller`/`--role`
input, then asks a configured `AuthorityProvider` whether *that role* may
merge *this specific PR* in *this specific repo*. The provider's answer is
the only thing that decides the outcome — there is no hardcoded role name,
hardcoded agent identity, or hardcoded service endpoint anywhere in
`merge.authority` itself. **Which role may authorize a merge is config, full
stop** — the same role-vocabulary discipline every other `clagentic: loadout` seam
follows (roles like `builder`/`reviewer`/`merger`/`lead`, never an agent's
name).

This mirrors the merge-authority posture of the internal predecessor gate
this module generalizes: that gate also tied merge authority to a single
role verified against an external service, but baked in one specific
service, one fixed URL, and one hardcoded role name.
`clagentic: loadout` preserves the **posture** (role-gated, externally verified,
fail-closed) while making the **mechanism** — which service, which URL,
which role — entirely a deployment's own configuration.

## 2. Fail-closed, with no fail-open variant

Every path through `merge.authority` that cannot **positively confirm**
authority refuses the merge. There are exactly three ways a check can end,
and only one of them allows the merge:

| Outcome | Result |
|---|---|
| Provider returns `True` | Merge authority confirmed — gate passes |
| Provider returns `False` | Refused — role explicitly not authorized |
| Provider cannot be consulted (unreachable, malformed response) | Refused — same as an explicit deny |

An unreachable provider, a malformed response, and a role absent from the
configured allow-set are **all refusals**. None of them is a silent allow.
There is no fail-open code path in this module, and no flag that produces
one — a deployment that wants a different posture has to build it outside
this seam, not toggle it on inside `merge.authority`.

This is a deliberate contrast with a **human-operator** release tool, which
might reasonably warn-and-allow when its authority-verification service is
down (a human is still in the loop to catch a bad outcome). An **autonomous**
merge gate has no such backstop — the merge itself is the last check before
code lands unattended — so `merge.authority` is unconditionally fail-closed.
The provider seam's own exception type makes this explicit in code, not just
in prose: `AuthorityProviderError` is raised specifically so a caller cannot
mistake "the service could not be reached" for "the service said no" and
accidentally treat the ambiguity as a pass — both refuse identically; the
distinction survives only in the error message, for diagnosis after the
fact.

## 3. Configuring your own attestation source

`AuthorityProvider` is a `Protocol`, not a base class — implement its one
method and pass an instance to `merge.authority.check_authority`. There is
no import coupling: a deployment's provider does not need to inherit from
anything `clagentic: loadout` ships, and `clagentic: loadout`'s gate code depends only on the method
signature.

```python
from clagentic_loadout.merge.authority import AuthorityProvider

class MyDirectoryProvider:
    def authority_allows(self, role: str, owner: str, repo: str, pr_number: int) -> bool:
        # call out to whatever trust-label / directory service you run
        ...
```

Two shapes of provider exist for this seam:

- **External provider — composed in, not shipped here.** A directory-style
  trust-label service (a system that tracks which role/identity is
  currently trusted to act on which repo) is the shape an internal
  predecessor deployment used. `clagentic: loadout` does **not** ship a client for any
  such service — that would hardcode a specific operator's infrastructure
  into a public package. A deployment that has (or builds) a
  directory-equivalent service implements `AuthorityProvider` against it
  and wires the instance in at the call site that constructs the merge
  gate; `clagentic: loadout` never imports or names the service.
- **Standalone provider — ships in-package, no network call.**
  `StaticRoleAuthorityProvider` is the built-in fallback for a deployment
  with no external directory/policy service at all — see "The built-in
  fallback" below for exactly what it grants.

### Config keys and their tier

A deployment does not have to hand-construct a provider at the call site
every time — the repo-tier config schema gives the
`StaticRoleAuthorityProvider` shape a declarative home in
`.clagentic/loadout/config.yaml`:

```yaml
merge:
  authorized_roles:
    - merger
  required_reviewer_roles:
    - reviewer
```

- **`merge.authorized_roles`** — **repo-tier**, `.clagentic/loadout/config.yaml`
  (committed, public-safe). The list of role names permitted to hold merge
  authority; feeds `StaticRoleAuthorityProvider` exactly like the CLI's
  repeated `--authorized-role` flag does. This is a **policy** value (which
  roles, not which identities), so it is safe to commit — see design call #1
  in [docs/provisioning.md](provisioning.md#merge-gate-config-homes)
  for the full repo-tier-vs-deployment-tier rationale. Absent or empty means
  **no role holds merge authority** — fail-closed by construction, matching
  `StaticRoleAuthorityProvider`'s own empty-set behavior (see §2 above:
  "role absent from the configured allow-set" is a refusal, not a pass).
- **`merge.required_reviewer_roles`** — **repo-tier**, same file. Adjacent to
  `authorized_roles` but a *different* gate: the reviewer-verdict fence
  (`merge.verdict`), not merge authority. Listed here because both are role
  lists read from the same `merge:` config section and both ultimately
  answer "which role" questions the gate chain asks — see
  [docs/verbs.md](verbs.md)'s `loadout-merge` section, gate steps 2 and 5,
  for how they diverge downstream.

  **Absence semantics (not symmetric with `authorized_roles`).**
  `authorized_roles`' absence is safe to leave implicit — see the bullet
  above, and §2: an empty/absent set is already a refusal, so silence can
  never be mistaken for a pass. `required_reviewer_roles` cannot make the
  same claim: its absence has historically meant "no reviewer verdict is
  required" — fail-**open** — which is indistinguishable, one config level
  up, from a repo that deliberately decided it wants no reviewer gate. Once
  a repo's `merge:` section exists at all (i.e. it has already opted into
  repo-tier gate config), omitting `required_reviewer_roles` from it is now
  a config-load error, not a silent fail-open default — declare the real
  role(s), or declare `required_reviewer_roles: []` as an explicit,
  deliberate opt-out. A repo with no `merge:` section at all is unaffected:
  there is nothing to be explicit about when no gate declaration exists in
  the first place. See
  [docs/provisioning.md](provisioning.md#merge-gate-config-homes) and
  `clagentic_loadout.merge.gate_config`'s module docstring ("ABSENCE
  SEMANTICS") for the full contrast and rationale.
- Identity-bearing values that a resolved role maps to (a login, an email, a
  display name) are never repo-tier — they live in the **deployment-tier**
  user-level `~/.config/clagentic/loadout/config.yaml` instead
  (`builder_identity`, `review.reviewer_logins`). A cloned repo's own
  committed config must never be able to name the identity a gate trusts;
  see [docs/provisioning.md](provisioning.md#merge-gate-config-homes)
  for the full deployment-tier schema. `merge.authorized_roles` itself stays
  role-only — it never carries a login or account name, so this split does
  not create a second identity-bearing key inside the repo-tier section.

#### Where the gate is read from

The **gate keys** (`merge.required_reviewer_roles`,
`merge.required_scanners`, `merge.pre_checks`, `merge.required_ci_checks`,
`merge.ci_strict`) are read from a **tracked** file,
`.clagentic/loadout/gate.yaml` (same `merge:` section shape), exactly as it
exists at the **PR's base commit** (`git show <base_sha>:<path>`, with the base
SHA taken from the PR payload the merge already reads). They are never read
from the working tree: a tree holding the PR head would otherwise let the PR
under review delete its own required scanners, corrupt the file to force the
flags-only fallback, or drop its pre_checks. Because the file is tracked, a
change to the gate goes through review like any other change.

| Key | Source |
| --- | --- |
| `merge.required_reviewer_roles`, `merge.required_scanners`, `merge.pre_checks`, `merge.required_ci_checks`, `merge.ci_strict` | tracked `.clagentic/loadout/gate.yaml` at the PR base commit |
| `merge.post_merge_steps`, `merge.sync_tree_after_merge`, `merge.git_working_tree`, `merge.enforce_*`, `merge.require_model_attestation`, host paths, `push:` keys | per-deployment `.clagentic/loadout/config.yaml` (typically gitignored) |

**CI gate.** `loadout-merge` reads CI evidence for the PR head from both
sources: commit statuses (Forgejo Actions publishes its job results there, as
do legacy GitHub status posters) and, on GitHub, check-runs (GitHub Actions
posts only these, so the legacy status endpoint is empty for such a repo).

- With no `merge.required_ci_checks`, every check name present at the head
  gates. For one name, all of its runs are considered: any run not yet
  completed refuses as pending; any run of the latest check suite that did not
  succeed refuses (a `skipped` or `neutral` run is not a failure); and at least
  one run must have succeeded. A re-run inside the same suite replaces the
  earlier attempt.
- `merge.required_ci_checks` (a list of check-run names or commit-status
  contexts, read from the base commit) narrows the gate to those names. A
  listed name must have actually run and succeeded (a skip does not satisfy
  it) and a listed name that was never reported refuses. Use it when a repo has
  runs that must not gate, such as a duplicate run that fails whenever a job
  is skipped. `--required-ci-check` (repeatable) adds names for one invocation.
- A head with no check-runs and no commit statuses passes and is reported as
  "no CI found at head", so repos without a CI runner still merge.
  `merge.ci_strict: true` (or `--ci-strict`) refuses such a head instead.
- A refusal names each failing check, its conclusion and the head SHA. The
  merge result JSON carries `ci_source` (`check-runs`, `statuses`,
  `check-runs+statuses` or `none`).
- The two keys fail independently, each with a warning. A malformed
  `required_ci_checks` at base falls back to the default CI gate (every
  check-run at the head must be green), so the merge that lands the corrected
  file is never blocked by the broken one. A malformed `ci_strict` is read as
  strict, so a broken file never relaxes the gate.
- A check-run or commit-status entry the gate cannot read (null, not an object,
  or missing a field the gate reads such as a name, status, id, check suite or
  state), or a response whose run total or list cannot be read, refuses the
  merge as an unavailable gate fact naming the head SHA. An unreadable entry is
  never skipped.

A gate key found in the deployment file is **ignored**, with a warning that
names the tracked location, so a deployment that has not migrated is told
rather than silently running a weaker gate than it believes it has. A tracked
file that is absent at base declares nothing (no warning).

**Bootstrap.** The PR that introduces the tracked file is judged by its base,
which has none, so it runs flags-only; the declaration is enforced from the
next merge on. A PR that fixes a broken gate file is judged by base's broken
file, by each key's rule below (reviewer roles and scanners fall back;
`pre_checks` refuses, and `--skip-pre-checks` lets the fix land); its corrected
file takes effect from the next merge.

`loadout-merge` enforces `merge.required_reviewer_roles` as a **floor**: the
roles it requires are the union of that list and
any `--required-reviewer` flags. Declaring the key gates the merge. Two rules
keep a broken config from blocking the merge that would fix it:

- If the tracked file at base cannot be read (including invalid UTF-8), or
  `required_reviewer_roles` or `required_scanners` cannot be loaded
  (malformed, an explicit `null` for either, or a `merge:` section that omits
  `required_reviewer_roles`), or the PR payload carries no base SHA, **those
  two keys together** fall back to flags-only: neither is enforced, including
  one that was valid. A stderr warning names the commit, the file and the
  error. The fallback is deliberate, so the merge that lands the corrected
  config is never blocked by the broken one. This whole-pair fallback is for a
  gate file that cannot be read or parsed, and for nothing else.
- `merge.pre_checks` is **not** part of that fallback. A `pre_checks`
  declaration at base that is malformed, or that cannot be read at all (a base
  commit that cannot be fetched or shown, invalid UTF-8, a file that does not
  parse), **refuses** the merge (`EXIT_PRE_CHECKS_FAILED`), as it did before
  the gate moved to base. `--skip-pre-checks` bypasses it; `--ignore-repo-gate`
  does not. A PR payload that carries no base SHA gives nothing trusted to
  read, so it fails closed: `pre_checks` **refuse** (the base SHA is the only
  source of the declaration, and an unverifiable declaration is never read as
  "no checks"), and the reviewer pair falls back with a warning.
- `pre_checks` execute in a private git clone of the merge result (the PR head
  merged onto its base commit) under `TMPDIR`, removed afterwards however the
  run ends; the shared `--repo-path` checkout is neither read for execution nor
  modified. The clone is made with `git clone --shared --no-checkout` (it borrows
  the shared object store read-only and never hardlinks it) and has its own
  `.git`, so a hook, config entry, ref or object a check writes in its own
  repository does not reach the shared one. `HEAD` in the clone is the merge
  result: a head that is a fast-forward of base is checked out as it is,
  otherwise the merge is committed in the clone (synthetic author and
  committer, hooks off, no signing) and checked out, so the tree is clean and
  `git diff origin/<base>..HEAD` shows only the PR change. The shared
  repository's remote-tracking refs (`refs/remotes/origin/*`, and `origin/HEAD`
  when present) are copied into the clone and `refs/remotes/origin/<base branch>`
  is pinned to the PR's base commit, so a check that compares against `origin`
  sees the true base. The clone has no usable remote: its `remote.origin`
  configuration is removed once those refs are installed (the refs stay), so
  `git push origin` and `git fetch origin` from a check fail and can neither
  write refs into the shared repository nor overwrite the pinned
  `origin/<base>`. A head that conflicts with base refuses the merge before
  `merge_pr`. The
  clone carries no untracked or ignored state from that checkout (no in-repo
  virtualenv, build artifacts or uninitialised submodules), so a check must not
  depend on them. Their child processes receive the merger's environment minus
  identity and attestation variables, the `CLAGENTIC_LOADOUT_*` namespace and
  credential-shaped names (case-insensitive: any name containing `TOKEN`,
  `SECRET`, `PASSWORD`, `PASSWD`, `CREDENTIAL`, `API_KEY`, `ACCESS_KEY` or
  `PRIVATE_KEY`; any name ending `_KEY`, `_PAT`, `_PASS` or `_DSN`;
  `SSH_AUTH_SOCK`, `DATABASE_URL`, `GOOGLE_APPLICATION_CREDENTIALS`; and the
  `AWS_*`, `AZURE_*`, `GH_*`, `FORGEJO_*`, `BAO_*`, `VAULT_*` families); `PATH`,
  `HOME`, `LANG`, `LC_*`, `VIRTUAL_ENV`, `PYTHONPATH` and `TMPDIR` pass through.
  Variables that point at a credential source are withheld too: `GIT_ASKPASS`,
  `SSH_ASKPASS`, `SUDO_ASKPASS`, `NETRC`, `KUBECONFIG`, `DOCKER_CONFIG` and the
  `GIT_CONFIG_*` family (which can inject a credential helper). The git
  repository/location selectors (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`,
  `GIT_COMMON_DIR`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`,
  `GIT_NAMESPACE`, `GIT_CEILING_DIRECTORIES`, `GIT_DISCOVERY_ACROSS_FILESYSTEM`,
  `GIT_PREFIX`) are always withheld, so a check, and every git command loadout
  runs for the merge result, resolves its repository from the working directory;
  they cannot be passed through (naming one is a configuration error).
  **Limits of the scrub and of the clone.** The shared repository and tree are
  no longer handed to the checks, but a check still runs as the merger's user
  with the same `HOME`: it can read that user's credential files
  (`~/.git-credentials`, `~/.netrc`, ...) and the merger's own unscrubbed
  `/proc/<pid>/environ`, and it can write anywhere that user can by absolute
  path (the shared `.git`, `~/.gitconfig`) or start daemons. Closing that needs
  process isolation (a sandbox), which loadout does not provide.
  Every git command loadout runs for the merge result runs with hooks, fsmonitor
  and signing disabled and with the same scrubbed environment as the checks.
  The optional deployment-tier `merge.pre_checks_env_passthrough` list keeps
  named variables (valid environment variable names, matched in full; a trailing
  newline is a configuration error). It widens what unmerged PR code can read, so
  it is honoured **only from an untracked** deployment config file: when the file
  supplying the key is tracked by git in `--repo-path` (or git cannot say), the
  key is ignored for the scrub and the merge log names the file and the reason.
  `loadout-doctor` also warns when the config file holding that key is tracked.
- A malformed `merge.git_working_tree` is the post-merge tree sync's own error,
  reported only after the merge has landed, so it must not let the gate be
  skipped. The reviewer pair falls back with a warning, and `pre_checks` are
  read from the `--repo-path` tree itself at the base commit: declared checks
  still run, and a `--repo-path` that is not a git tree **refuses**.
- A declared reviewer role the deployment cannot resolve to a platform login
  degrades **per role**, not as a pair: that role is dropped from the reviewer
  floor and its own `required_scanners` entry is skipped (it can never be
  checked), each with a warning naming the role, the platform and the missing
  mapping. Every other resolvable role, and every scanner requirement on a
  resolvable role, stays enforced. It is never a refusal, so a repo's
  declaration cannot demand deployment config the deployment was not told
  about. `loadout-doctor` judges the tracked gate file at HEAD by asking the
  merge runtime for the gate and printing that gate's own warnings and
  `pre_checks` error verbatim, so it never describes a rule differently from
  merge. A role merge would drop, and a gate file merge could not decode or
  parse, are failures there, never a crash; a gate key sitting in the
  deployment file is a warning (it is ignored, and its roles are not
  resolved). A role named by `--required-reviewer` is the caller's assertion and still
  errors as before when it cannot resolve.
- A config that loads but cannot be satisfied (a declared role with no
  verdict; a clean verdict that records no scanner
  outcomes, or reports a required scanner failed, for a role with
  `required_scanners`) refuses the merge. `--ignore-repo-gate` is the
  deliberate escape hatch for exactly two gates, `merge.required_reviewer_roles`
  and `merge.required_scanners`: it is logged to stderr, printed in the merge
  output, and recorded in the merge-completion attestation. It lifts nothing
  else; model attestation and the single-fence requirement stay enforced.
- A `required_scanners` entry for a role that is not a required reviewer (not in
  `required_reviewer_roles` and not named by `--required-reviewer`) could never
  gate anything, so it refuses the merge naming the role, with the same
  `--ignore-repo-gate` escape hatch. An entry with an empty scanner list
  declares nothing.
- Role names and scanner names in these keys are trimmed of surrounding
  whitespace when loaded; two `required_scanners` keys that collide after
  trimming are a malformed config.

**Role to login: the single resolution path.** Every role name, whether it
comes from `--required-reviewer` or `merge.required_reviewer_roles`, resolves
through `merge.reviewer_login.resolve_reviewer_login`: on Forgejo the bare role
is the login; on GitHub it is the `github_app.slugs.<role>` entry (or the
single `github_app.slug`) plus `[bot]`. That map is keyed by the same names a
deployment uses for its callers, so a deployment whose slugs are keyed by
caller names has no entry for a role token. For a declared role that is now a
warning and that role being dropped from the floor; add the
`github_app.slugs.<role>` entry (or pass `--required-reviewer <role>:<login>`)
to enforce it.

On Forgejo a declared role is first looked up in the deployment's `role_callers`
map (the mapped caller is the account and the name the role is required under);
an unmapped role stays required under its bare name, as before the mapping
existed, whether or not a `callers` list is declared. No Forgejo role is ever
dropped. See `docs/provisioning.md`.

**Migration (since the repo reviewer floor landed).** A deployment needs no new
configuration to keep merging: a declared role that cannot resolve is dropped
(with its scanners) with a warning while every other role stays enforced, a
malformed reviewer/scanner declaration falls back with a warning, and
`pre_checks` keep refusing when malformed. One behaviour does tighten: a PR
payload with no base SHA now refuses `pre_checks` (previously it declared none);
integrations that build their own PR payloads must carry `base.sha`, or pass
`--skip-pre-checks`. Gate keys must now live
in the tracked gate file; the same keys in the deployment file are ignored
with a warning.

**Upgrading.** This is a breaking change for a repo that declared
`required_reviewer_roles` decoratively. Its merges now need those verdicts.
Declare `required_reviewer_roles: []` for no reviewer gate. A repo that kept
its gate keys in the deployment file must move them to the tracked gate file:
until then they are ignored (with a warning) and only the flags apply.
`merge.authorized_roles`
and `merge.merge_requirements` are still read only by `loadout-doctor` and by a
caller building its own invocation.

## 4. The built-in fallback: what it actually grants

`StaticRoleAuthorityProvider` is the **standalone** reference
implementation — a caller-supplied set of role names that are always
authorized, evaluated **entirely locally, with no network call**:

```python
StaticRoleAuthorityProvider(authorized_roles=frozenset({"merger"}))
```

Read this carefully if you are pointing `loadout-merge` at this provider
(directly, or via `merge.authorized_roles` in repo-tier config):

- **It grants merge authority to every caller presenting an authorized
  role, unconditionally, for every repo and every PR.** The `owner`,
  `repo`, and `pr_number` arguments are accepted (to satisfy the
  `AuthorityProvider` protocol) but explicitly **discarded** —
  `StaticRoleAuthorityProvider` is not per-repo and not per-PR scoped. If
  your role list includes `merger`, then any caller correctly presenting
  `merger` as its role is authorized to merge **any** PR in **any** repo
  this provider is wired to, not just the one your dispatch layer intended.
- **`StaticRoleAuthorityProvider` itself does not verify the role claim —
  but a separate, in-package binding now does, upstream of it.** This
  provider answers "is this role name in my configured set?" — it does not
  independently confirm that the caller invoking `loadout-merge` is who it
  claims to be; that has not changed. What HAS changed:
  `clagentic_loadout.transport.caller_binding.bind_caller` now runs, inside
  `loadout-merge` itself, BEFORE `merge.authority` is ever consulted — it
  compares an EXPLICIT `--role` against this process's own attested invoking
  identity (`transport.attestation.resolve_identity`: a configured provider,
  a config-driven sidecar adapter, or the built-in OS-invoking-user
  fallback, in that order) and refuses fail-closed on any mismatch, before
  any token is minted or any authority check runs. Every mutating
  `clagentic: loadout` verb (`push`, `merge`, `merge close-pr`,
  `merge post-merge`, `review`, `acquire`, `git-host-api`) calls this same
  binding now — it used to be wired into `git-host-api` alone, which is what
  let an unattested process act as any role by typing its name on any of the
  other verbs; that gap is closed.

  **`--caller`/`--role` is STILL consumed as an opaque config key by
  `merge.authority`/`transport.credential_provider` themselves** — neither
  of those two seams re-derives or re-verifies identity; they trust the
  string exactly as far as their own config says to, exactly as before.
  What changed is that, by the time a role string reaches either of them
  from an EXPLICIT `--caller`/`--role`, it has already survived the
  attested-identity binding above — so this provider (or your own
  `AuthorityProvider`) is no longer the only thing standing between an
  unattested process and merge authority; it is the SECOND of two
  independent checks, not the only one. A role's deeper entitlement (which
  attested identity may act as which role — as opposed to "is this really
  the identity it claims to be") remains a MINT-TIME concern, layered in
  front of `merge.authority`/`transport.credential_provider` by whatever
  `AuthorityProvider`/`TokenProvider` a deployment wires in — an internal
  predecessor deployment's own gatekeeper-style minting service verifies the
  attested caller's entitlement to a role, and that a role's configured
  GitHub App slug is the one the broker actually issued, BEFORE minting a
  token for it. `StaticRoleAuthorityProvider` and `resolve_token`'s
  `StaticTokenProvider`/`CommandTokenProvider` do not themselves perform
  that entitlement check — see the bullet above — but they now always run
  downstream of the identity-binding check, never in its absence for an
  explicit `--caller`/`--role`.

  **The built-in OS-user fallback layer of `transport.attestation` still
  grants write capability** (a deliberate, named trade-off — see
  `transport.caller_binding`'s own module docstring, "REQUIREMENT 5"):
  a deployment that has not configured `attestation.identity_env` or a
  sidecar adapter still gets a genuine attested identity (the OS-reported
  invoking user), and an explicit `--caller`/`--role` is checked against
  THAT value. A deployment whose threat model needs a stronger attested
  source than the OS-invoking-user configures `transport.attestation`'s
  `identity_env` or `sidecars` config accordingly; this package does not
  make that call on a deployment's behalf.
- **Without an injected `AuthorityProvider`, the merge verb's
  `--authorized-role` check is not a security boundary.** The list is
  supplied by the caller, so a caller can name its own role; the boundary in
  that configuration is the role-bound credential mint, which issues a token
  only to an identity entitled to the role.
- **An empty `authorized_roles` set denies every role.** This is the
  fail-closed default described in §2 — a deployment that never configures
  a role here has correctly configured "nobody may merge," not "everyone
  may merge."

If you run `StaticRoleAuthorityProvider` (directly or via
`merge.authorized_roles`) thinking you have configured attestation against
an external directory/trust-label service, you have not — you have
configured a static, unauthenticated-beyond-role-name allowlist. That may be
exactly the right choice for a low-risk repo with a small, trusted role set
(and is the recommended starting point for a standalone deployment with no
directory-equivalent service — see §3). It is the wrong choice for a
deployment that needs per-repo or per-PR authority scoping, or that needs
the role claim itself independently verified — those deployments implement
and wire in their own `AuthorityProvider` (§3, "External provider").

## 5. The git-host attestation mark

After `loadout-merge` actually executes a merge (the final step in the gate
chain — see [docs/verbs.md](verbs.md)'s `loadout-merge` section, step 9), it
posts exactly one git-host-visible comment recording that **it** performed
the merge: `clagentic_loadout.merge.attestation.build_attestation_body`. The
header is a stable, greppable marker:

```
Merged via clagentic-loadout v<X.Y.Z>
```

The body renders as a markdown field/value table, restoring the
presentation the retired reference gate-note (an earlier internal merge
tool's own gate-note builder) used. It records only git-host/product data
already computed by the gate
chain by the time this fires — nothing invented for the comment, nothing
sourced from outside the merge that just happened:

| Field | Value |
| --- | --- |
| Gated HEAD SHA | `<sha>` |
| Merged SHA | `<sha>` |
| Reviews | `<login>, <login>` |
| Verdicts | `<role>: fence <outcome>, gate <outcome>[; disposition applied: ...]` |
| CI status | `<disposition>` |
| task_id | `<opaque work-item ref>` |
| Issue | `#<NN>` |

- **Tool identity and version** — `clagentic-loadout vX.Y.Z` in the header
  line above the table, the package version the merge actually ran under.
- **Gated HEAD SHA** — the commit SHA every gate above evaluated
  (`--expected-head-sha` / the PR's live head at gate time).
- **Merged SHA** — the SHA that actually landed. Kept as a separate field
  from the gated SHA (even though today they are the same value) so a
  future backend response that reports a distinct merge-commit SHA can be
  wired in without a signature change here.
- **Reviews** — the resolved git-host **logins** (never agent names) of the
  reviewers whose clean verdicts gated this merge, rendered as a
  `Reviews` row listing `<login>, <login>`. Omitted entirely (not
  placeholdered — the row is absent, not present with an empty value) when
  no reviewer-verdict gate was configured for the merge — an earlier
  `(none required)` placeholder misread as "unreviewed" even on merges that
  did carry a clean review.
- **CI disposition** — the CI-status gate's own already-computed
  disposition string (e.g. a combined-state summary, or the explicit
  no-runner-by-design pass — see [docs/verbs.md](verbs.md)'s CI-status gate
  description). Not recomputed for the comment; passed through exactly as
  the gate decided it.
- **task_id / Issue** — each an independently optional row, omitted
  entirely (never placeholdered) when the invocation carried no `--task-id`
  or the PR body carried no `Closes #NN` trailer, respectively.

**Every interpolated value is table-cell-escaped.** Once the
body is a markdown table, an unescaped `|` or newline inside a value stops
being cosmetic and becomes structural — either can split a cell into extra
columns or break the row entirely. `build_attestation_body` runs every
interpolated value (SHAs, reviewer logins, `task_id`, CI disposition)
through the same escape before placing it in a cell: `|` becomes `\|`, and
any run of `\r`/`\n` collapses to a single space. `required_reviewer_logins`
and `task_id` are the two fields this matters for in practice — both are
merger-role-trusted CLI input today, not attacker-reachable through this
call path, so this is hardening against a future/looser caller rather than
a fix for a live exploit.

**No "Authorize rationale" row (named trade-off).** The retired
reference gate-note also carried a rationale line, sourced from
`pre_checks_summary` — a per-repo pre-checks config the reference module
loaded and rendered a digest of. `merge.verb` never loads or computes an
equivalent value; adding this row would mean either reintroducing a
`pre_checks_summary`-shaped parameter (one of the seams this port
deliberately left stripped — see `merge.verb`'s module docstring, "IDENTITY
/ SEAM STRIP FROM THE SOURCE MODULE" point 6) or fabricating a rationale
string by restating the CI-status/Reviews rows already in the table, which
would not be a genuine rationale. The table ships without this row; it can
be added later only if `merge.verb` grows a lore-free, caller-identity-free
"why this passed" value of its own to pass through.

**A failed attestation POST never fails the merge.** By the time this
comment is attempted, the merge has already succeeded — refusing to POST a
comment cannot un-merge a PR, and treating a comment failure as a merge
failure would be misleading (the code landed; only the git-host-visible record
of it did not). A network error or non-2xx response from the comment POST
is logged to stderr and swallowed: it never changes `loadout-merge`'s exit
code and never blocks the post-merge steps that follow. This is the one
intentionally **fail-open** step in the entire merge verb — contrast with
every gate *before* the merge call, all of which are fail-closed (§2 above,
and [docs/verbs.md](verbs.md)'s gate-chain list) — because by this point
there is no longer a "refuse the merge" outcome available to fail closed
into.

## Computed verdicts at the gate

A reviewer posted with `loadout-review post` records, in its fence, the verdict the
evaluator computed (`verdict_outcome`: `CLEAN`, `BLOCKED`, `DEGRADED` or `STALE`, at
`fence_schema_version` 3) and the findings it was computed from (`evaluated_findings`, each
with the facts the reviewer stated). `loadout-merge` does not take that outcome on trust and
does not re-judge a finding. For each required reviewer role it takes the newest valid fence
and runs the same evaluator ([review-evaluator.md](review-evaluator.md)) over the same
findings **at merge time**. No model is called at the gate.

**One rule governs every choice below.** Anything that can lower a verdict must be positively
established from trusted input, and an absent, unknown, unrecognised or unverifiable value
resolves to the worst case. The gate is a one-way barrier: it adds refusals and never clears
a block on its own authority.

### Policy is read at the live base

Dispositions and stakes are read from the PR's **live base revision**, where the base is the
one the host reports for the PR at merge time. They are read through the host API with the
run's own credential, so no local checkout is needed; an explicit `--repo-path` is an optional
local source (`git show <base>:<path>`), used instead of the host when given, the same choice
`loadout-review` makes. The `policy_base_sha` a fence or findings file records is
provenance only and is never used to pick the revision; a reviewer editing its own findings
file, or a fence recording a base with a permissive policy, changes nothing. The expected
head is the PR's current head. Consequences:

- A disposition merged to the base between review and merge **applies**, because the policy
  is on the trusted base, which the PR under review cannot have written.
- A base that gained a stricter policy, or whose disposition has expired, since the review
  **blocks** at merge.
- A policy file that cannot be read (a host error, or a base commit the named local tree does
  not hold), or no known base revision, makes the gate's computation DEGRADED. A fence that
  recorded CLEAN is then refused, naming the missing input. There is no fallback from one
  source to the other.

### Per-PR rulings at the gate

When a ruling registry is configured ([review-evaluator.md](review-evaluator.md), "Per-PR
rulings"), the gate also reads the rulings recorded for exactly this repository and pull
request, for each fence it evaluates, and applies them in the same place it applies
base-revision dispositions. Under `--report-only` this is the same chain. A role with several
fences at a head has the registry read for each of them, so a ruling clears its finding in
every fence that carries it.

- A fence carries a finding's `fingerprint` in `evaluated_findings` only when a registry is
  configured; a ruling is keyed to it, so a fence without fingerprints cannot be ruled on.
  Unlike a disposition that narrows by fingerprint (which never counts here), a ruling
  matches only a finding that carries the very fingerprint it names, so its match is shown.
- **One roster for the gate and the ruling verb.** The reviewer roles a merge requires are the
  roles the tracked gate file declares at the pull request's base commit (a floor) plus the
  `--required-reviewer` values the merge is given. `loadout-ruling record` resolves them with
  the same function (`merge.required_reviewers`), taking the flag as bare role names, so a
  role the gate enforces is a role whose fences a ruling can be attributed to. With no tracked
  gate file the floor is empty and the flags alone decide, on both verbs. The flag on
  `loadout-ruling` decides only whose comment counts as a role's verdict: who may record a
  ruling is unchanged, the flag never removes a declared role, a `name:login` pair is refused
  (a ruling is matched by fingerprint, rule and path, so a typed login would let any account's
  comment supply the fingerprint), and a role in neither the floor nor the flags is refused.
  A clean fence still never hides a blocked one at the same head; a recorded ruling is the
  only way to clear its finding, and the merge names it in `applied_rulings`.
- A ruling never clears a security-floor finding. The refusal names the finding and says it
  clears only through a dispositions entry on the base branch, or by fixing it.
- Every ruling that cleared a finding is named on stderr, in the merge result
  (`applied_rulings`) and in the attestation, with who recorded it, who requested it, when,
  its kind, the rationale, the source reference and any work-item reference. A ruling for the
  PR that cleared nothing is named with the reason (`unapplied_rulings`). A registry that is
  untrusted or unusable makes the gate's computation DEGRADED and the refusal names it.
- Every outcome, CLEAN and allowed-DEGRADED included, names the rulings that cleared a finding,
  the ones that cleared nothing, expired ones, and a registry that was not used, in its
  notices on stderr whenever a registry has something to report.
- With no registry configured, the gate's output and result are unchanged.
- **Registry trust and a documented residual.** The registry is refused on every read and write
  when it is inside any git work tree or the reviewed repository, or when it or any directory
  above it is group- or world-writable or owned by neither the process owner nor root. This
  does not stop code running as the same user as the gate: a pull request's `pre_checks`
  executed at merge time can write the registry unless the optional sandbox
  (`merge.pre_checks_sandbox`) is configured. Configure the sandbox for any deployment that
  uses a ruling registry.

### Decision table

`F` is the fence's outcome, `G` the gate's own.

| Outcome | Result |
|---|---|
| `F` or `G` is STALE | Refused, naming the fence head and the merge head. |
| `F` is CLEAN, `G` is anything else | Refused, naming the difference: the expired entry, the policy base that moved, the degraded input, the findings that now block. |
| `F` is BLOCKED, `G` is CLEAN, the fence's `findings_open` is non-empty, and **every** finding in it is individually matched by a standing disposition on the live base | Passes. The gate names each applied disposition on stderr, in the merge result and in the attestation. A disposition on some other finding clears nothing; a recorded finding that stopped blocking for any other reason, or that no disposition matches, refuses the merge and is named. A BLOCKED fence with an empty or absent `findings_open` refuses (nothing can be shown to be cleared). Finding ids are derived from file, line and rule and can be shared by findings with different messages: every finding holding the id must be covered. A disposition that narrows by `fingerprint` never counts: the fence does not carry fingerprints, so it cannot be shown to match. |
| BLOCKED in `F` or `G` (otherwise) | Refused, naming each open blocking finding and the exact `dispositions.yaml` entry that would clear it (or that the security floor forbids clearing it as `by_design`). |
| DEGRADED in `F` or `G`, nothing blocked | Refused, naming the reasons, unless `merge.evaluator.allow_degraded` is true for the role. |
| Both CLEAN | Passes, subject to the rest of the gate chain. |

A fence that recorded CLEAN but whose poster asked for `blocking` is still refused: the
poster's stricter status stands.

An evaluated fence that does not carry its `evaluated_findings` cannot be checked. It is
refused unless the fence itself already records a refusal (BLOCKED, STALE, DEGRADED without
the role allowed), in which case it is refused on what it records.

### `merge.evaluator`

Read from the tracked gate file at the PR base commit, like every other key that changes what
a merge needs (so the PR under review cannot relax it):

```yaml
merge:
  evaluator:
    allow_degraded:        # per reviewer role; default false for every role
      reviewer: false
    require_evaluated: false   # migration default
```

- **`allow_degraded`** — a mapping of reviewer role to boolean. Only a role set `true` may
  merge on a DEGRADED verdict; the merge result and the attestation record that it did and
  why. It never lets a BLOCKED or STALE verdict through.
- **`require_evaluated`** — a fence below `fence_schema_version` 3 has no computed verdict:
  its `review_status` was typed by whoever posted it. With `false` (the migration default)
  the gate applies that status exactly as before and prints a notice that the verdict was not
  evaluated. With `true` it refuses, naming the fence.

A `merge.evaluator` declaration that cannot be read is the strict one (no role may merge on a
DEGRADED verdict, evaluation required), with a warning naming the problem; it never relaxes
the gate. `--ignore-repo-gate` does not lift it.

### Seeing the outcome without merging

`loadout-merge --report-only` runs the whole chain and prints, per reviewer, the outcome the
fence recorded and the one the gate computed (`verdict <role>: fence CLEAN, gate CLEAN`), then
exits without merging. A real merge records the same lines in its JSON result
(`verdict_evaluations`) and in the attestation's `Verdicts` row.

### Open findings come from the computed set

`loadout-review post` derives the fence's `findings_open` from the computed blocking set, after
dispositions: a finding a base-revision disposition demotes to advisory is not held open. A
`findings_open` in `--state-file` is ignored with a warning. The ids are derived from the
finding's file, line and rule (`open_finding_ids` in the post result), and a later fence clears
a finding by naming that id in `cleared_claims`. The carry-forward refusal for findings an
earlier fence held open therefore operates on computed state.

## See also

- [docs/verbs.md](verbs.md) — the full `loadout-merge` gate chain (steps
  1–9) this document's authority check and attestation mark are one and the
  last part of, respectively.
- [docs/provisioning.md](provisioning.md#merge-gate-config-homes) —
  the full `.clagentic/loadout/config.yaml` `merge:` section schema
  (`pre_checks`, `merge_requirements`, `required_reviewer_roles`,
  `authorized_roles`) and the deployment-tier identity sections
  (`builder_identity`, `review.reviewer_logins`), including the design calls
  recorded for the repo-tier/deployment-tier split.
- `src/clagentic_loadout/merge/authority.py` — the `AuthorityProvider`
  protocol, `StaticRoleAuthorityProvider`, and `check_authority`; read the
  module and class docstrings for the exact contract this document
  summarizes.
- `src/clagentic_loadout/merge/attestation.py` — `build_attestation_body`;
  read the function docstring for the exact field-by-field provenance of
  the attestation comment.
