"""review.clearing_sources — gather the facts review.clearing decides on.

Reads the reviewer's earlier verdicts from the host, places the sources of
resolution (the run directory, the findings document, the caller's rulings, the
verdict's own base-revision dispositions and the reviewer's drops) and hands
them to review.clearing. Every read that fails clears nothing: the posted
verdict then leaves the earlier findings open, which the merge gate refuses.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from clagentic_loadout.acquire.contract import IssueCommentBackend, RangeDiffBackend
from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.review.chunking import plan_chunks
from clagentic_loadout.review.clearing import (
    Clearing,
    LineMapper,
    derive_clearing,
    one_line,
    prior_open_findings,
)
from clagentic_loadout.review.evaluate import Verdict
from clagentic_loadout.review.evaluation import finding_state_id
from clagentic_loadout.review.prior_view import locate_prior_line
from clagentic_loadout.review.resolutions import Resolution
from clagentic_loadout.review.run_resolved import RunResolved

_SHORT = 12


def _demotions(verdict: Verdict, base_sha: str | None) -> dict[str, str]:
    """Finding id -> evidence for each finding a base-revision disposition
    demoted to advisory."""
    at = f" at {base_sha[:_SHORT]}" if base_sha else ""
    demoted: dict[str, str] = {}
    for finding in verdict.findings:
        if finding.praise or finding.blocking or not finding.disposition:
            continue
        kind = finding.disposition.get("kind", "disposition")
        by = finding.disposition.get("by")
        who = f" by {by}" if by else ""
        demoted[finding_state_id(finding.file, finding.line, finding.rule_id)] = one_line(
            f"demoted by base disposition {kind}{who}{at}"
        )
    return demoted


def _line_mapper(backend: Any, owner: str, repo: str, head_sha: str) -> LineMapper:
    """Maps a line numbered at an earlier head to this head through the host's
    commit range, the way a delta review places a prior finding. None when the
    range is unavailable or not a fast-forward."""
    cache: dict[str, list | None] = {}

    def chunks_since(prior_head: str):
        if prior_head not in cache:
            cache[prior_head] = None
            if isinstance(backend, RangeDiffBackend):
                try:
                    span = backend.fetch_range_diff(
                        owner=owner, repo=repo, base_sha=prior_head, head_sha=head_sha
                    )
                except AcquireFetchError:
                    span = None
                if span is not None and span.fast_forward:
                    cache[prior_head] = plan_chunks(span.diff_text)
        return cache[prior_head]

    def map_line(prior_head: str, file: str, line: int) -> int | None:
        if prior_head == head_sha:
            return line
        chunks = chunks_since(prior_head)
        return None if chunks is None else locate_prior_line(chunks, file, line).mapped

    return map_line


def compute_clearing(
    *,
    backend: Any,
    owner: str,
    repo: str,
    pr_number: int,
    reviewer: str,
    head_sha: str,
    base_sha: str | None,
    verdict: Verdict,
    raised_ids: Sequence[str],
    document_resolved: Sequence[Mapping[str, Any]],
    caller_resolved: Sequence[Mapping[str, Any]],
    caller_rulings: Sequence[Resolution],
    drops: Sequence[Mapping[str, Any]],
    run: RunResolved,
) -> Clearing:
    """What the verdict being posted for *head_sha* clears of the reviewer's
    earlier open findings. *document_resolved* are the resolved entries of the
    findings document passed to post; *caller_resolved* are those post itself
    applied from the caller's rulings."""
    if not isinstance(backend, IssueCommentBackend):
        return Clearing((), (), (), ("this transport cannot list PR comments; nothing is cleared",))
    try:
        comments = backend.fetch_issue_comments(owner=owner, repo=repo, pr_number=pr_number)
    except AcquireFetchError as exc:
        return Clearing((), (), (), (f"earlier verdicts could not be read ({exc}); nothing is cleared",))
    prior_open, notes = prior_open_findings(
        comments, pr_number=pr_number, reviewer=reviewer, head_sha=head_sha
    )
    if not prior_open:
        return Clearing((), (), (), tuple(notes))

    source_notes: list[str] = list(notes)
    if run.conflict:
        reviewer_side: list[Mapping[str, Any]] = []
        source_notes.append("nothing is cleared from the run's or the findings document's resolved list")
        source_notes.extend(run.notes)
    else:
        reviewer_side = [*run.entries, *document_resolved]
        if not reviewer_side:
            source_notes.append("the reviewer-side resolved list is missing, so it clears nothing")
            source_notes.extend(run.notes)
    return derive_clearing(
        prior_open,
        head_sha=head_sha,
        raised_ids=set(raised_ids),
        resolved_entries=[*reviewer_side, *caller_resolved],
        caller_rulings=caller_rulings,
        demoted=_demotions(verdict, base_sha),
        drops=drops,
        map_line=_line_mapper(backend, owner, repo, head_sha),
        source_notes=source_notes,
    )
