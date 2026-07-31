# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

There is no automated release process for this project: entries below are
added by hand, per release, by whoever cuts it.

## [0.1.0] - 2026-07-25

Initial public release.

### Added

- **CLI verbs** — `push`, `review post`, `merge`, `git-host-api`,
  `stage-body`, `acquire`, `release detect`, `release dispatch`,
  `provision-allowlist`, `poll-wait`, `scoped-test-wait`, `doctor`,
  `close-pr`, and `post-merge`, each shipped as its own console-script
  entry point and reachable through the `clagentic-loadout` umbrella CLI.
  Every verb supports `--help` and `--version`.
- **`push`** — bot-attributed commit push, issue-trailer linking, and PR
  open/update. Never merges, never pushes to a protected branch.
  Platform auto-detected from the git remote or supplied explicitly.
- **`review post`** — post-and-verify a review comment on either Forgejo
  or GitHub behind one shared contract, including a fail-closed
  emit-and-verify verdict route with parity across both platforms.
- **`merge`** — the release gate: namespace guard, merge-authority check,
  platform guard, stale-head-SHA refusal, reviewer-verdict fences,
  diff-scope cap, PR-title gate, and a CI-status gate that treats an
  empty CI-evidence result as an explicit pass for repos with no runner
  wired up by design. Only merges once every gate has passed. Includes an
  optional, configurable post-merge step runner.
- **`git-host-api`** — authenticated, redirect-hardened Forgejo REST
  transport with a mandatory post-and-verify readback for comment writes.
- **`stage-body`** — writes a caller-namespaced body file and its
  identity-stamp sidecar atomically, closing the off-argv/off-pipe body
  transport contract shared by `git-host-api` and `review-post`.
- **`acquire`** — platform-agnostic PR diff and changed-file acquisition
  directly from the host API, with an optional scratch-directory staging
  mode for local security scanners.
- **`release detect` / `release dispatch`** — tag-triggered release
  detection and an HMAC-signed event dispatcher, with a caller-supplied
  endpoint, secret, and dispatcher name — no baked-in host or service.
- **`provision-allowlist`** — per-role Bash permission-allowlist fragment
  generator, sourced from a repo's own role declarations.
- **`doctor`** — a read-only deployment-conformance check suite:
  credential-helper resolution, per-role identity coverage, and
  repo-config schema validation, through the same loaders the runtime
  verbs use.
- **`close-pr`** — closes a PR without merging it, behind the same
  namespace and merge-authority gates as `merge`.
- **`post-merge`** — re-runs post-merge steps for an already-merged PR,
  without re-running the full merge gate chain.
- **Attested identity** — every invocation resolves which role is acting
  through an attested detection chain, never a hardcoded agent name.
- **Credential provider seam** — a `TokenProvider` protocol with a
  zero-dependency `StaticTokenProvider` and a `CommandTokenProvider` for a
  deployment's own minting process, selected independently per platform
  (Forgejo, GitHub).
- **Merge-authority provider seam** — merge authority is bound to a role
  through an `AuthorityProvider` seam, with a fail-closed guarantee: an
  unreachable provider, a malformed response, or a role outside the
  configured allow-set all refuse the merge, with no fail-open path.
- **Envelope schemas** — schema-validated work orders and results,
  transport-agnostic so any orchestration layer that can deliver JSON can
  drive a loadout.
- **Guard policy surface** — a harness-agnostic policy library covering
  Bash command classification, Write/Edit scope enforcement, credential
  guarding, and git-operation guarding, for a caller's own hook and
  allowlist generator.
- **`scripts/install.sh`** — a tiered installer (pipx, then uv, then
  `pip install --user`, then a self-managed venv for PEP 668
  externally-managed environments), with PATH-visibility verification and
  idempotent re-install.
