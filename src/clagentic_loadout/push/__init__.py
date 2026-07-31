"""clagentic_loadout.push — bot-attributed push + PR-open verb.

Wave B slice 3 (lr-09ca, tome #688). Ported from the reference agent-push
transport; the source module stays primary until its separate CUT OVER +
RETIRE + VERIFY-GONE task per the migration plan.

WHAT THIS PACKAGE DOES: push exactly one branch's commits, re-authored to a
configured bot identity, then open (or update) a PR — never merge, never
push to main. Merge authority is the merge verb's job (a later loadout
slice), not this one.

Submodules:
  namespace_guard  — config-driven allowed-owner allowlist (never a
                      hardcoded brand-owner literal — repo CLAUDE.md hard
                      rule 1).
  identity         — bot-identity re-authoring + HEAD-author verification.
  git_push         — token-safe `git push` via GIT_ASKPASS + HOME isolation.
  issue_link       — 'Closes #NN' trailer normalization + enforcement.
  forgejo_backend   — Forgejo PR create/update, reusing transport.git_host_api.
  github_backend    — GitHub PR create/update (stdlib urllib, redirect-guarded).
  verb             — the CLI entry point tying the above together.
  errors           — shared exception vocabulary.

ROLE, NOT AGENT NAME: nothing in this package hardcodes an acting identity.
The bot identity (name + email) and the caller's credential-resolution role
are always inputs — config or CLI flags — never names baked into the code
path.
"""

from __future__ import annotations
