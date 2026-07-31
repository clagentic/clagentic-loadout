# Credential provider seam — full reference

This is the detailed reference for `transport.credential_provider`'s `TokenProvider`
seam: the argv-substitution grammar for `CommandTokenProvider`, the option-injection
guard, per-platform selection precedence, and the protocol-compatibility trade-off behind
the optional `repo` context. See the README's "Credential provider seam" section for the
short version (the one-seam concept, the standalone-vs-minted split, and the env-var
selection example) — this document is where that section's detail relocated to, so
nothing here is new, only expanded.

## `TokenProvider` and its two built-in implementations

`credential_provider.py` ships a `TokenProvider` protocol plus two concrete providers
that need no external minting service imported into this package:

- **`StaticTokenProvider`** — reads a role-scoped `.env` file on disk
  (`CLAGENTIC_LOADOUT_GIT_HOST_TOKEN`, mode-600 enforced). The standalone fallback,
  unchanged default behavior. Forgejo works fully standalone this way: drop a static
  personal access token into the role's `.env` file and every Forgejo-path verb (`push`,
  `review post`, `merge`, `git-host-api`) works with no other moving parts.
- **`CommandTokenProvider`** — the git-credential-helper pattern: point `clagentic: loadout`
  at a command instead of writing a `TokenProvider` implementation. It execs a
  deployment-configured argv (`shell=False`, always), reads the token from its stdout
  (trailing newline stripped), and fails closed — a distinct, resolved-values error,
  never the token itself — on a nonzero exit, empty stdout, or oversize stdout. This is
  how a deployment plugs an existing credential-minting process (an internal
  secret-broker self-fetch, a GitHub-App-style installation-token mint, or anything else
  that prints a token to stdout) into the seam with zero product code.

GitHub's App-token path needs a minting provider: the GitHub backends authenticate as a
GitHub App using short-lived, installation-scoped tokens for correct bot attribution, and
an installation token has to be *minted*, not just read from a static file.
[clagentic: gatekeeper](https://github.com/clagentic/clagentic-gatekeeper) is the
reference implementation of that minting provider — or bring your own, implementing the
same one-method `TokenProvider` protocol. Nothing in `clagentic: loadout` imports gatekeeper or any
other minting service; it is named only in this document and the module's docstring as
an example of *a* command a deployment might configure.

## Optional repo context for repo-scoped minting (`{repo}`)

Some minting processes are repo-scoped — a GitHub App installation token, for example, is
minted for one specific `owner/repo` and cannot be minted correctly without knowing
which. `resolve_token(role, provider, *, repo=None)` carries that context end to end:
every landed verb (`push`, `review post`, `merge`) resolves the target `owner/repo`
before it calls this seam anyway (namespace guards, platform guards, and the merge gate
all need it too) and passes it straight through; `git-host-api` derives it from the
request path when the path is repo-scoped, and passes `None` otherwise (e.g.
`/api/v1/user`).

A configured `CommandTokenProvider` command opts in by including a `{repo}` argv
placeholder — e.g. `mint --role {role} --repo {repo}` — substituted with the resolved
`"owner/repo"` string at call time, the same argv-token-only, never-a-shell-string
substitution `{role}` already uses. A command with **no** `{repo}` placeholder behaves
byte-identically to before this feature, regardless of whether a caller happens to have
repo context available. A command **with** `{repo}` invoked when no repo context is
available fails closed with a resolved-values `CredentialProviderError` — it never execs
the command with a literal, unsubstituted `{repo}` string.

**Protocol-compatibility trade-off, named:** a keyword-only `repo` parameter with a
default is chosen over introducing a new "call context" object. A context object would
need its own versioning story the moment a second piece of context shows up, and would
force every existing custom `TokenProvider` implementation — including ones outside this
package this seam cannot see — to accept it or break. The module-level `resolve_token()`
function inspects the resolved provider's `resolve_token` SIGNATURE (`inspect.signature`,
a one-time, deterministic check), not a fragile try/except-and-retry, to decide whether
it can accept a `repo` keyword at all, and forwards `repo=` only when it can. Every
pre-existing custom `TokenProvider` implementation with the old `resolve_token(self,
role)` signature is therefore called exactly as before this feature existed —
`provider.resolve_token(role)`, zero keyword arguments added — regardless of whether the
calling verb happens to have repo context to offer. See
`transport.credential_provider`'s module docstring for the full trade-off statement,
including why a signature-blind unconditional `resolve_token(role, repo=repo)` and a
try/except-TypeError-and-retry were both rejected.

## Argv-level option-injection guard on `{role}`/`{repo}` substitution

`shell=False` already rules out shell-metacharacter injection, but a substituted value
starting with `-` is not a shell metacharacter — it is a normal argv byte a
getopt/argparse-style CLI (the deployment's own minting command) would parse as the start
of a flag. Both `role` and `repo` are validated against a bare-token grammar (mirroring
`git_host_api`'s own `--caller` validation) BEFORE `CommandTokenProvider` ever substitutes
them:

- `role` must match `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$` (the same grammar
  `StaticTokenProvider`'s role-scoped `.env` lookup already enforces).
- `repo` must match `^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$` (two
  segments, each starting with an alphanumeric character — never a leading `-` or `.` —
  separated by exactly one `/`).

This is enforced at the provider itself, not only at whichever verb call site happens to
pre-validate its own input, since not every verb's `--caller`/`--role` does.

## Per-platform provider selection

Forgejo and GitHub each name their own provider **independently** — a deployment can
point Forgejo at one minting process and GitHub at a different one, and every verb's
token resolution goes through a single shared factory
(`transport.provider_config.resolve_platform_provider`) so this rule lives in exactly one
place.

Selection precedence per platform, highest first:

1. Env var: `CLAGENTIC_LOADOUT_TOKEN_PROVIDER_FORGEJO` / `_GITHUB` = `static` |
   `command`, plus `CLAGENTIC_LOADOUT_TOKEN_COMMAND_FORGEJO` / `_GITHUB` for the
   command's argv string (consulted only when the provider kind is `command`).
2. Config file: **user-level** `~/.config/clagentic/loadout/config.yaml`'s
   `credentials:` section — `token_provider_forgejo` / `token_provider_github` (`static`
   | `command`) and `token_command_forgejo` / `token_command_github` (the argv string).
   This is the same user config root every other `clagentic: loadout` convention uses — **never** a
   repo-local file.
3. Default: `static` — unchanged behavior; a deployment that configures nothing keeps
   working exactly as before this feature landed.

A repo-local `.clagentic/loadout/config.yaml`'s `credentials:` section is **never** read
for provider selection, and its presence is rejected with a warning to stderr rather than
silently ignored. Letting a cloned repo's own committed config name the command the
credential factory execs would be arbitrary command execution via a hostile repo's
checked-in config — the same attack class as an untrusted repo's committed
`.vscode/settings.json` naming a task's shell command. Configure the credentials tier at
the user level, or via the env vars above.

The command argv string (env var or config file) is split with Python's `shlex.split` —
standard POSIX shell quoting rules apply (quote an argument containing spaces) — but the
result is passed straight to `subprocess.run(..., shell=False)`; no shell ever interprets
it.

Example (today's typical split: Forgejo self-fetches, GitHub goes through a minting
command):

```sh
export CLAGENTIC_LOADOUT_TOKEN_PROVIDER_FORGEJO=command
export CLAGENTIC_LOADOUT_TOKEN_COMMAND_FORGEJO="/path/to/forgejo-self-fetch.sh"

export CLAGENTIC_LOADOUT_TOKEN_PROVIDER_GITHUB=command
export CLAGENTIC_LOADOUT_TOKEN_COMMAND_GITHUB="/path/to/mint-github-token.sh"
```

## Convergence rationale: one minting command for both platforms

Rationale this design preserves: when a forge platform's own next major version lets one
minting service handle both platforms, moving both to the same command is a config-only
change (point both env vars/config keys at the same command) — zero code, because
selection has always treated the two platforms as independent config, never as a
hardcoded pairing.

The `{repo}` context above is exactly the piece this convergence needed: a
minting command that is repo-scoped can only stand in for both platforms'
`credentials:` config once it can resolve a token FOR a specific `owner/repo`, not just a
role. A future forge-platform major version ("v16" in today's roadmap terms) that lets
one minting service issue tokens for both Forgejo and GitHub repos is, at that point, a
pure config change here too — set both `token_command_forgejo` / `token_command_github`
(or the env var equivalents) to the same `{role}`/`{repo}`-templated command — with zero
code changes in `clagentic: loadout`, because every verb already resolves and passes `owner/repo`
through this seam today, independently of which provider kind is configured.
