"""merge.criterion_evidence -- whether something that ran shows each acceptance
criterion of a pull request's sealed spec, reported at merge.

A criterion may carry an evidence line: test ids in the form the test-report store
records (``<classname>::<name>``), or one named run for a criterion no test can show.
The spec is found by the hash the reviewer fences carry, in the local spec store,
exactly as merge.interface_notice finds it. The outcomes are read from the newest
complete, clean-tree run in the test-report store whose recorded commit equals the
pull request's head exactly and whose repository is the one being merged; an older
head or a dirty tree is never evidence. Each criterion then reads one of:

  * ``shown``: every named test passed in that run (or the named run is complete, clean,
    failure-free and at the head);
  * ``not shown``: each named id that is absent from the run, failed or was skipped is
    named with which, or the reason no run could be read;
  * ``no evidence named``: the criterion carries no evidence line.

A spec none of whose criteria carry an evidence line yields one line saying so.
The merge prints the lines and carries them in its completion attestation, and
proceeds: nothing here refuses, blocks or changes the outcome of a merge, and nothing
here starts a test run. Every failure to compute is returned as a note for the merge
to print and a line saying so for the attestation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clagentic_loadout.merge.interface_notice import load_fence_specs
from clagentic_loadout.repo_test import junit, store
from clagentic_loadout.repo_test.report_cmd import report_format_of
from clagentic_loadout.repo_test.repo_match import repo_matches
from clagentic_loadout.repo_test.spec import REPORT_JUNIT

_SHORT = 12
ABSENT = "absent"
SHOWN = "shown"
NOT_SHOWN = "not shown"
NO_EVIDENCE = "no evidence named"


@dataclass(frozen=True)
class CriterionEvidence:
    #: One line per criterion (or the single no-evidence-lines or not-computed line),
    #: for the merge output and the attestation row.
    lines: tuple[str, ...] = ()
    #: Why something could not be computed or read, for the merge output only.
    notes: tuple[str, ...] = ()


class _HeadRun:
    """The run at the head, read once: its per-id outcomes, or why there are none."""

    def __init__(self, outcomes: Mapping[str, tuple[str, ...]] | None, problem: str | None) -> None:
        self.outcomes = outcomes
        self.problem = problem


def _not_computed(reason: str, *notes: str) -> CriterionEvidence:
    return CriterionEvidence(
        lines=(f"not computed: {reason}",),
        notes=(f"criterion evidence not computed: {reason}", *notes),
    )


def _matches_repo(run: store.RunRecord, repo_names: Sequence[str]) -> bool:
    return any(repo_matches(run.repo, name) for name in repo_names)


def _find_head_run(
    root: Path, head_sha: str, repo_names: Sequence[str]
) -> tuple[store.RunRecord | None, str | None]:
    """The newest complete clean-tree run recorded at exactly *head_sha* for the
    repository, or None and why not."""
    try:
        runs = store.list_runs(root)
    except store.StoreError as exc:
        return None, str(exc)
    wanted = head_sha.lower()
    fit = [
        run
        for run in runs
        if run.state == store.STATE_COMPLETE
        and run.dirty is False
        and run.head_sha is not None
        and run.head_sha.lower() == wanted
        and not store.is_future_dated(run)
        and not store.is_undatable(run)
        and _matches_repo(run, repo_names)
    ]
    if not fit:
        return None, f"no complete clean-tree run at head {head_sha} was found in the test-report store"
    return fit[-1], None


def _read_outcomes(root: Path, run: store.RunRecord) -> _HeadRun:
    if report_format_of(run) != REPORT_JUNIT:
        return _HeadRun(None, f"run {run.run_id} has no JUnit report, so per-test outcomes cannot be read")
    path = root / run.run_id / store.JUNIT_FILE
    if path.is_symlink():
        return _HeadRun(None, f"run {run.run_id}: {path.name} is a symlink, refusing it")
    try:
        return _HeadRun(junit.parse_outcomes(path), None)
    except junit.JunitError as exc:
        return _HeadRun(None, f"run {run.run_id}: {exc}")


def _test_status(outcomes: tuple[str, ...] | None) -> str:
    """How one id reads across its cases: passed only when every case of it passed,
    else failed if any case failed, else skipped; absent when the run holds no case
    of that id."""
    if not outcomes:
        return ABSENT
    if junit.OUTCOME_FAILED in outcomes:
        return junit.OUTCOME_FAILED
    if all(outcome == junit.OUTCOME_PASSED for outcome in outcomes):
        return junit.OUTCOME_PASSED
    return junit.OUTCOME_SKIPPED


def _named_run_problem(
    root: Path, run_id: str, head_sha: str, repo_names: Sequence[str]
) -> str | None:
    """Why the run a criterion names does not show it, or None when it does."""
    try:
        run = store.load_run(root, run_id, include_unfinished=False)
    except store.StoreError as exc:
        return f"run {run_id} could not be read: {exc}"
    if run.state != store.STATE_COMPLETE:
        return f"run {run_id} is {run.state or 'unreadable'}, not complete"
    if not _matches_repo(run, repo_names):
        return f"run {run_id} was recorded for another repository"
    if run.head_sha is None or run.head_sha.lower() != head_sha.lower():
        return f"run {run_id} was not taken at head {head_sha}"
    if run.dirty is not False:
        return f"run {run_id} was not taken on a clean tree"
    counts = run.result.get("counts")
    failed = counts.get("failed") if isinstance(counts, dict) else None
    if failed != 0:
        return f"run {run_id} did not record zero failures"
    return None


def _row(
    criterion: Mapping[str, Any],
    head: _HeadRun | None,
    run_problem: str | None,
    run_checks: Mapping[str, str | None],
) -> str:
    cid = criterion["id"]
    evidence = criterion.get("evidence")
    if not evidence:
        return f"{cid}: {NO_EVIDENCE}"
    passed: list[str] = []
    problems: list[str] = []
    tests = evidence.get("tests") or []
    if tests:
        if head is None or head.outcomes is None:
            problems.append(run_problem or (head.problem if head else None) or "no run was read")
        else:
            for test_id in tests:
                status = _test_status(head.outcomes.get(test_id))
                if status == junit.OUTCOME_PASSED:
                    passed.append(f"{test_id} passed")
                else:
                    problems.append(f"{test_id} {status}")
    named = evidence.get("run")
    if named:
        problem = run_checks.get(named)
        if problem is None:
            passed.append(f"run {named} complete, no failures")
        else:
            problems.append(problem)
    if problems:
        return f"{cid}: {NOT_SHOWN} ({'; '.join(problems)})"
    return f"{cid}: {SHOWN} ({', '.join(passed)})"


def collect_criterion_evidence(
    *,
    head_sha: str | None,
    repo_names: Sequence[str],
    fence_spec_hashes: Sequence[str],
    repo_path: str | None = None,
    config_root: str | Path | None = None,
    reports_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> CriterionEvidence:
    """The per-criterion evidence rows for the spec(s) the reviewer fences name, read
    against the run recorded at *head_sha* for the repository *repo_names* identify
    (any one matching a recorded repository suffices). *reports_dir* overrides the
    test-report store location; by default it is resolved as ``test report`` does."""
    docs, spec_notes = load_fence_specs(fence_spec_hashes, repo_path, config_root)
    if not docs:
        return _not_computed("no sealed spec could be read", *spec_notes)
    criteria = [criterion for doc in docs for criterion in doc["acceptance_criteria"]]
    if not any(criterion.get("evidence") for criterion in criteria):
        return CriterionEvidence(
            lines=("the spec carries no evidence lines",), notes=tuple(spec_notes)
        )
    if not head_sha:
        return _not_computed("the pull request names no head commit", *spec_notes)
    notes = list(spec_notes)
    head: _HeadRun | None = None
    run_problem: str | None = None
    root: Path | None = None
    try:
        root = Path(reports_dir) if reports_dir is not None else store.reports_root(env, config_root=config_root)
    except OSError as exc:
        run_problem = f"the test-report store could not be resolved: {exc}"
    wants_tests = any((c.get("evidence") or {}).get("tests") for c in criteria)
    if root is not None and wants_tests:
        run, run_problem = _find_head_run(root, head_sha, repo_names)
        if run is not None:
            head = _read_outcomes(root, run)
            if head.problem:
                notes.append(f"criterion evidence: {head.problem}")
        else:
            notes.append(f"criterion evidence: {run_problem}")
    elif run_problem:
        notes.append(f"criterion evidence: {run_problem}")
    run_checks: dict[str, str | None] = {}
    for criterion in criteria:
        named = (criterion.get("evidence") or {}).get("run")
        if named and named not in run_checks:
            run_checks[named] = (
                run_problem
                if root is None
                else _named_run_problem(root, named, head_sha, repo_names)
            )
            if run_checks[named]:
                notes.append(f"criterion evidence: {run_checks[named]}")
    lines = tuple(_row(criterion, head, run_problem, run_checks) for criterion in criteria)
    return CriterionEvidence(lines=lines, notes=tuple(notes))


__all__ = ["CriterionEvidence", "collect_criterion_evidence"]
