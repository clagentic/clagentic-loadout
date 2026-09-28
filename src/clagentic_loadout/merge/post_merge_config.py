"""merge.post_merge_config — repo-local `post_merge_steps` config surface
(lr-77d6, lr-3812) + the deployment-owned env-override seam (lr-52d7).

Reads a merged repo's OWN repo-local config file (default
`.clagentic/loadout/config.yaml` — see `repo_config.py`, lr-446c35, for the
shared path constant/legacy-fallback loader every section owner in this
package now resolves through; the same one-file/one-section-per-verb
convention `wait.config` and `provisioning.roles` already established — see
those modules' docstrings) for a `post_merge_steps`
list under the `merge:` top-level section — this module's own verb owns that
section, same as `wait.config` owns `wait:` and `provisioning.roles` owns
`roles:`. No role name (e.g. a merge-authority role from a particular
deployment's own `roles:` taxonomy) is ever the section key here: this is
loadout-merge's OWN post-merge configuration, independent of which role
invokes it.

This is REPO-LOCAL config (unlike `transport.provider_config`'s credentials
tier, which is deliberately USER-LEVEL only, lr-0818): the whole point of
post_merge_steps is "what does THIS repo want run in ITS OWN working tree
after a merge lands" — e.g. `scripts/install.sh` to self-install a package
build. A repo-local file choosing what runs IN that repo's own root is the
correct trust boundary (contrast with the credentials tier, where a cloned
repo's config choosing the command that mints a caller's git-host TOKEN
would be a privilege-escalation surface across repos).

CONFIG-ROOT VS GIT-TREE-ROOT (lr-93d718): `--repo-path` (the caller-supplied
root this whole module resolves config FROM) is not always a git working
tree. A "wrapper layout" repo keeps its `.clagentic/loadout/config.yaml`
alongside other non-git tooling state at a wrapper directory, while the ACTUAL
git checkout (the `.git` directory `merge.tree_sync.advance_repo_to_merged_sha`
needs to `git fetch`/`git checkout` in) lives at a subdirectory of that
wrapper (e.g. `<wrapper>/repo/.git`). Before this task, `merge.verb._run`
passed the SAME `--repo-path` to both `advance_repo_to_merged_sha` (which
requires a git working tree) and `load_post_merge_steps` (which requires the
config-bearing root) — no single value satisfies both for that layout: passing
the wrapper fails tree_sync ("not a git repository", EXIT_POST_MERGE_FAILED);
passing the git-tree subdirectory instead silently loads zero
`post_merge_steps` (config discovery never finds it there).

`git_working_tree` (an OPTIONAL key inside the `merge:` section, read by
`resolve_git_working_tree` below) closes this gap WITHOUT adding a second
YAML-reading path: when present, it names a path RELATIVE to the resolved
config root (e.g. `repo`) that `merge.verb._run` joins onto that root before
calling `advance_repo_to_merged_sha` — config discovery itself (this module's
`load_post_merge_steps`) is entirely unaffected and keeps reading from the
config root exactly as before. When the key is ABSENT (the common, flat-
layout case), `merge.verb._run`'s tree-sync target is UNCHANGED: `--repo-path`
itself, exactly as before this task — this is a purely additive, narrowing-
safe knob (absent key = old behavior, never a behavior change for a repo that
never declares it).

OPTIONS CONSIDERED (naming the trade-off, per this package's own CLAUDE.md
"Principle conflict" rule): an upward `.git`-search from `--repo-path` (walk
ancestors until a `.git` is found) was considered as a more "automatic"
alternative that would need no new config key at all. Rejected in favor of
the explicit knob here: an upward search cannot distinguish "this repo's own
git tree is one level down" from "there happens to be an unrelated git
checkout somewhere above `--repo-path` on this machine" — it would silently
resolve to whatever `.git` it finds first, which is exactly the kind of
surprising auto-resolution this package's CLAUDE.md rule 2 (explicit inputs/
outputs, no action-at-a-distance) warns against. An explicit, repo-committed
`git_working_tree:` value is unambiguous, versionable, and requires no
directory-walk heuristic at all.

Every step is validated (`merge.post_merge.validate_post_merge_steps`) at
LOAD time — a malformed step in the repo's own committed config is a
config-load-time error, never a mid-run surprise after the merge already
executed.

DEPLOYMENT ENV-OVERRIDE SEAM (lr-52d7): `post_merge_steps` runs each step
with `env={**os.environ, **assignments}` (`merge.post_merge`), where
`assignments` comes ONLY from a leading `VAR=VALUE` token parsed off the
step's own `cmd` — there was previously no supported way for a DEPLOYMENT
to inject an env var (e.g. `HOME`, in an isolated-HOME spawn harness) into a
step WITHOUT hand-editing that machine value into the step's `cmd` string in
the repo's own COMMITTED `.loadout/config.yaml` (a rule-1 violation: no
machine-specific values in product code, see this repo's own CLAUDE.md).

`resolve_env_overrides` closes that gap by mirroring
`transport.provider_config`'s established USER-LEVEL config-file tier
EXACTLY — same file (`~/.config/clagentic/loadout/config.yaml`), same
loader (`transport.provider_config.load_user_config_section`), same
env-var-wins-over-config-file precedence — rather than inventing a second
config surface or a second YAML parser:

  1. Env vars matching `CLAGENTIC_LOADOUT_POST_MERGE_ENV_<NAME>` — e.g.
     `CLAGENTIC_LOADOUT_POST_MERGE_ENV_HOME=/root` injects `HOME=/root`.
     Read directly from the invoking process's own environment, so a
     deployment's spawn harness supplies this the same way it supplies any
     other env var — no file write required.
  2. The user-level config file's `post_merge_env:` section — a flat
     mapping of `VAR: value` pairs, e.g.:

         post_merge_env:
           HOME: /root

     This file lives OUTSIDE the repo tree entirely (default
     `~/.config/clagentic/loadout/config.yaml`) — it is never part of a git
     checkout, never committed, and therefore categorically excluded from
     both the repo's own tracked `.loadout/config.yaml` (rule 1) and this
     repo's published tree (which only ever carries tracked repo content,
     see this repo's CLAUDE.md rule 8). A deployment's OWN provisioning step
     (an installer, a spawn harness's own bootstrap) writes this file once;
     loadout ships the reader, never a machine value.

Tier 1 wins over tier 2 per-variable (matching `provider_config`'s
`env.get(...) or config_section.get(...)` precedence) — a deployment can
override one variable at spawn time without touching its own installed
config file. Both tiers are OPTIONAL and independent of `post_merge_steps`
itself: a repo with no `post_merge_env` anywhere behaves exactly as before
this feature (empty overrides, `env=None` passthrough unchanged).

Resolved overrides are the LOWEST-precedence env source at step-execution
time: `merge.post_merge.run_post_merge_steps` layers
`{**os.environ, **deployment_overrides, **step_var_assignments}`, so a
step's own inline `VAR=VALUE` prefix (repo-local, committed, explicit for
THIS step) always wins over a deployment-wide override for the same name —
the repo config still has the last word for its own steps; the deployment
seam only fills gaps `os.environ` does not already cover.

THE lr-e84ae1 CHICKEN-AND-EGG (fixed, do not reintroduce): `resolve_env_overrides`
is called from `merge.verb._run` with NO explicit `config_root` (production
call shape), so tier 2 falls through to `transport.provider_config.
DEFAULT_USER_CONFIG_ROOT` — which is derived from `release.secrets_config.
DEFAULT_CONFIG_ROOT`, itself resolved ONCE at import time from the invoking
process's OWN home directory. A deployment whose isolated-HOME spawn harness
sets `HOME=''` (present but empty, not merely unset) hit a `Path.home()`
quirk: CPython's `Path.home()` treats `HOME` as authoritative whenever the
key is PRESENT, even when its value is the empty string, so it short-circuits
to `PosixPath('/')` instead of falling through to the passwd-database lookup
an entirely-UNSET `HOME` would trigger — sending every user-level config
lookup (this seam's own tier 2 included) to `/.config/clagentic/loadout/`
instead of the real `~/.config/clagentic/loadout/`. A CORRECTLY-provisioned
tier-2 file at the real path was therefore never found, even though the seam
itself (this module, `run_post_merge_steps`'s env layering) was always
correct — the break was one layer further down, in how the process's own
home directory got resolved in the first place. Fixed in
`release.secrets_config._resolve_home_dir`, which falls through to
`pwd.getpwuid(os.getuid()).pw_dir` whenever `HOME` is falsy (empty OR
unset), matching what an unset `HOME` already did correctly.

Alternatives considered and rejected (naming the trade-off, per this
package's own CLAUDE.md "Principle conflict" rule):

  - **A per-step `env:` structured field inside `post_merge_steps` itself.**
    Rejected: that field would live in the SAME repo-local, COMMITTED
    `.loadout/config.yaml` `post_merge_steps` already reads from — the
    exact file this seam exists to keep machine-value-free. A structured
    field in a committed file is not a deployment override, it is the same
    rule-1 problem with better syntax.
  - **An env-passthrough ALLOWLIST on the verb** (e.g. `--pass-env HOME`,
    letting a step opt into inheriting one named var from the CALLING
    process). Rejected as the sole mechanism: it still requires the
    invoking deployment process itself to already have the right value in
    ITS OWN environment before exec'ing `loadout-merge` — which was
    already true before this task (the prior, docs-only revision of this
    same task documented exactly that precondition) and does not close the
    gap for a harness whose own spawn env is deliberately stripped/isolated
    (this repo's own isolated-HOME spawn harness, the motivating case) and
    cannot simply export more into itself.
  - **A `.loadout-local` gitignored overlay file inside the repo tree**
    (merging over the committed `.loadout/config.yaml`). Rejected in favor
    of the user-level file: this package already has ONE established
    non-repo-local config surface (`~/.config/clagentic/loadout/config.yaml`,
    `transport.provider_config` / `transport.git_host_api`) that is
    unambiguously outside any repo checkout and therefore trivially
    excluded from this repo's published tree by construction — a repo-tree
    overlay file would need ITS OWN gitignore entry, its own publication
    exclusion rule, and a second "is this file real deployment config or
    did it leak into a commit" trust question the user-level file does not
    have. Reusing the established tier is strictly less new surface for an
    equivalent result.

POST_MERGE_STEP_TIMEOUT_SECONDS (lr-d6e52b): a repo-tier, `merge:`-section,
OPTIONAL key giving `merge.post_merge.run_post_merge_steps` a
`default_timeout_seconds` fallback for any ORDINARY (non-detached) step that
does not set its own `timeout_seconds` -- see that module's docstring, "THE
lr-d6e52b HARDENING", for the full defect this closes (a step that hangs
because it forks a daemon without being flagged `detaches: true` previously
blocked the whole merge process forever; NOW it fails loudly after this
bound). Absent (the default, `None`): NO repo-wide bound is applied -- every
step keeps `subprocess.run`'s unbounded wait, BYTE-IDENTICAL to this feature
never existing, exactly the same "additive, config-gated, sane-default-is-
off" posture `enforce_merge_shape` and `sync_tree_after_merge` both already
established in this same section. A DIFFERENT built-in numeric default (e.g.
"300 seconds for everyone") was considered and rejected: this repo's own
CLAUDE.md non-negotiable constraint for this task is explicit -- "do not
convert a currently-hanging step into a currently-FAILING step without the
timeout being configurable" -- and a shipped tool with external users whose
post_merge_steps legitimately take longer than any single guessed default
would see an already-passing step start failing the moment this ships, for a
value it never chose. A per-repo OPT-IN default (this key) plus a per-step
override (`timeout_seconds` on the step itself, always wins when both are
set) gives every deployment exactly the bound ITS OWN steps' actual duration
profile calls for, never a guessed one.

SYNC_TREE_AFTER_MERGE (lr-d95cdb): a repo-tier, `merge:`-section, replace-
not-merge key controlling whether `merge.verb._run` advances `--repo-path`
to the merged main tip AT ALL (via `merge.tree_sync.advance_repo_to_merged_sha`
plus, after any `post_merge_steps` run, `merge.tree_sync.land_on_base_branch`)
-- DEFAULTING TO ON. Before this key existed, tree_sync ran ONLY as a
prerequisite to `post_merge_steps`: a repo with none configured got NO local
sync after a merge at all, and the tree stayed wherever the caller last left
it (typically the feature branch HEAD that opened the PR) -- a recurring,
silent-until-late failure mode for whatever gets dispatched into that tree
next. `resolve_sync_tree_after_merge` below reads the SAME `merge:` section
`load_post_merge_steps` / `resolve_git_working_tree` already read (no new
YAML-reading path), same replace-not-merge convention `merge.gate_config`
already established for `merge_requirements`/`required_reviewer_roles`/
`authorized_roles`: a repo that sets `sync_tree_after_merge: false` gets
exactly that value; a repo that never mentions the key gets the built-in
default (`True`), never a silent absence-means-off surprise.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

from clagentic_loadout.merge.post_merge import PostMergeConfigError, validate_post_merge_steps
from clagentic_loadout.repo_config import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    LEGACY_CONFIG_RELATIVE_PATH,
    resolve_repo_config_path,
    resolve_repo_config_root,
)
from clagentic_loadout.transport.provider_config import load_user_config_section

#: Top-level section key this module owns within the repo-local config
#: file — loadout-merge's own section, matching wait.config's `wait:` /
#: provisioning.roles' `roles:` naming (verb name, never a role identity).
CONFIG_SECTION_MERGE = "merge"

#: Key within the `merge:` section holding the ordered post-merge step list.
CONFIG_KEY_POST_MERGE_STEPS = "post_merge_steps"

#: Key within the `merge:` section holding an OPTIONAL path, relative to the
#: resolved config root, naming the git working tree `merge.tree_sync.
#: advance_repo_to_merged_sha` should target (lr-93d718 — see module
#: docstring, "CONFIG-ROOT VS GIT-TREE-ROOT"). Absent by default: a repo that
#: never declares this key keeps the pre-lr-93d718 behavior of targeting
#: `--repo-path` itself for tree_sync.
CONFIG_KEY_GIT_WORKING_TREE = "git_working_tree"

#: Key within the `merge:` section controlling whether `merge.verb._run`
#: advances `--repo-path` to the merged main tip at all (lr-d95cdb — see
#: module docstring, "SYNC_TREE_AFTER_MERGE"). Defaults to `True` when
#: absent -- a repo that never declares this key still gets the tree synced.
CONFIG_KEY_SYNC_TREE_AFTER_MERGE = "sync_tree_after_merge"

#: Built-in default for CONFIG_KEY_SYNC_TREE_AFTER_MERGE when the repo's own
#: config never mentions it (lr-d95cdb, operator directive: sync ON by
#: default, per-repo opt-out).
DEFAULT_SYNC_TREE_AFTER_MERGE = True

#: Key within the `merge:` section giving `run_post_merge_steps` a repo-wide
#: fallback `timeout_seconds` for any ORDINARY (non-detached) step that does
#: not set its own (lr-d6e52b -- see module docstring,
#: "POST_MERGE_STEP_TIMEOUT_SECONDS"). Absent by default -- a repo that never
#: declares this key gets NO repo-wide bound, matching pre-lr-d6e52b
#: behavior exactly.
CONFIG_KEY_POST_MERGE_STEP_TIMEOUT_SECONDS = "post_merge_step_timeout_seconds"

#: Built-in default for CONFIG_KEY_POST_MERGE_STEP_TIMEOUT_SECONDS when the
#: repo's own config never mentions it: `None`, i.e. no bound at all.
DEFAULT_POST_MERGE_STEP_TIMEOUT_SECONDS = None

#: Key within the `merge:` section controlling whether a detected
#: requested-vs-actual merge-shape mismatch (merge.merge_shape.
#: check_merge_shape) is a hard failure (EXIT_MERGE_SHAPE_MISMATCH) or a
#: stderr-only warning (lr-14f704 item 3 -- see merge.merge_shape's own
#: docstring, "WARN-BY-DEFAULT, CONFIG-GATED STRICTNESS", for the full
#: trade-off). Defaults to False (warn only) -- a repo opts into the
#: stricter behavior explicitly; this key never changes behavior for a repo
#: that never declares it.
CONFIG_KEY_ENFORCE_MERGE_SHAPE = "enforce_merge_shape"

#: Built-in default for CONFIG_KEY_ENFORCE_MERGE_SHAPE when the repo's own
#: config never mentions it.
DEFAULT_ENFORCE_MERGE_SHAPE = False

#: Key within the `merge:` section controlling whether a reviewer-verdict
#: comment body carrying MORE THAN ONE fenced ```review-result``` block is a
#: hard gate refusal (lr-5260f9 -- see merge.verdict.
#: assert_verdict_block_count_at_most_one and merge.verb's reviewer-verdict
#: step). ENFORCE-BY-DEFAULT, CONFIG-GATED OPT-OUT (deliberately the inverse
#: shape of CONFIG_KEY_ENFORCE_MERGE_SHAPE -- see below for why this one key
#: does NOT mirror that trade-off): a multi-fence body is unambiguously
#: malformed input at THIS gate specifically -- the actual merge-gate READ
#: path (merge.verdict.read_reviewer_verdict), which decides what lands on
#: main. Both known tool-owned producers (transport.git_host_api's
#: --expect-verdict-block and review.verb's --verdict-review-status route)
#: now REFUSE to construct a multi-fence body in the first place, so no
#: known-good caller of this gate can be producing one; a caller who somehow
#: still is has been getting a SILENTLY MIS-PARSED verdict (last-fence-wins
#: can resolve a blocking+clean pair to clean) every time, which is strictly
#: worse for them than a loud refusal with a one-line documented opt-out.
#: Defaults to True (hard refusal via
#: merge.verdict.assert_verdict_block_count_at_most_one) -- a repo with
#: legacy multi-fence comments it cannot immediately clean up sets this key
#: to `false` explicitly to fall back to the pre-existing, unconditional
#: last-fence-wins parse; this key is an OPT-OUT escape hatch, not a
#: safety toggle a caller must remember to switch on. Deliberately homed in
#: THIS module rather than merge.gate_config: gate_config's own "BLAST
#: RADIUS" docstring section documents, and tests/test_doctor_checks.py's
#: test_merge_verb_and_push_verb_never_import_gate_config statically locks,
#: the invariant that merge.verb NEVER imports merge.gate_config (every
#: raise gate_config can produce must stay diagnostic-only, reachable only
#: from loadout-doctor, never a reason loadout-merge itself refuses to run
#: -- see that module's docstring for the full bootstrap-safety rationale).
#: This key's resolver IS called from merge.verb (the reviewer-verdict gate
#: step, mirroring resolve_enforce_merge_shape's own call site), so it
#: belongs in this module, which merge.verb already imports for exactly
#: this class of repo-tier merge-gate knob.
CONFIG_KEY_ENFORCE_SINGLE_VERDICT_FENCE = "enforce_single_verdict_fence"

#: Built-in default for CONFIG_KEY_ENFORCE_SINGLE_VERDICT_FENCE when the
#: repo's own config never mentions it: True (enforce). A repo that never
#: declares this key gets the SAFE behavior -- the merge gate refuses a
#: multi-fence reviewer-verdict body -- not the permissive one. Contrast
#: with DEFAULT_ENFORCE_MERGE_SHAPE (False): that key gates a NEW hard
#: failure for a flag (--merge-method) that was previously silently
#: ignored by every caller, so defaulting to warn-only avoided breaking
#: callers mid-fix-rollout. This key gates a gate-integrity property (which
#: fence a merge-authorizing verdict actually reads) with no known-good
#: caller left who could regress -- see this key's own docstring above.
DEFAULT_ENFORCE_SINGLE_VERDICT_FENCE = True

#: Key within the `merge:` section controlling whether a required
#: reviewer's `clean` verdict must ALSO carry a genuine `model_attested`
#: declaration (lr-95543d -- see merge.model_attestation's module
#: docstring for the full trust model, and merge.verb's reviewer-verdict
#: step for the call site). OPT-IN, DEFAULTS OFF -- deliberately the SAME
#: shape as CONFIG_KEY_ENFORCE_MERGE_SHAPE, NOT
#: CONFIG_KEY_ENFORCE_SINGLE_VERDICT_FENCE's enforce-by-default posture:
#: unlike the multi-fence check (where every known-good producer already
#: refuses to construct the bad shape, so nothing legitimate could regress),
#: `model_attested` is a BRAND NEW field essentially no reviewer producer
#: emits yet at the time this key is introduced -- defaulting to ON would
#: refuse every existing 'clean' verdict from every reviewer that has not
#: yet adopted the field, the exact "a currently-passing gate starts
#: failing for a value the caller never chose" hazard this package's own
#: CLAUDE.md non-negotiable (see merge.post_merge_config's own
#: POST_MERGE_STEP_TIMEOUT_SECONDS precedent) warns against. A deployment
#: opts in explicitly once its reviewer producers are updated to emit the
#: field.
CONFIG_KEY_REQUIRE_MODEL_ATTESTATION = "require_model_attestation"

#: Built-in default for CONFIG_KEY_REQUIRE_MODEL_ATTESTATION when the
#: repo's own config never mentions it: False (no-op) -- see that key's own
#: docstring for the rollout-hazard rationale.
DEFAULT_REQUIRE_MODEL_ATTESTATION = False

#: Key within the `merge:` section giving `merge.model_attestation.
#: assert_model_attested` an OPTIONAL, deployment-specific list of
#: additional case-insensitive denylist terms (e.g. a known degraded-
#: fallback model name) to reject a `model_attested` value against, ON TOP
#: of the built-in bare-tier-alias/no-digit-shape check that key's own
#: resolver always applies regardless of this list's contents. Absent or
#: empty by default -- a repo that never declares this key gets ONLY the
#: built-in shape check, never a silent extra-strict default.
CONFIG_KEY_MODEL_ATTESTATION_DENYLIST = "model_attestation_denylist"

#: Top-level section this module owns within the USER-LEVEL
#: <config_root>/config.yaml (lr-52d7) — the deployment env-override tier,
#: read through the SAME `load_user_config_section` loader/config-root
#: convention `transport.provider_config`'s credentials tier and
#: `transport.git_host_api`'s git-host base-URL tier both already use. A flat
#: mapping of env-var name -> value, e.g. `{"HOME": "/root"}`.
CONFIG_SECTION_POST_MERGE_ENV = "post_merge_env"

#: Prefix for the env-var tier (highest precedence): a var named
#: `CLAGENTIC_LOADOUT_POST_MERGE_ENV_<NAME>` injects `<NAME>=<value>` into
#: every post_merge_steps subprocess's environment for THIS invocation only —
#: no config-file write required. `<NAME>` must itself be a valid bare env-var
#: identifier (see `_ENV_OVERRIDE_VAR_RE`); anything not matching the prefix
#: is not consulted by this tier at all.
ENV_OVERRIDE_PREFIX = "CLAGENTIC_LOADOUT_POST_MERGE_ENV_"

#: Matches an env-override source var's NAME suffix (the part after
#: ENV_OVERRIDE_PREFIX) against the same bare-identifier grammar a shell
#: env-assignment target requires — mirrors merge.post_merge._ENV_ASSIGN_RE's
#: own grammar for the VAR half of a `VAR=VALUE` prefix, so both env-override
#: sources (a step's own inline prefix, and this deployment-wide tier) accept
#: the identical variable-name shape.
_ENV_OVERRIDE_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")



def _read_yaml_mapping(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PostMergeConfigError(f"{path}: could not be read as YAML: {exc}.") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise PostMergeConfigError(
            f"{path}: top-level document must be a mapping, got {type(raw).__name__}."
        )
    return raw


def load_post_merge_steps(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> list[dict]:
    """Resolve the post_merge_steps list for a repo.

    Reads `<repo_root>/<config_relative_path>` (default
    `.clagentic/loadout/config.yaml`, falling back to the legacy
    `.loadout/config.yaml` path when only that one exists — see
    `repo_config.resolve_repo_config_path`), expecting a `merge:` top-level
    section holding a `post_merge_steps` key (a list of step mappings — see
    `merge.post_merge.run_post_merge_steps` for the step shape).

    Returns `[]` (no steps to run — a no-op, never an error) when
    *repo_root* is None, the config file is absent, the `merge:` section is
    absent, or the `post_merge_steps` key is absent within it. A repo that
    never opted into post-merge automation is unaffected.

    Raises:
        PostMergeConfigError: the config file exists but is unreadable/
            malformed YAML, `post_merge_steps` is present but not a list, or
            any individual step fails `validate_post_merge_steps` (missing
            cmd, shell-operator token in a cmd string, invalid on_failure,
            etc.) — always at LOAD time, before any step executes.
    """
    if repo_root is None:
        return []

    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path
    )
    raw = _read_yaml_mapping(config_path)

    merge_section = raw.get(CONFIG_SECTION_MERGE)
    if merge_section is None:
        return []
    if not isinstance(merge_section, dict):
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_SECTION_MERGE!r} section must be a mapping, "
            f"got {type(merge_section).__name__}."
        )

    steps = merge_section.get(CONFIG_KEY_POST_MERGE_STEPS)
    if steps is None:
        return []

    validate_post_merge_steps(steps)
    return steps


def resolve_git_tree_relative_config_paths(
    config_root_hint: str | Path,
    git_tree_path: str | Path,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
    legacy_relative_path: str = LEGACY_CONFIG_RELATIVE_PATH,
) -> tuple[str | None, str | None]:
    """Resolve `load_post_merge_steps_from_git_sha`'s `config_relative_path`/
    `legacy_relative_path` arguments so they name the SAME config file
    `load_post_merge_steps(config_root_hint)` (the pre-sync read) resolved --
    expressed relative to *git_tree_path* instead of the config root (lr-cd3644
    fold-in #3 -- see `merge.verb`'s module docstring, "CONFIG-ROOT VS
    GIT-TREE-ROOT", and `resolve_git_working_tree`'s own docstring, for the
    wrapper-layout split this closes).

    THE BUG THIS CLOSES: `load_post_merge_steps_from_git_sha` previously ran
    `git show <sha>:<config_relative_path>` with `cwd=git_tree_path` UNCHANGED
    from its caller-facing default (`DEFAULT_CONFIG_RELATIVE_PATH`, i.e.
    `.clagentic/loadout/config.yaml`) -- correct ONLY when *git_tree_path*
    equals the config root, which the flat-layout case satisfies but the
    wrapper-layout case (a `merge.git_working_tree` key declared, tree_sync's
    target a SUBDIRECTORY of the config root) does not: in that split, the
    committed config lives at `<config_root>/.clagentic/loadout/config.yaml`,
    which is OUTSIDE the inner git tree at `<config_root>/<git_working_tree>`
    entirely (a *different* repository, never tracked by IT at that relative
    path) -- so `git show` there always reported "path absent," permanently
    disabling the lr-cd3644 drift check for every wrapper-layout repo that
    declares this knob, never comparable, regardless of any actual drift.

    THE FIX: resolve the config-bearing ROOT via the SAME
    `resolve_repo_config_root` bounded-hop walk `resolve_repo_config_path`
    (and therefore `load_post_merge_steps`) already uses for
    *config_root_hint*, then re-express each of that root's two candidate
    config paths (new, legacy) RELATIVE TO *git_tree_path* -- so a caller
    passing the RESULT into `load_post_merge_steps_from_git_sha` runs `git
    show <sha>:<path>` against a path that correctly resolves to the SAME
    file the pre-sync read used, from *git_tree_path*'s own perspective as
    the `git show` subprocess's cwd.

    Returns a `(config_relative, legacy_relative)` pair, each either a POSIX-
    style relative-path string (forward slashes, matching git's own path
    convention regardless of host OS) or `None` when that candidate's
    resolved absolute path does NOT lie within *git_tree_path* -- i.e. the
    flat-layout case where the config root and the git tree coincide always
    yields two non-None values (unchanged from before this fix, since the
    relative path is then unchanged); the wrapper-layout case where the
    config root sits ABOVE the git tree yields `(None, None)` for both
    candidates, correctly signaling "not trackable by this git tree, ever" --
    `load_post_merge_steps_from_git_sha`'s caller (`merge.verb._run`) must
    skip the git-object read entirely rather than construct an outside-the-
    repository `git show`/`git cat-file` argument, which `git` itself refuses
    with a DIFFERENT, genuine error (an "outside repository" failure, not a
    path-absent result) that this module's own fail-loud contract (see
    `load_post_merge_steps_from_git_sha`'s `_show` helper, which now verifies
    path presence via `git cat-file -e <sha>:<path>` before ever calling `git
    show`) would otherwise (correctly) raise on -- this function exists
    precisely so that call is never attempted for an inherently-untrackable
    wrapper-layout config.
    """
    config_root = resolve_repo_config_root(
        config_root_hint, config_relative_path, legacy_relative_path
    ).resolve()
    git_tree_resolved = Path(git_tree_path).resolve()

    def _relative_or_none(relative_path: str) -> str | None:
        candidate = (config_root / relative_path).resolve()
        if candidate != git_tree_resolved and git_tree_resolved not in candidate.parents:
            # Correct direction check for THIS use (contrast
            # resolve_git_working_tree's own escape check, which asks
            # whether a DECLARED subdirectory stays within its config root):
            # here we ask whether the config file sits AT OR BENEATH the git
            # tree -- git show can only ever resolve a path inside the
            # repository `git_tree_path` names.
            return None
        return candidate.relative_to(git_tree_resolved).as_posix()

    return (
        _relative_or_none(config_relative_path),
        _relative_or_none(legacy_relative_path),
    )


def load_post_merge_steps_from_git_sha(
    git_tree_path: str | Path,
    merged_sha: str,
    *,
    config_relative_path: str | None = DEFAULT_CONFIG_RELATIVE_PATH,
    legacy_relative_path: str | None = LEGACY_CONFIG_RELATIVE_PATH,
) -> list[dict] | None:
    """Resolve the post_merge_steps list AS COMMITTED at *merged_sha* --
    read directly from the git object database via `git show
    <merged_sha>:<path>`, never from `git_tree_path`'s WORKING TREE (lr-cd3644
    -- see `merge.verb`'s own module docstring, "STALE PRE-SYNC CONFIG DRIFT
    CHECK", for the call site this exists to correct).

    RETURNS `None`, NOT `[]`, WHEN THE CONFIG PATH IS ABSENT FROM
    *merged_sha*'S TREE ENTIRELY -- this is the load-bearing distinction this
    function makes that `load_post_merge_steps` does not need to (that
    function's caller always wants "no steps configured" collapsed to a
    single `[]`; THIS function's caller, the lr-cd3644 drift check, must be
    able to tell "the merged commit's tree has no config file at all" apart
    from "the merged commit's tree HAS a config file, and it declares zero
    steps"). The reason: `.clagentic/loadout/config.yaml` is, BY DESIGN,
    sometimes GITIGNORED and never committed at all (this repo's own
    `.gitignore` does exactly this for its OWN dogfooding config -- see that
    file's comment) -- a perfectly valid, common deployment shape where the
    config lives purely on disk, is never touched by `git checkout`, and
    therefore CANNOT drift relative to the merged SHA by construction (a
    `git checkout --detach` never modifies an untracked file). Returning `[]`
    for "absent from git" in that shape would make every ordinary merge of
    an untracked-config repo look like `steps` "disagrees" with a fabricated
    empty list, even though the ACTUAL config the caller correctly resolved
    from disk is perfectly current. Only a repo that DOES commit this file
    (the observed incident's own repo, clagentic-triage — confirmed by its
    `git diff --stat <old-sha> <new-sha> -- .clagentic/loadout/config.yaml`
    showing a real diff between two commits, i.e. the file IS tracked there)
    can ever produce a genuine, comparable git-object reading here; the
    caller (see merge.verb._run) treats a `None` return as "not comparable,
    skip the check" and a `list` return (even an empty one) as "comparable,
    diff it against the pre-sync `load_post_merge_steps` resolution."

    THE FIX: this function reads the SAME config file `load_post_merge_steps`
    reads (new path, falling back to the legacy path -- identical resolution
    order), but pulls its CONTENT from the git object database at
    *merged_sha* specifically, via `git show <merged_sha>:<relative_path>`,
    never from whatever the working tree happens to have checked out. This
    needs no checkout of its own -- `git show` reads a blob directly -- so
    calling this is safe even on the lr-173768 no-checkout (fetch-only) path,
    where `git_tree_path`'s working tree is deliberately left untouched.

    Resolution order (mirrors `resolve_repo_config_path`'s own new-path/
    legacy-path preference, but keyed on git-tree-object presence at
    *merged_sha* rather than filesystem presence):
      1. `git show <merged_sha>:<config_relative_path>` -- if this exits 0,
         its stdout is parsed as the config file's content. Skipped
         entirely when *config_relative_path* is `None`.
      2. Otherwise, `git show <merged_sha>:<legacy_relative_path>` -- if THIS
         exits 0, its stdout is parsed instead (no deprecation warning here;
         this is a read-only drift check, not a caller-facing config load).
         Skipped entirely when *legacy_relative_path* is `None`.
      3. Neither path exists in *merged_sha*'s tree (or both arguments were
         `None`): returns `None` (see above -- distinct from an
         empty-but-present `post_merge_steps: []`).

    THIS FUNCTION ITSELF performs NO wrapper-hop discovery -- it takes
    *config_relative_path*/*legacy_relative_path* as already-resolved,
    *git_tree_path*-relative paths (or `None`, meaning "not reachable from
    this tree at all") and looks inside exactly one tree object at
    *merged_sha*, nothing more. A caller whose config root and git tree
    genuinely coincide (the common, flat-layout case) may pass this
    function's own defaults unchanged, exactly as before this contract
    changed. A caller in the lr-93d718 wrapper-layout split (config root ABOVE
    the git tree tree_sync actually targets) MUST instead pass the paths
    `resolve_git_tree_relative_config_paths` computes -- see that function's
    own docstring for why a caller-unaware default here previously
    mis-resolved to a path that could never be tracked by *git_tree_path* at
    all, silently disabling the drift check for every wrapper-layout repo
    (lr-cd3644 fold-in #3).

    THE lr-cd3644 fold-in #3 STRUCTURAL FIX (LOCALE-INDEPENDENT, no stderr-text
    matching): a prior revision of this function classified a `git show`
    failure as "genuinely path-absent" vs. "a real git failure" by matching
    `git`'s stderr against a regex of its two documented English phrasings
    ("path '<p>' does not exist in '<ref>'" / "path '<p>' exists on disk, but
    not in '<ref>'"). That collapsed TWO distinct root causes onto the same
    stderr-matching code path (a genuinely absent path at a valid commit, and
    an invalid/unresolvable *merged_sha* itself -- git's own phrasing for an
    unresolvable-but-syntactically-SHA-shaped object name reuses the identical
    "path ... does not exist in" wording, so no regex on THAT output alone can
    ever tell the two apart) and was never locale-independent (git localizes
    this message under `LC_ALL`/`LANG`, so a non-English-locale spawn
    environment would silently fail every regex match and misclassify a
    genuine absence as a hard failure).

    NOW: *merged_sha* is verified ONCE, up front, via `git cat-file -e
    <merged_sha>^{commit}` -- a locale-independent existence+type check (exit
    code only, no stderr parsing) mirroring `tree_sync.fetch_merged_sha_object`
    's own verification readback. A non-zero exit HERE is unconditionally a
    genuine failure (an invalid, unresolvable, or non-commit *merged_sha*) and
    always raises -- this can never be "path absent," because no path lookup
    has been attempted yet. Only once the commit itself is confirmed to exist
    does path presence get tested, via `git cat-file -e
    <merged_sha>:<relative_path>` (again exit-code-only, no stderr text
    matched) -- a non-zero exit at THIS stage, with the commit already
    verified, unambiguously means the path is absent from that commit's tree,
    so it returns `None` for that candidate rather than raising. `git show` is
    then called only to fetch the CONTENT of a path already confirmed present.

    Raises:
        PostMergeConfigError: *merged_sha* itself fails the `git cat-file -e
            ...^{commit}` existence check (invalid, unresolvable, or
            non-commit object -- always a genuine failure, never treated as
            "path absent"), a confirmed-present path's `git show` content read
            fails for any other reason, the resolved blob content is not
            valid YAML, its top-level document is not a mapping, the `merge:`
            section (if present) is not a mapping, `post_merge_steps` (if
            present) is not a list, or any individual step fails
            `validate_post_merge_steps` -- mirrors `load_post_merge_steps`'s
            own fail-fast contract exactly, so a malformed committed config is
            caught the same way regardless of which path read it.
    """
    git_tree_path = Path(git_tree_path)

    # Verify *merged_sha* ITSELF exists and is a commit, exactly once, before
    # any path lookup is attempted -- see docstring above. A failure here can
    # never be reinterpreted as "path absent": no path has been looked up yet.
    commit_check = subprocess.run(
        ["git", "cat-file", "-e", f"{merged_sha}^{{commit}}"],
        capture_output=True,
        text=True,
        cwd=str(git_tree_path),
    )
    if commit_check.returncode != 0:
        raise PostMergeConfigError(
            f"git cat-file -e {merged_sha}^{{commit}} failed (exit "
            f"{commit_check.returncode}) in {git_tree_path} -- {merged_sha!r} "
            f"does not resolve to a commit object in this tree's object "
            f"database: {commit_check.stderr.strip()[:400]}"
        )

    def _show(relative_path: str) -> str | None:
        # *merged_sha* is already confirmed to exist and be a commit (above)
        # -- a non-zero exit HERE unambiguously means *relative_path* is
        # absent from ITS tree specifically, never a bad-SHA artifact. Tested
        # via `cat-file -e` (exit code only) rather than parsing `git show`'s
        # own stderr text, so this is locale-independent regardless of
        # LC_ALL/LANG.
        path_check = subprocess.run(
            ["git", "cat-file", "-e", f"{merged_sha}:{relative_path}"],
            capture_output=True,
            text=True,
            cwd=str(git_tree_path),
        )
        if path_check.returncode != 0:
            return None

        result = subprocess.run(
            ["git", "show", f"{merged_sha}:{relative_path}"],
            capture_output=True,
            text=True,
            cwd=str(git_tree_path),
        )
        if result.returncode != 0:
            # The path was JUST confirmed present at a JUST-verified commit --
            # a `git show` failure now is some OTHER genuine error (e.g. a
            # corrupted object database), never a path-absence result this
            # function's own `None` contract is reserved for. Fail loud.
            raise PostMergeConfigError(
                f"git show {merged_sha}:{relative_path} failed (exit "
                f"{result.returncode}) in {git_tree_path} after "
                f"cat-file -e confirmed the path present at that commit: "
                f"{result.stderr.strip()[:400]}"
            )
        return result.stdout

    content: str | None = None
    source_label = f"{merged_sha}:{config_relative_path}"
    if config_relative_path is not None:
        content = _show(config_relative_path)
    if content is None and legacy_relative_path is not None:
        content = _show(legacy_relative_path)
        source_label = f"{merged_sha}:{legacy_relative_path}"
    if content is None:
        return None

    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise PostMergeConfigError(f"{source_label}: could not be read as YAML: {exc}.") from exc
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise PostMergeConfigError(
            f"{source_label}: top-level document must be a mapping, got "
            f"{type(raw).__name__}."
        )

    merge_section = raw.get(CONFIG_SECTION_MERGE)
    if merge_section is None:
        return []
    if not isinstance(merge_section, dict):
        raise PostMergeConfigError(
            f"{source_label}: {CONFIG_SECTION_MERGE!r} section must be a "
            f"mapping, got {type(merge_section).__name__}."
        )

    steps = merge_section.get(CONFIG_KEY_POST_MERGE_STEPS)
    if steps is None:
        return []

    validate_post_merge_steps(steps)
    return steps


def config_path_tracked_at_git_sha(
    git_tree_path: str | Path,
    sha: str,
    *,
    config_relative_path: str | None = DEFAULT_CONFIG_RELATIVE_PATH,
    legacy_relative_path: str | None = LEGACY_CONFIG_RELATIVE_PATH,
) -> bool:
    """Whether the repo-local config file (new or legacy relative path) was
    GIT-TRACKED at *sha* -- i.e. `git cat-file -e <sha>:<relative_path>`
    succeeds for either candidate (lr-cd3644 fold-in #3, PR #30 re-review
    finding B).

    THE DISTINCTION THIS CLOSES: `load_post_merge_steps_from_git_sha`
    returning `None` for "config path absent at *merged_sha*" is ambiguous
    between two different root causes a caller (`merge.verb._run`'s drift
    check) must treat DIFFERENTLY:

      1. The config file was NEVER tracked at any commit reachable here (the
         common, deliberately-gitignored-config shape -- see that function's
         own docstring). Genuinely NOT COMPARABLE: an untracked file cannot
         drift relative to a commit by construction, so the drift check must
         skip it entirely.
      2. The config file WAS tracked at the caller's PRE-SYNC HEAD (the
         commit `--repo-path`'s working tree was on before this merge), but
         the merged commit's own tree no longer tracks it (deleted in, or
         before, the commit that merged). This is NOT "not comparable" --
         the merged commit is DELETING every post_merge_steps entry that
         pre-sync tree declared, which is itself a real, authoritative
         result: zero steps, not an unresolvable comparison. Treating case 2
         the same as case 1 (skip the check, keep the stale pre-sync
         `steps` value) would silently keep running steps a merged commit
         has already deleted -- the merged commit's own tree is always
         authoritative once it is known to be a comparable, tracked config
         surface at all.

    A caller passes *sha* as the PRE-SYNC HEAD (the commit `--repo-path` was
    on before `advance_repo_to_merged_sha`/`fetch_merged_sha_object` ran) to
    distinguish these two cases: `load_post_merge_steps_from_git_sha`
    returning `None` for *merged_sha* AND this function returning `True` for
    the pre-sync HEAD means case 2 (deleted-but-was-tracked, merged commit
    authoritative, treat as `[]`); `None` and `False` means case 1 (never
    tracked, genuinely not comparable, skip).

    Tested via `git cat-file -e <sha>:<relative_path>` (exit-code only, no
    stderr text parsed -- locale-independent, mirroring
    `load_post_merge_steps_from_git_sha`'s own path-presence check). Checks
    *config_relative_path* first, falling back to *legacy_relative_path* when
    the new path is not tracked there either -- same new-path/legacy-path
    preference order every other resolver in this module uses. A `None`
    argument skips that candidate entirely (mirrors
    `load_post_merge_steps_from_git_sha`'s own contract for a wrapper-layout
    config root that sits outside the git tree, lr-cd3644 fold-in #3 --
    `resolve_git_tree_relative_config_paths` may resolve either candidate to
    `None`).

    Returns `False` (never raises) when *sha* itself cannot be resolved to a
    commit -- this function's job is narrowly "was the config path tracked at
    a resolvable commit," not a second, redundant *sha*-validity gate;
    `load_post_merge_steps_from_git_sha`'s own up-front `git cat-file -e
    <merged_sha>^{commit}` check already owns failing loud on an invalid
    *merged_sha* specifically. A caller with a genuinely invalid *sha* here
    (e.g. a pre-sync HEAD from a corrupted local state) gets `False` --
    "not tracked, not comparable" -- which is the same safe, conservative
    answer an untracked config gets, never a silent authority claim over
    content that could not actually be read.
    """
    git_tree_path = Path(git_tree_path)

    def _tracked(relative_path: str) -> bool:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}:{relative_path}"],
            capture_output=True,
            text=True,
            cwd=str(git_tree_path),
        )
        return result.returncode == 0

    if config_relative_path is not None and _tracked(config_relative_path):
        return True
    if legacy_relative_path is not None and _tracked(legacy_relative_path):
        return True
    return False


def post_merge_steps_key_declared(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> bool:
    """Whether the repo's own live config EXPLICITLY names the
    `post_merge_steps` key inside its `merge:` section (lr-f9a01b followup
    — see `merge.verb`'s module docstring, "DEAD .crew/<role>.yaml
    post_merge_steps CROSS-CHECK", for the caller this exists for).

    `load_post_merge_steps` returns `[]` for BOTH "this repo configured no
    steps" (a key present as `post_merge_steps: []`, or a `merge:` section
    that omits the key while still wanting other `merge:` keys honored) AND
    "this repo's steps might be declared somewhere this loader does not
    read" (the key absent everywhere) — a caller that only has `load_post_
    merge_steps`'s own return value cannot tell those apart, which is
    correct for every call site `load_post_merge_steps` has served UNTIL
    NOW (`merge.verb`'s step 10, `merge.post_merge_verb`, `doctor.checks`'
    `check_repo_loadout_schema` all only ever needed "will steps run,
    yes/no" — see that function's own docstring). A cross-check that warns
    when a DIFFERENT config surface (`.crew/<role>.yaml`) mentions
    `post_merge_steps` needs the finer distinction: a repo that explicitly
    wrote `post_merge_steps: []` in its OWN live config has made an
    informed choice at the CORRECT file and should never be warned about a
    stale mention elsewhere, whereas a repo whose live config never
    mentions the key at all is exactly the silent-no-op shape worth
    flagging.

    Returns `True` only when the resolved config file exists, its `merge:`
    section exists and is a mapping, and that mapping contains a
    `post_merge_steps` key (whatever its value — an empty list still
    counts as "declared here"). Returns `False` when *repo_root* is None,
    the config file is absent, the `merge:` section is absent, or the
    section exists but never mentions the key.

    Never raises `PostMergeConfigError` on a malformed `post_merge_steps`
    value (unlike `load_post_merge_steps`) — this function only asks
    whether the key is PRESENT, not whether its value validates; the
    caller who needs the malformed-config failure already gets it from its
    own `load_post_merge_steps` call. Does raise on a malformed `merge:`
    section (not a mapping) or unreadable/invalid YAML, mirroring every
    other resolver in this module's fail-fast contract for those two
    failure modes specifically.
    """
    if repo_root is None:
        return False

    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path
    )
    raw = _read_yaml_mapping(config_path)

    merge_section = raw.get(CONFIG_SECTION_MERGE)
    if merge_section is None:
        return False
    if not isinstance(merge_section, dict):
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_SECTION_MERGE!r} section must be a mapping, "
            f"got {type(merge_section).__name__}."
        )

    return CONFIG_KEY_POST_MERGE_STEPS in merge_section


#: Directory name (repo-root-relative) holding per-role dispatch config
#: files (one `<role>.yaml` file per role) -- a DIFFERENT config surface
#: than this module's own repo-local config file. `load_post_merge_steps`
#: has never read from here (see its own docstring) -- this constant/the
#: scan function below exist ONLY to detect a `post_merge_steps` mention
#: declared in one of these files, which silently never runs (lr-f9a01b).
CREW_CONFIG_DIR_NAME = ".crew"


def find_crew_yaml_files_declaring_post_merge_steps(
    repo_root: str | Path | None,
) -> list[str]:
    """Return the sorted, stringified paths of every `<repo_root>/.crew/
    *.yaml` file that mentions a `post_merge_steps` key -- either at that
    file's own top level, or nested one level under a `merge:` section (the
    shape a repo author who has seen this module's own `merge:`-section
    convention might reasonably also try there, even though neither shape
    is ever read from `.crew/*.yaml`) (lr-f9a01b).

    SHARED SCAN LOGIC, ONE OWNER: both `doctor.checks.
    check_dead_crew_post_merge_config` (the diagnostic, `--repo-root`-gated
    doctor finding) and `merge.verb._run`'s step-10 warning (the same
    cross-check surfaced loudly at the point a merge actually happens,
    added because a doctor-only check only fires when someone thinks to
    run doctor -- see that module's docstring, "DEAD .crew/<role>.yaml
    post_merge_steps CROSS-CHECK") call this SAME function, so the two
    surfaces can never diverge on what counts as an offending file.

    Returns `[]` when *repo_root* is None, there is no `.crew/` directory,
    or no `.crew/*.yaml` file mentions the key anywhere -- the common case,
    nothing to warn about. A `.crew/*.yaml` file that is unreadable or
    malformed YAML, or whose top-level document is not a mapping, is
    silently skipped (never raised) -- this is a read-only scan of a
    config surface this module does not own the schema for; a malformed
    `.crew/*.yaml` file is a conformance concern for whatever schema DOES
    own that file's shape, not something this function should fail over.

    Read-only: never parses/returns `post_merge_steps`' own VALUE (the
    step list itself) -- only whether the KEY is present and which file
    declared it. A `.crew/*.yaml` mention is never treated as a source of
    steps to execute; `.crew/*.yaml` was not, and is not by this function,
    part of any executable step-loading path (see `load_post_merge_steps`'
    own docstring).
    """
    if repo_root is None:
        return []

    repo_root_path = Path(repo_root)
    crew_dir = repo_root_path / CREW_CONFIG_DIR_NAME
    if not crew_dir.is_dir():
        return []

    offending_files: list[str] = []
    for crew_config_path in sorted(crew_dir.glob("*.yaml")):
        try:
            raw = yaml.safe_load(crew_config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(raw, dict):
            continue
        declares_top_level = CONFIG_KEY_POST_MERGE_STEPS in raw
        merge_section = raw.get(CONFIG_SECTION_MERGE)
        declares_nested = (
            isinstance(merge_section, dict) and CONFIG_KEY_POST_MERGE_STEPS in merge_section
        )
        if declares_top_level or declares_nested:
            offending_files.append(str(crew_config_path))

    return offending_files


def resolve_git_working_tree(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
    legacy_relative_path: str = LEGACY_CONFIG_RELATIVE_PATH,
) -> Path | None:
    """Resolve the git working-tree root `merge.tree_sync.
    advance_repo_to_merged_sha` should target for *repo_root* (lr-93d718 —
    see module docstring, "CONFIG-ROOT VS GIT-TREE-ROOT").

    Reads the SAME `<repo_root>/<config_relative_path>` file/section
    `load_post_merge_steps` already reads (no second YAML-reading path) for an
    OPTIONAL `git_working_tree` key inside the `merge:` section: a path
    relative to the resolved config root (e.g. `"repo"`). By contract this
    MUST be a relative subpath that stays WITHIN the config root — never an
    absolute path, never a `..`-escape above it (lr-93d718 path-containment
    hardening): both are rejected below before this function returns.

    Returns:
      - `None` when *repo_root* is None, the config file is absent, the
        `merge:` section is absent, or `git_working_tree` is absent within
        it — the caller's contract in every one of these cases is "target
        `--repo-path` itself, unchanged from before this task."
      - `<config_root>/<git_working_tree>` (resolved via the SAME
        `resolve_repo_config_path`-driven root `load_post_merge_steps` uses,
        so a wrapper-hop repo's config root and its declared working-tree
        path are always resolved relative to the SAME root) when the key is
        present and resolves to a path within the config root.

    Raises:
        PostMergeConfigError: the config file exists but is unreadable/
            malformed YAML, the `merge:` section is present but not a
            mapping, `git_working_tree` is present but not a non-empty
            string, `git_working_tree` is an absolute path, or the resolved
            path escapes the config root (a `..` component climbing above
            it) — always at LOAD time, mirroring `load_post_merge_steps`'s
            own fail-fast contract. The escape/absolute-path check is
            defense-in-depth: the same actor already has commit access to
            this trusted repo-local config, which already grants arbitrary
            argv via `post_merge_steps` in this same file — but it is a
            cheap, correct hardening on code this knob just introduced.
    """
    if repo_root is None:
        return None

    config_path = resolve_repo_config_path(
        repo_root,
        config_relative_path=config_relative_path,
        legacy_relative_path=legacy_relative_path,
    )
    raw = _read_yaml_mapping(config_path)

    merge_section = raw.get(CONFIG_SECTION_MERGE)
    if merge_section is None:
        return None
    if not isinstance(merge_section, dict):
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_SECTION_MERGE!r} section must be a mapping, "
            f"got {type(merge_section).__name__}."
        )

    working_tree = merge_section.get(CONFIG_KEY_GIT_WORKING_TREE)
    if working_tree is None:
        return None
    if not isinstance(working_tree, str) or not working_tree.strip():
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_KEY_GIT_WORKING_TREE!r} must be a "
            f"non-empty string (a path relative to the config root), got "
            f"{working_tree!r}."
        )
    working_tree = working_tree.strip()
    if Path(working_tree).is_absolute():
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_KEY_GIT_WORKING_TREE!r} must be a path "
            f"RELATIVE to the config root, got an absolute path {working_tree!r}."
        )

    # Re-derive the config ROOT (as opposed to config_path, the resolved
    # FILE) via the SAME resolve_repo_config_root bounded-hop resolver
    # resolve_repo_config_path itself calls internally, using the SAME
    # (config_relative_path, legacy_relative_path) candidate pair this
    # function's OWN resolve_repo_config_path call above just used (lr-cd3644
    # fold-in #3, PR #30 re-review finding D) -- this function previously
    # hardcoded the module-level LEGACY_CONFIG_RELATIVE_PATH constant
    # directly here instead of threading its own *legacy_relative_path*
    # parameter through, which only happened to agree with the
    # resolve_repo_config_path call above because no caller today overrides
    # the legacy path from its own default. Any caller that DID override
    # *legacy_relative_path* (mirroring the override knob every other
    # resolver in this module already exposes) would have hit a config-root
    # resolution here that silently used a DIFFERENT legacy candidate than
    # the config-file resolution immediately above it -- a latent
    # inconsistency, now closed by passing the SAME parameter through both
    # calls, so a wrapper-hop repo's config root here always agrees with
    # load_post_merge_steps' own root for the same *repo_root*/legacy-path
    # input, never a second/divergent walk.
    config_root = resolve_repo_config_root(
        repo_root, config_relative_path, legacy_relative_path
    )
    resolved = (config_root / working_tree).resolve()
    config_root_resolved = config_root.resolve()
    if resolved != config_root_resolved and config_root_resolved not in resolved.parents:
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_KEY_GIT_WORKING_TREE!r} value "
            f"{working_tree!r} resolves to {resolved} which escapes the "
            f"config root {config_root_resolved} — refusing a value that "
            f"would point tree_sync's git subprocess outside the config "
            f"root it was declared relative to."
        )
    return config_root / working_tree


def resolve_sync_tree_after_merge(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> bool:
    """Resolve whether `merge.verb._run` should advance `--repo-path` to the
    merged main tip at all for this repo (lr-d95cdb — see module docstring,
    "SYNC_TREE_AFTER_MERGE").

    Reads the SAME `<repo_root>/<config_relative_path>` file/section
    `load_post_merge_steps` / `resolve_git_working_tree` already read (no
    second YAML-reading path) for an OPTIONAL `sync_tree_after_merge` key
    inside the `merge:` section.

    Returns `DEFAULT_SYNC_TREE_AFTER_MERGE` (`True`) when *repo_root* is
    None, the config file is absent, the `merge:` section is absent, or the
    key is absent within it -- replace-not-merge, matching
    `merge.gate_config`'s own convention: a repo that never mentions this key
    gets the built-in default, never a silent off.

    Raises:
        PostMergeConfigError: the config file exists but is unreadable/
            malformed YAML, the `merge:` section is present but not a
            mapping, or `sync_tree_after_merge` is present but not a bool --
            always at LOAD time, mirroring this module's other loaders' own
            fail-fast contract.
    """
    if repo_root is None:
        return DEFAULT_SYNC_TREE_AFTER_MERGE

    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path
    )
    raw = _read_yaml_mapping(config_path)

    merge_section = raw.get(CONFIG_SECTION_MERGE)
    if merge_section is None:
        return DEFAULT_SYNC_TREE_AFTER_MERGE
    if not isinstance(merge_section, dict):
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_SECTION_MERGE!r} section must be a mapping, "
            f"got {type(merge_section).__name__}."
        )

    value = merge_section.get(CONFIG_KEY_SYNC_TREE_AFTER_MERGE)
    if value is None:
        return DEFAULT_SYNC_TREE_AFTER_MERGE
    if not isinstance(value, bool):
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_KEY_SYNC_TREE_AFTER_MERGE!r} must be a "
            f"bool, got {type(value).__name__}."
        )
    return value


def resolve_enforce_merge_shape(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> bool:
    """Resolve whether `merge.verb._run` should treat a detected
    requested-vs-actual merge-shape mismatch (`merge.merge_shape.
    check_merge_shape`) as a hard failure for this repo (lr-14f704 item 3 —
    see `merge.merge_shape`'s own docstring, "WARN-BY-DEFAULT, CONFIG-GATED
    STRICTNESS", for the full trade-off this key resolves).

    Reads the SAME `<repo_root>/<config_relative_path>` file/section
    `load_post_merge_steps` / `resolve_sync_tree_after_merge` already read
    (no second YAML-reading path) for an OPTIONAL `enforce_merge_shape` key
    inside the `merge:` section.

    Returns `DEFAULT_ENFORCE_MERGE_SHAPE` (`False`, warn-only) when
    *repo_root* is None, the config file is absent, the `merge:` section is
    absent, or the key is absent within it — replace-not-merge, matching
    `resolve_sync_tree_after_merge`'s own convention: a repo that never
    mentions this key gets the built-in (permissive) default, never a
    silent strict-by-surprise switch.

    Raises:
        PostMergeConfigError: the config file exists but is unreadable/
            malformed YAML, the `merge:` section is present but not a
            mapping, or `enforce_merge_shape` is present but not a bool —
            always at LOAD time, mirroring this module's other loaders' own
            fail-fast contract.
    """
    if repo_root is None:
        return DEFAULT_ENFORCE_MERGE_SHAPE

    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path
    )
    raw = _read_yaml_mapping(config_path)

    merge_section = raw.get(CONFIG_SECTION_MERGE)
    if merge_section is None:
        return DEFAULT_ENFORCE_MERGE_SHAPE
    if not isinstance(merge_section, dict):
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_SECTION_MERGE!r} section must be a mapping, "
            f"got {type(merge_section).__name__}."
        )

    value = merge_section.get(CONFIG_KEY_ENFORCE_MERGE_SHAPE)
    if value is None:
        return DEFAULT_ENFORCE_MERGE_SHAPE
    if not isinstance(value, bool):
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_KEY_ENFORCE_MERGE_SHAPE!r} must be a "
            f"bool, got {type(value).__name__}."
        )
    return value


def resolve_enforce_single_verdict_fence(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> bool:
    """Resolve whether `merge.verb._run`'s reviewer-verdict step should treat
    a comment body carrying MORE THAN ONE fenced ```review-result``` block as
    a hard gate refusal for this repo (lr-5260f9 -- see this module's own
    `CONFIG_KEY_ENFORCE_SINGLE_VERDICT_FENCE`, "ENFORCE-BY-DEFAULT,
    CONFIG-GATED OPT-OUT", for the full trade-off this key resolves, why it
    is the inverse shape of `resolve_enforce_merge_shape`, and why it is
    homed here rather than merge.gate_config).

    Reads the SAME `<repo_root>/<config_relative_path>` file/section
    `resolve_enforce_merge_shape` / `load_post_merge_steps` already read (no
    second YAML-reading path) for an OPTIONAL `enforce_single_verdict_fence`
    key inside the `merge:` section.

    Returns `DEFAULT_ENFORCE_SINGLE_VERDICT_FENCE` (`True`, hard refusal on
    a multi-fence body) when *repo_root* is None, the config file is
    absent, the `merge:` section is absent, or the key is absent within it
    -- a repo that never mentions this key gets the built-in SAFE default,
    not the permissive one. A repo sets this key to `false` explicitly to
    opt OUT (fall back to `merge.verdict.read_reviewer_verdict`'s
    pre-existing, unconditional last-fence-wins parse) for legacy
    multi-fence comments it cannot immediately clean up. Otherwise mirrors
    `resolve_enforce_merge_shape`'s own replace-not-merge convention
    exactly -- only the DEFAULT direction differs, not the resolution
    mechanics.

    Raises:
        PostMergeConfigError: the config file exists but is unreadable/
            malformed YAML, the `merge:` section is present but not a
            mapping, or `enforce_single_verdict_fence` is present but not a
            bool -- always at LOAD time, mirroring this module's other
            loaders' own fail-fast contract.
    """
    if repo_root is None:
        return DEFAULT_ENFORCE_SINGLE_VERDICT_FENCE

    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path
    )
    raw = _read_yaml_mapping(config_path)

    merge_section = raw.get(CONFIG_SECTION_MERGE)
    if merge_section is None:
        return DEFAULT_ENFORCE_SINGLE_VERDICT_FENCE
    if not isinstance(merge_section, dict):
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_SECTION_MERGE!r} section must be a mapping, "
            f"got {type(merge_section).__name__}."
        )

    value = merge_section.get(CONFIG_KEY_ENFORCE_SINGLE_VERDICT_FENCE)
    if value is None:
        return DEFAULT_ENFORCE_SINGLE_VERDICT_FENCE
    if not isinstance(value, bool):
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_KEY_ENFORCE_SINGLE_VERDICT_FENCE!r} must "
            f"be a bool, got {type(value).__name__}."
        )
    return value


def resolve_require_model_attestation(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> bool:
    """Resolve whether `merge.verb._run`'s reviewer-verdict step should
    additionally require a genuine `model_attested` declaration on every
    required reviewer's `clean` verdict for this repo (lr-95543d -- see
    this module's own `CONFIG_KEY_REQUIRE_MODEL_ATTESTATION`, and
    `merge.model_attestation`'s module docstring, for the full trust model
    and the OPT-IN/rollout-hazard rationale for this key's default).

    Reads the SAME `<repo_root>/<config_relative_path>` file/section every
    other loader in this module reads (no second YAML-reading path) for an
    OPTIONAL `require_model_attestation` key inside the `merge:` section.

    Returns `DEFAULT_REQUIRE_MODEL_ATTESTATION` (`False`, no-op) when
    *repo_root* is None, the config file is absent, the `merge:` section is
    absent, or the key is absent within it -- replace-not-merge, matching
    `resolve_enforce_merge_shape`'s own convention: a repo that never
    mentions this key gets the permissive default, never a silent
    strict-by-surprise switch that could refuse every pre-existing clean
    verdict from a reviewer producer that has not adopted the field yet.

    Raises:
        PostMergeConfigError: the config file exists but is unreadable/
            malformed YAML, the `merge:` section is present but not a
            mapping, or `require_model_attestation` is present but not a
            bool -- always at LOAD time, mirroring this module's other
            loaders' own fail-fast contract.
    """
    if repo_root is None:
        return DEFAULT_REQUIRE_MODEL_ATTESTATION

    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path
    )
    raw = _read_yaml_mapping(config_path)

    merge_section = raw.get(CONFIG_SECTION_MERGE)
    if merge_section is None:
        return DEFAULT_REQUIRE_MODEL_ATTESTATION
    if not isinstance(merge_section, dict):
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_SECTION_MERGE!r} section must be a mapping, "
            f"got {type(merge_section).__name__}."
        )

    value = merge_section.get(CONFIG_KEY_REQUIRE_MODEL_ATTESTATION)
    if value is None:
        return DEFAULT_REQUIRE_MODEL_ATTESTATION
    if not isinstance(value, bool):
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_KEY_REQUIRE_MODEL_ATTESTATION!r} must "
            f"be a bool, got {type(value).__name__}."
        )
    return value


def resolve_model_attestation_denylist(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> frozenset[str]:
    """Resolve the OPTIONAL deployment-specific denylist term set for
    `merge.model_attestation.assert_model_attested` (lr-95543d -- see this
    module's own `CONFIG_KEY_MODEL_ATTESTATION_DENYLIST` for the key
    contract).

    Reads the SAME `<repo_root>/<config_relative_path>` file/section every
    other loader in this module reads (no second YAML-reading path) for an
    OPTIONAL `model_attestation_denylist` key inside the `merge:` section:
    a list of non-empty strings.

    Returns `frozenset()` (no additional denylist terms -- ONLY the
    built-in bare-tier-alias/no-digit-shape check applies) when
    *repo_root* is None, the config file is absent, the `merge:` section is
    absent, or the key is absent within it.

    Raises:
        PostMergeConfigError: the config file exists but is unreadable/
            malformed YAML, the `merge:` section is present but not a
            mapping, `model_attestation_denylist` is present but not a
            list, or any entry is not a non-empty string -- always at LOAD
            time, mirroring this module's other loaders' own fail-fast
            contract.
    """
    if repo_root is None:
        return frozenset()

    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path
    )
    raw = _read_yaml_mapping(config_path)

    merge_section = raw.get(CONFIG_SECTION_MERGE)
    if merge_section is None:
        return frozenset()
    if not isinstance(merge_section, dict):
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_SECTION_MERGE!r} section must be a mapping, "
            f"got {type(merge_section).__name__}."
        )

    value = merge_section.get(CONFIG_KEY_MODEL_ATTESTATION_DENYLIST)
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_KEY_MODEL_ATTESTATION_DENYLIST!r} must "
            f"be a list of non-empty strings, got {value!r}."
        )
    return frozenset(value)


def resolve_post_merge_step_timeout_seconds(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> int | float | None:
    """Resolve the repo-wide `default_timeout_seconds` fallback
    `merge.post_merge.run_post_merge_steps` applies to any ORDINARY
    (non-detached) step that does not set its own `timeout_seconds`
    (lr-d6e52b — see module docstring, "POST_MERGE_STEP_TIMEOUT_SECONDS",
    for the full trade-off this key resolves).

    Reads the SAME `<repo_root>/<config_relative_path>` file/section every
    other loader in this module reads (no second YAML-reading path) for an
    OPTIONAL `post_merge_step_timeout_seconds` key inside the `merge:`
    section.

    Returns `DEFAULT_POST_MERGE_STEP_TIMEOUT_SECONDS` (`None`, no bound)
    when *repo_root* is None, the config file is absent, the `merge:`
    section is absent, or the key is absent within it — replace-not-merge,
    matching this module's other resolvers' own convention: a repo that
    never mentions this key gets the built-in (permissive) default, never a
    silent bound-by-surprise.

    Raises:
        PostMergeConfigError: the config file exists but is unreadable/
            malformed YAML, the `merge:` section is present but not a
            mapping, or `post_merge_step_timeout_seconds` is present but not
            a positive int/float — always at LOAD time, mirroring this
            module's other loaders' own fail-fast contract.
    """
    if repo_root is None:
        return DEFAULT_POST_MERGE_STEP_TIMEOUT_SECONDS

    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path
    )
    raw = _read_yaml_mapping(config_path)

    merge_section = raw.get(CONFIG_SECTION_MERGE)
    if merge_section is None:
        return DEFAULT_POST_MERGE_STEP_TIMEOUT_SECONDS
    if not isinstance(merge_section, dict):
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_SECTION_MERGE!r} section must be a mapping, "
            f"got {type(merge_section).__name__}."
        )

    value = merge_section.get(CONFIG_KEY_POST_MERGE_STEP_TIMEOUT_SECONDS)
    if value is None:
        return DEFAULT_POST_MERGE_STEP_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_KEY_POST_MERGE_STEP_TIMEOUT_SECONDS!r} "
            f"must be an int or float number of seconds, got "
            f"{type(value).__name__}."
        )
    if value <= 0:
        raise PostMergeConfigError(
            f"{config_path}: {CONFIG_KEY_POST_MERGE_STEP_TIMEOUT_SECONDS!r} "
            f"must be > 0, got {value!r}."
        )
    return value


def resolve_env_overrides(
    *,
    env: dict[str, str] | None = None,
    config_root: str | Path | None = None,
) -> dict[str, str]:
    """Resolve the deployment-owned env-override mapping for post_merge_steps
    (lr-52d7 — see module docstring for the full seam design and why this
    mirrors `transport.provider_config`'s user-level config-file tier).

    Merges two OPTIONAL, independent sources into one flat `{VAR: value}`
    mapping, env-var tier winning per-variable:

      1. Every `CLAGENTIC_LOADOUT_POST_MERGE_ENV_<NAME>` var present in *env*
         (default `os.environ`) — `<NAME>` must match `_ENV_OVERRIDE_VAR_RE`;
         a source var whose suffix does not form a valid bare identifier is
         skipped rather than raising (an env namespace can carry arbitrary
         other `CLAGENTIC_LOADOUT_POST_MERGE_ENV_`-prefixed noise this seam
         was never meant to interpret — fail-soft-skip here, not fail-closed,
         since this is an additive convenience tier, not a security boundary
         the way the credentials tier is).
      2. The user-level config file's `post_merge_env:` section (via
         `transport.provider_config.load_user_config_section` — same file,
         same loader as every other user-level config tier in this package).
         A non-string value for a key is coerced with `str()` (YAML happily
         parses `HOME: /root` as a string already; this only matters for an
         author who wrote e.g. a bare number) so every returned value is
         `str`, matching what `subprocess.run`'s `env=` mapping requires.

    Returns `{}` when neither source has anything to contribute — the
    all-callers-safe default: a repo/deployment that never opted into this
    seam sees `run_post_merge_steps` behave EXACTLY as before this feature
    (env=None passthrough when a step also has no inline VAR=VALUE prefix).

    Never raises: a malformed `post_merge_env:` section (not a mapping) is
    treated as empty rather than surfacing a config-load error — this tier
    is additive and must never block an otherwise-valid post_merge_steps run
    from starting.
    """
    active_env = env if env is not None else dict(os.environ)
    overrides: dict[str, str] = {}

    config_section = load_user_config_section(
        CONFIG_SECTION_POST_MERGE_ENV, config_root=config_root
    )
    for key, value in config_section.items():
        if _ENV_OVERRIDE_VAR_RE.match(key):
            overrides[key] = str(value)

    for source_name, value in active_env.items():
        if not source_name.startswith(ENV_OVERRIDE_PREFIX):
            continue
        var_name = source_name[len(ENV_OVERRIDE_PREFIX):]
        if _ENV_OVERRIDE_VAR_RE.match(var_name):
            overrides[var_name] = value

    return overrides


__all__ = [
    "CONFIG_KEY_ENFORCE_MERGE_SHAPE",
    "CONFIG_KEY_ENFORCE_SINGLE_VERDICT_FENCE",
    "CONFIG_KEY_GIT_WORKING_TREE",
    "CONFIG_KEY_MODEL_ATTESTATION_DENYLIST",
    "CONFIG_KEY_POST_MERGE_STEP_TIMEOUT_SECONDS",
    "CONFIG_KEY_POST_MERGE_STEPS",
    "CONFIG_KEY_REQUIRE_MODEL_ATTESTATION",
    "CONFIG_KEY_SYNC_TREE_AFTER_MERGE",
    "CONFIG_SECTION_MERGE",
    "CONFIG_SECTION_POST_MERGE_ENV",
    "CREW_CONFIG_DIR_NAME",
    "DEFAULT_CONFIG_RELATIVE_PATH",
    "DEFAULT_ENFORCE_MERGE_SHAPE",
    "DEFAULT_ENFORCE_SINGLE_VERDICT_FENCE",
    "DEFAULT_POST_MERGE_STEP_TIMEOUT_SECONDS",
    "DEFAULT_REQUIRE_MODEL_ATTESTATION",
    "DEFAULT_SYNC_TREE_AFTER_MERGE",
    "ENV_OVERRIDE_PREFIX",
    "config_path_tracked_at_git_sha",
    "find_crew_yaml_files_declaring_post_merge_steps",
    "load_post_merge_steps",
    "load_post_merge_steps_from_git_sha",
    "post_merge_steps_key_declared",
    "resolve_enforce_merge_shape",
    "resolve_enforce_single_verdict_fence",
    "resolve_env_overrides",
    "resolve_git_tree_relative_config_paths",
    "resolve_git_working_tree",
    "resolve_model_attestation_denylist",
    "resolve_post_merge_step_timeout_seconds",
    "resolve_require_model_attestation",
    "resolve_sync_tree_after_merge",
]
