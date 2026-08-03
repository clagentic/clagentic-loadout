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

As of this writing, reading `merge.authorized_roles` /
`required_reviewer_roles` into `StaticRoleAuthorityProvider` / the
reviewer-verdict gate as the CLI's own flag *defaults* is a named follow-up
(see [docs/provisioning.md](provisioning.md#merge-gate-config-homes));
today a caller (a dispatch/lead layer) reads the config and passes the
resolved roles via `--authorized-role` / `--required-reviewer` explicitly.
The schema and its `loadout-doctor` validation are landed; the CLI-wiring
slice is not.

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
