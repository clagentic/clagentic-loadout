# Declared interfaces and consumers

Back to [README.md](README.md).

A repository that other tools depend on declares what they may depend on, in a tracked
file. Loadout can compare that declaration between two revisions and say whether the
change is breaking, say who consumes the elements that changed, pre-fill a spec's
consumers from that, print the consumers at push, and, at merge, name any consumer of a
breaking change that the spec leaves without a disposition.

Everything here is **report-only**. No check in this document refuses, blocks or fails a
push or a merge; the gate outcome is the same with or without it. A missing file, an
unreadable revision and an unavailable consumer map are reported, and the verb carries on.

## The interface file

`.clagentic/loadout/interface.yaml`, tracked in the producer repository:

```yaml
schema_version: 1
verbs:                                 # console scripts
  - name: <console script>
    subcommands: [<name>, ...]         # optional
    exit_codes: {<meaning>: <int>, ...}  # optional; the meaning is the key
schemas: [<schema file name>, ...]     # optional
hooks: [<hook name>, ...]              # optional
env: [<environment variable>, ...]     # optional
```

Names are unique within their list. A key the schema does not name is refused, so a
misspelt section cannot declare nothing and read as an empty interface.

This repository's own file is the first instance. A test (`tests/test_interface_self.py`)
reads the console scripts from `pyproject.toml`, the exit-code tables from the modules
those scripts run, the shipped schema files and the `CLAGENTIC_LOADOUT_*` names written in
the source, and fails when the file disagrees, so a change to one without the other cannot
land. An exit-code table is the module-level `EXIT_*` constants assigned an integer
literal; the meaning is the constant name without its prefix, in lower case. An alias of
another constant is not a code of its own.

## What is breaking

| Change | Breaking |
|---|---|
| a verb, subcommand, schema, hook or env name is removed | yes |
| an exit code meaning is removed | yes |
| an exit code meaning maps to a different number | yes |
| any of those is added | no |

A rename is the removal of the old name together with the addition of the new one; nothing
guesses that two names are the same element. A revision with no interface file is reported
(`base` / `head` are `present` or `missing`). A base with no file and a head with one is a
first declaration and is not breaking. A base that declares an interface and a head with no
file is breaking, with every base element listed as removed.

## `loadout-interface`

```
loadout-interface diff      --base REV --head REV [--repo-path DIR]
loadout-interface consumers [--repo NAME] [--repo-path DIR] [--consumers-file FILE]
                            [--element NAME]... [--path GLOB]... [--base REV --head REV]
```

(also `clagentic-loadout interface diff|consumers`.)

`diff` prints one JSON object: `breaking`, `base` and `head` (`present` or `missing`),
`removed`, `changed` and `added` (each change has `kind`, `element`, `name`, `change` and
`detail`) and `notes`. It exits `0` for a breaking change too: it reports.

`consumers` prints who consumes the named elements of the repository `NAME` (default: the
origin of `--repo-path`), from the consumer map. Elements are named by `--element`, by
`--path` globs matched against the files the map lists, or by the elements a `--base` /
`--head` diff changed. Each consumer is printed with the map `element` it consumes, its
`kind`, `confirmed_at` and `source`. A map element matches a name when it is that name or
its first word is, so a verb's name finds that verb's flags too.

Exit codes: `0` ok (including a breaking change and an unavailable map), `1` usage, `90` a
revision or the repository could not be read, `91` an interface file is not valid.

## The consumer map

Operator-owned data kept outside any repository under work, read only through loadout
verbs:

```yaml
schema_version: 1
producers:
  <producer repo>:                     # bare repository name
    - element: <verb, command, file or schema name>
      kind: verb | flag | file | schema | config-key
      consumers:
        - repo: <consumer repo>
          confirmed_at: "<ISO date the use was confirmed>"
          source: <task reference, or the file in the consumer repo where the use was seen>
unconfirmed: [...]                     # seeds that could not be confirmed; counted, not read
```

**Where it is.** In order: `--consumers-file`; the `CLAGENTIC_LOADOUT_CONSUMERS_FILE`
environment variable; the `rules.consumers_file` setting of the user-level config file. This
is the same resolution the groups file uses, and nothing is read from the repository under
work. With none of them set, with no file at the configured path, or with a file that
cannot be read, the verbs report that the map is unavailable and exit `0`.

## In the spec

`loadout-spec new` takes `--path GLOB`, `--element NAME` and `--consumers-file FILE`. When
paths or elements are named and the map is available, the draft's `consumers` section is
filled with one item per consumer and element, `source` set to `<path> (<origin>)`, each
carrying a placeholder disposition (`undisposed: ...`) for the author to replace with a
work item reference, or with `none` and a reason. An unavailable map leaves the section
empty with a note saying why. Naming nothing leaves the section exactly as it was. Items a
superseded draft already holds are kept as authored.

A consumer is **disposed** when its spec item names it (the repository name, as a whole
word, before the first `: `) and its disposition is text other than `none` alone or the
placeholder. Loadout assumes no tracker, so any such text is read as a work item reference.
A disposition is per consumer repository and covers every element that consumer consumes.

## At push

When the branch being pushed changes the interface file in a breaking way against its merge
base with the base branch, the push verification step prints `push: BREAKING INTERFACE
CHANGE (...)` and one line per consumer, or one line saying the map is unavailable or that
the producer could not be determined from the origin. A base that does not resolve prints
nothing. It never changes the push.

## At merge

When a pull request changes the interface file, `loadout-merge` reads it at the PR's
recorded base commit and at its head (through the local repository when `--repo-path` is
given and holds both, else through the host API). If the change is breaking and the map
lists consumers of what broke, the merge finds the PR's sealed spec by the `spec_hash` the
reviewer fences carry, in the local spec store, and compares. For each consumer the spec
leaves undisposed it prints, on standard error:

```
merge: NOTICE -- BREAKING INTERFACE CHANGE, consumer without a disposition: <consumer> consumes <element> (confirmed <date>); changed: <elements>. ...
```

and carries the same lines in the merge-completion attestation, in an `Interface notices`
row, and in an `undisposed_consumers` list in the final JSON. The merge proceeds with the
same outcome. Anything that stops the comparison (a revision that cannot be read, an invalid
interface file, an unavailable map, no spec hash in the fences, a spec not in the store) is
printed as `merge: NOTE -- ...` and changes nothing. A pull request that does not touch the
interface file, or a change that is not breaking, prints nothing and reads no revision.

## In `loadout-doctor`

`loadout-doctor` reports `consumers_map`: the map's location, how many confirmations it
holds, the oldest and newest `confirmed_at` with their age in days, and, when confirmations
are older than the 90-day window, how many. All of it is advisory: the check is always `OK`
and the exit code is unaffected, including for a map that is not configured, not there or
not valid.
