"""verdict_record.reader — read what the required gates measured at one exact commit.

This is the single path every consumer reads records through. ``read_gates``
answers, for each required gate, one of three things about the commit asked for:

  measured       a record at exactly that head exists and the newest one passed;
  failed         a record at exactly that head exists and the newest one did not
                 pass;
  not measured   no record exists at that head. A record at any other head says
                 nothing about this one, and its head is reported only to help
                 the reader see what ran.

Absence is never a pass: a gate that is required and has no record at the head is
``not measured``.

THE FENCE ADAPTER. A reviewer verdict posted before records existed is a fenced
block in a pull request comment, not a stored record. ``record_from_fence`` turns
a fence that the merge verb's own fence checks already accepted into the record
that fence amounts to, and ``read_gates`` takes those through its ``adapted``
argument next to the stored ones, so a pull request reviewed by an earlier
release is read by the same code as one reviewed by this release. Only the head
binding is decided here: an adapted record at another head is ignored exactly as
a stored one is.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from clagentic_loadout.verdict_record.record import (
    TOOL_FENCE,
    VerdictRecord,
    utc_timestamp,
)
from clagentic_loadout.verdict_record.store import (
    newest_record_for_gate,
    records_at_head,
)

STATUS_MEASURED = "measured"
STATUS_FAILED = "failed"
STATUS_NOT_MEASURED = "not-measured"


@dataclass(frozen=True)
class RequiredGate:
    """A gate a consumer requires, with the sentence it wants appended to the
    refusal when the gate was not measured (how to measure it)."""

    gate: str
    remedy: str = ""


@dataclass(frozen=True)
class GateReading:
    """What one required gate shows at the expected head."""

    gate: str
    expected_head: str
    status: str
    #: The record that decided the reading at the expected head; None when not measured.
    record: VerdictRecord | None = None
    #: Not measured only: the head of the newest record this gate has at any
    #: other commit, or None when it has none.
    other_head: str | None = None
    remedy: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_MEASURED


def _decisive(candidates: Sequence[VerdictRecord]) -> VerdictRecord:
    """The newest record by finish time, then by id."""
    return max(candidates, key=lambda record: (record.finished, record.id))


def read_gates(
    root: Path,
    required: Sequence[RequiredGate],
    head_sha: str,
    *,
    repo: str | None = None,
    adapted: Mapping[str, Sequence[VerdictRecord]] | None = None,
    untrusted_roots: Sequence[Path] = (),
) -> tuple[list[GateReading], tuple[str, ...]]:
    """Read every *required* gate at *head_sha*. Returns the readings in the
    order asked for and the files in the store that could not be read as records
    for this head (reported, never counted). *adapted* holds records derived from
    other evidence (see ``record_from_fence``), keyed by gate. A record naming a
    repository other than *repo* does not count."""
    at_head = records_at_head(root, head_sha, untrusted_roots=untrusted_roots)
    extra = adapted or {}
    readings: list[GateReading] = []
    for item in required:
        candidates = [
            record
            for record in (*at_head.records, *extra.get(item.gate, ()))
            if record.gate == item.gate
            and record.head_sha == head_sha
            and (repo is None or record.repo in (None, repo))
        ]
        if not candidates:
            elsewhere = newest_record_for_gate(
                root, item.gate, repo=repo, untrusted_roots=untrusted_roots
            )
            readings.append(
                GateReading(
                    gate=item.gate,
                    expected_head=head_sha,
                    status=STATUS_NOT_MEASURED,
                    other_head=elsewhere.head_sha if elsewhere is not None else None,
                    remedy=item.remedy,
                )
            )
            continue
        decisive = _decisive(candidates)
        readings.append(
            GateReading(
                gate=item.gate,
                expected_head=head_sha,
                status=STATUS_MEASURED if decisive.passed else STATUS_FAILED,
                record=decisive,
                remedy=item.remedy,
            )
        )
    return readings, at_head.skipped


def _fence_time(created_at: object) -> str:
    """The comment time as a record timestamp. A value that does not parse is
    the moment of reading: the fence was already accepted with a valid time."""
    if isinstance(created_at, str):
        try:
            parsed = datetime.fromisoformat(created_at.strip().replace("Z", "+00:00"))
        except ValueError:
            return utc_timestamp()
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return utc_timestamp(parsed)
    return utc_timestamp()


def record_from_fence(
    *,
    gate: str,
    head_sha: str,
    exit: int,
    comment_id: object,
    created_at: object,
    repo: str | None = None,
    docs_only: dict | None = None,
) -> VerdictRecord:
    """The record an accepted verdict fence amounts to. *head_sha* is the head
    the gate compared the fence against; *exit* is the outcome the gate reached
    on it (zero when its checks passed it, which can be so for a fence that
    records a blocking status when a standing ruling or disposition clears the
    findings); the evidence is the comment the fence was read from. The id is
    synthetic and the record is never stored: it exists so a fence is read by the
    same code as a stored record."""
    moment = _fence_time(created_at)
    return VerdictRecord(
        id=f"fence-comment-{comment_id}",
        gate=gate,
        head_sha=head_sha,
        tool=TOOL_FENCE,
        exit=exit,
        started=moment,
        finished=moment,
        repo=repo,
        evidence_ref=f"comment:{comment_id}",
        docs_only=docs_only,
    )


__all__ = [
    "STATUS_FAILED",
    "STATUS_MEASURED",
    "STATUS_NOT_MEASURED",
    "GateReading",
    "RequiredGate",
    "read_gates",
    "record_from_fence",
]
