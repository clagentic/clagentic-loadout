"""push.git_hermeticity — neutralize ambient git credential machinery for
every credentialed git subprocess this package spawns (lr-a868d2).

THE DEFECT THIS CLOSES: `push.git_push._credentialed_git_env` (the shared
environment-construction primitive both `git_push_with_token` and
`git_fetch_with_token` build their subprocess call on) previously performed
HOME ISOLATION ONLY — an empty per-call HOME directory, removing
~/.netrc / ~/.git-credentials / ~/.gitconfig as ambient credential sources
for that subprocess. HOME isolation does not reach two other scopes git
reads on every invocation:
  - SYSTEM scope (/etc/gitconfig, or wherever `git config --system` resolves
    to on the host) — entirely outside HOME, unaffected by isolating it.
  - REPO-LOCAL scope (the target repo's own `.git/config`) — read
    unconditionally by git regardless of HOME or any other environment
    variable; there is no way to disable reading it (see
    `check_repo_local_config_hazards` below).
A `credential.helper` configured at either scope can still win inside a
supposedly-isolated push, because HOME isolation never touched either
scope's discoverability.

THE OPERATOR CONSTRAINT THIS DESIGN IS BUILT AGAINST: workspace credentials
may ALWAYS exist, and their shape differs across environments — a fix
assuming the host is clean, or that ambient credentials have been removed by
some other means, addresses one machine, not the product. This module
neutralizes the ambient surface unconditionally, on every credentialed call,
regardless of what the host happens to have configured.

WHY `-c credential.helper=""` ALONE DOES NOT WORK (the mistake this module
corrects — an empty `-c` override was initially assumed sufficient and is
not): per git-config(1), `-c foo.bar=` (an empty value) sets a *single*
command-scope override to the empty string / boolean false. `credential.helper`
is MULTI-VALUED — git consults every configured helper, at every scope, in
order, until one supplies credentials — and a single empty command-scope
entry does not clear or shadow helpers already configured at global or
system scope. The env-var recipe below is required BECAUSE an inline `-c`
override cannot deliver the guarantee alone.

THE RECIPE (all parts required, none sufficient alone):
  - GIT_CONFIG_GLOBAL=/dev/null — makes git treat "no global config" as
    authoritative rather than reading the real user config file.
  - GIT_CONFIG_SYSTEM=/dev/null — same, for system scope.
  - GIT_CONFIG_NOSYSTEM=1 — belt-and-braces: even if GIT_CONFIG_SYSTEM were
    somehow not honored by a given git build, this independently tells git
    to skip system config entirely.
  - GIT_TERMINAL_PROMPT=0 — already set by `_credentialed_git_env`; kept
    here in the neutralization list because it is part of the same "no
    ambient interactive-credential surface" posture (a helper that would
    otherwise prompt on a terminal must not get the chance to).
  - GIT_ASKPASS / SSH_ASKPASS / GIT_SSH removed from the subprocess
    environment entirely — NOT emptied, REMOVED, so no ambient askpass
    program or SSH wrapper the real environment happens to export can be
    consulted. `_credentialed_git_env` re-sets GIT_ASKPASS to its OWN
    generated script immediately after calling this function — this
    function's job is to strip whatever the AMBIENT environment supplied
    first, never to interfere with loadout's own subsequent, deliberate
    GIT_ASKPASS assignment.
  - GIT_CONFIG_COUNT / GIT_CONFIG_KEY_<n> / GIT_CONFIG_VALUE_<n> stripped —
    git 2.30+ reads these as a config-injection channel purely via
    environment variables; an ambient environment carrying a stray
    GIT_CONFIG_COUNT from an unrelated tool (a CI runner, a wrapper script)
    could otherwise inject a config entry (including a credential.helper)
    that neither GIT_CONFIG_GLOBAL/SYSTEM nor GIT_CONFIG_NOSYSTEM touch,
    since this is a distinct, additive config source at a higher
    precedence than any file-based scope.
  - HOME isolation to an empty per-call temp dir — UNCHANGED, still
    performed by `_credentialed_git_env` itself, not duplicated here.

NOT IN THIS RECIPE, AND WHY: `-c credential.helper=""` is layered on TOP of
this env-level neutralization by `_credentialed_git_env` itself (as a
command-scope override) — see that function's own docstring. It is not
sufficient alone (see above) but is still worth keeping as defense-in-depth
once the env-level neutralization already makes it redundant for the
multi-valued-list problem specifically.

REPO-LOCAL CONFIG CANNOT BE SUPPRESSED — THIS IS A DESIGN CONSTRAINT, NOT A
GAP LEFT UNADDRESSED: `.git/config` is read unconditionally; no environment
variable disables it (unlike global/system scope, which GIT_CONFIG_GLOBAL/
SYSTEM/NOSYSTEM above can redirect to /dev/null). Three specific repo-local
hazards cannot be neutralized by environment isolation alone:
  - `credential.helper` at repo-local scope (also multi-valued).
  - `http.<url>.extraheader` — exactly where CI runners (GitHub Actions,
    GitLab) write a token into repo-local config; leaks into stderr on a
    401/403 if not caught upstream of the redaction choke point.
  - `includeIf.gitdir` / `includeIf.onbranch` / `includeIf.hasconfig`
    (git 2.13+) — can load arbitrary config from anywhere on the
    filesystem, making repo-local config a channel for indirection to a
    config file that is not itself under `.git/`.
`check_repo_local_config_hazards` below inspects the target repo's
resolved, effective config (`git config --local --list`, which is populated
exclusively from `.git/config` — the "resolved value observed," not a
guess, per CLAUDE.md hard rule 4) for these three hazard classes BEFORE any
credentialed git subprocess runs, and this package's credentialed callers
(`push.git_push.git_push_with_token` / `git_fetch_with_token`) FAIL CLOSED
when any hazard is found — see `RepoLocalConfigHazardError`. A caller that
needs a legitimate repo-local `http.<url>.extraheader` (e.g. a CI runner's
own token injection, wholly unrelated to this package's minted-token path)
must not route that repo through this package's credentialed push at all;
there is no bypass flag, because the operator constraint this module is
built against — ambient credentials may always exist, and must never be
allowed to silently win — applies exactly as much to a hazard a caller
insists is benign as to one it does not recognize.

MINIMUM GIT VERSION: 2.20, required for git's own "protected configuration"
guarantee — that a command-line `-c` override takes precedence over
repo-local config for security-sensitive keys including `credential.helper`.
`GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` stabilized in git 1.7.12+;
`GIT_TERMINAL_PROMPT` in 2.3+; `includeIf` (the hazard this module scans
for) in 2.13+ — 2.20 is the binding floor because it is the newest
prerequisite among these. `check_git_version` reports the RESOLVED version
string this host's `git` actually returned (CLAUDE.md hard rule 4) — never a
stale assumption about what version might be installed.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

#: Minimum git version this package requires for its hermeticity guarantee
#: to hold (see module docstring, MINIMUM GIT VERSION).
MIN_GIT_VERSION: tuple[int, int] = (2, 20)

#: Env vars carrying an ambient askpass/SSH-wrapper program this module
#: strips before a caller lays its own deliberate GIT_ASKPASS on top (see
#: module docstring). GIT_SSH_COMMAND is included alongside GIT_SSH: either
#: can name an ambient SSH wrapper/agent-forwarding configuration this
#: package's HTTP(S)-token push path has no use for and must not inherit.
_AMBIENT_ASKPASS_AND_SSH_ENV_VARS = (
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
)

#: git 2.30+ config-injection-via-environment channel (see module docstring)
#: — GIT_CONFIG_COUNT plus every GIT_CONFIG_KEY_<n>/GIT_CONFIG_VALUE_<n> pair
#: it declares. Stripped unconditionally: this package never uses this
#: mechanism itself, so any occurrence in the ambient environment is by
#: definition something OTHER than this package injected.
_GIT_CONFIG_INJECTION_RE = re.compile(r"^GIT_CONFIG_(COUNT|KEY_\d+|VALUE_\d+)$")

#: Repo-local config key prefixes that constitute a hermeticity hazard (see
#: module docstring, REPO-LOCAL CONFIG CANNOT BE SUPPRESSED). Matched against
#: the KEY half of each `git config --local --list` line (before the `=`).
_HAZARD_KEY_PREFIXES = ("credential.", "includeif.")

#: `http.<url>.extraheader` is a THIRD hazard shape, but its key is not a
#: fixed prefix -- the URL segment varies per remote. Matched as "starts
#: with http., ends with .extraheader", case-insensitively (git config keys
#: are case-insensitive for the section/variable name).
_HTTP_EXTRAHEADER_RE = re.compile(r"^http\..*\.extraheader$", re.IGNORECASE)


class GitVersionTooOldError(Exception):
    """Raised when the resolved `git --version` is below MIN_GIT_VERSION.

    Message reports the RESOLVED version string this host's git actually
    printed (CLAUDE.md hard rule 4) -- never a stale guess about what might
    be installed.
    """


class RepoLocalConfigHazardError(Exception):
    """Raised when the target repo's LOCAL `.git/config` carries one of the
    three unsuppressable hermeticity hazards (see module docstring):
    a repo-local `credential.*` entry, an `http.<url>.extraheader` entry, or
    an `includeIf.*` directive. Fail-closed by design -- see module
    docstring for why there is no override flag."""


def _parse_git_version(version_output: str) -> tuple[int, int, int] | None:
    """Parse `git --version`'s stdout into a (major, minor, patch) tuple.

    Returns None if the output does not match git's own documented
    "git version X.Y.Z" shape (e.g. a corrupted PATH pointing at a
    non-git binary) -- callers treat an unparseable version as a hard
    failure, never silently proceed as if the check passed.
    """
    match = re.search(r"git version (\d+)\.(\d+)(?:\.(\d+))?", version_output)
    if not match:
        return None
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch) if patch is not None else 0)


def check_git_version(*, git_cwd: Path | None = None) -> tuple[int, int, int]:
    """Verify the `git` binary on PATH meets MIN_GIT_VERSION.

    Runs `git --version` (no repository state touched, no network call) and
    parses the RESOLVED version this host actually reports. Raises
    GitVersionTooOldError naming that resolved version (never a guess) when
    it is below MIN_GIT_VERSION, or when the output could not be parsed at
    all (a version this function cannot confirm is safe is never treated as
    passing).

    Returns the resolved (major, minor, patch) tuple on success, so a caller
    that wants to log/report it does not need to re-invoke `git --version`
    itself.
    """
    result = subprocess.run(
        ["git", "--version"],
        capture_output=True,
        text=True,
        cwd=str(git_cwd) if git_cwd is not None else None,
    )
    resolved = _parse_git_version(result.stdout)
    if resolved is None:
        raise GitVersionTooOldError(
            f"could not parse a git version from `git --version` output "
            f"(resolved stdout: {result.stdout.strip()!r}, exit "
            f"{result.returncode}) -- refusing to proceed with a version "
            f"this check cannot confirm meets the minimum "
            f"{'.'.join(str(p) for p in MIN_GIT_VERSION)} this package's "
            f"hermeticity guarantee requires (protected-configuration scopes "
            f"ensuring a command-line -c override takes precedence over "
            f"repo-local config for security-sensitive keys)."
        )
    if resolved[:2] < MIN_GIT_VERSION:
        raise GitVersionTooOldError(
            f"resolved git version {'.'.join(str(p) for p in resolved)} is "
            f"below the minimum {'.'.join(str(p) for p in MIN_GIT_VERSION)} "
            f"this package's hermeticity guarantee requires (protected-"
            f"configuration scopes ensuring a command-line -c override "
            f"takes precedence over repo-local config for security-"
            f"sensitive keys, including credential.helper). Upgrade git on "
            f"this host before retrying."
        )
    return resolved


def neutralize_ambient_git_env(env: dict[str, str]) -> dict[str, str]:
    """Return a COPY of *env* with the ambient credential-machinery surface
    neutralized (module docstring, THE RECIPE) -- global/system config
    scope redirected to /dev/null, system config reading independently
    disabled, any ambient askpass/SSH-wrapper env vars removed, and any
    git-config-injection-via-environment channel stripped.

    Does NOT touch HOME (the caller, `push.git_push._credentialed_git_env`,
    isolates HOME itself and calls this function alongside that, not as a
    replacement for it) and does NOT set GIT_ASKPASS to anything -- the
    caller sets its OWN GIT_ASKPASS (pointing at loadout's generated
    token-reading script) immediately after calling this function; this
    function's job is only to strip whatever ambient value was present
    first, so loadout's own subsequent assignment is never shadowed or
    raced by an environment key this function left behind.
    """
    neutralized = dict(env)
    neutralized["GIT_CONFIG_GLOBAL"] = "/dev/null"
    neutralized["GIT_CONFIG_SYSTEM"] = "/dev/null"
    neutralized["GIT_CONFIG_NOSYSTEM"] = "1"
    neutralized["GIT_TERMINAL_PROMPT"] = "0"
    for key in _AMBIENT_ASKPASS_AND_SSH_ENV_VARS:
        neutralized.pop(key, None)
    for key in list(neutralized):
        if _GIT_CONFIG_INJECTION_RE.match(key):
            del neutralized[key]
    return neutralized


def check_repo_local_config_hazards(git_cwd: Path | None = None) -> tuple[str, ...]:
    """Inspect the target repo's LOCAL config (`git config --local --list`,
    populated exclusively from `.git/config` -- never global/system scope)
    for the three unsuppressable hermeticity hazards (module docstring):
    a repo-local `credential.*` entry, an `http.<url>.extraheader` entry, or
    an `includeIf.*` directive.

    Returns a tuple of hazard descriptions (KEY only, never the VALUE --
    a credential.helper's configured command, or an extraheader's carried
    token, is not this function's to reproduce in a message any caller
    might print) -- empty when no hazard is found. `git config --local
    --list` exiting non-zero (e.g. no `.git/config` at all, a bare
    repository) is NOT a hazard -- it means there is nothing repo-local to
    find, and this function returns an empty tuple rather than treating an
    absent file as suspicious.

    This function INSPECTS ONLY -- it does not raise. Callers (see
    push.git_push._credentialed_git_env) decide the fail-closed policy and
    raise RepoLocalConfigHazardError with these descriptions folded in.
    """
    result = subprocess.run(
        ["git", "config", "--local", "--list"],
        capture_output=True,
        text=True,
        cwd=str(git_cwd) if git_cwd is not None else None,
    )
    if result.returncode != 0:
        return ()
    hazards: list[str] = []
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key = line.split("=", 1)[0].strip().lower()
        if key.startswith(_HAZARD_KEY_PREFIXES) or _HTTP_EXTRAHEADER_RE.match(key):
            hazards.append(key)
    return tuple(hazards)


__all__ = [
    "MIN_GIT_VERSION",
    "GitVersionTooOldError",
    "RepoLocalConfigHazardError",
    "check_git_version",
    "check_repo_local_config_hazards",
    "neutralize_ambient_git_env",
]
