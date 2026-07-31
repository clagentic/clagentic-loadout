# Documentation index

Back to [../README.md](../README.md).

`clagentic: loadout` is platform-agnostic substrate for controlled agent behavior:
role-scoped verbs, attested identity, per-use minted credentials, guard hooks, and a
merge gate, for autonomous coding agents. Git-host operations on Forgejo and GitHub are
the largest current surface, not the definition. This directory is the detailed
reference — the root README covers the pitch and install; this is where the integrator
reference lives.

## Start here (new integrator reading order)

1. [loadout-init.md](loadout-init.md) — scaffold a repo's
   `.clagentic/loadout/config.yaml` via the guided `/loadout-init` workflow.
   Start here: nothing else below works without a config file on disk.
2. [credentials.md](credentials.md) — get a token seam working: the
   `TokenProvider` seam, the standalone Forgejo path, and what GitHub's
   App-token path additionally needs.
3. [verbs.md](verbs.md) — what you can now run: every landed CLI verb, its
   flags, and its gate behavior.
4. [provisioning.md](provisioning.md) — let a role actually invoke those
   verbs: declaring a role's verb set and generating its
   permission-allowlist fragment so a harness doesn't block the call.
5. [merge-authority.md](merge-authority.md) — the enforcement model, part 1:
   how a role is bound to merge authority, the fail-closed guarantee, and
   the built-in fallback provider's actual grant.
6. [guard-policy.md](guard-policy.md) — the enforcement model, part 2: the
   harness-agnostic guard library backing command/write-scope enforcement.
7. [integration.md](integration.md) — the harness runtime contract: exactly
   what a spawn environment must supply (env vars, config-file tiers,
   precedence) for a `clagentic: loadout` verb to run unattended.

## All docs

| Doc | Covers |
|---|---|
| [loadout-init.md](loadout-init.md) | The guided `/loadout-init` workflow for scaffolding a repo's `.clagentic/loadout/config.yaml`. |
| [credentials.md](credentials.md) | The full credential-provider-seam reference: `{repo}`-scoped minting, the argv option-injection guard, per-platform selection precedence, and the shared-minting-command convergence rationale. |
| [verbs.md](verbs.md) | Every landed CLI verb: purpose, flags, gate behavior, examples. |
| [provisioning.md](provisioning.md) | The per-role permission-allowlist side: declaring which verbs a role may invoke and generating its allowlist fragment. |
| [merge-authority.md](merge-authority.md) | The `loadout-merge` identity-binding model, fail-closed guarantee, attestation-source configuration, and the git-host attestation mark. |
| [guard-policy.md](guard-policy.md) | The `clagentic_loadout.guard` policy contract: every guard category, its API, its config shape. |
| [integration.md](integration.md) | The runtime contract a harness spawning a loadout-driven agent must satisfy: env vars, config-file tiers, precedence. |
