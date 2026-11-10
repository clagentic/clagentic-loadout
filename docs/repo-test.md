# Running a repository's tests: `clagentic-loadout test`

Back to [README.md](README.md).

`clagentic-loadout test` runs the tests a repository declares and lets anyone read the
results by run id. Running tests and reading results are the same for every repository, so
the verb takes its commands from the repository's [profile](repo-profile.md) rather than from
a script that lives with one deployment.

Interface version: **1**. The JSON field names, the run id grammar, the test id format and the
exit codes below are the contract; a change to any of them is a breaking change.

```
clagentic-loadout test run full|impacted [--repo-root DIR] [--base REV] [--timeout SECONDS] [--detach]
clagentic-loadout test run files PATH... [--repo-root DIR] [--timeout SECONDS] [--detach]
clagentic-loadout test run tier NAME [--repo-root DIR] [--timeout SECONDS] [--detach]
clagentic-loadout test report [RUN_ID|latest] [--repo R] [--sha S] [--wait SECONDS]
                              [--summary | --failing-ids | --failures | --diff BASE_RUN_ID]
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
- The report path is in a per-run scratch directory outside the run record, mode `1733`
  (others can enter and create files but not list them; the sticky bit stops anyone but a
  file's owner removing it). Loadout does not create the report: the command does, so the file
  belongs to whichever user the command wrote it as, which lets a command that switches to
  another unprivileged user still produce one. After the command ends, the report is refused
  (the run is `unrunnable`, `problem` says why, nothing is stored) when it is not a regular
  file with one link, is writable by any user, or is owned by a user that is neither the
  caller nor one the command's processes were seen running as. A verified report is recorded as
  `report_verification` (`status`, `owner_uid`, `command_uids`) in the run summary. Remaining
  risk: a report that is group-writable is accepted, and a user id is not seen when a process
  held it for less than the sampling interval, which refuses rather than trusts. Processes
  already seen are re-checked about every millisecond. The host-wide search for new processes
  in the command's session starts at every 4 ms and backs off to every 100 ms while nothing new
  appears, resetting on any change, so a process that is started and switches user within one
  such interval, while nothing else changes, can be missed. A path in the scratch directory that
  is not a regular file (for example a named pipe) is refused without being read.
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
`result.json` (written last: a directory without it is an unfinished run that `latest` and
`--sha` never select; a detached run is still readable by its run id, see below),
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
`command_exit_code`, `problem`, `repo` (name, path, remote URL), `head_sha`, `dirty`, timestamps)
and exits. It also prints one line to stderr before the command starts and one after it ends,
each naming the run id, repository name and path, head SHA (`unknown` outside a git work
tree) and `dirty`. Once the first line has printed, the second always prints, including when
the run is then refused (for example the result cannot be recorded): it reads
`finished (refused, exit <code>)` and the exit code is unchanged.

- `--expect-repo VALUE` refuses (exit `78`) unless the tree to test is that repository, matched
  like `test report --repo` (path, name, remote URL). `--expect-head SHA` refuses (exit `78`)
  unless HEAD is that commit (full SHA or a prefix of 7 or more hex characters), and also when
  the head is unknown. A refusal names the expected and the actual value and happens before the
  command runs and before any run is recorded.

| Code | Meaning |
|---|---|
| `0` | Every test passed. |
| `1` | Usage error (including a bad path or an unresolvable `--base`). |
| `70` | The tests failed: a failing test, or a non-zero command exit. |
| `71` | The repository has no valid profile. |
| `72` | The profile does not declare what this run needs; the message names the key. |
| `73` | The command could not run (not found, timed out) or left no readable JUnit report. |
| `74` | The run could not be recorded under the reports root. |
| `78` | Refused: the tree is not the repository or commit given by `--expect-repo` / `--expect-head`. |

With `--detach`, the table applies to the checks that run before the start (`1`, `71`, `72`, `74`,
`78`, and `73` when the supervisor process cannot be started); once the run has started the
exit code is `0`. See "Detached runs".

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
  `--repo` narrows `latest` to one repository, by path, name or remote URL. Without `--repo`,
  `latest` is scoped to the git repository holding the current directory (stated on stderr);
  when the current directory is in no git work tree and runs are on record for more than one
  repository, it exits `78` naming them and asking for `--repo`. With one repository on record
  it returns that repository's newest run. `--sha` is scoped the same way.
- **`RUN_ID --repo R`** asserts the run belongs to repository `R` (same matching as above) and
  exits `78`, naming both, when it does not.
- **`--sha S`** selects the newest clean run recorded for exactly that commit: the full SHA or
  an unambiguous prefix of 7 or more hex characters. It never falls back to `latest`. With no
  match it exits `77` and lists the runs that exist; a run on a dirty tree, or one that
  recorded no commit ("commit unknown"), is never selected. `--sha` cannot be combined with
  an explicit run id.
- **`--summary`** (default): JSON with the run id, `state` (`complete`, `unrunnable`, or for a detached run `running` or `interrupted`), mode,
  `tier` (the tier name for a tier run, else null), counts, `id_scheme` (the failing-id scheme the run
  was recorded with; null for a run recorded before it existed, and `id_basis` is added when the record
  has one), the repository, `head_sha`, `dirty`, `base_sha` and `commit_known`.
- **`--failing-ids`**: the failing test ids, sorted, one per line. One line per failed test
  case, so an id that failed twice appears twice and the line count equals `counts.failed`.
- **`--failures`**: a JSON list with one record per failed test case, in report order (two
  failed cases that share an id each get a record). A record has `id`, `kind` (`failure` or
  `error`), `message` (the report's `message` attribute) and `excerpt` (the element text).
  Each value is redacted whole (URL userinfo, `Authorization` values, bearer and `token=`
  literals, terminal escapes and control characters) and only then cut: at most 1000
  characters of message, 4000 of excerpt per case and 40000 of text in total. The `id` is cut
  the same way, to at most 1000 characters. A cut value (id, message or excerpt) ends with
  `...[truncated: N characters dropped]`. A value of more than 8 MiB (8388608 characters)
  is never read: its text is replaced by `[omitted: value of N characters exceeds the 8 MiB
  redaction ceiling]`. When the total cap is reached the list ends with one record that has no
  `id`, only `omitted_cases` and `note`. A run with no failed case prints `[]`. An `unrunnable`
  or `interrupted` run, or one whose recorded report cannot be read, is refused (exit `78`); a
  `running` run exits `79`.
- **`--diff BASE_RUN_ID`**: JSON with `head_only`, `base_only` and `shared_count` of failing
  ids, and `repo_check`. Ids are compared as a multiset: an id that failed more often at head
  than at base is in `head_only` once per extra failure. A run recorded before per-case ids
  holds one entry per name; comparing it with a newer run compares presence only and adds
  `id_basis: "collapsed"` with a reason, and a `--summary` of such a run carries a
  `failing_id_note` when its counts differ. It refuses two runs proven to be from different repositories: remote
  URLs are compared when both runs recorded one, otherwise resolved paths. When only one run
  recorded a remote and the paths differ, or either run recorded no repository, `repo_check`
  is `unverified` instead of a refusal.
- `--diff` compares like with like: it refuses (exit `78`) when exactly one of the two runs is
  a tier run, or when both are tier runs of different tiers. Two runs of the same tier, and two
  non-tier runs, compare as before.
- An `unrunnable` run has no results, so `--failing-ids`, `--failures` and `--diff` refuse it (exit `78`)
  rather than report an empty set.

| Code | Meaning |
|---|---|
| `0` | The answer was printed. |
| `1` | Usage error. |
| `75` | The reports root does not exist, or holds no matching finished run. |
| `76` | The run id is malformed or no such run exists; the message names the id. |
| `77` | No clean run matches `--sha` (or the prefix is ambiguous). |
| `78` | Refused: `--diff` across repositories or unlike tiers, a run without results, `RUN_ID --repo` naming another repository, or `latest` outside git with several repositories on record. |
| `79` | The run is still running: `--wait` timed out, or `--failing-ids` / `--failures` / `--diff` was asked of a running run. |

The code `79` is also the installer helper's code for a settings file it could not read or write
(see below). That helper is a separate process the installer calls, never a `test report`
invocation, so a caller of this verb only ever sees the meaning above.

## Detached runs

A full suite can take longer than a caller is allowed to keep one command open. `--detach`
splits the run from the call:

```
clagentic-loadout test run full --detach --expect-head <sha>
clagentic-loadout test report <run-id> --wait 540
```

- `test run ... --detach` makes every check a foreground run makes, with the same exit codes
  (arguments, `--repo-root`, `--expect-repo`, `--expect-head`, the dirty-tree and commit
  record, the profile's command): nothing is started or recorded when one fails. It then starts
  a supervisor process in a new session, with no stdin and its output going to the run's
  `output.log`, so it keeps running when the caller exits or the caller's process group is
  killed. It prints one JSON object (`run_id`, `state: "running"`, `mode`, `tier`, `repo`,
  `head_sha`, `dirty`, `pid`, `started_at`, `run_dir`) and exits `0`. The run id is bound to
  the head and `dirty` recorded at that moment.
- The supervisor runs the command the way a foreground run does: the same per-run report
  directory, the same sampling of the command's user ids, the same report verification. A
  detached run is held to the report trust checks above, unchanged.
- The run's `provenance.json` gains a `process` object: the supervisor's pid, its start time
  (clock ticks since boot) and the boot id, plus the mode, tier, command and start time. A pid
  that has been reused, or the same pid after a reboot, is not the run. The run id keeps its
  grammar; its pid is the caller's, and `process.pid` is the supervisor's.
- Without `--detach`, the output and exit codes of `test run` are unchanged.

### States

`test report RUN_ID` reads one of four states. The first two exist only for detached runs.

| State | Meaning |
|---|---|
| `running` | The run has no `result.json` and its supervisor process is alive. `counts` is null. |
| `interrupted` | The run has no `result.json` and its supervisor process is gone (killed, or the host restarted). `counts` is null, `problem` says so. |
| `complete` | As before. |
| `unrunnable` | As before. |

An interrupted read is written once as `result.json` (atomically), so every later read gives the
same answer, including its `finished_at`. It is written only into a run directory owned by the
reader. `interrupted` is terminal. The command a killed supervisor started may still be running
in its own session; loadout does not stop it.

`--summary` of a running or interrupted run prints that state and exits `0`. `--failing-ids`,
`--failures` and `--diff` of a running run exit `79` and name the state; of an interrupted run they exit `78`,
like an unrunnable run. `latest` and `--sha` are unchanged: they select finished runs, and an
interrupted run, which has no result, is never selected by them, so what they pick does not
depend on whether anyone has read the interrupted run yet. Read it by its run id.

### `--wait SECONDS`

`test report RUN_ID --wait SECONDS` re-reads the run about twice a second until it is terminal
(`complete`, `unrunnable` or `interrupted`) or `SECONDS` have passed, then prints what it would
have printed without `--wait`. It exits `79` only when the run is still running at the end of
the wait; any other exit code is the one `test report` gives without `--wait`. `SECONDS` is a
number of zero or more, and is capped only by the caller; for an agent whose tool calls are
limited, use less than 600.

### Agent recipe

```
clagentic-loadout test run full --detach --expect-head <sha>      # prints the run id, exits 0
clagentic-loadout test report <run-id> --wait 540                 # repeat while the exit code is 79
clagentic-loadout test report <run-id> --summary                  # state, counts, head_sha, dirty
```

No call is open for more than the `--wait` given. Repeat the second line until its exit code is
not `79`; `complete` with `failed: 0` and `dirty: false` at the expected `head_sha` is a passing
run. `interrupted` means the run must be started again.

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
