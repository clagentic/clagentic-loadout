---
name: loadout-init
description: Guided, LLM-invocable setup of a repo's .clagentic/loadout/config.yaml. Copies the clean starter template (clagentic_loadout.loadout_init.starter_template), then runs interactive elicitation with the operator to fill in the merge pieces (post_merge_steps deployment values, ci_pass on/off, required_reviewer_roles/authorized_roles/pre_checks), then validates with loadout-doctor. NOT a silent scaffold -- every deployment-specific value is elicited, never templated from another deployment's real config. Reach for this whenever a repo has no .clagentic/loadout/config.yaml (or only the legacy .loadout/config.yaml) and needs one set up correctly instead of hand-authored.
version: 1.0.0
user_invocable: true
triggers:
  - loadout-init
  - /loadout-init
  - initialize loadout config
  - set up loadout config
  - scaffold .clagentic/loadout/config.yaml
  - loadout init
  - onboard repo to loadout
---

# loadout-init

Guided setup of a repo's `.clagentic/loadout/config.yaml`: copy the clean
starter template, elicit the deployment-specific pieces from the operator,
validate with `loadout-doctor`. This is the repeatable, driven flow for
fleet-wide rollout -- it replaces "read prose and hand-author
YAML" with a checklist a session actually walks. Mirrors the `git init` /
`lore init` idiom: guided scaffold of a fresh repo's config, not a form-fill.

This skill wraps `clagentic_loadout.loadout_init.starter_template`
(`copy_starter_template`) plus the `loadout-doctor` console script. Both live
in this repo (`clagentic-loadout`).

## When to use

- A repo has NO `.clagentic/loadout/config.yaml` at all.
- A repo has only the legacy `.loadout/config.yaml` and is migrating --
  initialize it onto the new canonical path rather than
  hand-editing the legacy file in place.
- An existing `.clagentic/loadout/config.yaml` needs a guided re-check
  (re-run steps 2-3 against the existing file instead of re-copying it).

## Workflow

### 0. Resolve the target repo (defaults to cwd)

Walk up from cwd to the nearest git repo root (`git rev-parse
--show-toplevel`) unless the invocation names an explicit repo path. If cwd
is not inside a git repo, ask for an explicit path -- do not guess.

Check whether `.clagentic/loadout/config.yaml` already exists:

- **Absent, and no legacy `.loadout/config.yaml` either** -- fresh
  initialization, proceed to step 1.
- **Absent, but legacy `.loadout/config.yaml` present** -- this is a
  migration. Proceed to step 1 anyway (the copy targets the canonical new
  path unconditionally, per `loadout_init.starter_template.target_config_path`
  -- it never redirects onto the legacy path even though one exists). Note
  for the operator that the legacy file should be removed once the new one
  is verified (`loadout-doctor` flags a lingering legacy dir as WARN, not a
  hard failure, so this is a cleanup step, not a blocker).
- **Already present** -- do NOT silently re-copy over it (the underlying
  `copy_starter_template` call refuses this without `force=True`, by
  design: `/loadout-init` never clobbers a working config). Ask the
  operator whether they want a full re-initialization (`force=True`,
  discards the existing file) or just to re-run the elicitation/validation
  passes (step 2-3) against what's already there.

### 1. Copy the starter template (never a silent scaffold)

```python
from clagentic_loadout.loadout_init.starter_template import copy_starter_template
copy_starter_template(repo_root)
```

This lands a clean, commented, role-keyed template at
`<repo_root>/.clagentic/loadout/config.yaml` -- see
`src/clagentic_loadout/loadout_init/starter_config.yaml` for exactly what
ships. It carries **no deployment values and no agent names** (rule 1): the
template's `roles:`/`merge.required_reviewer_roles`/`merge.authorized_roles`
use role vocabulary only (builder/reviewer/security/merger/lead), and its
`merge.post_merge_steps` section is commented out entirely -- there is
nothing in it to accidentally ship another deployment's real `HOME=`/forge
URL. `security` ships with a real verb set (`git-host-api`, `review-post`)
and is already named in `required_reviewer_roles` -- a prior fix addressed a
live incident where an earlier template version left `security` out of
`roles:` while still requiring it as a reviewer, an unsatisfiable gate step
2b below would otherwise reproduce (see "Elicit the merge pieces" step b).

This step is NOT the finish line. The file that lands is a starting point;
step 2 elicits the pieces that make it real for THIS repo.

### 2. Elicit the merge pieces -- ask, don't assume

Work through each of these with the operator as an actual conversation, not
a form to auto-fill. Do not invent a default for any of these on the
operator's behalf; a repo's own conventions (branch prefixes, whether it
even runs CI) determine every answer here.

**a. `merge.merge_requirements.ci_pass` -- on or off, no implicit default.**
Ask: "Does this repo have CI, and should a merge require it to be green?"
- `true` -- this repo has CI and it gates merges.
- `false` -- this repo has no CI (or CI is intentionally not a gate here).
  Precedent: this repo (`clagentic-loadout`) itself is `ci_pass: false`
  (`.clagentic/loadout/config.yaml`, ratified 2026-07-13) -- CI genuinely
  does not exist here and is not going to be stood up. That precedent does
  NOT transfer automatically to another repo; ask fresh every time.
Per-key replace-not-merge semantics
(`clagentic_loadout.merge.gate_config.REQUIREMENT_KEY_CI_PASS`): whichever
value you set REPLACES the template's own `ci_pass` default; leaving
`tests_pass`/`max_changed_files` alone keeps THEIR defaults untouched.

**b. `merge.required_reviewer_roles` / `merge.pre_checks` /
`merge.authorized_roles` -- role vocabulary only.**
Ask which roles this repo actually uses for review and merge authority
(the template ships `reviewer` **and** `security` as required-reviewer
placeholders, `merger` for authorized_roles -- confirm or rename). Never
write an agent's own name into any of these lists -- if the operator names
a specific agent, translate it to its ROLE, not its identity.

**Every role you keep in `required_reviewer_roles`/`authorized_roles` MUST
have a matching, non-empty verb set under `roles:` in the SAME file** -- a
role with no verb set (no `git-host-api`, no `review-post`, etc.) can never
post the verdict a gate requires from it, an **unsatisfiable gate**
(`loadout-doctor` FAILS this shape when the repo's own
`roles:` section is present but omits the role, rather than silently
passing). If the operator drops `security` (or any other role) from the
required-reviewer list because this repo has no separate security-review
step, drop it from `roles:` too -- don't leave an orphaned, unused role
declaration behind, and don't leave a gate role with nothing backing it.

`pre_checks` (commands run before a merge is authorized, same
`cmd`/`description`/`on_failure` shape as `post_merge_steps`) is optional --
only add it if the repo actually wants pre-merge validation commands beyond
CI + reviewer verdicts.

**c. `merge.post_merge_steps` -- deployment values, ALWAYS elicited, NEVER
templated.**
If this repo wants post-merge automation (e.g. a self-install step), ask
for the real values THIS deployment needs -- `HOME`, the forge base URL,
any other environment the step's `cmd` needs inline. Do NOT copy this
box's own values (or any other live deployment's values) into the file you
are editing (rule 1; see this repo's own `.clagentic/loadout/config.yaml`
header comment for why its inline `HOME=`/`--git-host-base-url` prefix is a
DEPLOYMENT-only value, never product-code-portable). If the repo has no
post-merge automation need, leave the section commented out/absent -- that
is a fully valid, common shape.

Edit `<repo_root>/.clagentic/loadout/config.yaml` directly with the
elicited values once the operator has answered each of a/b/c.

### 3. Validate

```sh
loadout-doctor --repo-root <repo_root>
```

(or `python3 -m clagentic_loadout.doctor.cli --repo-root <repo_root>` from a
source checkout without the console script installed).

Green (`doctor: N/N checks passed`, exit 0) = initialization complete. A
WARN on a lingering legacy `.loadout/` dir (`legacy_dir_present` in the
`repo_loadout_schema` check's resolved output) is not a hard failure but
should be cleaned up once the operator confirms the new config is correct --
delete the legacy directory/file once you're done. A FAIL means a malformed
section (bad role name, wrong type, unknown key) -- fix it and re-run;
`loadout-doctor` reports the exact offending value and the resolved config
path, never a stale guess. A FAIL can also mean an **unsatisfiable gate**:
a role in `required_reviewer_roles`/`authorized_roles` with no
matching `roles:` entry in this same file -- the message names the
offending role and file path and states the fix directly (add the role
under `roles:` with a real verb set, remove it from the gate list, or
declare `required_reviewer_roles: []` if no reviewer verdict should be
required at all). This is a `loadout-doctor` diagnostic only; it never
blocks `loadout-push`/`loadout-merge` themselves, so pushing the corrected
config always still works.

## Notes

- This skill never elicits or writes CREDENTIALS (no login, no email, no
  token) into the repo-local file -- that is deployment-tier config living
  at the USER level (`~/.config/clagentic/loadout/config.yaml`,
  `push.identity_config` / `review.login_config`), out of scope for
  per-repo initialization. See `docs/provisioning.md`'s "Deployment-tier
  identity sections" for that separate, user-level setup.
- `copy_starter_template` and `target_config_path` both resolve the
  CANONICAL new path (`.clagentic/loadout/config.yaml`) unconditionally --
  neither is redirected onto a legacy `.loadout/config.yaml` even if one
  already exists (unlike `repo_config.resolve_repo_config_path`'s own
  read-side legacy fallback, which is a READ concern for existing repos,
  not a `/loadout-init` write concern).
- See `docs/loadout-init.md` for the schema reference this workflow drives
  toward, mirroring `docs/verbs.md`/`docs/provisioning.md`'s own style.
