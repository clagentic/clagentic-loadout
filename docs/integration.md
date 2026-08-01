# Runtime integration contract

This is the CONSUMER-facing contract for a harness spawning a loadout-driven
agent: exactly what a spawn environment must (and may) supply for the
`clagentic: loadout` verbs to run without a hand-configured shell session.

See [docs/provisioning.md](provisioning.md) for the ALLOWLIST side (which
verbs a role may invoke) and [docs/verbs.md](verbs.md) for what each verb
does. This document covers the CREDENTIALS side: what must be TRUE of the
environment a verb actually runs in.

Vendor-neutral throughout: "git host" names the vendor-neutral platform
concept (Forgejo today; the naming holds if a second self-hosted backend is
ever added). `forgejo`/`github` still name the actual vendor wherever the
dual-platform backend split needs to distinguish them (`--platform`, the
`*_backend.py` modules) — this document does not erase that distinction.

## The two things a spawn env must supply

1. **Where the git host is** — the base URL `clagentic: loadout`'s Forgejo-path verbs
   call.
2. **How to get a token** — the credential-provider seam every verb
   resolves its git-host token through.

Neither requires a hardcoded host or a broker-specific client compiled into
`clagentic: loadout`. Both are resolved through config seams with sane defaults.

## 1. Git-host base URL

Consulted by: `git-host-api` (`transport.git_host_api`), `review-post` and
`merge` when `--platform forgejo`, and `push` when targeting Forgejo. Not
consulted at all for a GitHub-platform call — `github_backend` pins its own
public API base.

Resolution precedence, highest first (`transport.git_host_api._resolve_git_host_base`):

1. **`--git-host-base-url` flag** — explicit, always wins when non-empty.
   Every verb above exposes this flag directly.
2. **`CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL`** (env) — the branded var. This
   is the PRIMARY env-var source once no explicit flag is given. (A
   pre-rename `CLAGENTIC_LOADOUT_FORGE_BASE_URL` fallback tier that used to
   sit here has been removed — scorched earth, BREAKING for any
   deployment still exporting the old var; set the `GIT_HOST` name above
   instead.)
3. **A configurable compat-alias env var** — the NAME of this var is itself
   configurable via `CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL_COMPAT_ALIAS`
   (env); when that naming var is unset, the alias name defaults to
   `FORGEJO_BASE_URL`. This lets a harness that already exports a
   differently-named legacy base-URL var (pointing at the real git host)
   work with zero env-export change on that side. Consulted ONLY when tier
   2 above is unset/empty — a fallback, never a co-equal source.
4. **The user-level config file** — `~/.config/clagentic/loadout/config.yaml`
   (override the root via the `config_root` parameter in tests; there is no
   CLI/env override for the root in production — this is intentionally the
   SAME file and the SAME loader (`transport.provider_config.load_user_config_section`)
   the credential-provider config tier below uses), `git_host:` section,
   `base_url` key:

   ```yaml
   git_host:
     base_url: https://git.example.com
   ```

   This is the RELEASED, no-hand-export mechanism: `scripts/install.sh`
   WRITES this key at install time via `--git-host-base-url URL` (or
   `CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL` in the installer's own
   environment) — one install, and no deployment has to hand-export an env
   var into every spawn just to point `clagentic: loadout` at its git host. If no value
   is supplied at install time, the installer writes a clearly-commented
   TEMPLATE line (never a dead `localhost`, never a baked operator host)
   for a one-line hand-edit later. Re-running the installer never clobbers
   an already-seeded real value unless `--git-host-base-url` is supplied
   again that run (explicit re-supply is consent to replace it). Treat env
   vars (tiers 2-3) as the override layer for one-off invocations or CI,
   and this installer-written config file as the steady-state source of
   truth.

5. **`http://127.0.0.1:3000`** — the final fallback. This is a
   placeholder-only default (never an operator host, per repo rule 1); it
   only matters for `--help`/argument-parsing smoke tests. Any real
   invocation must resolve to a real base URL through one of tiers 1-4.

A harness wiring a NEW deployment should run `scripts/install.sh
--git-host-base-url <url>` (tier 4, installer-seeded) rather than
hand-exporting tier 2 in every spawn script. Tiers 2-3 exist for
override/transition scenarios, not as the primary mechanism.

## 2. Credentials (token resolution)

Consulted by: every verb, for every platform it targets. One seam:
`transport.credential_provider.resolve_token(role, provider, *, repo=None)`,
reached through the single per-platform factory
`transport.provider_config.resolve_platform_provider`.

**Zero external dependency by default.** No credential-minting service is
imported, required, or hardcoded anywhere in this seam. A deployment that
configures nothing gets `StaticTokenProvider` for both platforms —
unchanged, standalone behavior.

**Optional: a minting provider MAY report a verified identity alongside the
token.** `CommandTokenProvider`'s `resolve_token` may return
either a bare token string (unchanged default) or a `ResolvedToken`
carrying `token` plus an optional `app_slug` — a value the MINTING PROCESS
ITSELF verified (e.g. checked its own App slug against a broker at mint
time), not an operator-typed config string naming the same fact. This is
OPT-IN, per platform: set `token_command_emits_json_forgejo` /
`token_command_emits_json_github` (or the
`CLAGENTIC_LOADOUT_TOKEN_COMMAND_EMITS_JSON_FORGEJO` /
`_GITHUB` env vars) to true, and the configured command's stdout is parsed
as `{"token": "...", "app_slug": "..."}` JSON instead of read as a bare
string. Unset (the default): stdout is read as a bare token exactly as
before this feature existed, REGARDLESS of what the configured command
prints — this class never content-sniffs stdout to guess its shape. See
`transport.credential_provider`'s module docstring ("PROVIDER-SUPPLIED
VERIFIED IDENTITY") for the full mechanism, and
[docs/provisioning.md](provisioning.md)'s "Derived commit identity for a
recognized crew caller" section for how `push`'s bot-commit-identity
resolution consumes the resulting `app_slug`.

**`--caller`/`--role` is an already-attested value, never a free CLI arg.**
Every verb consumes this flag as an
opaque config key — the string that selects a role-scoped credential/
App-slug/authority entry — never as an identity claim it authenticates
itself. The harness/guard-hook that spawns and invokes a `clagentic: loadout` verb owns
verifying which role a given spawn is entitled to act as (e.g. by minting a
credential scoped to that role in the first place) BEFORE placing that
string on the invoking command line; `clagentic: loadout` has no visibility into how a
harness made that decision and, being orchestration-agnostic (this
package's CLAUDE.md rule 2), never reaches into a harness-specific identity
sidecar/side-channel to re-derive one — that would point-to-point couple
this seam to one orchestration layer's transport (the relay lesson). See
[docs/merge-authority.md](merge-authority.md)'s "The built-in fallback"
section for the full statement of this boundary on the merge-authority
side, and `transport.credential_provider`'s module docstring for the
parallel statement on token resolution.

**`transport.git_host_api` additionally BINDS an explicit `--caller` to
this process's own attested invoking identity, fail-closed.** This is
layer (1)->(2) of the three-layer trust model — attested
invoking identity -> crew role (`--caller`) -> credential grantor — and is
native to `clagentic: loadout`'s transport, not delegated to an external wrapper. An
EXPLICIT `--caller` value that does not equal the identity resolved by
`transport.attestation.resolve_identity` is refused (`GitHostApiError`,
`EXIT_CALLER_INVOKER_MISMATCH`) BEFORE any token mint or network I/O, with
no override — even a role a downstream `TokenProvider`/`AuthorityProvider`
allowlist would otherwise admit. An OMITTED `--caller` is never checked
against the attested identity (it carries no identity claim); this is
unchanged, pre-existing default-to-`DEFAULT_ROLE` behavior.

`transport.attestation.resolve_identity` resolves WHAT the identity is,
via a fixed, config-selectable chain (mirrors clagentic-gatekeeper's
`internal/attestation` package):

1. **Configured provider** — `CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_ENV` (env)
   or the user-level config file's `attestation.identity_env` key names
   ANOTHER env var this process's own spawn env carries the identity under.
2. **Sidecar adapter** — `CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH`
   (env) or `attestation.identity_sidecar_path` names a file whose stripped
   first-line content is the identity. `clagentic: loadout` does not assume any
   specific harness writes this file — the path is supplied by config,
   never hardcoded.
3. **Built-in fallback** — the OS-reported invoking user
   (`getpass.getuser()`). Always available, so a bare install still has an
   attested source rather than failing open with no identity at all.

A deployment that never configures a spawn-side `--caller` distinct from
its own OS user (the common single-role-per-host shape) gets a working
attestation source for free from layer 3 with zero configuration.

### Wiring per-spawn subagent attestation (multi-agent-dispatch consumers)

The chain above is generic across every deployment shape. This section is
for the specific shape where it goes wrong: a consumer whose dispatch loop
spawns MULTIPLE concurrent sub-processes under DIFFERENT role identities
from one parent process (a lead dispatching a builder sub-agent, a builder
dispatching a reviewer sub-agent, etc.) and expects `--caller` to resolve
each sub-process as ITSELF. Getting this wrong costs a full debugging
session, because the failure mode does not look like a configuration gap —
it looks like `--caller` resolving to the WRONG identity, or a raw opaque
value, with no obvious cause.

**Why layer 1 (`identity_env`) cannot do this.** `CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_ENV`
names ANOTHER env var, and that named var's value is read VERBATIM as the
attested subject — see `_ConfiguredEnvProvider` in
`transport.attestation`. This is a single, spawn-environment-wide
indirection: whatever env var it points at carries ONE value for every
process that inherits that spawn environment. A dispatched sub-process
normally inherits its parent's environment (that is what "dispatch" means
for most harnesses — a sub-process spawn, not a fresh login session), so
pointing `identity_env` at:

- **A var carrying the PARENT's own identity** (e.g. a harness-level
  "who is running this session" variable) resolves the PARENT's identity
  for every sub-process too — a builder sub-agent dispatched from a lead
  process attests as the LEAD, not as `builder`.
- **A var carrying an opaque per-spawn id** (a dispatch/session id, a raw
  process/agent identifier that is NOT the role name `--caller` expects)
  resolves that raw id verbatim — `bind_caller` then compares an explicit
  `--caller builder` against `identity.subject == "<opaque-spawn-id>"`,
  which never matches a role name, and refuses.

This is not a configuration mistake to route around with a cleverer env
var name — it is STRUCTURAL. `identity_env` has exactly one indirection
level (an env-var name that points at another env-var value), and an env
var is inherently spawn-environment-scoped, not per-process-scoped, when a
harness spawns sub-processes by inheriting environment rather than
constructing a fresh one per spawn (mirrors the spawn-scoped-env framing in
clagentic-gatekeeper's own consumer-wiring guide — identity must
be bound to the SPAWN, not to a shared parent environment, and not
re-derived per-command either). No value of `identity_env` closes this gap
for a multi-agent-dispatch consumer.

**Where the role name actually lives: the per-spawn sidecar file.** The
ONLY thing carrying a distinct value per sub-process is whatever the
dispatch loop itself writes freshly for THAT spawn — a file. Layer 2
(`_SidecarFileProvider`) reads the STRIPPED FIRST LINE of a file at a path
named by `CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH` (env) or the
config file's `attestation.identity_sidecar_path` key. `clagentic: loadout` does not
write this file, does not name it, and does not assume any particular
harness format beyond "one identity value, first line" — the consumer's
own dispatch loop is responsible for:

1. **Composing a per-spawn path** before invoking the sub-process (e.g.
   under a dispatch-id- or role-namespaced directory the consumer's own
   dispatch loop controls — the exact naming scheme is the consumer's
   choice; `clagentic: loadout` has no opinion on it).
2. **Writing the role name as that file's first line**, freshly, for THIS
   spawn, before the sub-process's first loadout-verb invocation.
3. **Setting `CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH` in THAT
   sub-process's own spawn environment** (not the parent's) to point at
   the path composed in step 1.

`clagentic: loadout` reads this path STATICALLY — it resolves whatever
`CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH` names in the resolving
process's own environment, at read time, with no dispatch-id lookup, no
discovery, and no fallback search across candidate paths. The consumer
composes the path (naming convention, directory layout, cleanup); `clagentic:
loadout` only reads whatever that env var points at, in the calling process's own
environment. See `_SidecarFileProvider`'s docstring in
`transport.attestation` for the symlink-refusal hardening on this read —
the configured path is `lstat`'d before any open, and a symlink or other
non-regular directory entry at that path is a hard `AttestationError`, not
a silent fall-through.

**The end state that works: drop `identity_env` entirely.** A
multi-agent-dispatch consumer's attestation configuration for a dispatched
sub-process needs ONLY the sidecar env var set, freshly, per spawn.
`CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_ENV` should be UNSET in that spawn's
environment — not pointed at anything. With it unset, layer 1 declines
(nothing configured, "nothing to offer," not a failure — see
`resolve_identity`'s resolution order above) and the chain falls through
to layer 2, which resolves the freshly-written per-spawn sidecar value.
This is not the first place a consumer reaches — `identity_env` looks like
the obvious single-env-var wiring point, which is exactly why getting this
wrong is the default outcome, not an edge case.

Worked example — a dispatch loop spawning a `builder`-role sub-process:

```
# Consumer's own dispatch loop, BEFORE spawning the sub-process:

sidecar_path="<per-spawn-sidecar-dir>/<dispatch-id>.identity"
printf '%s\n' "builder" > "$sidecar_path"

# The sub-process's own spawn environment carries ONLY the sidecar var --
# CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_ENV is NOT set here:
export CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH="$sidecar_path"

# Spawn the sub-process. Inside it, any loadout verb invoked with an
# explicit --caller builder now resolves against the sidecar's "builder"
# value (layer 2) rather than the parent's inherited identity or an
# opaque spawn id (layer 1) -- bind_caller sees a match and proceeds.
loadout-git-host-api --caller builder ...
```

A second concurrently-dispatched sub-process (e.g. a `reviewer`-role
sub-agent spawned from the same parent at the same time) gets its OWN
`sidecar_path` (a distinct `<dispatch-id>.identity` file) and its OWN
`CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH` value in ITS OWN spawn
environment — never the same path two concurrent spawns both read, and
never a single shared env var value both processes inherit. This mirrors
the caller-namespaced staging convention `--body-env` already uses (see
this document's "`--body-env`: the harness-side staging process" section)
for the identical reason: a value that varies per invocation must live at
a path that varies per invocation, never in one shared location two
concurrent callers could race on.

### Failure modes and their exact symptoms

| Consumer wiring | What `--caller <role>` resolves against | Symptom |
| --- | --- | --- |
| `identity_env` points at a var carrying the PARENT process's own identity | The parent's identity (inherited by the sub-process's environment) | `bind_caller` refuses with `EXIT_CALLER_INVOKER_MISMATCH` (or, worse, silently "succeeds" if the parent's identity happens to equal the role string, masking the misconfiguration until a second, differently-named sub-process is dispatched) — the sub-process is attested as its PARENT, never as its own role |
| `identity_env` points at a var carrying an opaque per-spawn id (dispatch id, session id, PID — not a role name) | The raw opaque value, read verbatim | `bind_caller` refuses with `GitHostApiError(code=EXIT_CALLER_INVOKER_MISMATCH)`: `--caller <role>` does not match the attested identity `<opaque-spawn-id>` (resolved via the `'configured'` attestation layer) |
| No attestation source configured at all (`identity_env` unset, no sidecar path set) | Layers 1 and 2 both decline; layer 3 resolves | The built-in OS-reported invoking user (`getpass.getuser()` — commonly a single shared OS/container user, e.g. `root` in an unconfigured container) — every sub-process across every role attests as the SAME OS user, so `--caller <role>` mismatches unless `<role>` happens to equal that OS username |
| Sidecar path set correctly, per spawn, `identity_env` unset | The freshly-written per-spawn sidecar value (layer 2) | Resolves correctly — this is the end state above |

In every failure row, the refusal happens BEFORE any token mint or network
I/O (`bind_caller` runs first) — a misconfigured consumer sees a clean,
immediate `EXIT_CALLER_INVOKER_MISMATCH` refusal rather than a token fetch
that silently succeeds under the wrong identity.

See [docs/provisioning.md](provisioning.md) for the separate ALLOWLIST
side of provisioning a dispatched sub-process's role (which verbs a role
may invoke) and [docs/loadout-init.md](loadout-init.md) for the guided
per-repo config setup workflow — neither of those covers the attestation
wiring documented in this section, which is a spawn-ENVIRONMENT concern
orthogonal to both.

### Which provider a spawn env gets, per platform

Selection precedence per platform (`forgejo` / `github` resolved
independently — a deployment may point each at a different provider),
highest first:

1. **Env var**: `CLAGENTIC_LOADOUT_TOKEN_PROVIDER_FORGEJO` /
   `CLAGENTIC_LOADOUT_TOKEN_PROVIDER_GITHUB` = `static` | `command`, plus
   `CLAGENTIC_LOADOUT_TOKEN_COMMAND_FORGEJO` / `_GITHUB` for the command's
   argv string (consulted only when the provider kind is `command`).
2. **User-level config file**: the SAME `~/.config/clagentic/loadout/config.yaml`
   the git-host base URL's config tier reads, `credentials:` section,
   `token_provider_forgejo` / `token_provider_github` (`static` |
   `command`) and `token_command_forgejo` / `token_command_github` (the
   argv string).
3. **Default**: `static` — `StaticTokenProvider` reads a role-scoped `.env`
   file at `~/.config/clagentic/loadout/roles/<role>.env`, key
   `CLAGENTIC_LOADOUT_GIT_HOST_TOKEN` (mode-600 enforced).

A repo-local `.clagentic/loadout/config.yaml`'s `credentials:` section is
**never** read for provider selection, on either platform — see
`transport.provider_config`'s module docstring for why (a cloned repo's own
committed config must never be able to name the command that mints a
caller's git-host token).

### `command`-provider spawn-env requirements

If a role uses the `command` provider kind for a platform, the spawn
environment must make the configured command's own dependencies available
(e.g. a network path to whatever it self-fetches from, or a mounted
credential file it reads) — `clagentic: loadout` execs the command with `shell=False`
and reads the token from its stdout; it does not manage the command's own
runtime requirements.

### Namespace restriction (push, merge)

`push` and `merge` optionally restrict the target owner via
`CLAGENTIC_LOADOUT_ALLOWED_NAMESPACES` (comma-separated) or an explicit
`--allowed-namespace` flag (repeatable). Unset/empty is PERMISSIVE (no
restriction) — set this in the spawn env for any deployment that wants a
hard namespace allowlist enforced at the verb level.

## Minimal spawn-env checklist

For a role that only calls Forgejo-path verbs with the `static` credential
provider and no namespace restriction, a harness needs to guarantee, before
first invocation:

- `~/.config/clagentic/loadout/config.yaml` exists with a `git_host:
  base_url:` key. PREFERRED: run `scripts/install.sh --git-host-base-url
  <url>` once at install time. Override/transition case:
  `CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL` is exported in the spawn env
  instead.
- `~/.config/clagentic/loadout/roles/<role>.env` exists, mode 600, with
  `CLAGENTIC_LOADOUT_GIT_HOST_TOKEN=<token>`.
- The role's permission allowlist fragment (see
  [docs/provisioning.md](provisioning.md)) is landed in the harness's own
  settings so the verb's console-script invocations are not blocked by a
  permission-prompt wall.
- **If the harness ever invokes `loadout-merge --repo-path <dir>`** (opting
  into post-merge automation, see the `loadout-merge` section of
  [docs/verbs.md](verbs.md)): each configured step needs a real value for
  any env var it depends on (e.g. `HOME`). `post_merge_steps` subprocesses
  with no leading `VAR=VALUE` prefix inherit the invoking process's own
  environment (`os.environ`) UNCHANGED, layered under the deployment
  env-override seam (see `verbs.md`'s "Deployment env-override
  seam" subsection) — `.clagentic/loadout/config.yaml` is repo-local
  COMMITTED config and can never carry a machine-specific value (CLAUDE.md
  rule 1), so a harness has two supported ways to supply one:

  1. **Export a real value in the invoking process's own environment**
     before exec'ing `loadout-merge` (works whenever the harness's own
     spawn env already carries the right value, e.g. an unmodified `HOME`
     inherited normally).
  2. **The deployment env-override seam** —
     `CLAGENTIC_LOADOUT_POST_MERGE_ENV_<NAME>` (env) or the user-level
     config file's `post_merge_env:` section — for a harness whose own
     spawn env is deliberately stripped or isolated (e.g. a per-agent
     isolated-`HOME` spawn harness that cannot simply export more into
     itself). This is the RELEASED mechanism a deployment installs once
     rather than relying on ambient inheritance.

  A `post_merge_steps` entry that itself fails fast on an empty/unset
  required var (like this repo's own `scripts/install.sh`) exits
  non-zero with `EXIT_POST_MERGE_FAILED` when neither channel above
  supplies it.

Nothing else is required for the standalone (non-minted) path. A deployment
adding a `command`-kind provider, GitHub App minting, or a namespace
restriction layers those in via the env vars / config keys above — none of
it changes this baseline.

## 3. Envelope enforce mode (a worked example, not a wired behavior)

`clagentic_loadout.envelope.validate_envelope()` validates a
dispatch envelope against the published envelope-in.json / envelope-out.json
contract and, by default (`mode="enforce"`), raises
`EnvelopeValidationError` when the envelope is invalid rather than returning
an error list a caller might forget to check. `mode="warn"` keeps the
original list[str]-return contract for a caller that wants to log-and-continue
instead.

`clagentic: loadout` does not own a hook, a dispatch transport, or any deployment's gate —
it only publishes the validation call. A deployment wires enforce mode into
its own gate however it dispatches agents; one common shape is a PreToolUse
hook that validates the outbound envelope before letting a spawn proceed:

```python
# example_pretooluse_hook.py — illustrative only, not shipped or imported
# by loadout. A deployment's own hook script owns this wiring.
from clagentic_loadout.envelope import EnvelopeValidationError, validate_envelope

def on_pre_tool_use(envelope: dict) -> None:
    try:
        validate_envelope(envelope)  # mode="enforce" is the default
    except EnvelopeValidationError as exc:
        # Refuse the dispatch; a deployment decides what "refuse" means
        # for its own hook framework (raise, sys.exit, return a block
        # signal — loadout has no opinion here).
        raise SystemExit(f"envelope rejected: {exc}") from exc
```

A deployment that wants to keep warning instead of blocking during a
transition period passes `mode="warn"` and inspects the returned list
itself — no code in this package changes behavior based on which mode a
caller picks beyond raise-vs-return.

Note: an earlier revision of this task also proposed a WARN-mode
`RecursionError` fix. A structured diagnosis pass on the originating report
(confidence 0.9) found the recursion does not reproduce and no such code
path exists in this validator — it was dropped as a proven non-bug rather
than shipped as a fix for nothing.

## 4. Envelope contract versions

Tracked per-schema, additive-only revisions to the published contract set.
Each schema's own `description` carries the same `contract_revision` note
inline; this table is the at-a-glance index.

| Schema | Revision | Change |
| --- | --- | --- |
| envelope-out.json | 2 | Added optional `followups[]`. |
| common.json | 2 | Added the `followup` fragment. |
| telemetry-dispatch-heartbeat.json | 1 | New schema — dispatch-liveness heartbeat. Sibling to telemetry-dispatch-record.json, does not modify it. |
| telemetry-dispatch-resume-ack.json | 1 | New schema — dispatch-resume liveness ack. Sibling to telemetry-dispatch-record.json, does not modify it. |

The `$id` path segment (`v1`) is unaffected by these revisions — see
common.json's `description` for why sibling schemas in this directory share
one path-version segment for relative `$ref` resolution, independent of
each file's own content revision.

## 5. Followup ingest (a worked example, not a wired behavior)

`clagentic_loadout.telemetry.emitter.emit_followup_events(task_id,
agent_name, followups, *, sink=None)` emits one
`telemetry-followup-event/v1` record per item in an envelope-out's
`followups[]`, to whichever `TelemetrySink` is configured (reusing the same
sink model as `emit_trace_event`/`emit_dispatch_record`/`emit_agent_run` —
see `clagentic_loadout.telemetry.sink`'s module docstring for the
none/filesystem/webhook sink kinds and their config). It emits a typed
event and stops there — no task is created, no tracker is contacted, no
deployment-specific behavior is assumed.

A deployment that wants a followup to become a tracked work item writes its
own consumer against the emitted record shape. Illustrative only:

```python
# example_followup_consumer.py — illustrative only, not shipped or
# imported by loadout. A deployment's own consumer owns this wiring; the
# tracker client shown here is a stand-in for whatever tool a given
# deployment actually uses.

def on_followup_event(record: dict) -> None:
    followup = record["followup"]
    # some_tracker_client is a deployment-supplied stand-in — loadout
    # does not import or assume any specific tracker.
    some_tracker_client.create_task(
        title=followup["summary"],
        source_task_id=record["task_id"],
        source_agent=record["agent_name"],
        tags=[followup["kind"]] if "kind" in followup else [],
    )

# A filesystem-sink deployment might tail the configured directory's
# *.jsonl files and call on_followup_event() for each new line; a
# webhook-sink deployment's collector calls it from its own POST handler.
# Both are deployment-side wiring loadout never sees.
```

Emitting to `sink="none"` (the default) is a true no-op — nothing is
written, nothing is called — so a deployment that has not configured a
followup consumer yet pays zero cost for calling `emit_followup_events()`.

## 6. Dispatch liveness: heartbeat + resume-ack (a worked example, not a wired behavior)

The problem (following an upstream investigation task): a caller
that dispatches a long-running agent — an
in-session sub-agent call, a resumed continuation, any mechanism — has no
signal distinguishing "still working" from "stalled" between the dispatch's
pre-spawn write and its terminal write, short of polling an unrelated proxy
(a git HEAD, an output-file mtime — both misleading: a multi-minute gap
between tool rounds looks identical to a hang). A resumed dispatch is worse
still — the caller gets no confirmation the resume was even received until
the resumed work finally produces something.

`clagentic_loadout.telemetry.emitter.emit_dispatch_heartbeat(dispatch_id,
*, ts=None, progress=None, sink=None)` and
`emit_dispatch_resume_ack(dispatch_id, *, ts=None, resuming=None,
sink=None)` publish the schema + emitter half of a liveness
convention, over the same sink model every other `emit_*` function in this
module uses:

- **`telemetry-dispatch-heartbeat/v1`** — a repeatable, non-terminal pulse
  a running dispatch may emit between long steps, carrying the SAME
  `dispatch_id` as the dispatch's pre-spawn `telemetry-dispatch-record.json`
  `in_flight` write, plus an optional short `progress` label.
- **`telemetry-dispatch-resume-ack/v1`** — a one-shot "received, working on
  X" ack a resumed dispatch may emit immediately, before starting its long
  task, carrying the same `dispatch_id` plus an optional `resuming` label.

**Design choice, named explicitly:** both are new SIBLING schemas, not an
added `status` value on `telemetry-dispatch-record.json`. The dispatch-record
schema is a strict two-write state machine (one `in_flight` pre-spawn write,
one terminal write carrying `completed_ts`/`result_summary`); a heartbeat is
neither pre-spawn nor terminal, and may fire any number of times. Folding it
into `status` would force every existing consumer of that schema to
special-case a non-terminal, repeatable status it never previously had to
expect. Two new schemas leave `telemetry-dispatch-record.json`'s shape and
`DISPATCH_STATUSES` enum byte-for-byte unchanged (true backward
compatibility) and let heartbeat/ack fields evolve independently later.

**What `clagentic: loadout` does NOT own here (CLAUDE.md hard rule 2):** `clagentic:
loadout` defines the record shape and a sink-routed emitter. It has no opinion on
heartbeat cadence, does not schedule emission, does not itself detect staleness, and
does not know anything about how a resume is delivered to a running vs
completed process. That last question — SendMessage/Agent-tool resume
delivery semantics inside the Claude harness — is an open harness-internals
question and is explicitly OUT OF SCOPE for this package; it lives outside
the `clagentic: loadout` tree entirely.

Illustrative only — neither side of this example ships with or is imported
by `clagentic: loadout`:

```python
# example_dispatch_liveness.py — illustrative only, not shipped or
# imported by loadout. Shows both the emitting side (a dispatched agent)
# and the consuming side (a lead/monitor reading the sink) as a stand-in
# for whatever deployment-specific dispatch loop actually calls these.

from clagentic_loadout.telemetry.emitter import (
    emit_dispatch_heartbeat,
    emit_dispatch_record,
    emit_dispatch_resume_ack,
)

# --- Emitting side: a freshly spawned dispatch -----------------------
dispatch_id = "dispatch-2026-07-09-001"  # caller-defined, opaque to loadout
emit_dispatch_record(dispatch_id, "in_flight")
# ... first long step ...
emit_dispatch_heartbeat(dispatch_id, progress="reading target files")
# ... second long step ...
emit_dispatch_heartbeat(dispatch_id, progress="running test suite")
emit_dispatch_record(dispatch_id, "completed", result_summary="PR opened")

# --- Emitting side: a RESUMED dispatch --------------------------------
# Same dispatch_id as the original in_flight write -- a resume continues
# an existing dispatch, it does not mint a new one.
emit_dispatch_resume_ack(dispatch_id, resuming="continuing PR #332 fix")
emit_dispatch_heartbeat(dispatch_id, progress="applying requested fix")
emit_dispatch_record(dispatch_id, "completed", result_summary="fix pushed")


# --- Consuming side: a lead/monitor reading a filesystem sink ---------
# some_sink_reader is a deployment-supplied stand-in -- loadout does not
# import or assume any specific consumer/collector.
def detect_stalled(dispatch_id: str, records: list[dict], *, now, stale_after_seconds: int) -> bool:
    """records: every telemetry-dispatch-record / -heartbeat / -resume-ack
    event observed for this dispatch_id, in emission order (a deployment's
    own sink reader assembles this list -- loadout only emits the events)."""
    relevant = [r for r in records if r["dispatch_id"] == dispatch_id]
    if not relevant:
        return False  # no data yet -- not this function's call to make
    if relevant[-1]["schema"] == "telemetry-dispatch-record/v1" and relevant[-1]["status"] != "in_flight":
        return False  # already terminal -- not stalled, it's done
    last_ts = relevant[-1]["ts"]
    return (now - last_ts).total_seconds() > stale_after_seconds
```

### Decision guidance: fresh `Agent()` dispatch vs resume, for small fully-specified fixes

Carried forward from an earlier investigation as documented guidance (not
enforced by any `clagentic: loadout` code — a deployment's dispatch loop applies this
itself):

- **Prefer a fresh dispatch** (a brand-new agent invocation, no prior
  context) when the fix is small and fully specified — the exact file, the
  exact change, no ambiguity requiring the original agent's accumulated
  context. A fresh dispatch gets a clean, unambiguous completion signal
  (its own terminal write) and sidesteps the resume-delivery question
  entirely.
- **Prefer resuming the existing dispatch** when context retention matters
  more than a clean completion signal — e.g. the same agent that built a
  PR fixing findings on that same branch, without restating the whole
  build from scratch. Pair a resume with an immediate
  `emit_dispatch_resume_ack()` call so the caller gets instant liveness
  confirmation rather than waiting on the resumed work's first heartbeat.
- Either path benefits from heartbeats during any step expected to run
  longer than the caller's own stall-detection threshold.

Open question for the harness/transport layer, explicitly out of scope for
this package: how a resume is actually delivered to a running vs
completed subprocess (queued, transcript-replayed, next-tool-round), and
whether resume-into-a-completed-agent is ever lossy. `clagentic: loadout`'s heartbeat and
resume-ack convention makes the SYMPTOM (no liveness signal) observable; it
does not diagnose or fix that harness-internals question.

## 7. `--body-env`: the harness-side staging process

This is the LOCAL PROCESS a consumer's agent harness must implement to use
`--body-env` (`loadout-git-host-api`, `loadout-review-post` — see
[docs/verbs.md](verbs.md)'s dedicated section for the full mechanism and
why it exists). Documented here, not just in the verb's own `--help`, per
the standing "document local processes" convention: a harness integrator
should not have to read this package's Python source to learn the staging
contract.

**Why a harness would want this at all:** `--body-stdin` (piped from
`echo '{...}' |` or a heredoc) puts the comment/PR body — which varies on
every single call — directly on the shell command line. A harness's own
static Bash-command analyzer (a gate separate from and in addition to
whatever this deployment's own guard hooks enforce) may flag any inline
brace-plus-quote sequence as suspicious, and because the body differs every
call, no static allowlist rule can ever cover the resulting family of
distinct argv strings — every invocation trips a manual approval prompt.
`--body-env` exists so the invoking command line can be a byte-for-byte
CONSTANT string instead.

**Concurrent same-TMPDIR callers:** the staged path is
namespaced per `--caller`, not a single path shared by every caller on a
TMPDIR root. A deployment where multiple loadout-driven callers can run
concurrently against the SAME `TMPDIR` (e.g. every spawn pinned to one
fixed TMPDIR root by deployment convention) MUST stage under each caller's
own namespaced path — using the single shared legacy path in that
configuration reintroduces the exact clobber this fix closes: one caller's
staging overwriting another's before its own read. A deployment where each
spawn already gets a genuinely per-spawn-isolated TMPDIR has no exposure
either way, but staging under the caller-namespaced path costs nothing and
is the convention documented below.

**Convention: staged intermediates live under
`$TMPDIR/clagentic-loadout/`.** This is the one sanctioned scratch root
(`$TMPDIR`-only; `$HOME` is no longer sanctioned) the abandoned-pair
reaper (see [docs/verbs.md](verbs.md)'s `--body-env` section) sweeps
opportunistically on ordinary stage/read traffic. A harness never composes
this path itself — `loadout-stage-body` computes it, below.

**The actual contract: stdin-only ingest into `loadout-stage-body`, for
EVERY caller and EVERY role.** A harness invokes the dedicated stage verb,
which writes the body AND its identity-stamp sidecar atomically through
`transport.body_env.stage_caller_body`. Body CONTENT is read from stdin —
never a caller-supplied filesystem path — and the staging LOCATION is
always computed by this package (`resolve_caller_body_path`), never chosen
by the caller. This is the ONLY sanctioned way to stage a body+stamp pair;
there is no hand-write alternative (see "Retired: hand-writing the staged
pair" below for why one existed briefly and why it does not anymore):

```
echo '{"body":"...", "review_status":"clean"}' | \
  loadout-stage-body --caller <role> --target-pr <pr-number> [--head-sha <sha>]
```

or, for the PR-creation path (no `--target-pr` exists yet), bind to the
branch that will open the new PR instead:

```
echo '{"body":"plain PR description text"}' | \
  loadout-stage-body --caller <role> --create-branch $(git rev-parse --abbrev-ref HEAD)
```

**HISTORICAL, REJECTED DESIGN (past tense — does not exist in this
package):** an earlier version of this fix added a caller-supplied
`--body-file <path>` flag as an alternative to stdin, with an
`EXIT_BODY_FILE_UNREADABLE` exit code refusing a path that was missing, a
directory, a symlink, or otherwise not a readable regular file. That flag
was REMOVED after a security audit and an explicit
operator correction: a validated arbitrary path still ACCEPTS a location
parameter, and every containment check is one canonicalization edge case,
one symlink race, one future refactor away from a bypass — content-input-
via-path was rejected unconditionally, not just this particular
implementation of it. `EXIT_BODY_FILE_UNREADABLE`'s numeric exit code is
kept RESERVED (unused, never raised) in the verb's own exit-code table so
the number is never silently repurposed, but no verb in this package
accepts `--body-file` or any other caller-supplied filesystem path for
PR-body content — stdin is the sole content-ingest path, full stop.

**`--body-env`: the READ side, consume-on-read.** Once staged, a caller
reads the body via `--body-env` (a bare switch, no value) on
`loadout-git-host-api`, `loadout-review-post`, or `loadout-push`. The read
is CONSUME-ON-READ (`transport.body_env.read_caller_body_bytes`): a
successful read deletes the staged body and its stamp sidecar, so a stale
body from a prior invocation can never be silently re-read by a later one.

**The staged content is always the `{"body": "..."}` JSON envelope, and
every reader UNWRAPS it.** `loadout-stage-body` stages the caller's stdin
bytes verbatim — the same `{"body": "<text>"}` shape `--body-stdin`
requires, validated at stage time
(`transport.git_host_api.validate_body_stdin_content`). Every `--body-env`
reader in this package (`loadout-git-host-api`, `loadout-review-post`,
`loadout-push`) parses that envelope and extracts the `body` field before
using the content, exactly as `--body-stdin` does — this is unconditional,
never content-sniffed: a reader does not inspect the staged bytes to guess
whether they look JSON-shaped, it always expects the envelope and fails
loudly (`BodyEnvError`/`BodyEmptyError`, mirroring `--body-stdin`'s own
malformed-input refusal) if the staged bytes are not valid JSON or lack a
non-empty `body` string. (`loadout-push`'s own `_read_body_env` previously
read the staged bytes verbatim instead of unwrapping them — the ONLY
outlier among this package's `--body-env` readers — which rendered the
literal JSON wrapper, escaped newlines included, as the PR body; fixed to
match every other reader's contract.)
The read also requires a matching identity stamp (`body.<caller>.stamp.json`
— see `loadout-stage-body`'s own dedicated section in
[docs/verbs.md](verbs.md)) binding the staged body to the SAME `--target-pr`
(or `--create-branch`) the reading verb resolves independently — a missing
stage or a binding mismatch fails closed rather than reading stale or
mismatched content.

This is the fix for the exact gap that motivated this section's own
existence: an agent's Bash permission allowlist commonly admits
`body.<caller>.json` for a raw staging write but categorically denies
`body.<caller>.stamp.json` (a stamp is a platform-computed provenance
value, not something an agent should be able to hand-author with an
arbitrary `target_pr`/`head_sha`) — routing the write through
`loadout-stage-body` means the allowlist never needs a `*.stamp.json`
entry at all. See [docs/verbs.md](verbs.md)'s `loadout-stage-body` section
for the full flag reference. This applies uniformly to every calling
role — builder or reviewer — with no role-specific staging path: a
harness's allowlist needs exactly one entry, `loadout-stage-body`, for
every caller that ever supplies a body.

**Also covers `loadout-push`'s PR-open path.**
`--target-pr` requires an EXISTING PR number to bind the staged body's
identity stamp to (the stale-read guard's whole contract) — a PR-open call
has, by definition, no PR number yet at the point its body is composed.
`loadout-stage-body --create-branch <branch>` closes that gap: it binds the
staged body's identity stamp to the git branch that will open the new PR
instead of an existing PR number — the SAME `stage_caller_body`/
`read_caller_body_bytes` API, extended with a `create_branch` field
alongside `target_pr` (exactly one of the two is ever set). `push --caller
<role> --title '...' --body-env` then reads it, resolving its OWN current
branch (`git rev-parse --abbrev-ref HEAD`) and requiring the staged stamp
to match — never a caller-typed branch name on `push`'s own argv.

An earlier version of this fix instead added a caller-supplied
`--body-file PATH` flag directly on `push` (plain text, validated against a
`$TMPDIR` scratch-boundary allowlist at read time, no identity
stamp). A security audit and an explicit operator correction rejected it: a
validated arbitrary path still ACCEPTS a location parameter, and every
containment check is one canonicalization edge case, one symlink race, one
future refactor away from a bypass — it left the door open by design. The
fix that shipped instead extends the create_branch binding above so
`--body-env` covers PR creation too, rather than adding any
caller-supplied-path flag anywhere in this package: the caller supplies
body CONTENT (via `loadout-stage-body`'s own stdin), never a filesystem
LOCATION, in either mode.

**The one-step contract every harness implements: invoke `loadout-stage-body`,
then invoke the reading verb with a constant argv.**

1. **Stage.** Pipe the body JSON into `loadout-stage-body --caller <role>
   --target-pr <n>` (existing-PR path) or `--create-branch <branch>`
   (PR-creation path) — see this verb's own section in
   [docs/verbs.md](verbs.md) for the full flag reference. This single
   invocation writes BOTH the body file and its identity-stamp sidecar
   atomically; the staging LOCATION (`$TMPDIR/clagentic-loadout/
   body.<caller>.json` + `.../body.<caller>.stamp.json`) is computed
   entirely by this package (`resolve_caller_body_path`) — the harness
   never composes, substitutes, or redirects to that path itself. A
   caller's own `--caller` value is the only thing that varies the
   resolved location, and that value never appears as a raw filesystem
   path anywhere in the invoking harness's own commands.
2. **Read.** Invoke the reading verb (`loadout-git-host-api`,
   `loadout-review-post`, or `loadout-push`) with a CONSTANT-SHAPE argv
   that includes `--body-env` (a bare switch, no value) and the SAME
   `--caller` value used to stage in step 1 — zero body data anywhere in
   the command line. The invoking argv string is identical across every
   invocation of a given caller regardless of what the staged body
   actually contains — the property that makes it genuinely
   allowlistable. The verb itself resolves `expect_target_pr` (and
   `expect_head_sha`, when available) from its own PATH/`--pr-sha`
   arguments — a harness never has to pass the PR number a second time via
   a new flag.

**Retired: hand-writing the staged pair.** An earlier revision of this
document also sanctioned a "hand-write alternative" — a harness composing
`$TMPDIR/clagentic-loadout/body.<caller>.json` and its
`.stamp.json` sidecar itself (via a raw shell redirect to a literal fixed
path), for a harness whose allowlist could not yet admit
`loadout-stage-body`. That alternative existed only because some calling
roles had no guard-admitted path to `loadout-stage-body` at the time; once
a deployment's own guard/allowlist layer admits `loadout-stage-body` for
every calling role, the hand-write alternative is retired: it was the
exact shell-improvisation
surface — a raw `printf`/`echo`/redirect two-step against a
harness-composed path — that produced a recurring family of guard-denial
and doc-reconciliation tasks (six of them, prior to this fix) each time a
role's allowlist and this document's own description of the sanctioned
path drifted apart. There is now exactly one write-side mechanism for
every role: `loadout-stage-body`. A harness that hits a guard denial on
`loadout-stage-body` has an allowlist gap to fix (see
[docs/provisioning.md](provisioning.md)), not a reason to fall back to a
hand-composed redirect.

**What the harness does NOT need to do:** delete or truncate the staged
file after the call. `--body-env` now consumes (unlinks) both the body and
its stamp sidecar itself, immediately after a successful, provenance-
matched read — a harness never has to clean up a
successfully-read staged file. **What the harness DOES need to do
differently now:** a RETRIED invocation of the same caller must RE-STAGE a
fresh body+stamp pair before invoking the verb again — a prior successful
read leaves nothing behind to reuse. This is a deliberate behavior change
from this mechanism's original design: the staged path used to be treated
as safely re-readable by a retry, which is exactly the assumption a real
incident (a stale, unrelated PR's leftover body being silently
re-read and re-posted under a later invocation's identity) proved unsafe.
A read whose `expect_target_pr`/`expect_head_sha` does not match the
staged stamp fails closed (`BodyEnvError`) WITHOUT consuming anything —
the mismatched pair is left in place, since it may belong to a different,
still-pending invocation. **If that pending invocation never arrives:**
the abandoned-pair reaper wired into `stage_caller_body` and
`read_caller_body_bytes` (see [docs/verbs.md](verbs.md)'s `--body-env`
section) removes it once it is over an hour old, on the next ordinary
stage/read call against the same `TMPDIR` — a harness still does not
need its own cleanup step for this case either, as long as some
`--body-env` traffic continues on that host.

**When NOT to use `--body-env`:** a harness whose allowlist genuinely
cannot admit `loadout-stage-body` at all (rather than a role-gap that
should be closed per [docs/provisioning.md](provisioning.md)) gets no
benefit from this route — it should continue using `--body-stdin`,
sourced from a delivery mechanism that is not itself a Bash tool call
subject to a static command-line analyzer (e.g. the body arriving
already in-process rather than being echoed through a shell producer).

## Channel parity — every write channel a harness grants needs the same containment

This package's guard predicates (`clagentic_loadout.guard.shell_parsing`,
`role_allowlist`, `scratch_policy`, `infra_ops` — see
[docs/guard-policy.md](guard-policy.md)'s "Coverage boundary" section for
the full enumeration) classify **shell-spelled** commands: recognized verb
prefixes, redirects, heredocs, compound/pipe structure. Any write channel a
harness grants an agent that is NOT a shell command string — most commonly
a direct `Write`/`Edit`-shaped tool wired into the harness's own
tool-dispatch layer, distinct from a `Bash` tool — produces file mutations
none of those predicates ever see, because there is no command line for
them to parse.

**The invariant an agent-builder must hold:** any write channel your
harness grants an agent must consult the SAME containment predicate set on
every channel, not just the shell-facing one. A containment boundary
enforced on one channel and silently absent on another is not a narrower
policy — it is a hole. An agent that is denied a given write shape on its
`Bash` channel but can produce the identical write unchecked through a
`Write`/`Edit`-shaped tool has not actually been contained at all; it has
had one door locked while an equivalent door next to it was left open.

**The motivating failure shape (anonymized): stranded debris.** A deployed
agent, operating correctly and without malicious intent, needed to write a
small scratch file as part of ordinary, documented-correct setup. Its
shell-facing channel had a scratch-containment guard wired in (the shape
`guard.scratch_policy.is_scratch_contained` implements): a shell command
targeting anywhere outside the sanctioned scratch root would have been
denied. But the agent's harness also granted it a separate, non-shell
file-write tool, and that tool's own admission path had never been wired to
consult the same (or any) containment predicate. The agent used the
unguarded channel and placed the scratch file inside a repository's own
governance/config directory instead of the sanctioned scratch root — a
write the shell-facing guard would have refused outright, admitted without
question on the channel nobody had told to check. Worse, the deletion path
(a later cleanup attempt, shell-spelled) WAS guarded, and correctly refused
to remove a file sitting inside a protected governance directory. The net
result: debris admitted through the unguarded channel, permanently
unremovable through the guarded one — a write channel and a delete channel
that disagreed about the same boundary, leaving stray state neither side
could resolve. Nothing here required an adversarial agent; an ordinary,
well-intentioned write through the one channel nobody had wired the check
into was sufficient.

**What this means in practice for an agent-builder wiring a new harness
tool:**

1. **Enumerate every write-capable tool granted to the agent** — not just
   `Bash`. A `Write`/`Edit`-equivalent tool, a file-upload tool, any
   harness capability that can create or modify a file on disk, all count.
2. **For each one, apply the same two-part boundary this package already
   enforces on its shell-facing channels**: scratch writes are
   `$TMPDIR`-only (reuse `guard.scratch_policy`'s public predicates —
   `is_scratch_contained` for a full command line,
   `resolve_scratch_boundary`/`resolve_all_scratch_boundaries` for a single
   already-resolved path — rather than re-deriving a second realpath/
   symlink-escape check), and repo governance/config directories are
   config-only (reuse `guard.write_scope.check_write_call` / its
   `WriteScopeConfig` containment logic, the same boundary a `SCOPED`-role
   Bash write is already checked against).
3. **Never assume a channel is safe because a sibling channel is guarded.**
   The stranded-debris failure above did not happen because the shell-side
   guard was wrong — it was correct, on the channel it was actually wired
   to. It happened because "guarded" was true of one channel and silently
   false of another, and nothing forced them to agree.

Whether this package should also OWN a harness-tool-facing containment
primitive (a ready-made check a harness wires directly to its own
`Write`/`Edit`-equivalent tool, rather than an integrator composing
`scratch_policy`/`write_scope`'s existing predicates by hand per harness)
is an open question, deliberately not decided here — see
[docs/guard-policy.md](guard-policy.md) for the predicates that exist
today; a future primitive, if built, would compose them rather than
replace them.

## Authoritative post-push remote state

**The defect this closes:** a caller can report a remote fact ("the push
happened, and the remote now has SHA X") that it never actually read back
from the remote — nothing in the substrate prevented this. The concrete
incident: an agent reported `status: ok` with a `head_sha` for a push that
was **never invoked**, because that field was computed via a LOCAL `git
rev-parse HEAD` — a true answer to "what is my HEAD" mistaken for an
answer to "what is on the remote." A code-verification pass found the
defect one layer deeper too: `push`'s own pre-existing `head_sha` field
was itself a local read, not a remote readback, despite being the value
callers were supposed to reach for instead of their own local read.

**The fix, and its scope:** `push` (create-PR path only — `--update-pr`
never pushes) now performs a genuine remote round-trip, via `git
ls-remote`, immediately after `git push` returns success, and folds the
result into the JSON success envelope as `remote_head_sha` +
`remote_head_sha_source` (always the literal `"git_ls_remote"` on
success). See [docs/verbs.md](verbs.md)'s `loadout-push` section for the
full field list, including the authorship-assertion fields and the
`builder_identity:` config wiring shipped alongside this.

**Why `git ls-remote`, not a platform API call:** it is a genuine
round-trip to the remote (not a cached/local value) and works
IDENTICALLY for a Forgejo or a GitHub remote — both are plain
git-over-HTTP(S) for this purpose, so no platform-specific branch here is
needed for the ref-advance half of the readback. This mirrors the
`git ls-remote`-based verification protocol a caller previously had to
perform BY HAND, out-of-band, to catch the incident above — folded into
the tool that should have done it in the first place, rather than left as
tribal knowledge in an agent's own operating procedure.

**Why this is additive, not enforcement, and what a consumer should (and
should not) assume:**

- **Additive only.** This PR ships the RETURN — a verb-supplied,
  provenance-tagged remote fact a caller CAN check its own claims against.
  It does not ship ENFORCEMENT (the verb refusing to exit `EXIT_OK` on a
  readback mismatch, or a caller being rejected for omitting one).
  Enforcement is an explicit, separately-scoped, config-gated follow-up —
  shipping it unconditionally here would impose a NEW failure mode on
  every existing external consumer of this verb, which a shipped tool with
  users beyond one crew must not do unilaterally.
- **A missing/`null` `remote_head_sha` is informative, not a push
  failure.** A transient failure of this diagnostic re-read (e.g. a
  network blip in the seconds after a successful push) never turns an
  already-successful push+PR-open into a hard failure — see
  [docs/verbs.md](verbs.md) for the exact envelope shape on that path.
- **Threat model, stated explicitly:** this defends against a caller that
  SKIPS the remote read and reports optimistically (the incident's actual
  shape — an omission, not an attack). It is not a cryptographic
  provenance mechanism (no signing, no HMAC) — that would be
  over-engineering for a failure mode the evidence does not present. A
  consumer with a genuinely adversarial threat model (an untrusted process
  forging a `remote_head_sha` value in a transport it does not control)
  needs a different mechanism than this one; this task explicitly scoped
  that out.
- **`--update-pr` carries none of these fields** — it never pushes, so
  there is nothing new to read back from the remote. It also carries no
  `head_sha` field at all (an explicit `"pushed": false` instead) — see
  [docs/verbs.md](verbs.md)'s `loadout-push` section for the full
  rationale (a LOCAL `git rev-parse HEAD` reported from a verb that never
  pushes is the same defect this section describes, one call site over).

**Merge/post-merge/close-PR — assessed, not (yet) extended in this PR:**
this task's primary evidence and the shipped fix are scoped to `push`, the
proven instance of the defect. A survey of the sibling remote-mutation
verbs found:

- `merge.post_merge_verb` **already performs** a genuine post-operation
  remote readback (a fresh `get_pr_info` GET, independent of any prior
  merge call) — no gap here.
- `merge.tree_sync.advance_repo_to_merged_sha` **already performs** a
  genuine post-merge git-level readback (`git fetch` + a verified
  checkout) for the local-working-tree-sync path, when `known_merged_sha`
  is available — a real, if narrower, precedent for the same pattern this
  task adds to `push`.
- `merge.verb`'s own reported `merged_sha` (in the PR attestation comment)
  is read from the mutating merge-API call's response body (or, on
  Forgejo, is unavailable — the merge endpoint returns no body at all) —
  **not** an independent post-merge GET. This is the same defect class,
  one layer over from `push`.
- `merge.close_verb` reports success straight from the closing PATCH
  itself — **no** follow-up GET confirming the PR's resulting `state`.

Extending the `push`-shaped fix to `merge`/`close-pr` is real, in-scope
follow-up work under this same task's evidence, deliberately NOT bundled
into this PR to keep the diff reviewable and scoped to the one proven
instance — see this PR's own body for the recommendation on how that
follow-up should be sequenced.

**UPDATE: the "one layer over from `push`" gap named above for
`merge.verb` is now PARTIALLY closed — the SHAPE half, not the SHA-identity
half.** A real instance of exactly the defect class this section predicted
was found and fixed: `--merge-method` was parsed but never
forwarded to either backend's `merge_pr`, so a requested squash/rebase
silently executed as a real merge commit with no readback ever confirming
what actually landed. `merge.merge_shape.check_merge_shape` closes that
specific gap with a genuine LOCAL git readback (`git log -1 --format=%P`
against the already-synced `--repo-path` tree — see
[docs/verbs.md](verbs.md)'s "`--merge-method` actually controls the merge"
section for the full mechanism, including why this is additive/warn-by-
default rather than enforcement-by-default, mirroring this same section's
own "why this is additive, not enforcement" framing above).

This closes the SHAPE dimension only (parent count — did the landed commit
look like the requested merge_method) — it does NOT close the SHA-IDENTITY
dimension named above (the attestation body's `merged_sha` field is still
read from the merge-API response body / assumed equal to `gated_head_sha`
on Forgejo, never an independent post-merge GET of the PR's own resolved
merge-commit reference). `merge.close_verb`'s gap is also unchanged by this
task — both remain real, scoped, deliberately-deferred follow-up work,
exactly as this section originally recommended.
