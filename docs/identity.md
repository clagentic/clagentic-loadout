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
| `provider` | `gatekeeper`, `jwks`, `static`, `prefer-assertion` or `module`. No default. |
| `provider_module` | Dotted module name implementing `resolve(*, env, config)`. |
| `assertion_provider` | `gatekeeper` or `jwks`: which assertion provider `prefer-assertion` answers with. |
| `gatekeeper.jwks` | Key-set locator of the one trusted issuer (`file:///`, `unix:///`, `https://`, loopback `http://`). |
| `gatekeeper.issuer` | Issuer identifier. Defaults to `gatekeeper.jwks` when that is a `file:///` or `unix:///` locator. |
| `issuers` | For `jwks`: a list of `{iss, jwks?}`. `jwks` defaults to the locator the `iss` form names. |
| `static.actor`, `static.roles` | For `static`: the declared actor and role list. |

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
| `--caller`/`--role` names anything else, including the actor's own name | refused; the message names the role requested and the roles present |
| omitted, and the actor holds exactly one role | that role |
| omitted, and the actor holds none or several | refused; the actor must name a role |
| the provider refuses (for example `gatekeeper` or `jwks` with `CLAGENTIC_IDENTITY_DIR` unset) | refused, naming the variable; never a parent, session or OS-user identity |

A refusal exits with the verb's existing caller-mismatch code (numbers unchanged).
`loadout-stage-body` had no such code and uses `5`. The role list is a ceiling, never
a grant: credential minting and merge authority still decide what the role may do.
An answer from the `prefer-assertion` per-spawn sidecar carries no role list and binds
by name equality, as the legacy chain does.

Every JSON result a binding verb prints carries two additive fields, `provider`
(`gatekeeper`, `jwks:<iss>`, `declared`, `module:<name>`, `legacy:<source>`, or
`legacy` when `identity.provider` is unset) and `self_declared` (`false` when
`identity.provider` is unset). The merge attestation comment carries them as
`Identity provider` and `Self-declared` rows. `loadout-git-host-api` prints the
host's response unchanged and adds nothing.

Under `gatekeeper` and `jwks`, `--body-env` staging and `loadout-acquire` scratch move
to `<dir>/tmp/` (so `<dir>/tmp/clagentic-loadout/` for bodies), where `dir` is the
verified delivery directory. The body's stamp records the staging actor and the
assertion `jti`, and a read by a different actor is refused. Under `static`, `module`
and with the provider unset, `$TMPDIR` applies as before.

`loadout-doctor` reports the provider, the key-set sources it trusts, the
`self_declared` flag and the number of `prefer-assertion` fallbacks the filesystem
telemetry sink has recorded (advisory, always exit 0).

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
