# Running a repository's tests: `clagentic-loadout test`

Back to [README.md](README.md).

`clagentic-loadout test` runs the tests a repository declares and lets anyone read the
results by run id. Running tests and reading results are the same for every repository, so
the verb takes its commands from the repository's [profile](repo-profile.md) rather than from
a script that lives with one deployment.

Interface version: **1**. The JSON field names, the run id grammar, the test id format and the
exit codes below are the contract; a change to any of them is a breaking change.

```
clagentic-loadout test run full|impacted [--repo-root DIR] [--base REV] [--timeout SECONDS]
clagentic-loadout test run files PATH... [--repo-root DIR] [--timeout SECONDS]
clagentic-loadout test run tier NAME [--repo-root DIR] [--timeout SECONDS]
clagentic-loadout test report [RUN_ID|latest] [--repo R] [--sha S]
                              [--summary | --failing-ids | --diff BASE_RUN_ID]
```

## Declaring the commands

The `test` block of `.clagentic/loadout/profile.yaml`:

```yaml
test:
  full: [python3, -m, pytest, "--junitxml={junit}"]
  files: [python3, -m, pytest, "--junitxml={junit}"]
  impacted: [./select-and-run-tests, --since, "{base}", --junit, "{junit}"]
  base: origin/main
  tiers:
    integration: [python3, -m, pytest, -m, integration, "--junitxml={junit}"]
```

- Each of `full`, `impacted` and `files` is an argv list. It is run without a shell, with the
  repository root as the working directory, with no stdin.
- Every command must contain the `{junit}` placeholder. Loadout replaces it with the path the
  command has to write its JUnit XML report to, and reads the counts and failing test ids
  from that file. A command that writes no readable report leaves an `unrunnable` run.
- `{base}` is allowed in `impacted` only. It is replaced by the full commit SHA of the base
  revision: `--base`, else the profile's `base`. `impacted` with neither is refused.
- `files` appends the paths given on the command line to its argv. A path must be an existing
  file inside the repository root and must not start with `-`.
- `tiers` maps a tier name to an argv list, run by `test run tier NAME`. The names are the
  repository's own. A name starts with a lowercase letter, then lowercase letters, digits, `-`
  or `_`, at most 32 characters. A tier argv follows the rules of `full`: it must contain
  `{junit}` and may not use `{base}`; errors name the key path (`test.tiers.<name>`). A tier run
  executes only that tier's command. An undeclared tier exits `72` naming `test.tiers.<name>`;
  a malformed name is a usage error (`1`).
- Every key is optional. Asking for a mode whose key is absent exits `72` and names the key
  (`test.files`); a missing, invalid or absent-`test` profile is refused cleanly too. The block
  is validated strictly like the rest of the profile, and `loadout-profile check` reports an
  invalid one with the key path.
- The profile is read from the working tree, because a run tests the change in front of you.
  The commands in it are run as you: run tests only in a repository you trust.
- A non-zero exit from the command counts as a failed run even when the report lists no
  failing test, and the summary's `problem` says so.

`--repo-root` names the directory that holds the profile and need not be a git work tree; in
that case the commit fields of the run are null.

## Where runs are kept

Each run is a directory named by its run id, `YYYYMMDDTHHMMSSZ-<pid>`, under the **reports
root**:

1. the `CLAGENTIC_LOADOUT_TEST_REPORTS_DIR` environment variable,
2. the user-level config (`~/.config/clagentic/loadout/config.yaml`), `test.reports_dir`,
3. `<user config root>/state/test-reports` (the user config root is
   `~/.config/clagentic/loadout`).

The reports root is created mode `0700` by `run`. A run directory holds `provenance.json`,
`result.json` (written last: a directory without it is an unfinished run that no reader sees),
`junit.xml` and `output.log`. When a run is unrunnable, `run` prints the end of the command's
output to stderr.

Every run records: the repository (top-level path, name, remote URL with any credentials
removed, or null if it cannot be parsed), the HEAD commit, whether the tree had uncommitted
changes, the base commit when one was used, the declared command, the command's exit code,
and the counts.

A test id is `<classname>::<name>` from the JUnit `testcase` (just `<name>` when there is no
class name). A case with a `failure` or `error` child is failing; one with `skipped` is
skipped.

## `test run`

Prints the run summary as JSON (`run_id`, `state`, `mode`, `tier`, `counts`, `failing_id_count`,
`command_exit_code`, `problem`, timestamps) and exits:

| Code | Meaning |
|---|---|
| `0` | Every test passed. |
| `1` | Usage error (including a bad path or an unresolvable `--base`). |
| `70` | The tests failed: a failing test, or a non-zero command exit. |
| `71` | The repository has no valid profile. |
| `72` | The profile does not declare what this run needs; the message names the key. |
| `73` | The command could not run (not found, timed out) or left no readable JUnit report. |
| `74` | The run could not be recorded under the reports root. |

The command's own exit code is recorded in the run and is never this verb's exit code.

A tier run has `mode` `tier` and `tier` set to the tier name; every other run has `tier` null.
Runs recorded before tiers existed have no `tier` field, and readers treat that as null. The
field is additive, so the interface version stays 1.

## `test report`

Read-only: it opens nothing for write and creates nothing, not even a missing reports root.
It takes run ids, never filesystem paths, so it can be allowed without a prompt (see
below).

- **Choosing the run.** `RUN_ID` (default `latest`) is untrusted input and must match the run
  id grammar before it touches a path; it is resolved directly under the reports root, and a
  run directory that is a symlink is refused. `latest` is the newest finished run.
  `--repo` narrows `latest` to one repository, by path, name or remote URL.
- **`--sha S`** selects the newest clean run recorded for exactly that commit: the full SHA or
  an unambiguous prefix of 7 or more hex characters. It never falls back to `latest`. With no
  match it exits `77` and lists the runs that exist; a run on a dirty tree, or one that
  recorded no commit ("commit unknown"), is never selected. `--repo` and `--sha` cannot be
  combined with an explicit run id.
- **`--summary`** (default): JSON with the run id, `state` (`complete` or `unrunnable`), mode,
  `tier` (the tier name for a tier run, else null), counts, the repository, `head_sha`, `dirty`, `base_sha` and `commit_known`.
- **`--failing-ids`**: the failing test ids, sorted, one per line.
- **`--diff BASE_RUN_ID`**: JSON with `head_only`, `base_only` and `shared_count` of failing
  ids, and `repo_check`. It refuses two runs proven to be from different repositories: remote
  URLs are compared when both runs recorded one, otherwise resolved paths. When only one run
  recorded a remote and the paths differ, or either run recorded no repository, `repo_check`
  is `unverified` instead of a refusal.
- `--diff` compares like with like: it refuses (exit `78`) when exactly one of the two runs is
  a tier run, or when both are tier runs of different tiers. Two runs of the same tier, and two
  non-tier runs, compare as before.
- An `unrunnable` run has no results, so `--failing-ids` and `--diff` refuse it (exit `78`)
  rather than report an empty set.

| Code | Meaning |
|---|---|
| `0` | The answer was printed. |
| `1` | Usage error. |
| `75` | The reports root does not exist, or holds no matching finished run. |
| `76` | The run id is malformed or no such run exists; the message names the id. |
| `77` | No clean run matches `--sha` (or the prefix is ambiguous). |
| `78` | Refused: `--diff` across repositories or unlike tiers, or a run without results. |

## The optional permission rule

`test report` is the one verb meant to run without an operator prompt, so the installer can
add a single allow rule for it to a Claude Code settings file. It is **off by default**:

```
scripts/install.sh --register-test-report-permission [--claude-settings-file FILE]
```

The installer is the only path that writes the rule: there is no `clagentic-loadout` verb or
subcommand for it. The installer calls an internal helper module in the installed environment
(`python -m clagentic_loadout.repo_test.permission`); it is not part of the interface.

The rule is exactly `Bash(clagentic-loadout test report *)`, added to `permissions.allow`.
Nothing else is changed: no other rule, no other key. The file is parsed as JSON and written
back with two-space indent, other contents preserved. Running it again changes nothing. A file
that is not a JSON object, whose `permissions` or `allow` has the wrong type, or that is a
symlink, is refused and left untouched, and the installer then fails.

The default settings file is `settings.json` in `$CLAUDE_CONFIG_DIR`, else `~/.claude`;
`CLAGENTIC_LOADOUT_CLAUDE_SETTINGS` or `--claude-settings-file` overrides it.
