<p align="center">
  <img src="media/logo/loadout-lockup-256.png" alt="clagentic: loadout" width="260" />
</p>

<h4 align="center">Role-scoped agent tooling. Built for builders.</h4>

<p align="center">
  <a href="https://clagentic.ai"><img src="https://img.shields.io/badge/clagentic.ai-00CFFF?style=flat&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjZmZmIiBzdHJva2Utd2lkdGg9IjIuNSIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIj48cGF0aCBkPSJNMjAgMTJhOCA4IDAgMSAxLTMuNTgtNi42NiIvPjwvc3ZnPg==&logoColor=white&label=clagentic.ai" alt="clagentic.ai" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-FSL--1.1--MIT-blue?style=flat" alt="License: FSL-1.1-MIT" /></a>
  <a href="https://ko-fi.com/clagentic"><img src="https://img.shields.io/badge/Ko--fi-FF5E5B?style=flat&logo=ko-fi&logoColor=white&label=support" alt="Support on Ko-fi" /></a>
</p>

Agents don't get tools — they get a loadout. Platform-agnostic substrate for agents to
behave well in controlled ways: role-scoped verbs, per-use credentials, attested
identity, guard hooks, and a merge gate nothing lands without. Git-host operations on
Forgejo and GitHub are the largest surface today; they are not the boundary of what
belongs here.
Part of the [clagentic](https://clagentic.ai) suite.

## What it does

Each agent role — builder, reviewer, security, merger, lead — is issued a loadout: the
exact verbs it may run, and nothing else. Wrong actions aren't forbidden; they're
unrepresentable.

## Composes with

clagentic: loadout integrates rather than owns: each of the following is a seam with a
standalone, in-package fallback — clagentic: loadout never imports any of them, and
bring-your-own is always supported.

- **Credential minting** — the `TokenProvider` seam (see "Credential provider seam"
  below). [clagentic: gatekeeper](https://github.com/clagentic/clagentic-gatekeeper) is
  the reference minting provider for GitHub's App-token path; Forgejo works fully
  standalone with a static token and no minting provider at all.
- **Merge authority** — the `AuthorityProvider` seam
  (`clagentic_loadout.merge.authority`); a directory-style attestation service is the
  reference provider, `StaticRoleAuthorityProvider` is the in-package standalone
  fallback. See [docs/merge-authority.md](docs/merge-authority.md).
- **Release and telemetry events** — emitted to generic, locally configured sinks (none,
  filesystem, webhook); no tracker or collector is assumed or imported.

Landed today (see [docs/verbs.md](docs/verbs.md) for the full description of each):

- **`push`** — bot-attributed commit push, issue-trailer linking, PR open/update. Never
  merges, never pushes to a protected branch.
- **`review post`** — post-and-verify a review comment on either Forgejo or GitHub behind
  one contract; `--platform` is mandatory and the platform guard always runs before any
  credential is minted.
- **`merge`** — the full gate chain (namespace guard, merge-authority check, stale-head-SHA
  refusal, reviewer-verdict fences, diff-scope cap, PR-title gate), then merge, on either
  the Forgejo or the GitHub path via `--platform`. This is the load-bearing release gate:
  every step fails closed, and the merge only executes once every gate above it has
  passed.
- **`git-host-api`** — authenticated, redirect-hardened Forgejo REST transport with a
  mandatory post-and-verify readback for comment writes.
- **Release detection and dispatch** — tag-triggered detection plus an HMAC-signed "task
  shipped" event hook, with a caller-supplied endpoint/secret/dispatcher-name (no baked-in
  host or service).
- **`provision-allowlist`** — generates a ROLE's permission-allowlist fragment (never a
  global, all-verbs list) from a repo's `.clagentic/loadout/config.yaml` role declaration,
  so a consuming agent can actually invoke its verbs without a permission-prompt wall. See
  [docs/provisioning.md](docs/provisioning.md) for the full integrator workflow.
- **`doctor`** — the deployment-conformance check suite: a read-only, safe-to-repeat verb
  (including in CI) that verifies credentials, attestation-source, builder-identity, and
  repo-schema configuration are actually wired up correctly. This is the integrator's
  entry point for confirming a fresh install or a config change didn't break the loadout
  contract.

Each verb above ships as its own console-script entry point (`loadout-push`,
`loadout-review-post`, `loadout-merge`, `loadout-git-host-api`, ...) AND is reachable through
the top-level `clagentic-loadout <verb> [<subverb>] ...` umbrella binary (e.g.
`clagentic-loadout push ...`, `clagentic-loadout release dispatch ...`). Every verb and the
umbrella itself support `--help` and `--version`.

- **Identity** — every invocation resolves which agent is acting through an attested
  detection chain.
- **Credentials** — every verb resolves its forge token through one
  `TokenProvider` seam; see "Credential provider seam" below for exactly what that does
  and does not depend on.
- **Envelopes** — agents receive schema-validated work orders and return schema-validated
  results. Transport-agnostic: any orchestration layer that can deliver JSON can drive a
  loadout.
- **Guards** — hook-level enforcement generated from the same role registry as the verbs:
  command allowlists, write-path scoping, credential-file denial.
- **Lifecycle** — one bootstrap takes a repo from bare to agent-operable: readiness audit,
  config scaffolding, credential preflight. (Not yet landed in this repo.)

Roles are generic. Your agents' names, models, and personalities live in your deployment
config — clagentic: loadout never learns them.

## Install

No tagged release yet, but a checkout can be installed today via `scripts/install.sh`:

```sh
scripts/install.sh                 # install this checkout via the best available installer
scripts/install.sh --editable      # editable/dev install
scripts/install.sh --help          # full option list (--source, --installer, --data-dir, --dry-run, ...)
```

This installs both the `clagentic-loadout` umbrella binary and each verb's own standalone
console script (`loadout-push`, `loadout-review-post`, `loadout-merge`, `loadout-git-host-api`,
`loadout-release-detect`, `loadout-release-dispatch`, `loadout-poll-wait`,
`loadout-scoped-test-wait`, `loadout-provision-allowlist`), then verifies (and, if needed,
repairs) PATH visibility for the directory they land in.

### Installer tiers

`install.sh` auto-detects the best available installer, in this order, falling through to
the next tier only if the current one is unusable:

1. **`pipx`** — preferred when present. Installs into its own isolated venv and symlinks
   console_scripts into a single well-known bin dir (`~/.local/bin` by default).
2. **`uv`** — same isolation property via `uv tool install`, used if `pipx` isn't found.
3. **`pip install --user`** — used if neither `pipx` nor `uv` is present, and the Python
   interpreter is not [PEP 668](https://peps.python.org/pep-0668/) externally-managed.
4. **Self-managed venv** (the tier most users on stock Debian/Ubuntu will actually hit) —
   used when neither `pipx` nor `uv` is present AND the interpreter reports a PEP 668
   `EXTERNALLY-MANAGED` marker (the default on current Debian/Ubuntu system Python, which
   refuses a bare `pip install --user`). `install.sh` creates and owns a virtualenv under
   its data dir, installs the package into it, and symlinks the resulting console_scripts
   out to a normal PATH bin dir — never passing `--break-system-packages`, which would
   defeat the protection PEP 668 exists to provide.

Re-running `install.sh` against an existing venv-tier install is idempotent: it reuses the
same virtualenv (upgrading the package in place) and refreshes the console-script symlinks
rather than duplicating or erroring.

### Overrides

- `--data-dir DIR` / `CLAGENTIC_LOADOUT_HOME` — base directory for the self-managed venv
  tier (default: `~/.local/share/clagentic/loadout`; ignored by the other tiers).
- `--bin-dir DIR` / `CLAGENTIC_LOADOUT_BIN_DIR` — symlink-target bin dir for the
  self-managed venv tier's console_scripts (default: `~/.local/bin`; ignored by the other
  tiers). Mirrors `PIPX_BIN_DIR`/`UV_TOOL_BIN_DIR` below for the venv tier's own
  HOME-derived symlink-target dir.
- `--path-dir DIR` (repeatable) — an additional console-script directory to verify/report
  for PATH visibility.
- `--installer {pipx|uv|pip|venv}` / `CLAGENTIC_LOADOUT_INSTALLER` — force a specific tier
  instead of auto-detecting.
- `--source PATH` / `CLAGENTIC_LOADOUT_SOURCE` — install from a different sdist/wheel/
  checkout path instead of the checkout `install.sh` itself lives in.
- `PIPX_BIN_DIR` / `UV_TOOL_BIN_DIR` — the pipx/uv tiers' own bin-dir overrides (read
  directly, no `clagentic-loadout`-prefixed alias); an empty/unset `HOME` with one of
  these set is sufficient compensation only when that tier is explicitly forced via
  `--installer pipx`/`--installer uv`.

### Verifying the install

```sh
clagentic-loadout --version
loadout-doctor
```

If the command isn't found immediately after a fresh install, `install.sh` prints the
`export PATH=...` line needed for the session it just ran in — add it to your shell rc file
so it persists across new shells.

`loadout-doctor` is the deeper check — see its description under "What it does" above for
what it verifies.

### Per-repo config

A repo's own `.clagentic/loadout/config.yaml` (roles, merge-gate policy, post-merge
steps) is per-deployment and never committed. Copy
[`.clagentic/loadout/config.yaml.example`](.clagentic/loadout/config.yaml.example) and
edit it, or run `/loadout-init` for a guided walkthrough — see
[docs/loadout-init.md](docs/loadout-init.md).

## Credential provider seam

clagentic: loadout does not require any external credential-minting service. Every verb
resolves its forge token through one seam — `transport.credential_provider.resolve_token(role,
provider)` — with real, built-in implementations:

- **Zero external dependency by default.** `StaticTokenProvider` reads a role-scoped
  `.env` file on disk (mode-600 enforced). No minting service is imported, required, or
  hardcoded anywhere in this seam.
- **Forgejo works fully standalone.** Drop a static personal access token into the
  role's `.env` file and every Forgejo-path verb works with no other moving parts.
- **GitHub's App-token path needs a minting provider**, since an installation token has
  to be *minted*, not just read from a static file.
  [clagentic: gatekeeper](https://github.com/clagentic/clagentic-gatekeeper) is the
  reference implementation — or bring your own `TokenProvider`. Nothing in clagentic:
  loadout imports gatekeeper or any other minting service.
- **`CommandTokenProvider`** wires in a deployment's own minting process as config — the
  git-credential-helper pattern: exec a configured argv (`shell=False`), read the token
  from stdout, fail closed on any nonzero exit or empty output.

Forgejo and GitHub each name their own provider **independently**, e.g.:

```sh
export CLAGENTIC_LOADOUT_TOKEN_PROVIDER_GITHUB=command
export CLAGENTIC_LOADOUT_TOKEN_COMMAND_GITHUB="/path/to/mint-github-token.sh"
```

See [docs/credentials.md](docs/credentials.md) for the full reference: per-platform
selection precedence, the `{repo}` repo-scoped-minting context and its
protocol-compatibility trade-off, the argv-level option-injection guard, `shlex`/
`shell=False` semantics, the repo-local-config rejection rationale, and the roadmap
convergence a future shared-minting-command setup relies on.

## Merge authority

`merge` is the load-bearing release gate — nothing lands without it. Merge
authority is bound to a **role**, never a hardcoded agent name, through the
`AuthorityProvider` seam (`clagentic_loadout.merge.authority`), and every
check that cannot positively confirm authority refuses the merge: an
unreachable provider, a malformed response, and a role absent from the
configured allow-set are all refusals, with no fail-open variant anywhere in
this seam. See [docs/merge-authority.md](docs/merge-authority.md) for the
full identity-binding model, how to point clagentic: loadout at your own attestation
source (a directory-style service, composed in from outside the package) vs.
the in-package `StaticRoleAuthorityProvider` fallback and exactly what it
grants, the `merge.authorized_roles` / `required_reviewer_roles` config keys
(and their repo-tier vs. deployment-tier homes), and the forge-visible
"Merged via clagentic-loadout" attestation comment posted after a
successful merge.

## Documentation

Full reference lives in [docs/README.md](docs/README.md) — the docs index,
with a "start here" reading order for a new integrator. The same docs, listed
inline:

| Doc | Covers |
|---|---|
| [docs/verbs.md](docs/verbs.md) | Every landed CLI verb: purpose, flags, gate behavior, examples. |
| [docs/integration.md](docs/integration.md) | The runtime contract a harness spawning a loadout-driven agent must satisfy: env vars, config-file tiers, precedence. |
| [docs/credentials.md](docs/credentials.md) | The full credential-provider-seam reference: `{repo}`-scoped minting, the argv option-injection guard, per-platform selection precedence, and the shared-minting-command convergence rationale. |
| [docs/merge-authority.md](docs/merge-authority.md) | The `loadout-merge` identity-binding model, fail-closed guarantee, attestation-source configuration, and the git-host attestation mark. |
| [docs/provisioning.md](docs/provisioning.md) | The per-role permission-allowlist side: declaring which verbs a role may invoke and generating its allowlist fragment. |
| [docs/loadout-init.md](docs/loadout-init.md) | The guided `/loadout-init` workflow for scaffolding a repo's `.clagentic/loadout/config.yaml`. |
| [docs/guard-policy.md](docs/guard-policy.md) | The `clagentic_loadout.guard` policy contract: every guard category, its API, its config shape. |

## Support

If clagentic: loadout is useful to you: [ko-fi.com/clagentic](https://ko-fi.com/clagentic)

## Disclaimer

Not affiliated with Anthropic or OpenAI. Claude is a trademark of Anthropic. Codex is a
trademark of OpenAI. Provided "as is" without warranty. Users are responsible for
complying with their AI provider's terms of service.

## License

[FSL-1.1-MIT](LICENSE) — Functional Source License 1.1, with MIT as the Change License.

Free for personal, internal-business, evaluation, research, and non-commercial use.
Not free for offering this tool (or a substantial fork) as a competing commercial product.
Each release auto-converts to MIT on its second anniversary.

## CLI Naming

This project follows the clagentic CLI naming conventions:

- Binary: `clagentic-loadout`
- Environment variables: `CLAGENTIC_LOADOUT_*`
- User config: `~/.config/clagentic/loadout/`
- Per-repo config: `.clagentic/loadout/config.yaml` (legacy `.loadout/config.yaml` is read
  as a transitional fallback with a deprecation warning, removed once every repo
  finishes migrating onto the new path)
