# `/loadout-init` — guided per-repo loadout config setup

`docs/provisioning.md` documents the schema a repo's
`.clagentic/loadout/config.yaml` supports (`roles:`, `merge:` and its
sub-keys). This document covers the OTHER half of onboarding a repo onto
that schema: getting a clean, correctly filled-in config file onto disk in
the first place, without hand-authoring YAML from prose or copying a live
deployment's own committed config (which may carry that deployment's real
`HOME=`/forge-base-URL values inline — never a valid template source, rule
1).

`.clagentic/loadout/config.yaml` is **per-deployment** and gitignored —
never committed, since it can carry a specific deployment's real values
(see `merge.post_merge_steps` in `docs/provisioning.md`). Two ways onto
disk: run `/loadout-init` below (recommended — walks the elicited pieces
interactively), or copy the tracked `.clagentic/loadout/config.yaml.example`
to `.clagentic/loadout/config.yaml` and edit it directly — same content as
the starter template this skill scaffolds from.

`/loadout-init` (`.claude/skills/loadout-init/SKILL.md`) is the LLM-invocable
skill that drives this as a guided workflow, mirroring the `git init`
idiom familiar from other project-scaffolding tools: a fresh repo's config
is scaffolded and then walked through, not silently generated end to end.
This is a **guided workflow**,
not a single console-script verb — see the skill file for the full,
step-by-step operator conversation; this page documents the schema/tooling
surface it drives against.

## Installing the skill

`/loadout-init` ships as a global, LLM-invocable skill
(`.claude/skills/loadout-init/`) — `scripts/install.sh` copies it out to a
skills directory a harness discovers from, alongside the console_scripts it
already installs onto PATH:

```sh
scripts/install.sh                          # default: ~/.claude/skills/loadout-init/
scripts/install.sh --skills-dir <dir>       # explicit target dir
CLAGENTIC_LOADOUT_SKILLS_DIR=<dir> scripts/install.sh   # same, via env var
```

Resolution order: `--skills-dir`, then `CLAGENTIC_LOADOUT_SKILLS_DIR`, then
a `HOME`-derived default (`~/.claude/skills`). The install is idempotent —
re-running overwrites only `<skills-dir>/loadout-init/`, never any other
skill subdirectory that may already live under the same skills dir. Skipped
(not a hard failure) when `HOME` is empty/unset and no override is given —
a caller that only wants the console_scripts installed is not blocked by
this optional step. See `scripts/install.sh`'s own `--help` and its
"Skill install" header section for the full contract.

## What ships

### The starter template (`src/clagentic_loadout/loadout_init/starter_config.yaml`)

A clean, commented, role-keyed starter config — the copy source
`/loadout-init` uses. Carries:

- `roles:` — five seed roles: builder/reviewer/security/merger/lead — see
  "Keeping the gate satisfiable" below for why `security` is included;
  `docs/provisioning.md`'s own reference mapping documents the same set.
- `merge.merge_requirements` — `ci_pass`/`tests_pass` both `true` by
  default (a repo copying this template is assumed to have CI until told
  otherwise — see the elicitation step below for the on/off conversation).
- `merge.required_reviewer_roles: [reviewer, security]` and
  `merge.authorized_roles: [merger]` — role vocabulary placeholders, never
  agent names. Every role named here has a matching, non-empty verb set
  under `roles:` above by construction — see "Keeping the gate satisfiable"
  below for why that must never drift apart again.
- `merge.pre_checks` and `merge.post_merge_steps` — both commented out.
  Neither is filled in by the template itself: `pre_checks` is optional
  policy a repo may or may not want, and `post_merge_steps` in particular
  carries DEPLOYMENT values (a real `HOME`, a real forge base URL) that
  must never be templated (see this repo's own
  `.clagentic/loadout/config.yaml` header comment for why).

The template contains **zero** deployment-specific values, agent names, or
operator host/path hardcodes — verified in
`tests/test_loadout_init_starter_template.py`.

### `clagentic_loadout.loadout_init.starter_template`

The library API `/loadout-init`'s skill workflow calls into:

- `starter_template_path()` — resolve the packaged template's own on-disk
  path (works from a source checkout or an installed wheel).
- `target_config_path(repo_root)` — resolve the CANONICAL write target,
  `<repo_root>/.clagentic/loadout/config.yaml`. Deliberately distinct from
  `repo_config.resolve_repo_config_path`: that function is a READ-side
  resolver with legacy-path fallback for an repo's EXISTING config;
  `/loadout-init` always targets the new canonical path when writing, even
  for a repo whose only existing config is the legacy
  `.loadout/config.yaml` — initialization is how a repo MOVES onto the new
  path, not a second reader of the old one.
- `copy_starter_template(repo_root, force=False)` — copy the template to
  `target_config_path(repo_root)`, creating parent directories as needed.
  Refuses to overwrite an existing file unless `force=True` — initialization
  never silently clobbers a working repo config.

No interactive I/O anywhere in this module — elicitation (the merge-piece
conversation) and validation (`loadout-doctor`) are separate steps the skill
workflow drives; this module is the deterministic "get a clean copy onto
disk" primitive underneath it, structured the same way every other verb in
this package separates its own read/write mechanics from any CLI/skill
layer that calls it.

## The elicited pieces

`/loadout-init` never fills these in from a default — see the skill file's
own step 2 for the full conversation shape:

| Key | Tier | Elicited how |
|---|---|---|
| `merge.merge_requirements.ci_pass` | repo | on/off, asked explicitly — no implicit default (`gate_config.REQUIREMENT_KEY_CI_PASS`, replace-not-merge per key) |
| `merge.required_reviewer_roles` | repo | role vocabulary only — confirm/rename the template's `reviewer`/`security` placeholders |
| `merge.authorized_roles` | repo | role vocabulary only — confirm/rename the template's `merger` placeholder |
| `merge.pre_checks` | repo | optional; only added if the repo wants pre-merge validation commands |
| `merge.post_merge_steps` | **deployment** | real `HOME`/forge-base-URL/etc. values for THIS deployment — never copied from another deployment's config |

## Keeping the gate satisfiable

A gate role that names no matching `roles:` entry can never emit a verdict —
an **unsatisfiable gate**, and a worse shape than an absent one, because it
reads as protection while gating nothing. This was a live incident: a repo
configured off an earlier version of this template shipped
`roles: builder/reviewer/merger/lead` (no `security`) while its
`merge.required_reviewer_roles` named `security` as a required reviewer —
`security` had no declared verb set (no `git-host-api`, no `review-post`)
and could never post the verdict the gate required from it.
`loadout-doctor` reported a clean pass on that config, because nothing
cross-checked the two sections against each other before this fix.

The root contributor was the template itself: it shipped with no `security`
role even though step 2b below already told the operator to confirm both
`reviewer` and `security` roles for the repo — following the guidance
against the shipped template produced the mismatch. The template now ships
`security: [git-host-api, review-post]` (the shape `docs/provisioning.md`'s
reference mapping and a working fleet deployment both already use) and
includes it in `required_reviewer_roles` by default, so the two sections
agree out of the box; rename or drop `security` from BOTH sections together
if a repo genuinely has no separate security-review role.

`loadout-doctor`'s `repo_loadout_schema` check now cross-references every
gate role against the repo's own declared `roles:` and reports the mismatch
at two severities, not one:

- The repo's own `roles:` section is **present** and a gate role matches
  nothing in it → **FAIL** (`ok=False`). The config as written can never
  satisfy its own gate — this is the exact incident shape above.
- The repo declares **no** `roles:` section at all (the check falls back to
  a reference role taxonomy, not this repo's own declaration) and a gate
  role matches nothing in that reference set → **WARN** (`ok` stays
  `True`) — not provably unsatisfiable, since the deployment may resolve
  roles through a mechanism outside this repo's own config.

This check is diagnostic-only: it runs inside `loadout-doctor`, never inside
`loadout-push`/`loadout-merge` — an unsatisfiable-gate config can always
still be pushed and corrected; nothing about this validation can lock a
repo out of fixing itself. See
`clagentic_loadout.merge.gate_config`'s module docstring ("BLAST RADIUS OF
EVERY RAISE THIS MODULE INTRODUCES") for the full contract.

## Validation

The final step runs `loadout-doctor --repo-root <repo>` — the SAME
conformance suite `docs/provisioning.md`'s own schema section is validated
against (`doctor.checks.check_repo_loadout_schema`, which itself loads every
section through the real owning loader: `provisioning.roles`,
`merge.gate_config`, `merge.post_merge_config`, `merge.pre_checks_config`).
Green means the initialized config is schema-valid and ready; a lingering
legacy `.loadout/` directory is a WARN (migration cleanup), never a hard
failure.

## Conformance

Exercised with a synthetic `tmp_path` repo, no LORE present, and the SAME
section-owning loaders the real runtime path uses (never a doctor-only or
`/loadout-init`-only second schema) — see
`tests/test_loadout_init_starter_template.py`.
