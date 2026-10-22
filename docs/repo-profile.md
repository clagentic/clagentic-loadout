# Repo profile

Back to [README.md](README.md).

A repo profile is one file, `.clagentic/loadout/profile.yaml`, that states the shape a
repository must keep. It replaces per-repo prose as the thing an agent or a reviewer checks a
change against: every repository answers the same question in the same place.

Two classes:

| Class | Meaning | Rules |
|---|---|---|
| `released-tool` | The tree ships to people outside the deployment that built it. | No agent or deployment names, hostnames, domains or usernames in the shipped tree. No absolute host paths. Other tools are optional and pluggable, never a hard requirement. A released tool never adds, removes or changes the tools or permissions of a particular agent or consumer based on its identity, and never names or special-cases a consumer. Behaviour that affects permissions for all sessions is a documented setting the user controls. |
| `internal` | The tree stays inside a deployment. | Other tools are consumed only through their documented interfaces: an installed command, or the top level of a package, never one of its internal modules. |

The profile is read from the **base revision**, like `stakes.yaml` and `dispositions.yaml`
(see [review-evaluator.md](review-evaluator.md)). A profile added or loosened in the change
under review does not apply to that change; land it first.

## The file

```yaml
class: released-tool            # required: released-tool | internal
paths:                          # optional; applied in order, a later match wins
  "tests/fixtures/**": {class: internal}
deny:                           # optional; released-tool only
  names: [some-agent-name]      # whole token, case-insensitive
  hosts: [host.example.test]    # a literal; a path prefix such as /srv/data/ also works
  users: [some-username]
tool_packages: [some_tool]      # optional; internal only
test:                           # optional; the commands `clagentic-loadout test run` uses
  full: [python3, -m, pytest, "--junitxml={junit}"]
```

A complete example, kept valid by the test suite, is
[`.clagentic/loadout/profile.yaml.example`](../.clagentic/loadout/profile.yaml.example).

The schema is strict. An unknown, misplaced or nested key, a class outside the two above, or a
list entry that is not a one-line string makes the whole file invalid, and the error names the
key path (for example `paths.tests/fixtures/**.class`). A half-understood file is never
half-applied.

A field is also refused where its class never applies: `deny` needs at least one
`released-tool` path (the top-level class or a `paths` override), and `tool_packages` needs at
least one `internal` path. Both are valid in a profile whose classes are mixed.

- **`class`** is the class of every path that no `paths` entry overrides.
- **`paths`** maps a path glob to `{class: ...}`. `*` and `?` stay inside one path segment and
  `**` spans segments. Use it for the parts of a tree that are not what ships, such as tool state
  or fixtures.
- **`deny`** adds values to the deployment's own denial list (below) for this repository. It
  can only add: nothing in a profile removes an entry from the deployment's list.
- **`tool_packages`** lists top-level Python packages of tools this repository consumes. No
  import is checked mechanically. The list is added to the rule statements the reviewer reads
  (and that `loadout-profile check` prints), so the reviewer judges whether code in an
  `internal` path reaches into their internal modules rather than the documented interface.

- **`test`** declares the argv `clagentic-loadout test run` uses for `full`, `impacted` and
  `files`, a default `base`, and named `tiers`. Review ignores it. Its shape and use are in
  [repo-test.md](repo-test.md).

## What review does with it

`loadout-review run` reads the profile at the PR's base revision and states what the class
rules say about the diff as ordinary findings with facts. The evaluator scores them like any
other finding; no severity is named by the rule.

| Finding rule | When |
|---|---|
| `repo-profile.denied-value` | An added line in a `released-tool` path contains a denied name, host or username. |
| `repo-profile.host-path` | An added line in a `released-tool` path contains a `/home/<user>`, `/Users/<user>` or `C:\Users\<user>` path. Placeholder users (`user`, `username`, `you`, `example`, `someone`, `x`, `foo`, `alice`, `bob`, `test` and a few like them) are ignored. |
| `repo-profile.missing` | The base revision has no profile. Always reported, with no setting to turn it off; it is advisory and scores `none`. |
| `repo-profile.invalid` | The base revision has a profile that is not valid; the message names the problem. Always reported: the file is there and is being ignored. |
| `repo-profile.more` | More than 100 rule hits in one diff; the rest are counted here. |

Only added lines are checked, and the profile file itself is never checked (it may list the
values it forbids). Nothing is checked mechanically for the `internal` class. Rules that need
judgement, such as "other tools stay optional" or "use a tool only through its documented
interface", are stated for the reviewer and are not guessed at mechanically.

**Stated rules in the reviewer prompt.** Each chunk prompt carries a `## Repository class rules`
section after the rulebook and before the diff. For every class that governs a file in the chunk
(by `class` and `paths`), it lists that class's stated rules and the chunk files it governs; the
`internal` section also carries the `tool_packages` list. The same applies to a delta review, for
the delta's chunks. The section is context for the reviewer to judge against: it is not a check,
names no severity, and is absent when the base revision has a missing or invalid profile (those
are reported as findings instead).

**How severe.** A rule hit states `reachable: no`: a shape violation is not an attack path, so
the evaluator holds it at `low`. At the default threshold (`block_at: high`) it is advisory; a
deployment that wants rule hits to block sets `review.evaluator.block_at: low`
(see [review-evaluator.md](review-evaluator.md#configuration)). A missing or invalid profile
states no impact at all and scores `none`, so it cannot block at any threshold.

**Never a merge refusal.** A missing or invalid profile is reported as a finding and nowhere
else. It is not a degraded input, and the merge gate has no code path that reads the profile:
the gate computes its outcome from the findings a verdict fence carries, and these findings are
advisory. A repository with no profile still merges; it only carries the finding.

## The deployment's settings

One setting belongs to the deployment that reviews a repository, not to the repository, so
it lives in the user-level config (`~/.config/clagentic/loadout/config.yaml`) and is never
read from the repo:

```yaml
review:
  repo_profile:
    deny:                       # values a released-tool tree must not carry
      names: [some-agent-name]
      hosts: [host.example.test]
      users: [some-username]
```

- **`deny`** is the denial list. The names a released tool must not carry are specific to the
  deployment, and the tool is the one place they must not be written down. `loadout-review run`
  merges this list with the profile's own `deny`.

A malformed `review.repo_profile` stops `run` with exit 3 before any model is called; the merge
gate never reads it.

## `loadout-profile`

```
loadout-profile check [--repo-path DIR] [--ref REV]
loadout-profile init  [--repo-path DIR] [--ref REV] [--force] [--stdout]
```

`check` validates the profile with the same loader the review uses and prints one JSON object:
the class, the rules the class carries, the overrides, how many denial entries the profile
adds, and `tool_packages`. `rules` is the statements of the top-level class; `class_rules` maps
each class in effect (top-level or a `paths` override) to its statements. By default it reads the working tree, so a profile can be checked
before it is committed; with `--ref` it reads that revision, as the review does.

`init` drafts a profile for a repository that has none, from the file list of a revision
(`--ref`, default `HEAD`). A root packaging manifest (`pyproject.toml`, `package.json`,
`Cargo.toml`, `go.mod` and similar) or a license file drafts `released-tool`; with neither the
draft is `internal`. For a `released-tool` draft, each top-level hidden directory that is not
repository or host configuration (`.github`, `.forgejo`, `.gitea`, `.gitlab`, `.clagentic`) is
proposed as an `internal` override, because tool state is not part of what ships. The draft
opens with a comment saying it is a draft and listing what it was inferred from. It is written
to `.clagentic/loadout/profile.yaml`; `--stdout` prints it and writes nothing; an existing file
is never replaced without `--force`. A draft is a starting point: review it, then commit it to
the base branch.

Exit codes: `0` ok, `1` usage, `60` no profile, `61` invalid profile, `62` repository,
revision or file could not be read or written, `63` the profile already exists. Both
subcommands are local, need no credential and bind no caller.
