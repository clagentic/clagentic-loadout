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

Every prior open finding ends a delta review as exactly one of:

  * carried: its file is not in the delta at all (the file is unchanged, so
    its line is unchanged), or it was too far down the listing cap to be
    shown to the reviewer. `carried_findings` returns it, so a delta review
    can never turn "clean" by simply not looking;
  * re-judged: its file is touched anywhere in the delta. The reviewer gets it
    in exactly one chunk, with its prior location, the line as it then read and
    a bounded excerpt of the new head around where it now is. The reviewer
    either reports it again (kept), or names its id in the reply's `resolved`
    list (resolved). A fix made elsewhere in the file (a new helper, a setUp, a
    refactor) is a resolution, which a hunk-coverage test on the old line could
    not see. Silence resolves nothing: a finding neither restated nor named
    stays open, re-anchored where the delta places it, so a reply that omits it
    can never turn a review clean;
  * resolved by the caller: the caller ruled it resolved or refuted (see
    review.resolutions). It is neither shown to the reviewer nor carried.

`account_for_prior` computes that outcome once the reviewer has answered.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.acquire.contract import AcquiredPr, RangeDiffBackend
from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.review.chunking import Chunk
from clagentic_loadout.review.prior_view import (
    MAX_LINE_CHARS,
    locate_prior_line,
    new_head_excerpt,
    prior_line_text,
)
from clagentic_loadout.review.resolutions import (
    BY_CALLER,
    BY_REVIEWER,
    Resolution,
    locator,
    resolved_entry,
    split_resolved,
)

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
    #: Open findings the caller ruled resolved, as `resolved` entries (see
    #: review.resolutions.resolved_entry). They are not in open_findings.
    caller_resolved: tuple[dict[str, Any], ...] = ()

    @property
    def prior_open_count(self) -> int:
        return len(self.open_findings) + len(self.caller_resolved)


@dataclass(frozen=True)
class DeltaResolution:
    """The outcome of deciding between a delta and a full review."""

    #: What to review: the delta diff when a delta applies, else the input.
    acquired: AcquiredPr
    #: None means a full-diff review.
    context: DeltaContext | None
    reason: str = ""
    detail: str = ""
    #: Refs of caller rulings that matched no prior finding (every ruling when
    #: the review is a full one, which has no prior findings to rule on).
    unknown_resolutions: tuple[str, ...] = ()

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
    resolutions: Sequence[Resolution] = (),
) -> DeltaResolution:
    """Decide whether *acquired* (the PR at its current head) is reviewed as a
    delta since *since_head*, and build what to review accordingly. The
    caller's *resolutions* take the open findings they match out of the
    review."""

    def full(reason: str, detail: str = "") -> DeltaResolution:
        return DeltaResolution(
            acquired, None, reason=reason, detail=detail,
            unknown_resolutions=tuple(r.ref for r in resolutions),
        )

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
    remaining, ruled, unknown = split_resolved(open_findings_of(prior_findings), resolutions)
    return DeltaResolution(
        dataclasses.replace(acquired, base_sha=since_head, diff_text=span.diff_text),
        DeltaContext(
            since_head=since_head,
            open_findings=tuple(remaining),
            caller_resolved=tuple(
                resolved_entry(finding, BY_CALLER, ruling.reason) for finding, ruling in ruled
            ),
        ),
        unknown_resolutions=tuple(unknown),
    )


def owner_chunk_index(finding: dict[str, Any], chunks: Sequence[Chunk]) -> int | None:
    """The one chunk that judges *finding*: the lowest-index chunk whose hunks
    cover its prior line, else the lowest-index chunk holding its file; None
    when no chunk holds the file. One owner keeps a file split across chunks
    from judging, and so reporting, the same finding twice."""
    ordered = sorted(chunks, key=lambda chunk: chunk.index)
    for chunk in ordered:
        if chunk.covers_prior_line(finding["file"], finding["line"]):
            return chunk.index
    for chunk in ordered:
        if finding["file"] in chunk.files:
            return chunk.index
    return None


def findings_for_chunk(
    context: DeltaContext, chunk: Chunk, chunks: Sequence[Chunk]
) -> tuple[dict[str, Any], ...]:
    """The open findings this chunk's prompt lists for re-judgment.

    Each finding is listed in exactly one chunk, its owner (see
    owner_chunk_index). Its line is in the numbering of the last reviewed head,
    so a fix that lands in another chunk of the same file is not seen by the
    owner; the finding then stays reported (kept), which fails safe. The
    listing cap applies, by position in the whole list."""
    return tuple(
        finding
        for finding in _listed(context)
        if owner_chunk_index(finding, chunks) == chunk.index
    )


def _render_listed_finding(finding: dict[str, Any], chunks: Sequence[Chunk]) -> list[str]:
    """One listed finding: its id, claim, prior location, the line as it read
    at the last reviewed head (when the diff shows it), and a bounded excerpt of
    the new head around where it now is (only lines the diff shows)."""
    lines = [
        f"- {finding['file']}:{finding['line']} [{finding['rule_id']}] "
        f"({finding['severity']}) {finding['message']} "
        f"(prior location: line {finding['line']})",
        f"  id: {locator(finding)}",
    ]
    old_text = prior_line_text(chunks, finding["file"], finding["line"])
    if old_text is not None:
        lines.append(f"  that line then read: {old_text.strip()[:MAX_LINE_CHARS]}")
    place = locate_prior_line(chunks, finding["file"], finding["line"])
    if place.mapped is not None:
        lines.append(f"  that line is now line {place.mapped} of the new head")
    else:
        lines.append(
            f"  that line is not present in the new head; the nearest new-head "
            f"position is line {place.anchor}"
        )
    excerpt = new_head_excerpt(chunks, finding["file"], place.anchor)
    if excerpt:
        lines.append("  new head around there (only lines this diff shows):")
        lines.extend(f"    {entry}" for entry in excerpt)
    return lines


def render_delta_note(
    context: DeltaContext,
    listed: Sequence[dict[str, Any]] | None = None,
    chunks: Sequence[Chunk] = (),
) -> str:
    """Prompt text framing each chunk as part of an incremental review.
    *listed* is the findings to list (default: every listable open finding,
    see findings_for_chunk for the per-chunk subset). *chunks* is the whole
    delta plan, which each listed finding's prior line text and new-head
    excerpt are read from."""
    lines = [
        "## Incremental review",
        "",
        f"The diff below holds ONLY the changes made since the last reviewed head "
        f"({context.since_head[:12]}). It is not the whole pull request.",
        "",
        "1. Report a new defect only when a line this diff adds or changes "
        "introduces it. Do not report problems on lines this diff does not touch.",
        "2. Each open finding listed below comes from the last review and names "
        "a file this diff changes. Its prior location is numbered as of the "
        "last reviewed head; the code may have moved, and a fix may sit "
        "anywhere in the file (a new helper, a setUp or tearDown, a refactor), "
        "not only on that line. Decide against the code as it now stands "
        "whether the finding still applies. If it does, report it again at the "
        "line of the new code where it now is, keeping its file and rule_id "
        "and the severity given. Only if it no longer applies, name its id in "
        'the "resolved" list of your reply (see the incremental reply form '
        "before the output contract). A finding you neither report again nor "
        "name in that list stays open.",
        "",
        "Open findings from the last review:",
    ]
    shown = _listed(context) if listed is None else listed
    if not shown:
        lines.append("- none")
    for finding in shown:
        lines.extend(_render_listed_finding(finding, chunks))
    omitted = len(context.open_findings) - len(_listed(context))
    if omitted > 0:
        lines.append(
            f"- ... and {omitted} more not listed; they are carried forward "
            "unchanged and stay open"
        )
    return "\n".join(lines)


def _listed(context: DeltaContext) -> tuple[dict[str, Any], ...]:
    return context.open_findings[:MAX_LISTED_FINDINGS]


def carried_findings(context: DeltaContext, touched_files: set[str]) -> list[dict[str, Any]]:
    """Open findings the delta review is not asked to re-judge, so they stay
    open unchanged: those on files the delta did not touch (the file is
    unchanged, so the finding's line still names the same code), and every
    finding past the listing cap regardless of file (the reviewer never saw
    it, so it cannot have resolved it). An open finding is never dropped."""
    return [
        dict(f)
        for position, f in enumerate(context.open_findings)
        if _is_carried(position, f, touched_files)
    ]


def _is_carried(position: int, finding: dict[str, Any], touched_files: set[str]) -> bool:
    return position >= MAX_LISTED_FINDINGS or finding["file"] not in touched_files


@dataclass(frozen=True)
class PriorAccounting:
    """How a delta review accounted for each prior open finding."""

    carried: list[dict[str, Any]]
    #: (prior finding, the reviewer's finding that re-anchors it at the new head)
    kept: list[tuple[dict[str, Any], dict[str, Any]]]
    #: `resolved` entries: those the reviewer resolved, then those the caller did.
    resolved: list[dict[str, Any]]
    #: Re-judged prior findings the reviewer neither restated nor explicitly
    #: resolved. They stay open, as findings of their own at the new head's line
    #: when it maps unambiguously (else at the prior line), each carrying
    #: `prior_line`.
    left_open: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    @property
    def kept_count(self) -> int:
        """Prior findings still open after re-judgment, restated or not."""
        return len(self.kept) + len(self.left_open)


def _pair_reports(
    priors: Sequence[tuple[int, dict[str, Any]]], reported: Sequence[dict[str, Any]]
) -> dict[int, int]:
    """Map the position of each prior finding the reviewer kept to the index of
    the reported finding that re-anchors it. A pair shares file and rule_id;
    the same message, then the closest line, is paired first, and each
    reported finding re-anchors at most one prior finding."""
    candidates = sorted(
        (
            (reported[index]["message"] != prior["message"],
             abs(reported[index]["line"] - prior["line"]), position, index)
            for position, prior in priors
            for index in range(len(reported))
            if reported[index]["file"] == prior["file"]
            and reported[index]["rule_id"] == prior["rule_id"]
        )
    )
    paired: dict[int, int] = {}
    claimed: set[int] = set()
    for _differs, _distance, position, index in candidates:
        if position not in paired and index not in claimed:
            paired[position] = index
            claimed.add(index)
    return paired


def reviewer_resolved_positions(
    context: DeltaContext,
    chunks: Sequence[Chunk],
    resolved_ids: Mapping[int, Collection[str]],
) -> frozenset[int]:
    """Positions in context.open_findings of the prior findings the reviewer
    explicitly resolved. Only the chunk that owns a finding (see
    owner_chunk_index) can resolve it, and only by naming its id; a finding
    that chunk was never shown (past the listing cap, or on an untouched file)
    cannot be resolved by anyone's reply."""
    positions: set[int] = set()
    for position, finding in enumerate(_listed(context)):
        owner = owner_chunk_index(finding, chunks)
        if owner is not None and locator(finding) in resolved_ids.get(owner, ()):
            positions.add(position)
    return frozenset(positions)


def _re_anchored(prior: dict[str, Any], chunks: Sequence[Chunk]) -> dict[str, Any]:
    """*prior* as a finding at the new head: at its mapped line when the delta
    places it unambiguously, else left at the prior line."""
    mapped = locate_prior_line(chunks, prior["file"], prior["line"]).mapped
    return {**prior, "line": mapped if mapped is not None else prior["line"], "prior_line": prior["line"]}


def account_for_prior(
    context: DeltaContext,
    touched_files: set[str],
    reported: Sequence[dict[str, Any]],
    chunks: Sequence[Chunk] = (),
    resolved_positions: Collection[int] = frozenset(),
) -> PriorAccounting:
    """Place every prior open finding in exactly one outcome.

    *reported* is what the reviewer reported for the delta; *resolved_positions*
    (see reviewer_resolved_positions) are the prior findings it explicitly
    resolved. A re-judged finding (neither carried nor ruled on by the caller)
    is:

      * kept when the reviewer reported a finding with its file and rule_id,
        pairing the closest unclaimed one;
      * resolved only when the reviewer named it as resolved;
      * otherwise left open, re-anchored via *chunks*. Silence never resolves.

    A reported finding is never dropped for resembling a prior one: the only
    removal is merge_findings' dedupe of an identical finding."""
    carried = carried_findings(context, touched_files)
    rejudged = [
        (position, prior)
        for position, prior in enumerate(context.open_findings)
        if not _is_carried(position, prior, touched_files)
    ]
    # An explicit resolution is authoritative, so it is not paired with a
    # report that may be an unrelated finding of the same rule.
    paired = _pair_reports(
        [(position, prior) for position, prior in rejudged if position not in resolved_positions],
        reported,
    )
    kept = [(prior, reported[paired[position]]) for position, prior in rejudged if position in paired]
    resolved = [
        resolved_entry(prior, BY_REVIEWER)
        for position, prior in rejudged
        if position not in paired and position in resolved_positions
    ]
    resolved.extend(context.caller_resolved)
    left_open = [
        _re_anchored(prior, chunks)
        for position, prior in rejudged
        if position not in paired and position not in resolved_positions
    ]
    return PriorAccounting(carried, kept, resolved, left_open)
