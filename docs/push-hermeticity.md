# Push hermeticity guarantee

Back to [README.md](README.md).

This documents `loadout-push`'s (`clagentic_loadout.push.git_push` +
`clagentic_loadout.push.git_hermeticity`) **hermeticity guarantee**: what ambient git
credential machinery a credentialed push/fetch subprocess ignores, what it cannot suppress
and validates instead, the minimum git version this requires, and why the guarantee is
built to hold in any deployment environment rather than depending on the host having been
cleaned of ambient credentials.

## The operator constraint this guarantee is built against

A deployment host's ambient git configuration **may always carry a credential** — a global
`credential.helper`, a `~/.netrc` entry, an SSH identity, or (on some hosts) a system-scope
`credential.helper` in `/etc/gitconfig` — and that configuration's shape differs across
environments. A fix that assumes the host has been cleaned of such credentials addresses one
machine, not the product: `loadout-push` must ignore ambient credential machinery
**unconditionally**, on every credentialed call, regardless of what a given deployment host
happens to have configured. There is no environment in which this guarantee is allowed to
depend on the absence of an ambient credential.

## What is neutralized, and how

Every credentialed git subprocess this package spawns (`git_push_with_token` and
`git_fetch_with_token`, both built on the shared `_credentialed_git_env` envelope) runs
under:

| Mechanism | Neutralized by |
|---|---|
| `~/.netrc`, `~/.git-credentials`, `~/.gitconfig` (global scope) | `HOME` isolated to an empty, per-call temp directory |
| System-scope `credential.helper` (`/etc/gitconfig`, or wherever `git config --system` resolves to) | `GIT_CONFIG_SYSTEM=/dev/null` **and** `GIT_CONFIG_NOSYSTEM=1` (belt-and-braces: either alone should suffice on a conforming git; both are set) |
| Global-scope config as an environment override | `GIT_CONFIG_GLOBAL=/dev/null` |
| An ambient `GIT_ASKPASS` / `SSH_ASKPASS` / `GIT_SSH` / `GIT_SSH_COMMAND` the real environment happens to export | Removed from the subprocess environment entirely, **before** this package sets its own `GIT_ASKPASS` (pointing at its generated token-reading script) |
| `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_<n>` / `GIT_CONFIG_VALUE_<n>` (git 2.30+ config-injection-via-environment) | Stripped unconditionally — this package never uses this mechanism itself |
| Interactive credential prompting | `GIT_TERMINAL_PROMPT=0` |
| A command-scope `credential.helper` override, as defense-in-depth on top of the above | `-c credential.helper=""` prepended to the subprocess argv |

**Why `-c credential.helper=""` alone is not enough:** per `git-config(1)`, an empty `-c`
value sets a single command-scope override to the empty string (false, in boolean context).
`credential.helper` is **multi-valued** — git consults every configured helper, at every
scope, in order, until one supplies credentials — and a single empty command-scope entry
does not clear or shadow helpers already configured at global or system scope. The
environment-variable recipe above is required *because* the `-c` override cannot deliver the
guarantee by itself; the `-c` override is kept anyway as an additional, independent layer.

## What cannot be suppressed, and what happens instead

**Repo-local `.git/config` is always read.** Unlike global or system scope, there is no
environment variable that disables reading a repository's own `.git/config` — command-line
`-c` can override a single-valued key there, but the file itself cannot be switched off.
Three specific repo-local hazards cannot be neutralized by environment isolation at all:

- A repo-local `credential.helper` entry (also multi-valued).
- `http.<url>.extraheader` — exactly where a CI runner (GitHub Actions, GitLab, etc.)
  commonly writes a token into repo-local config.
- `includeIf.gitdir` / `includeIf.onbranch` / `includeIf.hasconfig` (git 2.13+) — can load
  arbitrary config from anywhere on the filesystem, making repo-local config a channel for
  indirection to a file that is not itself under `.git/`.

**Before either `git_push_with_token` or `git_fetch_with_token` spawns any subprocess**,
`clagentic_loadout.push.git_hermeticity.check_repo_local_config_hazards` inspects the target
repository's resolved local config (`git config --local --list`) for these three hazard
classes. **This check fails closed, with no override flag**: if any hazard is found, the
call raises `RepoLocalConfigHazardError` naming the offending config **key** (never the
value — a credential.helper's configured command, or an extraheader's carried token, is
never reproduced in a message) before any credential is resolved or any network call is
attempted. A deployment that has a legitimate reason for repo-local `http.*.extraheader`
(e.g. a CI runner's own unrelated token injection) must not route that repository through
this package's credentialed push at all — there is deliberately no bypass, because the
"ambient credentials may always exist and must never silently win" constraint applies
exactly as much to a hazard a caller insists is benign as to one it does not recognize.

## Minimum git version

`clagentic_loadout.push.git_hermeticity.check_git_version` enforces **git 2.20** as the
floor, run before any credentialed call. 2.20 is git's own introduction of protected
configuration scopes — the guarantee that a command-line `-c` override takes precedence over
repo-local config for security-sensitive keys, including `credential.helper`. (For reference:
`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` stabilized in 1.7.12+, `GIT_TERMINAL_PROMPT` in 2.3+,
`includeIf` — the hazard this package scans for — in 2.13+; 2.20 is the newest prerequisite
among these and is therefore the binding floor.) A version below this, or a `git --version`
output this check cannot parse at all, raises `GitVersionTooOldError` **naming the resolved
version this host's git actually reported** — never a stale assumption about what might be
installed (this repo's own CLAUDE.md hard rule 4: report resolved facts, not guesses).

## Token delivery is unchanged, and deliberately so

This guarantee does not change how the minted token itself reaches git: `GIT_ASKPASS`
pointed at a generated script that reads the token from a mode-`0600` temporary file remains
the sole credential-delivery mechanism. This is the correct tier and is preserved verbatim:

- An authenticated URL (`https://user:token@host`) is the **worst** option — it leaks into
  process argv (visible via `ps`, `/proc/<pid>/cmdline`), `git remote -v`, and shell history.
  This package never constructs one.
- `http.<url>.extraheader` is better (not in argv, not in `git remote -v`) but **still leaks
  into stderr on a 401/403** — one more reason the redaction guarantee documented in
  [push-failure-reporting.md](push-failure-reporting.md) is mandatory, not optional, given
  this package's own `GIT_TRACE` passthrough deliberately widens what stderr an operator can
  see.
- `GIT_ASKPASS` reading from a private temp file is the tier this package uses: no argv
  exposure, no `git remote -v` exposure, no stderr exposure under ordinary operation.

## Testing this guarantee

`tests/test_push_git_hermeticity.py` plants a genuinely hostile ambient mechanism — a
system-scope `credential.helper` supplying a sentinel credential, a repo-local
`credential.helper`/`http.*.extraheader` carrying a sentinel token, a `~/.netrc` with a
sentinel login/password, and an ambient `GIT_SSH`/`GIT_SSH_COMMAND` naming a wrapper that
writes a sentinel file if ever invoked — and asserts the sentinel is never consulted,
never sent, and never appears in any raised message. Each assertion in that suite was
verified, during this guarantee's own development, to actually **fail** when the
corresponding neutralization code is disabled — proving the tests exercise the fix itself
and are not vacuously true regardless of it (a hazard this suite's own module docstring
calls out explicitly, since an assertion that would pass either way is worse than no test at
all). There is no published, widely-used standard hermetic-git-push test harness this suite
follows; the pattern (synthetic ambient config, a sentinel side effect proving consultation,
a real local HTTP stub server for the credential-resolution paths that only fire over
HTTP(S)) is well-grounded in real git/libcurl semantics but is novel as a suite, and this
document does not claim otherwise.
