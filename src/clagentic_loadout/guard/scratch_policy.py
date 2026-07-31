"""guard.scratch_policy — category-grant spawn-scratch containment (lr-5a8d,
task comment #2, operator-driven; supersedes the mkdir-symptom framing).

PROBLEM this replaces: a per-spawn agent doing ordinary, documented-correct
scratch setup (e.g. ``mkdir -p $TMPDIR/work``, then ``mv``/``touch``/
``mktemp`` inside it) hits an operator permission prompt for every verb an
enumerated allowlist has not individually blessed yet. Enumerating one
benign scratch verb at a time is an unbounded operator-interrupt stream and
structurally defeats autonomous operation — worse in a released, downstream-
deployed tool, where every deployment inherits the same gap.

THE FIX: a CATEGORY grant by target-path CONTAINMENT, not a verb list. Any
shell command whose entire write/effect is confined to a spawn-isolated
scratch root ($TMPDIR/ only — see "TMPDIR-ONLY NARROWING" below) is admitted
REGARDLESS OF VERB. The instant any argument's target resolves outside
that root, the command is denied — never partially admitted.

TMPDIR-ONLY NARROWING (lr-f8649f, operator directive 2026-07-28):
``SCRATCH_ROOT_ENV_VARS`` previously admitted BOTH ``$TMPDIR`` and ``$HOME``
as sanctioned scratch-staging roots. ``$HOME`` is now DROPPED — the
justification for treating it as a scratch-staging root (an earlier
`--body-file` design that needed an existing, addressable filesystem
location outside `$TMPDIR`) no longer holds: PR #136's `--create-branch`
staging path closed that gap, and no other caller depends on `$HOME` as a
scratch-write target. `$HOME` remains the per-spawn process-identity
directory; it is simply no longer an admitted scratch-staging REDIRECT
TARGET or `is_scratch_contained` boundary. This mirrors an identical
narrowing observed against a Forgejo deployment — see
`resolve_scratch_boundary`'s own docstring for the unset-``TMPDIR`` decision
this narrowing required.

This is deliberately NARROW in what it classifies: it answers exactly one
question, "does every filesystem-shaped argument in this command line
resolve under a scratch root," for a single, unpiped, uncompounded command.
It has no opinion on git/push/merge/network/repo-tree verbs — those stay on
the narrow ENUMERATED allowlist a caller maintains separately (this
function is not that surface; see module docstring's "NOT" list below).

NOT covered by this module (task comment #2, point 2 — the enumerated
allowlist is retained ONLY for this surface, scratch is explicitly not it):
  - git operations of any kind
  - push / merge / release verbs
  - any network call
  - any write that targets the repo tree (project root), even if the repo
    tree is coincidentally also writable

SINGLE SOURCE, BOTH SINKS (task comment #2, point 3): `is_scratch_contained`
is the one function both a guard-layer Bash pre-check (a caller's own
PreToolUse hook) and a settings/allowlist-fragment generator
(`guard.settings_export`) must call — never two independently-maintained
copies of "what counts as scratch." A caller's guard hook and its outer
harness-level permission classifier reading from two different sources is
exactly the failure class that produced the original operator-prompt
symptom (task comment #2's OBSERVED section).

NOT a validator for a verb's own in-process file reads (lr-e1e2fb,
security-audit finding on PR #136 -- design correction): an earlier fix
attempt added a `guard.scratch_policy`-backed containment check
(`is_path_under_scratch_boundary`, since removed) for a verb-level
caller-supplied `--body-file PATH` argument. That flag was itself REJECTED
after a security audit and an explicit operator correction: a validated
arbitrary path still ACCEPTS a location parameter, and every containment
check is one canonicalization edge case, one symlink race, one future
refactor away from a bypass. The fix that shipped instead (see
`transport.body_env`'s module docstring, "CREATE-MODE STAGING") removes the
location-parameter surface entirely rather than validating it -- this
module's containment logic stays scoped to its original purpose (guard-hook
shell-command classification), not a general-purpose "is this caller path
safe" check other modules reach for.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass

#: Env var NAMES this module treats as scratch roots — never a literal path.
#: lr-f8649f: narrowed from `("TMPDIR", "HOME")` to `TMPDIR`-only (operator
#: sign-off; see module docstring "TMPDIR-ONLY NARROWING"). `HOME` is no
#: longer a sanctioned scratch-staging root at all.
SCRATCH_ROOT_ENV_VARS: tuple[str, ...] = ("TMPDIR",)

#: Argv[0] tokens this module treats as pure filesystem-mutation verbs safe
#: to classify by target-path containment alone. Deliberately excludes any
#: verb with git/network/process-execution semantics (a command that can
#: itself spawn a further command, or reach the network, is never scratch-
#: safe merely because one of its arguments happens to look like a $TMPDIR
#: path) — see module docstring's "NOT covered" list. This is an allow-set
#: for WHICH VERBS may be considered at all; whether a specific invocation
#: is admitted still depends entirely on target-path containment below.
SCRATCH_SAFE_VERBS: frozenset[str] = frozenset(
    {
        "mkdir",
        "touch",
        "mv",
        "cp",
        "rm",
        "mktemp",
        "rmdir",
        "ln",
        "chmod",
    }
)

#: Flags for SCRATCH_SAFE_VERBS whose value is itself an option, never a
#: filesystem target — skipped when walking argv for target-shaped tokens,
#: so e.g. ``chmod 0600 $TMPDIR/x`` correctly classifies only the path
#: token, not "0600", as a target to resolve.
_NON_PATH_LEADING_TOKEN_PREFIXES: tuple[str, ...] = ("-",)


class ScratchContainmentError(ValueError):
    """Raised by `is_scratch_contained` when the command cannot be
    classified at all (empty, compound/piped, or an unsafe verb) — a
    caller's guard hook treats this the same as "not admitted" but the
    distinct exception lets a caller log WHY, rather than folding every
    denial into a single undifferentiated boolean."""


@dataclass(frozen=True)
class ScratchBoundary:
    """A resolved scratch root: the env var name that named it and its
    canonicalized (`os.path.realpath`) absolute path."""

    env_var: str
    resolved_path: str


def _uid_home_fallback() -> str | None:
    """Resolve the process's real uid home directory to back the `TMPDIR`
    scratch boundary when `$TMPDIR` itself is unset or empty.

    lr-f8649f DELIBERATE DECISION (operator directive, dropping `HOME` from
    `SCRATCH_ROOT_ENV_VARS`): with `HOME` no longer a scratch root at all, an
    unset/empty `$TMPDIR` would otherwise yield ZERO resolvable boundaries —
    every scratch-safe verb invocation fails closed with no boundary to
    check against, even though a spawn always has SOME real, per-spawn
    filesystem location the OS can name. Rather than inventing a novel
    "uid-tmp" concept, this function is REPOINTED (not removed) to back
    `TMPDIR`'s own unset-fallback case, reusing the exact same uid-passwd-
    database directory the reference `HOME` fallback used to consult —
    mirroring an identical narrowing observed against a Forgejo deployment's
    own guard-hook implementation: "there is no separate 'uid-tmp' concept in
    POSIX; reusing the uid-home directory as the $TMPDIR fallback boundary is
    deliberate — it is still a machine-identity fact (not attacker-controlled
    input)." This module aligns with that posture rather than choosing a
    third, divergent behavior (e.g. fail-closed-with-error) for the same
    unset-TMPDIR question that deployment already answered.

    Returns None if the uid has no home directory entry (e.g. a stripped
    container image) — that is a hard "cannot resolve," not a silent
    permissive default; `resolve_scratch_boundary("TMPDIR", ...)` then
    correctly returns no boundary at all rather than fabricating one.
    """
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_dir or None
    except (KeyError, ImportError, OSError):
        return None


def resolve_scratch_boundary(
    env_var: str, *, env: dict[str, str] | None = None
) -> ScratchBoundary | None:
    """Resolve *env_var* (in practice, always ``"TMPDIR"`` as of lr-f8649f —
    see `SCRATCH_ROOT_ENV_VARS`) to a canonicalized `ScratchBoundary`.

    *env* overrides `os.environ` for tests.

    lr-f8649f: an empty/unset `$TMPDIR` now falls back to the process's real
    uid home directory (`_uid_home_fallback`, repointed from the removed
    `HOME`-boundary fallback — see that function's own docstring for the
    DELIBERATE DECISION this represents) rather than resolving to no
    boundary at all. This mirrors a spawn's own real, per-process-identity
    filesystem location — a fact about the machine the process runs on, not
    attacker-controlled input — matching an identical fallback shape observed
    against a Forgejo deployment. If the uid genuinely has no resolvable home
    directory entry either (e.g. a stripped container image), this returns
    None — a command naming `$TMPDIR/...` with BOTH `$TMPDIR` unset AND no
    uid-home fallback available is correctly treated as having no scratch
    boundary at all, never a silent permissive default.
    """
    active_env = env if env is not None else os.environ
    raw = active_env.get(env_var, "").strip()
    if not raw:
        if env_var == "TMPDIR":
            raw = _uid_home_fallback() or ""
        if not raw:
            return None
    resolved = os.path.realpath(raw)
    return ScratchBoundary(env_var=env_var, resolved_path=resolved)


def resolve_all_scratch_boundaries(
    *, env: dict[str, str] | None = None
) -> list[ScratchBoundary]:
    """Resolve every configured scratch root (`SCRATCH_ROOT_ENV_VARS` —
    `TMPDIR` only, as of lr-f8649f) that is actually resolvable in *env* (or
    `os.environ`). A boundary that fails to resolve (see
    `resolve_scratch_boundary`, including its uid-home fallback for an
    unset/empty `$TMPDIR`) is simply omitted — an *env* where neither
    `$TMPDIR` nor the uid-home fallback resolves returns an empty list, not
    an error; `is_scratch_contained` then correctly fails closed."""
    boundaries = []
    for env_var in SCRATCH_ROOT_ENV_VARS:
        boundary = resolve_scratch_boundary(env_var, env=env)
        if boundary is not None:
            boundaries.append(boundary)
    return boundaries


def _expand_target(raw_target: str, boundaries: list[ScratchBoundary], *, env: dict[str, str]) -> str:
    """Expand *raw_target* using the SAME env values the boundaries were
    resolved against (never `os.environ` directly — a caller that passed an
    explicit *env* mapping for testing must see that mapping honored end to
    end, or a boundary resolved against a fake $TMPDIR could be checked
    against a target expanded from the real one)."""
    expanded = raw_target
    for boundary in boundaries:
        token = f"${boundary.env_var}"
        braced = f"${{{boundary.env_var}}}"
        value = env.get(boundary.env_var, "")
        if not value and boundary.env_var == "TMPDIR":
            value = _uid_home_fallback() or ""
        expanded = expanded.replace(braced, value)
        expanded = expanded.replace(token, value)
    return expanded


def _is_path_contained(raw_target: str, boundaries: list[ScratchBoundary], *, env: dict[str, str]) -> bool:
    """True iff *raw_target*, once expanded and canonicalized, resolves
    under (or equal to) one of *boundaries* — symlink-escape and `..`-
    traversal safe, since `os.path.realpath` resolves both before the
    comparison. A target with no `$TMPDIR` reference at all
    (already a bare path, or a relative path) is checked exactly the same
    way: canonicalize, then test containment. A relative path is resolved
    against the current working directory exactly as the shell itself
    would resolve it, which means a relative path can and does fail
    containment if cwd is not itself under a scratch root — this is
    intentional, not a gap: a scratch-relative command run from a non-
    scratch cwd is not distinguishable from a repo-tree-relative one
    without more context than a single argv token carries, so it fails
    closed.
    """
    if not boundaries:
        return False
    expanded = _expand_target(raw_target, boundaries, env=env)
    resolved = os.path.realpath(expanded)
    for boundary in boundaries:
        if resolved == boundary.resolved_path or resolved.startswith(
            boundary.resolved_path + os.sep
        ):
            return True
    return False


def _looks_like_path_token(token: str) -> bool:
    """True iff *token* is shaped like a filesystem target this module
    should classify — i.e. not a bare option flag, and not a value that is
    itself an option's argument (best-effort: a token starting with '-' is
    always treated as a flag/option, never a path, since none of
    SCRATCH_SAFE_VERBS take a leading-dash path argument in normal use)."""
    if not token:
        return False
    return not token.startswith(_NON_PATH_LEADING_TOKEN_PREFIXES)


def is_scratch_contained(
    command: str, *, env: dict[str, str] | None = None
) -> bool:
    """True iff *command* (a single, unpiped, uncompounded shell command
    line) is a SCRATCH_SAFE_VERBS invocation whose every path-shaped
    argument resolves under a configured scratch boundary
    (`SCRATCH_ROOT_ENV_VARS`, see `resolve_all_scratch_boundaries`).

    This is a CATEGORY grant (task comment #2): the verb itself is not what
    is being authorized — containment is. Any verb in SCRATCH_SAFE_VERBS is
    admitted as soon as ALL of its filesystem-shaped arguments are
    contained; the instant ONE argument resolves outside every boundary,
    the whole command is denied (never a partial grant on the contained
    arguments alone — a single escaping argument makes the WHOLE command
    unsafe to admit, since e.g. `mv $TMPDIR/x /workspace/y` still performs
    a real mutation against /workspace).

    Raises:
        ScratchContainmentError: *command* is empty, contains a shell
            compound/pipe/redirect metacharacter (`;`, `|`, `&&`, `||`,
            backtick, `$(`, or a bare `>`/`<`/`>>` redirect — this module
            classifies exactly one command, never a compound shell
            expression, since a compound expression's true effects are not
            fully described by argv alone), or its argv[0] is not in
            SCRATCH_SAFE_VERBS.

    *env* overrides `os.environ` for tests.
    """
    stripped = command.strip()
    if not stripped:
        raise ScratchContainmentError("empty command cannot be classified.")

    for metachar in (";", "|", "&&", "||", "`", "$(", ">", "<"):
        if metachar in stripped:
            raise ScratchContainmentError(
                f"command contains {metachar!r} — compound/piped/redirected "
                f"shell expressions are never classified by this module "
                f"(each simple command must be evaluated on its own); "
                f"command={stripped!r}."
            )

    try:
        tokens = shlex.split(stripped)
    except ValueError as exc:
        raise ScratchContainmentError(
            f"command could not be tokenized (unbalanced quoting?): {exc}; "
            f"command={stripped!r}."
        ) from exc

    if not tokens:
        raise ScratchContainmentError("empty command cannot be classified.")

    verb = tokens[0]
    if verb not in SCRATCH_SAFE_VERBS:
        raise ScratchContainmentError(
            f"verb {verb!r} is not in SCRATCH_SAFE_VERBS "
            f"({sorted(SCRATCH_SAFE_VERBS)!r}) — this module only "
            f"classifies pure filesystem-mutation verbs; git/push/merge/"
            f"network/process-execution verbs are never scratch-eligible "
            f"regardless of their arguments (see module docstring)."
        )

    active_env = env if env is not None else dict(os.environ)
    boundaries = resolve_all_scratch_boundaries(env=active_env)
    if not boundaries:
        return False

    path_tokens = [tok for tok in tokens[1:] if _looks_like_path_token(tok)]
    if not path_tokens:
        # A safe verb with no path-shaped argument at all (e.g. bare
        # "mktemp" with no target, which creates its OWN temp file under
        # the system default tempdir) is NOT admitted here — this module
        # only grants containment it can actually verify against an
        # explicit target; a verb invoked with nothing to check is refused
        # rather than assumed safe.
        return False

    return all(_is_path_contained(tok, boundaries, env=active_env) for tok in path_tokens)


__all__ = [
    "SCRATCH_ROOT_ENV_VARS",
    "SCRATCH_SAFE_VERBS",
    "ScratchBoundary",
    "ScratchContainmentError",
    "is_scratch_contained",
    "resolve_all_scratch_boundaries",
    "resolve_scratch_boundary",
]
