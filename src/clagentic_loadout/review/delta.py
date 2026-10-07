"""review.delta — delta mode: review only what changed since the last
reviewed head.

A full-diff review of every push re-reads unchanged lines each round, so a
reviewer keeps finding new things in old code and a PR never converges. Delta
mode narrows a re-review to what a reviewer is actually accountable for:

  * whether the open findings of its own last verdict were resolved, and
  * new defects introduced by the delta (last reviewed head..current head).

The caller supplies its own last verdict: the head that verdict was stamped
for, and the findings it posted. Nothing here assumes a particular reviewer
role or how many reviewer roles a PR has; each role's re-review is keyed on
that role's own last verdict.

A full-diff review stays the default. Delta mode applies only when the current
head is a strict fast-forward of the last reviewed head, and every other case
(same head, rebased or force-pushed head, range unreadable, nothing in the
range, or a transport that cannot answer the question) falls back to the full
diff, naming the reason, because reviewing less than the whole PR is only
sound when the narrower question is well defined.

An open finding on a file the delta does not touch, or one too far down the
listing cap to be shown to the reviewer, stays open by construction:
`carried_findings` returns it so the merged result still describes the whole
PR, and a delta review can never turn "clean" by simply not looking.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.acquire.contract import AcquiredPr, RangeDiffBackend
from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.review.chunking import Chunk

STATUS_DELTA = "ok"
STATUS_FALLBACK = "fallback"

REASON_SAME_HEAD = "SAME_HEAD"
REASON_NON_FAST_FORWARD = "NON_FAST_FORWARD"
REASON_RANGE_UNAVAILABLE = "RANGE_UNAVAILABLE"
REASON_DELTA_EMPTY = "DELTA_EMPTY"
REASON_RANGE_UNSUPPORTED = "RANGE_UNSUPPORTED"

MODE_FULL = "full"
MODE_DELTA = "delta"

#: A prompt that lists every open finding of a very large review would crowd
#: out the diff, so only the listing is bounded: findings past the cap are
#: carried forward as still open (see carried_findings), never dropped.
MAX_LISTED_FINDINGS = 50


@dataclass(frozen=True)
class DeltaContext:
    """What a delta review is measured against."""

    since_head: str
    #: Findings of the last verdict that are still open: everything but praise.
    open_findings: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class DeltaResolution:
    """The outcome of deciding between a delta and a full review."""

    #: What to review: the delta diff when a delta applies, else the input.
    acquired: AcquiredPr
    #: None means a full-diff review.
    context: DeltaContext | None
    reason: str = ""
    detail: str = ""

    @property
    def mode(self) -> str:
        return MODE_DELTA if self.context is not None else MODE_FULL

    def stage_fields(self) -> dict[str, str]:
        if self.context is not None:
            return {"mode": MODE_DELTA, "since_head": self.context.since_head}
        return {"mode": MODE_FULL, "reason": self.reason}


def open_findings_of(findings: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """The findings a later review must still answer for."""
    return tuple(f for f in findings if f.get("severity") != "praise")


def resolve_delta(
    backend: object,
    acquired: AcquiredPr,
    since_head: str,
    prior_findings: list[dict[str, Any]],
) -> DeltaResolution:
    """Decide whether *acquired* (the PR at its current head) is reviewed as a
    delta since *since_head*, and build what to review accordingly."""

    def full(reason: str, detail: str = "") -> DeltaResolution:
        return DeltaResolution(acquired, None, reason=reason, detail=detail)

    if since_head == acquired.head_sha:
        return full(REASON_SAME_HEAD, "the PR head has not moved since the last review")
    if not isinstance(backend, RangeDiffBackend):
        return full(REASON_RANGE_UNSUPPORTED, "this transport cannot read a commit range")
    try:
        span = backend.fetch_range_diff(
            owner=acquired.owner,
            repo=acquired.repo,
            base_sha=since_head,
            head_sha=acquired.head_sha,
        )
    except AcquireFetchError as exc:
        return full(REASON_RANGE_UNAVAILABLE, str(exc))
    if not span.fast_forward:
        return full(
            REASON_NON_FAST_FORWARD,
            "the current head does not descend from the last reviewed head",
        )
    if not span.diff_text.strip():
        return full(REASON_DELTA_EMPTY, "the range holds no reviewable change")
    return DeltaResolution(
        dataclasses.replace(acquired, base_sha=since_head, diff_text=span.diff_text),
        DeltaContext(since_head=since_head, open_findings=open_findings_of(prior_findings)),
    )


def split_files(chunks: Sequence[Chunk]) -> frozenset[str]:
    """Files whose diff is spread over more than one chunk."""
    seen: set[str] = set()
    split: set[str] = set()
    for chunk in chunks:
        for name in chunk.files:
            (split if name in seen else seen).add(name)
    return frozenset(split)


def findings_for_chunk(
    context: DeltaContext, chunk: Chunk, split: frozenset[str]
) -> tuple[dict[str, Any], ...]:
    """The open findings this chunk's prompt lists for judgment.

    A finding is listed only in a chunk that holds its file; a chunk without
    the file cannot see the code and would otherwise answer for it. A finding
    on a file split across chunks is further limited to the chunk whose own
    hunks cover its line (re-reporting a finding a sibling chunk resolved, or
    dropping one it never saw, is the failure this prevents). The listing cap
    still applies, by position in the whole list."""
    return tuple(
        finding
        for finding in _listed(context)
        if finding["file"] in chunk.files
        and (finding["file"] not in split or chunk.covers(finding["file"], finding["line"]))
    )


def render_delta_note(
    context: DeltaContext, listed: Sequence[dict[str, Any]] | None = None
) -> str:
    """Prompt text framing each chunk as part of an incremental review.
    *listed* is the findings to list (default: every listable open finding,
    see findings_for_chunk for the per-chunk subset)."""
    lines = [
        "## Incremental review",
        "",
        f"The diff below holds ONLY the changes made since the last reviewed head "
        f"({context.since_head[:12]}). It is not the whole pull request.",
        "",
        "1. Report a new defect only when a line this diff adds or changes "
        "introduces it. Do not report problems on lines this diff does not touch.",
        "2. Each open finding listed below comes from the last review. When this "
        "diff changes the file a finding names, decide whether the change "
        "resolves it. If it does not, report it again, keeping its file and "
        "rule_id and the severity given. If it does, say nothing about it. "
        "When this diff does not change that file, say nothing about the finding.",
        "",
        "Open findings from the last review:",
    ]
    shown = _listed(context) if listed is None else listed
    if not shown:
        lines.append("- none")
    for finding in shown:
        lines.append(
            f"- {finding['file']}:{finding['line']} [{finding['rule_id']}] "
            f"({finding['severity']}) {finding['message']}"
        )
    omitted = len(context.open_findings) - len(_listed(context))
    if omitted > 0:
        lines.append(
            f"- ... and {omitted} more not listed; they are carried forward "
            "unchanged and stay open"
        )
    return "\n".join(lines)


def _listed(context: DeltaContext) -> tuple[dict[str, Any], ...]:
    return context.open_findings[:MAX_LISTED_FINDINGS]


def carried_findings(
    context: DeltaContext,
    touched_files: set[str],
    chunks: Sequence[Chunk] = (),
) -> list[dict[str, Any]]:
    """Open findings the delta review is not asked to re-judge, so they stay
    open: those on files the delta did not touch (it cannot have resolved what
    it never changed), and every finding past the listing cap regardless of
    file (the reviewer never saw it, so it cannot have resolved it). With
    *chunks*, also a finding on a file no chunk holds, and one on a split file
    that no chunk's hunks cover: no chunk was asked about it, so none can have
    resolved it. An open finding is never dropped."""
    split = split_files(chunks)
    return [
        dict(f)
        for position, f in enumerate(context.open_findings)
        if position >= MAX_LISTED_FINDINGS
        or f["file"] not in touched_files
        or (bool(chunks) and not any(f["file"] in chunk.files for chunk in chunks))
        or (
            f["file"] in split
            and not any(chunk.covers(f["file"], f["line"]) for chunk in chunks)
        )
    ]
