"""push.contention_check — optional, config-gated pre-flight READ that
refuses a write when another unit of work is already in flight in the same
checkout (lr-78a584).

WHY THIS EXISTS, AND WHY IT LOOKS THE WAY IT DOES: a prior crew-side
deployment shipped a STATEFUL advisory lock for the identical problem
(lr-edc731, PR #534) and it failed in production within a day, in three
documented ways. This module is shaped explicitly to avoid each one:

  1. NO STATE (crew-side failure: acquire-on-one-hook / release-on-another,
     the release never fired, and the NORMAL successful-dispatch path
     self-blocked until a 2h TTL expired). This module holds nothing between
     invocations — no lock file, no TTL, no release step, no janitor, no
     orphan class. `check_working_tree_contention` computes its verdict
     FRESH, from live git state, on every call.
  2. AN OVERRIDE FLAG IS MANDATORY (crew-side failure: no operator-reachable
     escape hatch existed at all — an entire follow-up task (lr-1edad5)
     failed to invent one, because the only clearing mechanism needed a
     lead-staged params file the build agent could not reach: a circular
     dead end). `override` is a required, always-honored parameter
     here; `push.verb`'s own `--override-contention-check` CLI flag is the
     operator-reachable escape hatch this class exists to guarantee — see
     that flag's own `--help` text, which documents it ALONGSIDE the enable
     switch so an operator can find it WHILE BLOCKED, not only after reading
     source.
  3. IT MUST SEE EVERY CALLER (crew-side failure: enforcement lived in a
     PreToolUse/Agent dispatch hook, which only ever fires for crew-dispatched
     work — direct operator use and any non-crew caller were invisible to
     it). This check is wired into `push.verb` itself, the verb every caller
     — crew-dispatched or direct operator invocation — goes through to
     mutate a checkout. There is no second, hook-level copy of this logic
     anywhere.

DESIGN PRINCIPLE (operator's own words, quoted in the originating task):
"enforce good behavior, do not add manipulations to how code is modified."
This module is a READ. It never writes a file, never touches an index lock,
never stashes, never checks anything out, never mutates any git ref. Every
function below is `git status`/`git rev-parse`/`git symbolic-ref` read-only
plumbing.

THE DIRTINESS ADJUDICATION (task-mandated, stated here AND in the PR body —
this is the load-bearing decision the counter-example in the task exists to
force): the checked-out BRANCH NAME is the PRIMARY signal — a branch matching
the configured `in_flight_branch_pattern` (default: a Conventional-Commits-
style `feat/`, `fix/`, `chore/`, etc. prefix) indicates in-flight work on its
own, regardless of dirtiness. Working-tree DIRTINESS is a WEAKER SECONDARY
signal, and the task supplies a VERIFIED COUNTER-EXAMPLE that a naive "dirty
implies busy" rule gets wrong: a tree sitting on the DEFAULT branch with four
files still showing modified, entirely stale residue from a PR that had
already merged hours earlier — a state a naive dirty-check would have
refused a legitimate write against.

THE RULE THIS MODULE APPLIES, stated explicitly: dirtiness is consulted ONLY
when the checked-out branch ALREADY matches the in-flight branch pattern —
never independently, and NEVER on the default/protected branch. In other
words, dirtiness never fires a refusal by itself; it only ever adds detail
("uncommitted changes present") to a refusal the branch-name check already
decided to raise. On the default/protected branch, this check proceeds
regardless of how dirty the tree is — closing exactly the counter-example
above, because "which branch is this" is a hard git fact this process can
read, while "is this dirt fresh interleaved work or stale post-merge
residue" is NOT reliably answerable by any local git read this module has
available (no lock, no timestamp-of-last-legitimate-write, no cross-process
signal) — treating it as a hard signal would manufacture false confidence
this module does not actually have. A deployment that wants a stricter
default-branch posture can tighten `in_flight_branch_pattern` accordingly;
this module does not invent a second, less-legible dirtiness-only gate to
compensate.

THE KNOWN BOUNDED GAP (task-mandated, accepted, NOT closed here): a build
agent that has not yet created its feature branch — sitting on the default
branch, about to start work — is invisible to a branch-name check. A
stateful lock covered exactly this window; a stateless read structurally
cannot, without reintroducing the state this module exists to avoid. This is
an ACCEPTED gap, not a defect: adding state to close it would recreate
defect (1) above. See docs/verbs.md's own `loadout-push` section and
docs/provisioning.md for the user-facing statement of this gap — an operator
enabling this feature must know what it does NOT cover.

WHICH VERBS THIS APPLIES TO (task-mandated adjudication): `push.verb`'s
create-PR path is the only wiring in this task — it is the one loadout verb
that mutates the working tree on the ordinary write path used by every
build-agent caller (`push.identity.pin_commits_to_bot_identity`'s
`git filter-branch` rewrite of HEAD, immediately followed by `git push`).
`push.verb`'s own `--update-pr` path performs no git-tree mutation at all
(metadata-only PATCH; see that module's own docstring) and is NOT gated —
there is nothing for this check to protect there. `merge.verb`/
`merge.post_merge_verb` DO mutate a `--repo-path` working tree
(`merge.tree_sync.advance_repo_to_merged_sha` / `land_on_base_branch`, a
`git fetch` + `git checkout --detach`/`git checkout -B`) — but that mutation
is filed and tracked SEPARATELY as its own contention source (lr-173768,
explicitly out of scope for this task; the originating task description
itself calls out this exact code path as "worth examining independently").
Wiring this same check into the merge verb is a natural next step but is
deliberately NOT done in this change — see this task's own PR body.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ContentionCheckUnavailableError(Exception):
    """Raised when the underlying git reads themselves fail (not a git repo,
    git not on PATH, etc.) — a check-execution failure, distinct from
    finding contention. Callers treat this as a soft-fail (warn, proceed) —
    mirrors push.cleanliness_check.CleanlinessCheckError's own posture: this
    check's own inability to run must never refuse a write that would
    otherwise be clean."""


class WorkingTreeContentionError(Exception):
    """Raised when another unit of work is judged to be in flight in the
    same checkout and no override was supplied. Carries the branch and
    dirtiness detail for the caller to report — see push.verb's own message
    construction, which names the branch and states the compliant action
    (finish/land the in-flight branch, or pass the override flag)."""

    def __init__(self, message: str, *, branch: str, dirty: bool) -> None:
        super().__init__(message)
        self.branch = branch
        self.dirty = dirty


@dataclass(frozen=True)
class ContentionVerdict:
    """The outcome of one contention check — always returned even when the
    check was disabled or overridden, so a caller can log/print a consistent
    shape regardless of which path was taken."""

    in_flight: bool
    overridden: bool
    branch: str
    dirty: bool
    reason: str


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def _current_branch(repo_root: Path) -> str:
    result = _run_git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=repo_root)
    if result.returncode != 0:
        # Detached HEAD (or a check that cannot resolve a symbolic ref at
        # all) has no branch name to match against the in-flight pattern —
        # treated as "not a matching branch name" rather than an error here;
        # a detached HEAD read failure downstream (e.g. push.verb's own
        # protected-branch refusal) is a SEPARATE, already-existing check.
        return ""
    return result.stdout.strip()


def _is_dirty(repo_root: Path) -> bool:
    result = _run_git(["status", "--porcelain"], cwd=repo_root)
    if result.returncode != 0:
        raise ContentionCheckUnavailableError(
            f"git status --porcelain failed (exit {result.returncode}) in "
            f"{repo_root}: {result.stderr.strip()[:400]}"
        )
    return bool(result.stdout.strip())


def check_working_tree_contention(
    repo_root: str | Path,
    *,
    enabled: bool,
    branch_pattern: str,
    override: bool,
) -> ContentionVerdict:
    """Run the pre-flight contention read (see module docstring for the full
    design rationale — no state, mandatory override, sees every caller).

    Args:
        repo_root: the working tree to inspect.
        enabled: the resolved `push: contention_check:` config value.
            `enabled=False` short-circuits before any git command runs —
            byte-identical to today's behavior (hard acceptance criterion).
        branch_pattern: the configured (or default)
            `in_flight_branch_pattern` regex.
        override: the caller-supplied override flag
            (`push.verb --override-contention-check`). When True, this
            function NEVER raises — it still computes and returns the real
            verdict (with `overridden=True` when contention was found) so
            the caller can print "overridden" rather than silently
            proceeding with no record of the decision (see module docstring,
            defect (2): an override that cannot be seen to have fired is not
            meaningfully different from one that does not exist).

    Returns a ContentionVerdict describing what was found. Raises
    WorkingTreeContentionError when contention is found AND override is
    False — the caller (push.verb) translates this to its own reserved exit
    code, never silently swallowed.

    A ContentionCheckUnavailableError from the underlying git reads is a
    SOFT-FAIL here too: caught internally and folded into a
    ContentionVerdict with `in_flight=False` (this check's own inability to
    run must never refuse a write that would otherwise be clean) — but see
    push.verb's own wiring, which additionally prints a warning to stderr in
    that case so the soft-fail is not entirely silent.
    """
    if not enabled:
        return ContentionVerdict(
            in_flight=False, overridden=False, branch="", dirty=False,
            reason="contention check disabled",
        )

    repo_root = Path(repo_root)
    branch = _current_branch(repo_root)

    branch_matches = bool(branch) and re.search(branch_pattern, branch) is not None

    if not branch_matches:
        # THE ADJUDICATED RULE (module docstring, "THE DIRTINESS
        # ADJUDICATION"): dirtiness is NEVER consulted independently of the
        # branch-name signal. A tree on the default/protected branch (or any
        # branch that does not match the configured in-flight pattern) is
        # never refused here, however dirty it is -- closing the task's own
        # verified counter-example (a merged-PR tree on the default branch
        # still showing stale residue).
        return ContentionVerdict(
            in_flight=False, overridden=False, branch=branch, dirty=False,
            reason=(
                f"branch {branch!r} does not match the configured in-flight "
                f"pattern {branch_pattern!r} (dirtiness not consulted -- "
                f"see push.contention_check's own module docstring for why "
                f"dirtiness is never an independent signal)"
            ),
        )

    try:
        dirty = _is_dirty(repo_root)
    except ContentionCheckUnavailableError as exc:
        # Soft-fail: the branch-name signal alone is not enough to refuse on
        # its own once the dirtiness read is unavailable -- mirrors
        # push.cleanliness_check/push.branch_commit_check's own
        # never-block-on-the-check's-own-failure posture. The caller
        # (push.verb) prints this on stderr.
        return ContentionVerdict(
            in_flight=False, overridden=False, branch=branch, dirty=False,
            reason=f"contention check could not complete -- {exc}",
        )

    reason = (
        f"checked-out branch {branch!r} matches the configured in-flight "
        f"pattern {branch_pattern!r}"
        + (" and the working tree is dirty" if dirty else "")
    )

    if override:
        return ContentionVerdict(
            in_flight=True, overridden=True, branch=branch, dirty=dirty, reason=reason,
        )

    raise WorkingTreeContentionError(
        f"working-tree contention detected: {reason}. Another unit of work "
        f"appears to be in flight in this checkout. Compliant action: land "
        f"or clean up the in-flight branch {branch!r} first, or pass "
        f"--override-contention-check if you know this refusal is wrong "
        f"(e.g. a stale branch nobody is actively working from) -- see "
        f"docs/verbs.md's `loadout-push` section for the full contract, "
        f"including the accepted bounded gap this check does not cover.",
        branch=branch,
        dirty=dirty,
    )


__all__ = [
    "ContentionCheckUnavailableError",
    "ContentionVerdict",
    "WorkingTreeContentionError",
    "check_working_tree_contention",
]
