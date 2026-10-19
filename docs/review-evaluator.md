# Review evaluator

Back to [README.md](README.md).

`loadout-review evaluate` turns a reviewer's findings into a computed verdict. The
reviewer states **facts** about each finding; code decides severity and blocking. Nothing
downstream re-judges the result.

Design rules this follows:

- The reviewer supplies facts only. Severity and blocking are computed.
- Policy that can lower a verdict is read only from the trusted base revision.
- Deduplication annotates; it never drops a blocking finding.
- A fact that is absent, invalid or `unknown` resolves to the worst case for its
  dimension, so omitting a fact never makes a finding look milder.

The subcommand is offline and read-only: it reads a findings file and a local repository,
makes no host call, posts nothing, and binds no caller identity. `run` and `post` use the
same evaluator (see [Use in `run` and `post`](#use-in-run-and-post)). `loadout-merge` runs it
again at merge time over the findings a fence carries, with policy read at the live base
(see [Computed verdicts at the gate](merge-authority.md#computed-verdicts-at-the-gate)).

## Facts vocabulary

A finding may carry a `facts` object. The closed vocabulary is published as the schema
`review-facts.json` (packaged under `clagentic_loadout/schemas/`). A finding without
`facts` stays valid; it is simply evaluated at the worst case.

| Fact | Values |
|---|---|
| `category` | `injection`, `authn_authz`, `secrets_exposure`, `crypto`, `deserialization`, `ssrf`, `path_traversal`, `input_validation`, `supply_chain`, `ci_workflow`, `data_exposure`, `resource_exhaustion`, `concurrency`, `error_handling`, `correctness`, `api_contract`, `performance`, `maintainability`, `test_gap`, `other` |
| `cwe` (optional) | `CWE-<number>`, for example `CWE-79` |
| `reachable` | `yes`, `no`, `unknown` |
| `attacker_precondition` | `none`, `can_open_pr`, `can_merge`, `has_host_access`, `unknown` |
| `impact.confidentiality`, `impact.integrity`, `impact.availability` | `none`, `partial`, `full`, `unknown` |
| `durability` | `durable`, `ephemeral`, `unknown` |
| `evidence` (optional) | short string, at most 300 characters |

Example:

```json
{
  "file": "src/handlers/upload.py",
  "line": 41,
  "rule_id": "path-traversal",
  "message": "The upload name is joined to the storage root without normalisation.",
  "facts": {
    "category": "path_traversal",
    "reachable": "yes",
    "attacker_precondition": "none",
    "impact": {"confidentiality": "full", "integrity": "partial", "availability": "none"},
    "durability": "durable",
    "evidence": "name comes from the multipart filename field"
  }
}
```

The security-floor categories are `injection`, `authn_authz`, `secrets_exposure`,
`deserialization`, `ssrf` and `path_traversal`. A finding with no `category` (absent or
invalid) is treated as a floor category, the worst case for that dimension.

A finding whose own `severity` is `praise` is not a defect: it is listed as advisory and
not scored. Every other finding is scored from its facts, whatever `severity` it states.
This applies to a findings file handed to `evaluate` or `post`. A model reply in `run` is
different: it never carries a severity (see below), so nothing a model writes can mark a
finding as praise.

## Rubric

Computed per finding from its facts and the stakes of its path.

1. **Impact points.** Each of confidentiality, integrity and availability scores `none` 0,
   `partial` 1, `full` 3, `unknown` 3. The path's data class then shifts the sum:
   `public` -1, `internal` 0, `confidential` +1, `regulated` +2. The result is kept at 1 or
   more when any impact was stated and is 0 when none was. Points map to an impact tier:
   0 is `none`, 1 to 2 is `limited`, 3 to 5 is `serious`, 6 or more is `severe`.
2. **Effective precondition rank.** `none` 0, `can_open_pr` 1, `can_merge` 2,
   `has_host_access` 3, `unknown` 0. An exposed path lowers the rank by one (minimum 0). A
   path whose merges are uncontrolled collapses `can_merge` to rank 1, because anyone who
   can open a pull request can then land it.
3. **Table lookup.** Severity is read from the table below.
4. **Durability.** `ephemeral` lowers the result one level (a result above `none` stays at
   least `low`). `durable` and `unknown` change nothing.
5. **Reachability.** `no` caps the result at `low`. `yes` and `unknown` change nothing.

| Impact tier | Rank 0 | Rank 1 | Rank 2 | Rank 3 |
|---|---|---|---|---|
| severe | critical | high | high | medium |
| serious | high | high | medium | low |
| limited | medium | medium | low | low |
| none | none | none | none | none |

Severities, lowest first: `none`, `low`, `medium`, `high`, `critical`.

**Blocking.** A finding blocks when its severity is at or above the configured threshold
(default `high`), or when it is in a security-floor category on an internet-exposed path.
A matched disposition demotes it to advisory (see below).

Determinism: the same findings, policy and date always produce the same verdict.

## Stakes

Stakes are resolved per path from three sources:

- **The base tree.** Exposure markers; CODEOWNERS is recorded as context only.
- **The host.** Repository visibility only: passed with `--visibility` to `evaluate`, read
  from the repository record in the PR metadata by `run` and `post`. `public` marks every
  path exposed. Branch protection and rulesets are not read.
- **A profile** in `stakes.yaml`, selected with `--profile` or the file's own `profile` key.

Rules:

- A tree marker or a public repository that says **exposed** overrides a profile claim of
  `internal`. A profile can raise stakes, never hide them.
- Anything not stated resolves to the worst case: `exposed`, data class `regulated`, merge
  control `uncontrolled`.
- `merge_control` is `controlled` **only** when the profile committed at the base revision
  declares it. A CODEOWNERS match proves nothing about whether review is enforced (the
  file can be unenforced, stale, or name a principal that cannot approve), so it never
  changes a rank. Without a profile claim the path is `uncontrolled`.
- A value that is not positively established from the base tree, the host or the base
  profile is the worst case. Policy that can lower a verdict is an allowlist: only a
  recognised value lowers anything, never "anything not on a deny list".

Exposure markers. A file matching one of these globs anywhere in the base tree marks the
directory that holds it, and everything below, as exposed (a marker at the repository root
exposes the whole repository):

- public ingress manifests: `**/ingress*.yaml`, `**/ingress*.yml`, `**/*-ingress.yaml`,
  `**/*-ingress.yml`, `**/ingress/**`
- listener configs: `**/nginx.conf`, `**/Caddyfile`, `**/haproxy.cfg`, `**/traefik*.yaml`,
  `**/traefik*.yml`, `**/*listener*.yaml`, `**/*listener*.yml`, `**/*listener*.conf`

Packaging files (`pyproject.toml`, `package.json` and the like) are not markers: publishing
is a supply-chain property, not an attack vector, so a repository whose only marker is a
packaging file has no path marked exposed by the tree.

CODEOWNERS is looked up at `.github/CODEOWNERS`, `CODEOWNERS`, `docs/CODEOWNERS`, then
`.forgejo/CODEOWNERS`; the first that exists is used. Each finding's stakes record whether
an owner (`@user`, `@org/team` or an email address) covers its path, as informational
context only.

### `stakes.yaml`

Read from `.clagentic/loadout/stakes.yaml` at the base revision.

```yaml
profile: service            # default profile when --profile is not given
profiles:
  service:
    exposure: internal      # exposed | internal
    data_class: confidential # public | internal | confidential | regulated
    merge_control: controlled # controlled | uncontrolled
    paths:                  # applied in order; later matches override field by field
      "deploy/edge/**": {exposure: exposed}
      "docs/**": {data_class: public}
```

Globs: `*` and `?` stay inside one path segment, `**` spans segments.

The schema is strict. `paths` is allowed only directly inside a profile; any other unknown,
misplaced or nested key (for example a `paths` key inside a path entry) makes the whole file
malformed. No policy from a malformed file applies, and the verdict is DEGRADED naming the
key path, such as `profiles.service.paths.deploy/**.paths`.

## Dispositions

Read from `.clagentic/loadout/dispositions.yaml` at the base revision.

```yaml
dispositions:
  - path: "src/legacy/**"
    rule_id: sql-string-building
    kind: accepted_risk       # by_design | false_positive | accepted_risk | mitigated
    rationale: "Internal admin tool, behind the VPN, scheduled for removal."
    by: a-maintainer
    at: 2026-09-01
    expires: 2026-12-01       # optional; accepted_risk defaults to 30 days after `at`
    fingerprint: "fp2:0123456789abcdef"   # optional link hint
```

- An entry matches a finding on the path glob plus the exact `rule_id`. A `fingerprint`
  narrows the match only when the finding carries one too; it is a hint, never an identity.
- A matched finding is annotated with the disposition and demoted to advisory. It is never
  deleted and is still printed.
- A security-floor finding cannot be cleared by `by_design`. The entry is reported as
  refused and the finding keeps blocking.
- An expired entry does not apply and is listed in the verdict.
- The schema is strict: an unknown, misplaced or nested key makes the file malformed. A
  malformed file applies nothing and makes the verdict DEGRADED, naming the key path.
- **Because the file is read from the base revision, an entry that exists only in the
  change under review does not apply to that change.** Land the entry first.

## Per-PR rulings

A disposition lands on the base branch through a reviewed change. A decision made once for
one open pull request ("this finding is by design here", "defer this to follow-up work")
needs a faster route that the pull request still cannot use to clear its own findings. That
route is the **ruling registry**: a file outside every repository, written only by
`loadout-ruling record`, read by the evaluator and the merge gate.

```yaml
rulings:
  - repo: some-owner/some-repo
    pr: 33
    fingerprint: "fp2:0123456789abcdef"   # the finding's fingerprint, required
    rule_id: sql-string-building           # exact; a check on the fingerprint
    path: src/legacy/export.py             # exact file path; a check on the fingerprint
    kind: accepted_risk                    # by_design | false_positive | accepted_risk | mitigated
    rationale: "Internal admin tool, behind the VPN."
    by: some-role                          # the attested role that recorded it
    requested_by: some-caller              # who asked, as the deployment names callers
    source_ref: "design note 4"            # opaque reference to where it was decided
    at: 2026-10-01
    expires: 2026-10-31                    # optional; accepted_risk defaults to 30 days after `at`
    reference: ABC-123                     # optional deferral to a work item; needs `expires`
```

- A ruling covers a kept finding the way a disposition does: the same kinds, the same expiry
  rules, the same demotion to advisory. Nothing is deleted. It is keyed to the finding's
  fingerprint, to one repository and to one pull request, so it never clears another PR or
  another finding. A finding with no fingerprint cannot be ruled on.
- **A ruling never clears a security-floor finding, whatever its kind.** A floor finding
  clears only through a dispositions entry added to the base branch by a reviewed change, or
  by being fixed. The evaluator reports the ruling as not applied, naming the finding and
  that route; `loadout-ruling record` refuses a floor finding when it is asked to write one.
- A ruling that clears a finding is named in the output with who recorded it, who requested
  it, when, its kind, the rationale, the source reference and any work-item reference. A ruling
  for the pull request that cleared nothing (expired, matched no kept finding, refused by the
  floor, or already superseded by an earlier entry) is listed too, with the reason.
- Base-revision dispositions apply first; a ruling applies only where they leave a finding
  blocking.
- **Trust.** The registry is a file named by user-level configuration (or
  `CLAGENTIC_LOADOUT_RULINGS_FILE`), never by repository configuration. One check, applied on
  every read and every write, refuses it (on a read the verdict is DEGRADED and the reason is
  named) when: it sits inside any git work tree, or inside the reviewed repository when that
  is known; or the file, or any directory above it up to the root, is writable by its group or
  by every user, or is owned by someone other than the process owner or root. A sticky shared
  directory (a system temporary directory) is allowed as an ancestor. The registry must be
  owned by the reading process's uid or by root. The location check does not depend on any
  optional flag.
- **Residual: code running as the same user.** These checks stop other users and a checkout.
  They cannot stop code that runs as the same user as the reader or the writer, for example a
  pull request's `pre_checks` executed at merge time, which can write the registry. Configure
  the optional pre-checks sandbox (`merge.pre_checks_sandbox`, see
  [merge-authority.md](merge-authority.md)) so that code does not run as that user, and keep
  the registry under a directory only the recording role's user can write.
- **Size.** A write that would grow the registry past the size its readers accept
  (512 KiB) is refused (exit 55) and leaves the file unchanged; remove expired rulings first.
- With no registry configured, nothing changes: output is identical to a build without the
  feature, and `EVALUATOR_VERSION` is unchanged.

```yaml
# ~/.config/clagentic/loadout/config.yaml
rulings:
  path: /var/lib/loadout/rulings.yaml     # absolute; omit for no registry
  reference_pattern: '^[A-Z]+-[0-9]+$'    # optional grammar for deferral references
```

### Who may record a ruling

`loadout-ruling record` is gated by role. The attested caller's role must hold merge
authority: it must hold the `ruling` verb. No caller-controlled input decides that: the
authority comes only from an injected authority provider, else from the effective role-to-verb
mapping, which is the `roles:` mapping in the user-level config file when that key is present,
else the built-in default (`provisioning.roles.DEFAULT_ROLE_VERBS`, which grants `ruling` to
`merger`). No flag, environment variable, repository or checkout contributes, and there is no
`--authorized-role` flag. Which agent holds a role is the deployment's configuration, never
this package's.

The user-level source is the `config.yaml` under the loadout config root (the file that holds
the `rulings:` section), a top-level `roles:` key shaped exactly like the repo-tier `roles:`
mapping (role name to a non-empty list of verb labels):

```
roles:
  clerk:
    - ruling
  releaser:
    - merge
    - push
```

Here only `clerk` may record rulings. The mapping REPLACES the built-in default entirely when
present (a role it omits, `merger` included, holds nothing); the default applies only when the
key is absent. It decides nothing but who may use `loadout-ruling record`: provisioning,
`loadout-doctor`, model routing and every other verb keep their own sources. The file is read
only if it passes the registry's location check (not inside a git work tree, and neither the
file nor any directory above it writable by a group or by everyone or owned by another user);
otherwise `record` is refused with exit 52 and names the file. A present `roles:` key whose value is
empty, `null` or not a mapping is malformed, exit 3, never the default. A deployment owns the whole `roles:` key: an installer should merge it into the user
config alongside the other sections and never touch unrelated sections. `--repo-path` scopes the ruling (a registry inside it is refused) and supplies no
role policy. `--role` must match the attested identity, as for every
other caller-bound verb. `--requested-by` records who asked, as the deployment names callers.

### Where the finding comes from

A reviewer's findings file lives in scratch that is gone by the time a ruling is asked for, and
regenerating it would not reliably reproduce the finding. The finding is therefore identified
from the reviewer's POSTED verdict fence, which is durable and which the host attributes to a
login. `--fence-comment ID` names that comment; `record` reads it through the host API with the
caller's own role credential (the credential provider the merge verb uses for `--role`) and
accepts it only if all of these hold:

- the comment is on the requested repository's requested pull request (a comment id that is not
  among that pull request's comments is refused);
- its author's login is the login of a required reviewer role, resolved as the merge gate
  resolves it, where the required roles are the ones the merge enforces (one shared resolution,
  `merge.required_reviewers`): the roles the repository's tracked gate declares at the pull
  request's BASE commit, read through the host API (content the pull request cannot change, and
  never a checkout the caller supplies), plus the bare roles named with `--required-reviewer`,
  whose logins the tool derives. The flag adds roles and never removes a declared one; it takes
  no `name:login` pair, because a ruling is matched by fingerprint, rule and path and not by
  the login that posted the fence. It decides only whose comment counts as a role's verdict,
  never who may record. A repository whose base declares no required reviewer role and a call
  with no `--required-reviewer`, or a gate that cannot be read and no flag, attributes no fence;
- it is a well-formed verdict fence for that reviewer, read by the merge gate's own fence reader,
  for the requested pull request, at the pull request's current head (or the full SHA given with
  `--head`);
- it is an evaluated fence carrying its `evaluated_findings`, and one of them has `--fingerprint`
  (the fingerprint is carried only when the reviewing deployment has a registry configured).

The finding's rule, path and facts, and so its floor status, are taken from the fence, not from
anything the caller types. `--findings`, `--rule-id` and `--path` no longer exist.

```
loadout-ruling [--role ROLE] record --platform github|forgejo --repo OWNER/REPO --pr N \
  --fence-comment ID --fingerprint FP \
  --kind by_design|false_positive|accepted_risk|mitigated --rationale TEXT \
  --requested-by CALLER --source-ref REF [--expires YYYY-MM-DD] [--reference ITEM] \
  [--head SHA] [--required-reviewer ROLE]... [--repo-path DIR] [--git-host-base-url URL]
loadout-ruling list [--repo OWNER/REPO] [--pr N]
```

Exit codes: `0` ok, `1` usage, `2` no credential for the caller's role, `3` configuration, `4`
`--platform` does not match the backend, `7` identity mismatch, `21` role not authorized, `27`
the host could not be read, `32` the comment is not an acceptable fence (not found, wrong
author, malformed, another pull request or head, no evaluated findings, or no required reviewer
can be established), `50` floor finding, `51` the fence holds no finding with the fingerprint,
`52` registry location (or the user-level role mapping file) untrusted, `53` no registry
configured, `54` an unexpired ruling already covers the finding, `55` registry could not be
written.

### Migration

Additive. A deployment that configures no `rulings.path` sees no change. New: the
`loadout-ruling` verb (also `clagentic-loadout ruling`); the user-level `rulings` section and
`CLAGENTIC_LOADOUT_RULINGS_FILE`; `loadout-review evaluate --repo/--pr`; and the optional
`fingerprint` on a fence's `evaluated_findings` entries, carried only when a registry is
configured, so the merge gate can match a ruling to the same finding the evaluator did. No
existing flag or exit code changes. `ruling` is a verb label in the role-to-verb vocabulary
and in the default `merger` set, so a generated `merger` allowlist fragment also names
`loadout-ruling`.

## Outcomes

| Outcome | Exit code | Meaning |
|---|---|---|
| CLEAN | 0 | Nothing blocks. Advisory findings are listed. |
| BLOCKED | 40 | At least one open finding blocks. Each is listed with the exact dispositions entry that would clear it, or with the note that the floor forbids clearing it as `by_design`. |
| DEGRADED | 41 | A required input is missing: facts absent or invalid on a blocking finding, a required scanner did not report or reported any status other than `ran` or `not_applicable` with a non-empty reason, or a policy file or the base revision could not be read. The verdict cannot clear the change. |
| STALE | 42 | The head the findings are for is not the expected head. |

Precedence: STALE, then DEGRADED, then BLOCKED, then CLEAN. Other exit codes: `1` a SHA
argument is not a full commit SHA, `3` the evaluator configuration is invalid, `32` the
findings file is unreadable or invalid.

A finding with no facts resolves to the worst case, which blocks, and its missing facts make
the verdict DEGRADED; the reason names the finding.

Required scanners are an allowlist: a scanner is satisfied only by status `ran`, or
`not_applicable` with a non-empty reason. Every other value, including an unrecognised
string such as `timeout`, is DEGRADED and names the scanner and its status. A scanner that
reports more than once is satisfied only if every report is.

Deduplication: findings with identical file, line, rule and message are merged. The copy
kept is the strongest: a non-praise copy always beats a praise or advisory one, then a
blocking copy beats a non-blocking one, then the most severe wins. A blocking finding is
never suppressed by a merge. Findings on the same file and rule at different lines or with
different messages are both kept and annotated `possible_duplicates`.

## Configuration

User-level only (`~/.config/clagentic/loadout/config.yaml`), never read from the
repository, because a threshold the pull request can change could lower its own verdict:

```yaml
review:
  evaluator:
    block_at: high              # low | medium | high | critical
    required_scanners: [secret-scan]
```

## Running it

```
loadout-review evaluate --findings findings.json \
  --base <merge-base-sha> --head <head-sha> \
  [--expected-head <current-pr-head-sha>] [--profile <name>] \
  [--visibility public|private|internal] [--repo-path <dir>] [--format json|text] \
  [--repo <owner/repo> --pr <number>]
```

`--repo` and `--pr`, given together, apply the rulings recorded for that pull request (see
Per-PR rulings). Without them no ruling is applied.

`--findings` is a `run` output or a bare JSON array. A `run` output may carry
`scanners_run`. `--format json` (default) prints one JSON object with the verdict and the
human rendering under `rendering`; `--format text` prints the rendering only.

## Use in `run` and `post`

`loadout-review run` and `post` are the same evaluator with its inputs assembled for
them. The reviewer model is asked for facts only (the prompt is built from the vocabulary
above), and the reply contract changes accordingly:

- A reply states, per finding, `file`, `line`, `rule_id`, `message`, the `facts` object and
  optionally `failure_sequence`. It states no severity and no verdict.
- A reply that still carries a `severity` (an older rulebook asks for one) is accepted. The
  value is discarded whatever it holds, and a notice on stderr says so. The computed
  severity is used.
- A finding with no `facts`, or facts outside the vocabulary, is accepted and resolves to
  the worst case: it blocks, and the verdict is DEGRADED naming the finding. There is no
  lenient path.

`run` also states the findings of the repo profile read from the same base revision (a diff
that breaks the repository's class rules, an invalid profile, and a missing one when the
deployment asks for it), with facts, beside the reviewer's; see
[repo-profile.md](repo-profile.md). They are scored like any other finding
and never hold a merge on their own.

`run` evaluates after the chunks are merged and, in delta mode, after prior findings are
accounted for. Policy is read from the PR's base revision (not the last reviewed head) through
the host API, or from the repository at `--repo-path` when that is given explicitly;
repository visibility comes from the host's repository record
under the same reader credential as the diff. A base revision the source cannot serve, a failed
or truncated read, a policy file that cannot be read, or a visibility the host did not state each
make the outcome DEGRADED with the reason named (an unstated visibility counts as public).

Each finding in `findings.json` gains `computed_severity`, `blocking`, `facts_state`,
`disposition`, `disposition_refused` and `possible_duplicates`; its legacy `severity` is
derived from the result (`blocking` or `nit`). The document gains `verdict_outcome`,
`verdict_reasons`, `evaluator_version`, `policy_base_sha` and `repository_visibility`.
Identical findings (same file, line, rule and message) are merged into the strongest copy;
a finding a base-revision disposition clears is kept as advisory with the disposition
named; findings that may be the same defect are annotated and kept. In delta mode a kept
or carried prior finding is evaluated again under the current base and annotated; a
standing computed in an earlier round is not carried over.

`post` recomputes the verdict itself from the facts in the findings file and the host's and
the base revision's records. The outcome the file records is not trusted. The posted comment
is rendered from the computed outcome: STALE says which head the findings are for;
BLOCKED lists each blocking finding with the exact `dispositions.yaml` entry that would
clear it (or the note that the security floor forbids clearing it as `by_design`); DEGRADED
lists its reasons; CLEAN lists the advisory findings. `--status` is optional: `clean` over
any other outcome is refused, naming both. The fence records `verdict_outcome`,
`verdict_reasons`, `evaluator_version` and `policy_base_sha` at `fence_schema_version` 3,
plus `evaluated_findings` (each finding with its facts, so the merge gate can compute the
outcome again; a finding's fingerprint is carried only when a ruling registry is configured)
and `findings_open`, the computed blocking set after dispositions and rulings; a fence
without them reads as not evaluated.

To reproduce a posted outcome offline, run `evaluate` on the same `findings.json` with the
recorded `policy_base_sha` as `--base`, the head, and the visibility the run recorded as
`repository_visibility` (`--visibility`). At `post`, `scanners_run` comes from `--state-file`;
`evaluate` takes it from the findings file.

### Standalone reviewer example

A reviewer that is not part of any larger system can use the evaluator on its own:

1. Review the change and write each finding with its facts to `findings.json`, recording the
   head it reviewed:

   ```json
   {
     "head_sha": "<head-sha>",
     "findings": [
       {
         "file": "src/handlers/upload.py",
         "line": 41,
         "rule_id": "path-traversal",
         "message": "The upload name is joined to the storage root without normalisation.",
         "facts": {
           "category": "path_traversal",
           "reachable": "yes",
           "attacker_precondition": "none",
           "impact": {"confidentiality": "full", "integrity": "partial", "availability": "none"},
           "durability": "durable"
         }
       }
     ]
   }
   ```

2. Compute the verdict against the merge base:

   ```
   loadout-review evaluate --findings findings.json \
     --base "$(git merge-base origin/main HEAD)" --head "$(git rev-parse HEAD)" \
     --format text
   echo "exit: $?"
   ```

3. On BLOCKED, fix the code or, for a finding that is genuinely acceptable, copy the
   printed `dispositions:` entry into `.clagentic/loadout/dispositions.yaml` on the base
   branch with a real rationale, then run the evaluation again.
