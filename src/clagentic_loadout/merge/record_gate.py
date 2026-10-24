"""merge.record_gate -- the merge verb's verdict-record requirement.

A gate output is a record or a refusal. After every other check has passed, the
merge verb asks, for the exact head it is about to merge, whether each gate the
configuration requires left a verdict record (verdict_record). A required gate
with no record at that head is NOT MEASURED and refuses the merge; a record that
exists but did not pass refuses it too. Absence is never a pass.

WHAT IS REQUIRED is exactly what the merge configuration already requires, and
nothing more:

  * one gate per required reviewer role (``reviewer:<role>``);
  * one per configured pre-merge check (``pre_check:<label>``), unless the checks
    were skipped by the caller's explicit flag;
  * the CI read (``ci``);
  * one per configured push verification entry (``push.verify:<name>``), when the
    repository configures ``push.verify``.

WHO WRITES WHAT. The pre-merge checks and the CI read are run by this verb, so
this verb writes their records at merge time, for the head it is about to merge,
and reads them back through the same reader as every other gate. Reviewer
records come from the review verdict post and, for a verdict posted before
records existed, from the fence the gate has already accepted at the exact head
(verdict_record.reader.record_from_fence). Push verification records come from
the push verb.

The refusal uses the existing gate-refusal exit code of the merge verb.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.verdict_record.emit import emit_record
from clagentic_loadout.verdict_record.reader import (
    STATUS_FAILED,
    GateReading,
    RequiredGate,
    read_gates,
)
from clagentic_loadout.verdict_record.record import (
    GATE_CI,
    KIND_PRE_CHECK,
    KIND_PUSH_VERIFY,
    KIND_REVIEWER,
    RecordFormatError,
    VerdictRecord,
    gate_name,
)
from clagentic_loadout.verdict_record.store import StoreError, records_root

#: ``tool`` on a record this verb writes about its own checks.
TOOL_MERGE = "loadout-merge"

_LABEL_LIMIT = 80

_PUSH_VERIFY_REMEDY = (
    "to record it for a commit that is already pushed, run `loadout-push --update-pr --pr <n> "
    "--append-body` from a clean checkout of exactly that commit; the configured verification "
    "commands re-run at that head and leave a record"
)


@dataclass(frozen=True)
class GateRun:
    """When a gate this verb ran started and finished (record timestamps)."""

    started: str
    finished: str


def pre_check_labels(steps: Sequence[Mapping]) -> list[str]:
    """One distinct label per configured pre-merge check, in declaration order:
    the step's description when it has one, else its command, shortened; a repeat
    gets its position appended so two checks never share a gate."""
    labels: list[str] = []
    seen: set[str] = set()
    for index, step in enumerate(steps, start=1):
        description = step.get("description")
        if isinstance(description, str) and description.strip():
            base = description.strip()
        else:
            cmd = step.get("cmd")
            base = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        base = " ".join(base.split())[:_LABEL_LIMIT] or f"check {index}"
        label = base if base not in seen else f"{base} #{index}"
        seen.add(label)
        labels.append(label)
    return labels


def required_gates(
    *,
    reviewer_names: Sequence[str],
    pre_check_steps: Sequence[Mapping],
    push_verify_names: Sequence[str],
) -> list[RequiredGate]:
    """Every gate the configuration requires at merge, in a stable order."""
    gates = [RequiredGate(gate_name(KIND_REVIEWER, name)) for name in reviewer_names]
    gates += [
        RequiredGate(gate_name(KIND_PRE_CHECK, label)) for label in pre_check_labels(pre_check_steps)
    ]
    gates.append(RequiredGate(GATE_CI))
    gates += [
        RequiredGate(gate_name(KIND_PUSH_VERIFY, name), _PUSH_VERIFY_REMEDY)
        for name in push_verify_names
    ]
    return gates


def emit_merge_records(
    *,
    head_sha: str,
    repo: str,
    ci: GateRun,
    pre_check_steps: Sequence[Mapping],
    pre_checks: GateRun | None,
    untrusted_roots: Sequence[Path] = (),
    root: Path | None = None,
) -> list[VerdictRecord]:
    """Write the records for the gates this verb ran and that all passed: the CI
    read and each configured pre-merge check. The exit is the gate's outcome (a
    refusal never reaches here), not a single command's code. Raises
    StoreError or RecordFormatError."""
    store = root if root is not None else records_root()
    written = [
        emit_record(
            gate=GATE_CI,
            head_sha=head_sha,
            tool=TOOL_MERGE,
            exit=0,
            started=ci.started,
            finished=ci.finished,
            repo=repo,
            root=store,
            untrusted_roots=untrusted_roots,
        )
    ]
    if pre_checks is not None:
        for label in pre_check_labels(pre_check_steps):
            written.append(
                emit_record(
                    gate=gate_name(KIND_PRE_CHECK, label),
                    head_sha=head_sha,
                    tool=TOOL_MERGE,
                    exit=0,
                    started=pre_checks.started,
                    finished=pre_checks.finished,
                    repo=repo,
                    root=store,
                    untrusted_roots=untrusted_roots,
                )
            )
    return written


def _describe(reading: GateReading) -> str:
    if reading.status == STATUS_FAILED:
        record = reading.record
        return (
            f"gate {reading.gate!r} measured head {reading.expected_head} and did not pass "
            f"(exit {record.exit}, record {record.id})"
        )
    found = reading.other_head if reading.other_head is not None else "none"
    line = (
        f"gate {reading.gate!r}: expected a record at head {reading.expected_head}, "
        f"record found for head {found}"
    )
    if reading.remedy:
        line += f" ({reading.remedy})"
    return line


def check_gate_records(
    required: Sequence[RequiredGate],
    *,
    head_sha: str,
    repo: str,
    adapted: Mapping[str, Sequence[VerdictRecord]],
    untrusted_roots: Sequence[Path] = (),
    root: Path | None = None,
) -> str | None:
    """None when every *required* gate has a passing record at *head_sha*, else
    the refusal message naming each gate that did not, the head expected and the
    head of the record found (or none). Files in the store that could not be read
    are warned about on stderr. Raises StoreError when the store cannot be used."""
    store = root if root is not None else records_root()
    readings, skipped = read_gates(
        store, required, head_sha, repo=repo, adapted=adapted, untrusted_roots=untrusted_roots
    )
    for reason in skipped:
        print(f"merge: WARNING -- verdict record ignored: {reason}", file=sys.stderr)
    problems = [reading for reading in readings if not reading.ok]
    if not problems:
        return None
    lines = "\n".join(f"  - {_describe(reading)}" for reading in problems)
    return (
        f"not-measured: {len(problems)} required gate(s) have no passing verdict record at the "
        f"exact head {head_sha} being merged. A gate that did not run, or ran against another "
        f"commit, is not a pass.\n{lines}"
    )


def unmeasured_reviewer_message(
    reviewer_name: str,
    *,
    head_sha: str,
    repo: str,
    untrusted_roots: Sequence[Path] = (),
    root: Path | None = None,
) -> str | None:
    """The not-measured refusal text for one reviewer role, or None when the
    role is measured at *head_sha* or the store cannot be read. Used to name the
    gate when the fence checks refuse a role that has no verdict at the head, so
    that refusal states what was expected and what was found."""
    try:
        return check_gate_records(
            [RequiredGate(gate_name(KIND_REVIEWER, reviewer_name))],
            head_sha=head_sha,
            repo=repo,
            adapted={},
            untrusted_roots=untrusted_roots,
            root=root,
        )
    except (StoreError, RecordFormatError):
        return None


__all__ = [
    "TOOL_MERGE",
    "GateRun",
    "RecordFormatError",
    "StoreError",
    "check_gate_records",
    "emit_merge_records",
    "pre_check_labels",
    "required_gates",
    "unmeasured_reviewer_message",
]
