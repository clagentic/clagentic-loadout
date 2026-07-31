# clagentic-loadout — contributor rules

Public product repo. `clagentic: loadout` is the role-scoped agent tooling platform.

## What this repo is

Substrate for controlled agent behavior: platform-agnostic capabilities autonomous
agents act through — attested identity, envelope schemas, guard hooks (command
allowlists, write-path scoping, credential-file denial), a credential-provider seam,
CLI conformance, and role-scoped verbs. Git-host operations (push, review, merge gate,
api, release dispatch) are the largest current surface, not the definition — a
capability an agent uses is in scope here by default, whether or not it touches git.
Roles (builder / reviewer / security / merger / lead), never agent names.

## Hard rules

1. **No internal identity in product code.** No agent names, no operator org/host/path
   hardcodes. Namespaces, endpoints, registries, and casts arrive via config. `clagentic`
   appears only as brand, never as a hardcoded owner check.
2. **Orchestration boundary, not a capability limit.** loadout does not own agent
   spawning, agent identity/roster, or agent-to-agent transport — that's the harness's
   or crew's job, and loadout composes with a credential-minting provider, a
   merge-authority provider, and release/telemetry sinks, all via seams, rather than
   importing any of them directly. This rule constrains WHO owns orchestration; it does
   NOT narrow what capability surface belongs in this package. A capability agents act
   through — a behavior control, a guard, a verb, an attestation mechanism — is in scope
   by default even where it has nothing to do with git. Read narrowly ("if it isn't a git
   operation it doesn't belong here") is a misreading of this rule, not an application of
   it.
3. **Brand and naming standards are review blockers.** Binary `clagentic-loadout`, env
   `CLAGENTIC_LOADOUT_*`, user config `~/.config/clagentic/loadout/`, per-repo config
   `.clagentic/loadout/config.yaml`. License: FSL-1.1-MIT.
4. **CLI hygiene is a tested conformance suite, not prose.** Every verb: `--help`/
   `--version`, a reserved exit-code range with child-process codes remapped off it, and
   error messages that report resolved values (never stale guesses).
5. **Tests move with their subjects.** No verb lands without its test coverage.
6. **No hard dependencies on external or unreleased tooling.** Telemetry/attribution
   events are defined by loadout's own schemas and emitted to generic, configurable sinks:
   none (default), filesystem, webhook (URL + token/HMAC). Identity env and sidecar names
   are `CLAGENTIC_LOADOUT_*`-branded with configurable compat aliases. `task_id` is an
   opaque work-item reference with a configurable pattern — loadout does not assume any
   particular tracker. Merge authority is a provider seam, with a standalone
   static-role-config fallback when no external authority service is configured.
   Conformance gate: the test suite must pass with a synthetic registry, invented agent
   names, and sink `none` — no real deployment identity is required to prove the code
   correct.
7. **Release discipline.** The installer owns PATH/settings wiring. No scratch files in
   the tree — per-spawn `TMPDIR` only.
8. **Internal task IDs belong in code comments and docstrings ONLY — never in a string a
   user sees.** An internal task-tracker id (or any other internal task-tracker
   reference) may appear in a hash-comment or a module/class/function docstring, because that is
   contributor-facing provenance a person reading the source benefits from. It must NEVER
   appear in: argparse `help=`/`description=`/`epilog=`/`usage=` text, a raised exception
   message, a `print()` argument, a JSON Schema `description` field that ships in the
   published contract set, or any other string an external CLI user, integrator, or
   schema-doc renderer actually reads. The same boundary applies to internal repo names
   (e.g. citing an internal PR number or an internally-named repo as evidence inside an
   error message) and to other internal-only tool names used as a reader-facing analogy —
   if an external reader has no way to resolve the reference, it does not belong in a
   user-facing string. Before writing a task id or an internal citation into any string,
   ask whether that string is read by code (fine) or by a person outside this deployment
   (not fine) — when unsure, treat it as user-facing. Enforced mechanically:
   `tests/test_anonymization_guard.py`'s AST-based check walks every string constant in
   `src/**/*.py` (excluding docstrings, which are structurally exempt by Python grammar)
   and fails on an internal-identifier match — it is keyed on AST structure, not a
   hand-maintained file/line list, so a new violation anywhere in `src/` is caught
   automatically rather than requiring a future review pass to notice it.

## Commit convention

Commit subjects follow [Conventional Commits](https://www.conventionalcommits.org/):
`type(scope): description`, optionally with a trailing PR reference like `(#123)`.

Types that trigger a release:

- `feat` — minor version bump.
- `fix`, `perf` — patch version bump.
- A `!` after the type/scope (e.g. `feat!:`), or a `BREAKING CHANGE:` paragraph in the
  commit body, signals a breaking change — normally a major bump, or (while this project
  is still in `0.x`) a minor bump instead.

Types that are valid but release nothing: `build`, `chore`, `ci`, `docs`, `style`,
`refactor`, `test`.

A commit subject that does not conform is **silently ignored** by the release
tooling — it produces no release and no error. Following the convention is what makes a
change show up in a release at all.

The trailing `(#123)` PR-reference form is parsed and rendered as a structured link in
the generated changelog — it is the supported way to reference a pull request from a
commit subject.

## Before opening a PR

A PR is reviewable once all of the following are green:

```sh
python3 -m pytest              # full suite
```

This includes the CLI conformance suite (every verb's `--help`/`--version`/exit-code
behavior) and the anonymization guard (`tests/test_anonymization_guard.py`), which blocks
internal identity, operator hosts, and internal repo references from landing in product
code or public-facing docs — plus an AST-based dimension of the same guard that walks
every non-docstring string constant in `src/**/*.py` (raised messages, `print()` output,
argparse help/description/epilog/usage) and fails on an internal task id or identifier
appearing anywhere a CLI user actually reads it (see Hard rule 8).

See [docs/README.md](docs/README.md) for the documentation front door — start there for
the integrator reading order (config scaffolding, credentials, verbs, provisioning, merge
authority, guard policy, integration contract).
