"""merge.ci_status — the CI-status gate decision (lr-afba CI-status-gate
slice, folded from lr-afba comment #6; HEAD-scoping fix lr-2d2293).

CONTEXT: this repo (and any repo with no CI runners wired up by design — see
lr-368c, "runner explicitly out of scope -> deletion not stub") legitimately
has ZERO CI statuses at a PR's HEAD. Observed directly on PR #49 (lr-482c20)
@ head 6a8fcbd: the Forgejo combined-status endpoint returned an empty state
— this is expected, not a gate failure. A CI-status gate that fails closed on
an EMPTY result would falsely refuse every merge in a no-runner-by-design
repo.

HEAD-SCOPING (lr-2d2293): emptiness is determined ONLY from HEAD-scoped
commit-status evidence (`status_count`, from the combined commit-status
endpoint). A repo-global signal such as Forgejo's `/actions/tasks`
total_count is NOT HEAD-scoped — it counts mirror-sync tasks and all
historical Actions tasks for the whole repo, not just this PR's HEAD. Keying
emptiness off that signal produced a false refusal on a mirror-runner
repo with no CI runner: zero commit statuses at HEAD (correctly "no CI ran")
but a non-zero repo-global task count made is_empty False, so the gate fell
through to a genuinely-empty combined_state and refused with "<no combined
state reported despite non-empty CI evidence>" — see session d5aee241. The
`run_count` field is kept on CiStatusResult as diagnostic-only metadata (a
fetcher may still populate it for logging) but it MUST NOT feed is_empty.

THE GATE DECISION (explicit, not an accidental fall-through):
  - Zero commit-status entries at HEAD => PASS. No-runner-by-design is a
    legitimate, common repo shape, not a missing gate. This module's
    check_ci_status() returns cleanly (no exception) for this case.
  - A non-empty combined state => gate on the REAL state:
      "success"            => pass
      "failure" / "error"  => refuse (merge.errors.CiStatusFailedError)
      "pending" / anything
        else non-empty      => refuse (still running or unrecognized; a
                                gate cannot authorize a merge against CI that
                                has not conclusively passed)

THIS IS A DECISION-LAYER MODULE, not a transport: it takes an already-fetched
CiStatusResult (see below) and decides pass/refuse — the platform-specific
HTTP fetch (and its OWN fail-closed contract: unreachable/non-200 raises
merge.errors.GateFactUnavailableError, mirroring every other gate-fact
fetcher in this package) lives in merge.forgejo_backend.fetch_ci_status /
merge.github_backend.fetch_ci_status. Keeping the fetch and the decision
separate matches merge.diff_scope's own shape (a pure policy check over an
already-fetched list, Wave B slice 4) — this module has no transport/
credential coupling and is trivially unit-testable against synthetic
CiStatusResult values.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from clagentic_loadout.merge.errors import CiStatusFailedError

#: Combined-state values that represent a real, currently-running or
#: not-yet-concluded check. Not a success and not a genuine failure —
#: refused for the same reason a truly failed state is: the gate cannot
#: authorize a merge against CI that has not conclusively passed.
_NON_TERMINAL_STATES = frozenset({"pending", "in_progress", "queued", "waiting"})

#: Combined-state values that represent a definite, conclusive failure.
_FAILURE_STATES = frozenset({"failure", "error", "cancelled", "timed_out"})

#: The one combined-state value that passes the gate outright.
_SUCCESS_STATE = "success"


@dataclass(frozen=True)
class CiStatusResult:
    """Platform-agnostic CI-status gate fact for one PR HEAD SHA.

    `combined_state` is the platform's own combined/rollup status string
    (Forgejo and GitHub both expose one under this name), lowercased by the
    fetcher before construction; empty string when the platform reported no
    combined state at all (distinct from a real state value).

    `status_count` is the HEAD-scoped commit-status count backing the "is
    this truly empty" determination — a fetcher populates it from the
    combined commit-status endpoint (the only HEAD-scoped, CI-meaningful
    signal both platforms expose: Forgejo `/commits/{sha}/status`, GitHub
    `/commits/{sha}/status`). `run_count` is diagnostic-only metadata (e.g.
    GitHub check-runs at HEAD, or a repo-global Actions-task count) — it is
    reported alongside status_count for operator visibility but MUST NOT
    feed is_empty (lr-2d2293: a repo-global count is not HEAD-scoped CI
    evidence and produced a false refusal on mirror-runner repos). Kept as
    two separate counts (rather than one summed total) so a caller
    inspecting a refusal/pass can see which signal(s) were actually present,
    matching this package's "report resolved values, never a collapsed
    guess" CLI-hygiene rule.
    """

    combined_state: str = ""
    status_count: int = 0
    run_count: int = 0
    raw_states: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        """True iff there are NO commit-status entries at this HEAD.
        HEAD-scoped only (lr-2d2293): a non-zero run_count (diagnostic-only,
        possibly repo-global) never overrides this. Zero HEAD-scoped commit
        statuses is the no-runner-by-design case that must PASS, not
        refuse."""
        return self.status_count == 0


def check_ci_status(
    result: CiStatusResult,
    pr_number: int,
    owner: str,
    repo: str,
) -> None:
    """Gate decision over an already-fetched CiStatusResult.

    - result.is_empty (zero HEAD-scoped commit statuses) => PASS (return
      cleanly). This is the explicit no-runner-by-design case (lr-368c) — a
      repo with no CI wired up must never have its merges refused by a gate
      checking for CI that was never going to exist. run_count (diagnostic
      only, possibly repo-global) never overrides this (lr-2d2293).
    - A non-empty result gates on combined_state:
        "success"                    => PASS
        a recognized failure/pending
          state, or any other
          non-empty unrecognized
          value                       => REFUSE (CiStatusFailedError),
                                          reporting the actual state seen
                                          (never a collapsed/guessed label)

    Does NOT fetch anything itself — see this module's docstring for the
    fetch/decide split. Fetch failures (unreachable API, non-200) are the
    caller's own merge.errors.GateFactUnavailableError, raised by the
    fetcher BEFORE this function is ever called; that fail-closed path is
    unrelated to (and stricter than) the empty-is-pass decision made here.
    """
    if result.is_empty:
        return

    state = result.combined_state
    if state == _SUCCESS_STATE:
        return

    seen = state or "<no combined state reported despite non-empty CI evidence>"
    raise CiStatusFailedError(
        f"CI-STATUS GATE FAILED — PR #{pr_number} in {owner}/{repo} has "
        f"{result.status_count} commit-status entr{'y' if result.status_count == 1 else 'ies'} "
        f"and {result.run_count} check/workflow run(s) at HEAD, combined "
        f"state={seen!r}. Refusing: only a conclusive {_SUCCESS_STATE!r} "
        f"combined state authorizes a merge. Re-run or fix the failing "
        f"check(s), then retry once CI reports success."
    )


__all__ = ["CiStatusResult", "check_ci_status"]
