# Identity providers

An identity is **verified**, **explicitly declared**, or **refused**. It is never
inferred from a parent session or from the operating-system user. The identity
provider seam (`clagentic_loadout.identity`) resolves one `Actor` from the provider
named by `identity.provider`, or raises a typed `IdentityError`.

With `identity.provider` set, the caller-bound verbs take their identity from the
seam (see [Verb binding](#verb-binding)). With it unset, every verb keeps its
current attestation behavior exactly, whether or not legacy `attestation` keys are
present: the no-default refusal below applies to `clagentic_loadout.identity.resolve()`
and `loadout-identity whoami` only, not to verbs, until the legacy chain is removed.

## Configuration

User-level config (`~/.config/clagentic/loadout/config.yaml`), section `identity`:

| Key | Meaning |
|---|---|
| `provider` | `gatekeeper`, `jwks`, `static`, `prefer-assertion`, `assertion-or-legacy` or `module`. No default. |
| `provider_module` | Dotted module name implementing `resolve(*, env, config)`. |
| `assertion_provider` | `gatekeeper` or `jwks`: which assertion provider `prefer-assertion` and `assertion-or-legacy` answer with. |
| `gatekeeper.jwks` | Key-set locator of the one trusted issuer (`file:///`, `unix:///`, `https://`, loopback `http://`). |
| `gatekeeper.issuer` | Issuer identifier. Defaults to `gatekeeper.jwks` when that is a `file:///` or `unix:///` locator. |
| `issuers` | For `jwks`: a list of `{iss, jwks?}`. `jwks` defaults to the locator the `iss` form names. |
| `static.actor`, `static.roles` | For `static`: the declared actor and role list. |
| `caller_names.actor_prefix`, `caller_names.roles` | Optional. A `--caller` naming the actor (see [Verb binding](#verb-binding)): the prefix to add to a bare name, and a map from name to one role. |
| `staging_root` | Optional template for the body-staging and acquire-scratch root under `gatekeeper` and `jwks` (see [Verb binding](#verb-binding)). Placeholders `{actor_name}`, `{sid}`, `{spawn}`. Unset: `<dir>/tmp`. |

Nothing is read from a repository-local config: a cloned repository cannot choose
the provider, the issuers or the module.

## The three classes

**Class 1, `gatekeeper`.** Reads `CLAGENTIC_IDENTITY_DIR/identity.jwt` and verifies
it against `identity.gatekeeper`. `Actor.provider` is `gatekeeper`.

**Class 2, `jwks`.** The same verification against any issuer listed in
`identity.issuers`, including a launcher that signs its own assertions. No
gatekeeper is installed or contacted. `Actor.provider` is `jwks:<iss>`. A
user-supplied provider is class 2 as well: set `identity.provider_module`; the
module's `Actor` is used and `Actor.provider` is recorded as `module:<name>`
whatever the module wrote there.

**Class 3, `static`.** Returns the declared actor with `self_declared: true`,
`provider: declared` and grade `declared`. It reads no environment and consults no
issuer. What an attestation provider adds over `static` is a signature: a verified
assertion carries an issuer's statement of who is acting, under which role ceiling
and until when, bound to one delivery directory. A `static` actor carries only what
the local config says.

Under `gatekeeper` and `jwks` there is no fallback of any kind. An unset,
unreadable or invalid `CLAGENTIC_IDENTITY_DIR`, or an invalid `identity.jwt`,
refuses with an error naming the variable (delivery failures) or the failure code
(verification failures).

## The provider interface

```python
class Provider(Protocol):
    def resolve(self, env: Mapping[str, str]) -> Actor: ...
```

A provider returns an `Actor` or raises `IdentityError`; it never returns a partial
answer. `Actor` fields: `sub`, `actor` (the outermost `act.sub`, else `sub`),
`chain` (current actor outward, ending with `sub`), `roles` (a ceiling, never a
grant), `grade`, `origin`, `dir`, `sid`, `spawn`, `provider`, `contract_version`
and `self_declared`.

## Accepted contract majors

The verifier accepts identity assertion contract majors **1 and 2** and refuses any
other `cia` as `contract_version_unsupported`. Major 2 adds the origin `driven`:
such an assertion is always grade `workload` and never names a human `sub`, so a
`driven` actor is never reported as human. Verification follows the published
order, so two verifiers agree on the code for a token with several faults. The
vendored conformance vectors for both majors run in the test suite
(`tests/identity_contract/`, pinned to a recorded producer commit).

## `prefer-assertion`

A transition value for a deployment that has not yet delivered assertions
everywhere. With `assertion_provider` naming the assertion class:

* `CLAGENTIC_IDENTITY_DIR` set: the assertion provider answers, and refuses on any
  failure.
* `CLAGENTIC_IDENTITY_DIR` unset: the only answer accepted is a `scope: per-spawn`
  sidecar adapter that resolves on its own `session_id_env`. A configured env value,
  an unscoped adapter config, the session scope, the OS user and any miss all refuse
  with `identity_dir_missing`, naming the variable. Each attempt writes one notice
  to stderr and emits one telemetry event (schema `identity-fallback/v1`) to the
  configured sink.

## `assertion-or-legacy`

Optional, and not the default: a value you choose when some callers receive a signed
assertion and others have only the legacy attestation chain, and you want each to
resolve the way it would on its own. With `assertion_provider` naming the assertion
class:

* `CLAGENTIC_IDENTITY_DIR` set: the assertion provider answers exactly as
  `gatekeeper` (or `jwks`) does for the same delivery, and refuses with the same
  error code and exit on any failure.
* `CLAGENTIC_IDENTITY_DIR` unset or blank: the full legacy attestation chain answers
  (layer 1 configured env, the configured bound identity, sidecar adapters of any
  scope, the OS user), exactly as with `identity.provider` unset. `loadout-identity
  whoami` reports origin `legacy` and provider `legacy:<source>`, and never refuses
  with `provider_unset` or `identity_dir_missing` for want of legacy attestation keys.
  The assertion issuer config is not read on this path.

It adds no refusal, stderr notice or telemetry event of its own: every refusal under
it is one the assertion provider or the legacy chain already raises. It differs from
`prefer-assertion`, which on an unset directory accepts only a per-spawn sidecar,
refuses every other legacy source, and writes a notice and an event for each attempt.
A caller-bound verb with the directory unset binds through its existing legacy chain,
so its result reports provider `legacy` and `self_declared` `false`.

## Verb binding

With `identity.provider` set, `loadout-push`, `loadout-review` and `loadout-review-post`,
`loadout-acquire`, `loadout-merge`, `loadout-close-pr`, `loadout-post-merge`,
`loadout-git-host-api`, `loadout-ruling` and `loadout-stage-body` bind `--caller` /
`--role` to the seam's `Actor` before any I/O, and `loadout-release-dispatch` does the
same for the role whose secret it reads. The mapping from an actor to the role a verb
mints is:

| Request | Result |
|---|---|
| `--caller`/`--role` names a role in `Actor.roles` | that role is minted |
| `--caller`/`--role` names the actor (the actor string, or the actor string minus `caller_names.actor_prefix`) and `caller_names.roles` maps it to a role the actor holds | that role is minted |
| `--caller`/`--role` names the actor, has no `caller_names.roles` entry, and the actor holds exactly one role | that role is minted |
| `--caller`/`--role` names the actor, has no entry, and the actor holds none or several; or its entry names a role the actor does not hold | refused; the message names the roles present and `identity.caller_names.roles` |
| `--caller`/`--role` names anything else | refused; the message names the role requested and the roles present |
| omitted, and the actor holds exactly one role | that role |
| omitted, and the actor holds none or several | refused; the actor must name a role |
| the provider refuses (for example `gatekeeper` or `jwks` with `CLAGENTIC_IDENTITY_DIR` unset) | refused, naming the variable; never a parent, session or OS-user identity |

A name binds only the check: every verb passes the `--caller`/`--role` value
downstream exactly as given (credential minting, merge authority, reviewer fence,
staging key), so one value behaves the same with and without a provider.
`identity.caller_names` is optional and read only when a value is not a held role:

```yaml
identity:
  provider: static
  static: {actor: "svc:builder-one", roles: [builder, reviewer]}
  caller_names:
    actor_prefix: "svc:"          # --caller builder-one names svc:builder-one
    roles: {builder-one: builder} # needed when the actor holds several roles
```

With a single-role actor, `actor_prefix` alone is enough. It is never read on the
legacy path or for an answer that carries no role list.

A refusal exits with the verb's existing caller-mismatch code (numbers unchanged).
`loadout-stage-body` had no such code and uses `5`. The role list is a ceiling, never
a grant: credential minting and merge authority still decide what the role may do.
An answer from the `prefer-assertion` per-spawn sidecar carries no role list and binds
by name equality, as the legacy chain does.

Every JSON result a binding verb prints carries two additive fields, `provider`
(`gatekeeper`, `jwks:<iss>`, `declared`, `module:<name>`, `legacy:<source>`, or
`legacy` when `identity.provider` is unset, or is `assertion-or-legacy` with no
delivery directory) and `self_declared` (`false` when
`identity.provider` is unset, or is `assertion-or-legacy` with no delivery directory). The merge attestation comment carries them as
`Identity provider` and `Self-declared` rows. `loadout-git-host-api` prints the
host's response unchanged and adds nothing.

Under `gatekeeper` and `jwks`, `--body-env` staging and `loadout-acquire` scratch move
to `<dir>/tmp/` (so `<dir>/tmp/clagentic-loadout/` for bodies), where `dir` is the
verified delivery directory. The body's stamp records the staging actor and the
assertion `jti`, and a read by a different actor is refused. Under `static`, `module`
and with the provider unset, `$TMPDIR` applies as before.

The delivery directory may be a place a session cannot write. `identity.staging_root`
moves the root elsewhere, for example `<scratch-root>/{actor_name}/{sid}`. Placeholders:

| Placeholder | Value |
|---|---|
| `{actor_name}` | The verified actor string after its `kind:` prefix (the whole string when it has none). |
| `{sid}` | The assertion `sid` claim, always present. |
| `{spawn}` | The assertion `spawn` claim, when the assertion carries one. |

The template must name `{sid}` or `{spawn}`, so two spawns, and so two actors, never
share a root. At config load the key is refused as `config_invalid`, naming the key and
the template, when it is not a string, uses another placeholder, or names neither. At
staging time a verb is refused, naming the key, the template and the placeholder or
value, when a referenced claim is absent, a claim value is not a single path component
(empty, `.`, `..`, or containing `/`), or the rendered path is not absolute. There is no
fallback to another root. The root and its `clagentic-loadout` subdirectory are created
with mode `0700` when absent. With the key unset the root is `<dir>/tmp/`, exactly as
before. The key has no effect under `static`, `module` or a legacy answer. It is a
location only: it grants no access to the directory it names.

`loadout-doctor` reports the provider, the key-set sources it trusts, the
`self_declared` flag and the number of `prefer-assertion` fallbacks the filesystem
telemetry sink has recorded (advisory, always exit 0). Under `assertion-or-legacy` it
reports `self_declared` false and the key-set sources of the configured assertion
provider, with no fallback count.

## No provider configured

With no `identity.provider` and no `provider_module`: a config that still carries
legacy `attestation` keys (any key present, even an empty `sidecars: []`) keeps the
legacy chain; a config with neither refuses with `provider_unset`, naming
`identity.provider`. This refusal is the seam's own: no verb applies it yet, so on a
config without `identity.provider` every verb resolves through the legacy chain.

## `loadout-identity`

```
loadout-identity whoami
loadout-identity verify TOKEN [--dir DIR | --peer-id ID] [--now SECONDS]
```

`whoami` prints the `Actor` as JSON, including `provider` and `self_declared`.
`verify` checks one token against the trusted issuers of the configured assertion
provider and prints the claims. Exit codes: `0` ok, `1` usage, and one code per
failure kind in `90`-`111`:

| Code | Kind | Code | Kind |
|---|---|---|---|
| 90 | `identity_dir_missing` | 101 | `not_yet_valid` |
| 91 | `identity_dir_unsafe` | 102 | `ttl_exceeded` |
| 92 | `identity_file_missing` | 103 | `claims_invalid` |
| 93 | `identity_file_symlink` | 104 | `audience_unexpected` |
| 94 | `malformed` | 105 | `audience_mismatch` |
| 95 | `alg_not_allowed` | 106 | `dir_mismatch` |
| 96 | `issuer_untrusted` | 107 | `provider_unset` |
| 97 | `key_not_found` | 108 | `config_invalid` |
| 98 | `signature_invalid` | 109 | `jwks_unavailable` |
| 99 | `contract_version_unsupported` | 110 | `provider_module_invalid` |
| 100 | `expired` | 111 | `attestation_refused` |

`loadout-doctor` reports the configured provider and whether it is self-declared, as
an advisory that never fails the run.

## Dependency

Signature verification uses PyJWT (`PyJWT>=2.8`) with its crypto backend
`cryptography` (`>=42`). Header and claim handling, the algorithm allow-list, issuer
trust, key selection and the directory binding are this package's own code; the
library only checks the signature once the key has been chosen from the trusted
issuer's key set.
