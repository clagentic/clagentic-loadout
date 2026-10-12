"""merge.ci_status — the CI-status gate decision (CI-status-gate slice;
HEAD-scoping fix; check-run evaluation fix).

CONTEXT: this repo (and any repo with no CI runners wired up by design)
legitimately has ZERO CI evidence at a PR's HEAD. Observed directly on a
Forgejo PR: the combined-status endpoint returned an empty state — this is
expected, not a gate failure. A CI-status gate that fails closed on an EMPTY
result would falsely refuse every merge in a no-runner-by-design repo.

TWO EVIDENCE SOURCES, BOTH HEAD-SCOPED:
  - commit statuses (Forgejo Actions and any legacy GitHub status poster):
    the platform's combined state over `statuses`.
  - check-runs (GitHub Actions and GitHub Apps): a per-run `status` and
    `conclusion`. GitHub Actions posts ONLY check-runs; the legacy combined
    status endpoint is empty for such a repo, so a gate that read statuses
    alone passed a head whose every check-run had failed. Check-runs are
    therefore evaluated in their own right, not kept as a diagnostic count.

HEAD-SCOPING: emptiness is determined ONLY from HEAD-scoped evidence
(`status_count` and the `check_runs` list, both read for the exact commit). A
repo-global signal such as Forgejo's `/actions/tasks` total_count is NOT
HEAD-scoped — it counts mirror-sync tasks and all historical Actions tasks for
the whole repo, and keying emptiness off it produced a false refusal on a
mirror-runner repo with no CI runner. The `run_count` field is kept on
CiStatusResult as diagnostic-only metadata (a fetcher may still populate it for
logging) but it MUST NOT feed is_empty.

THE GATE DECISION (explicit, not an accidental fall-through):
  - No statuses AND no check-runs at HEAD => PASS, reported as "no CI found at
    head". No-runner-by-design is a legitimate, common repo shape. An opt-in
    `strict` setting turns this into a refusal for a repo that expects CI.
  - Statuses present => the combined state must be "success".
  - Check-runs present => each check name must be green (see below).
  - Anything pending, unrecognized or failing refuses: a gate cannot authorize
    a merge against CI that has not conclusively passed.

WHICH CHECK NAMES GATE. With no `required_checks`, every check-run name present
at HEAD gates. With `required_checks` (a list read from TRUSTED configuration —
the base branch's gate file or the deployment, never the PR head), only those
names gate and a name with no run or status at HEAD refuses as not reported.
A repo uses the list to state which runs gate when it deliberately has runs
that must not (for example a duplicate run that fails whenever a job is
skipped).

PER-NAME RULE. For one check name, over every run at HEAD (a re-run inside the
same check suite replaces the earlier run):
  1. any run not yet completed => pending => refuse;
  2. any run of the LATEST suite (highest suite id) that did not conclude
     successfully => refuse, even when an older suite succeeded;
  3. at least one run must have succeeded. A `skipped` or `neutral` run counts
     as success only when no `required_checks` list is configured; a name the
     list requires must have actually run and succeeded.

THIS IS A DECISION-LAYER MODULE, not a transport: it takes an already-fetched
CiStatusResult and decides pass/refuse. The platform-specific HTTP fetch (and
its OWN fail-closed contract: unreachable/non-200 raises
merge.errors.GateFactUnavailableError) lives in merge.forgejo_backend.
fetch_ci_status / merge.github_backend.fetch_ci_status.
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

#: Check-run `status` value of a run that has finished.
_RUN_COMPLETED = "completed"

#: Check-run conclusions that do not fail a run. `skipped` and `neutral` are
#: non-failing but only count as a pass when no required list is configured.
_RUN_SUCCESS = "success"
_RUN_NON_FAILING = frozenset({_RUN_SUCCESS, "neutral", "skipped"})

#: Source labels recorded in the merge result.
SOURCE_CHECK_RUNS = "check-runs"
SOURCE_STATUSES = "statuses"
SOURCE_BOTH = "check-runs+statuses"
SOURCE_NONE = "none"


@dataclass(frozen=True)
class CheckRun:
    """One check-run read for the PR head. `status` and `conclusion` are
    lowercased by the fetcher; `conclusion` is empty until the run completes.
    `suite_id` orders runs of one name by recency (a later suite is a later
    attempt); `run_id` orders re-runs inside one suite."""

    name: str
    status: str
    conclusion: str = ""
    suite_id: int | None = None
    run_id: int | None = None


@dataclass(frozen=True)
class StatusEntry:
    """One commit status (a `context` and its `state`) at the PR head."""

    context: str
    state: str


@dataclass(frozen=True)
class CiStatusResult:
    """Platform-agnostic CI gate fact for one PR HEAD SHA.

    `combined_state` is the platform's own combined/rollup status string
    (Forgejo and GitHub both expose one under this name), lowercased by the
    fetcher before construction; empty string when the platform reported no
    combined state at all (distinct from a real state value).

    `status_count` is the HEAD-scoped commit-status count. `check_runs` holds
    the HEAD-scoped check-runs (GitHub only). `statuses` carries each commit
    status's context so a required name can be matched against it.

    `run_count` is diagnostic-only metadata (e.g. the check-runs total the
    platform reported, or a repo-global Actions-task count) and MUST NOT feed
    is_empty.
    """

    combined_state: str = ""
    status_count: int = 0
    run_count: int = 0
    raw_states: tuple[str, ...] = field(default_factory=tuple)
    check_runs: tuple[CheckRun, ...] = field(default_factory=tuple)
    statuses: tuple[StatusEntry, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        """True iff there is NO HEAD-scoped CI evidence: no commit-status
        entries and no check-runs. A non-zero run_count (diagnostic-only,
        possibly repo-global) never overrides this."""
        return self.status_count == 0 and not self.check_runs

    @property
    def source(self) -> str:
        """Which evidence source(s) answered: check-runs, statuses, both, or
        none."""
        has_runs = bool(self.check_runs)
        has_statuses = self.status_count > 0
        if has_runs and has_statuses:
            return SOURCE_BOTH
        if has_runs:
            return SOURCE_CHECK_RUNS
        if has_statuses:
            return SOURCE_STATUSES
        return SOURCE_NONE


def _latest_attempts(runs: list[CheckRun]) -> list[CheckRun]:
    """Collapse re-runs: within one check suite only the highest run id of a
    name stands."""
    best: dict[int | None, CheckRun] = {}
    for run in runs:
        held = best.get(run.suite_id)
        if held is None or (run.run_id or 0) >= (held.run_id or 0):
            best[run.suite_id] = run
    return list(best.values())


def _judge_check_name(name: str, runs: list[CheckRun], *, skipped_counts: bool) -> str | None:
    """Return a one-line problem for check *name*, or None when it is green."""
    attempts = _latest_attempts(runs)
    pending = [r for r in attempts if r.status != _RUN_COMPLETED]
    if pending:
        return f"check {name!r} is still pending (status={pending[0].status!r})"

    latest_suite = max((r.suite_id for r in attempts if r.suite_id is not None), default=None)
    latest = [r for r in attempts if r.suite_id == latest_suite]
    failing = [r for r in latest if r.conclusion not in _RUN_NON_FAILING]
    if failing:
        return f"check {name!r} concluded {failing[0].conclusion or '<none>'!r}"

    passing = _RUN_NON_FAILING if skipped_counts else frozenset({_RUN_SUCCESS})
    if not any(r.conclusion in passing for r in attempts):
        seen = sorted({r.conclusion or "<none>" for r in attempts})
        return f"check {name!r} has no successful run (conclusions seen: {seen!r})"
    return None


def _judge_status_state(name: str, state: str) -> str | None:
    if state == _SUCCESS_STATE:
        return None
    if state in _NON_TERMINAL_STATES:
        return f"status {name!r} is still pending (state={state!r})"
    return f"status {name!r} reported {state or '<none>'!r}"


def _required_problems(result: CiStatusResult, required: tuple[str, ...]) -> list[str]:
    problems: list[str] = []
    for name in required:
        runs = [r for r in result.check_runs if r.name == name]
        if runs:
            problem = _judge_check_name(name, runs, skipped_counts=False)
        else:
            states = [s.state.lower() for s in result.statuses if s.context == name]
            if not states:
                problem = f"required check {name!r} was not reported at head"
            else:
                problem = next(
                    (p for p in (_judge_status_state(name, st) for st in states) if p), None
                )
        if problem:
            problems.append(problem)
    return problems


def _all_check_problems(result: CiStatusResult) -> list[str]:
    by_name: dict[str, list[CheckRun]] = {}
    for run in result.check_runs:
        by_name.setdefault(run.name, []).append(run)
    problems = []
    for name, runs in by_name.items():
        problem = _judge_check_name(name, runs, skipped_counts=True)
        if problem:
            problems.append(problem)
    return problems


def check_ci_status(
    result: CiStatusResult,
    pr_number: int,
    owner: str,
    repo: str,
    *,
    head_sha: str = "",
    required_checks: tuple[str, ...] = (),
    strict: bool = False,
) -> None:
    """Gate decision over an already-fetched CiStatusResult.

    Returns cleanly on a pass; raises CiStatusFailedError naming each failing
    check, its conclusion and the head SHA otherwise. See the module docstring
    for the full rules. *required_checks* and *strict* must come from trusted
    configuration, never from the PR head.

    Does NOT fetch anything itself. Fetch failures (unreachable API, non-200)
    are the caller's own merge.errors.GateFactUnavailableError, raised by the
    fetcher BEFORE this function is ever called.
    """
    required = tuple(dict.fromkeys(required_checks))
    where = f"PR #{pr_number} in {owner}/{repo}" + (f" at head {head_sha}" if head_sha else "")

    if result.is_empty:
        if required:
            raise CiStatusFailedError(
                f"CI-STATUS GATE FAILED — {where}: no check-runs and no commit "
                f"statuses were found, but the repo requires {list(required)!r}. "
                f"Refusing."
            )
        if strict:
            raise CiStatusFailedError(
                f"CI-STATUS GATE FAILED — {where}: no CI found at head and strict "
                f"CI is enabled. Refusing: this repo expects CI evidence."
            )
        return

    if required:
        problems = _required_problems(result, required)
    else:
        problems = []
        if result.status_count > 0 and result.combined_state != _SUCCESS_STATE:
            seen = result.combined_state or "<no combined state reported despite non-empty CI evidence>"
            problems.append(f"combined commit-status state={seen!r}")
        problems.extend(_all_check_problems(result))

    if not problems:
        return
    raise CiStatusFailedError(
        f"CI-STATUS GATE FAILED — {where}, CI source: {result.source}, "
        f"{result.status_count} commit-status entr{'y' if result.status_count == 1 else 'ies'} "
        f"and {len(result.check_runs) or result.run_count} check/workflow run(s) at HEAD. "
        f"{'; '.join(problems)}. Refusing: only conclusive success on every "
        f"{'required ' if required else ''}check authorizes a merge. Re-run or fix the "
        f"failing check(s), then retry once CI reports success."
    )


__all__ = [
    "SOURCE_BOTH",
    "SOURCE_CHECK_RUNS",
    "SOURCE_NONE",
    "SOURCE_STATUSES",
    "CheckRun",
    "CiStatusResult",
    "StatusEntry",
    "check_ci_status",
]
