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
makes no host call, posts nothing, and binds no caller identity. `run`, `post` and the
merge gate are unchanged by it.

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

- **The base tree.** Exposure markers and CODEOWNERS.
- **The host.** Repository visibility only, passed with `--visibility`. `public` marks every
  path exposed. Branch protection and rulesets are not read.
- **A profile** in `stakes.yaml`, selected with `--profile` or the file's own `profile` key.

Rules:

- A tree marker or a public repository that says **exposed** overrides a profile claim of
  `internal`. A profile can raise stakes, never hide them.
- Anything not stated resolves to the worst case: `exposed`, data class `regulated`, merge
  control `uncontrolled`.
- `merge_control` is `controlled` when CODEOWNERS covers the path with at least one owner
  (the last matching rule wins) or the profile says so.

Exposure markers. A file matching one of these globs anywhere in the base tree marks the
directory that holds it, and everything below, as exposed (a marker at the repository root
exposes the whole repository):

- public ingress manifests: `**/ingress*.yaml`, `**/ingress*.yml`, `**/*-ingress.yaml`,
  `**/*-ingress.yml`, `**/ingress/**`
- listener configs: `**/nginx.conf`, `**/Caddyfile`, `**/haproxy.cfg`, `**/traefik*.yaml`,
  `**/traefik*.yml`, `**/*listener*.yaml`, `**/*listener*.yml`, `**/*listener*.conf`
- published packaging files: `**/pyproject.toml`, `**/setup.py`, `**/setup.cfg`,
  `**/package.json`, `**/Cargo.toml`, `**/*.gemspec`, `**/pom.xml`, `**/*.nuspec`,
  `**/Chart.yaml`

CODEOWNERS is looked up at `.github/CODEOWNERS`, `CODEOWNERS`, `docs/CODEOWNERS`, then
`.forgejo/CODEOWNERS`; the first that exists is used.

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
- A malformed file applies nothing and makes the verdict DEGRADED.
- **Because the file is read from the base revision, an entry that exists only in the
  change under review does not apply to that change.** Land the entry first.

## Outcomes

| Outcome | Exit code | Meaning |
|---|---|---|
| CLEAN | 0 | Nothing blocks. Advisory findings are listed. |
| BLOCKED | 40 | At least one open finding blocks. Each is listed with the exact dispositions entry that would clear it, or with the note that the floor forbids clearing it as `by_design`. |
| DEGRADED | 41 | A required input is missing: facts absent or invalid on a blocking finding, a required scanner reported `failed` or `not_invoked` (or did not report), or a policy file or the base revision could not be read. The verdict cannot clear the change. |
| STALE | 42 | The head the findings are for is not the expected head. |

Precedence: STALE, then DEGRADED, then BLOCKED, then CLEAN. Other exit codes: `1` a SHA
argument is not a full commit SHA, `3` the evaluator configuration is invalid, `32` the
findings file is unreadable or invalid.

A finding with no facts resolves to the worst case, which blocks, and its missing facts make
the verdict DEGRADED; the reason names the finding.

Deduplication: findings with identical file, line, rule and message are merged (the most
severe copy is kept). Findings on the same file and rule at different lines or with
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
  [--visibility public|private|internal] [--repo-path <dir>] [--format json|text]
```

`--findings` is a `run` output or a bare JSON array. A `run` output may carry
`scanners_run`. `--format json` (default) prints one JSON object with the verdict and the
human rendering under `rendering`; `--format text` prints the rendering only.

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
