"""verdict_record.record — the verdict record: one gate's measurement of one commit.

A gate output is a record or a refusal. Every step that measures a commit (the
push verification run, the review verdict post, the merge verb's own pre-merge
checks and CI read) writes one record per gate it ran; the merge verb reads them
back for the exact head it is about to merge, and a gate with no record at that
head is not measured. The shape is the published contract
``schemas/verdict-record.schema.json`` (``SCHEMA_VERSION`` 1); this module builds
and validates instances of it and names the gates.

Gate names are ``<kind>:<name>`` for a gate the configuration names (a reviewer
role, a pre-merge check, a push verification entry) and the bare ``ci`` for the
CI read. Every name comes from configuration the caller already holds; nothing
here lists roles or checks.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clagentic_loadout.envelope import validate_against_schema

SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "verdict-record.schema.json"

KIND_REVIEWER = "reviewer"
KIND_PRE_CHECK = "pre_check"
KIND_PUSH_VERIFY = "push.verify"
GATE_CI = "ci"

#: Source tags for the ``tool`` field of a record that was not written by the
#: step that measured but derived from evidence that step left behind.
TOOL_FENCE = "verdict-fence"

RECORD_ID_RE = re.compile(r"\A[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\Z")
_FULL_SHA_RE = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class RecordFormatError(ValueError):
    """A record, or a value meant for one, does not satisfy the published
    schema; the message names the field and the value it held."""


def gate_name(kind: str, name: str) -> str:
    """The gate name for a configured gate of *kind*."""
    return f"{kind}:{name}"


def utc_timestamp(moment: datetime | None = None) -> str:
    """*moment* (default now) as the schema's UTC second-resolution string."""
    when = moment if moment is not None else datetime.now(timezone.utc)
    return when.astimezone(timezone.utc).strftime(_TIME_FORMAT)


def parse_timestamp(value: str) -> datetime:
    """The datetime a record timestamp names. Raises RecordFormatError."""
    try:
        return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise RecordFormatError(f"{value!r} is not a UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)") from exc


def new_record_id(moment: datetime | None = None) -> str:
    """A fresh record id: the write time and eight random hex digits."""
    when = moment if moment is not None else datetime.now(timezone.utc)
    return f"{when.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def is_full_sha(value: object) -> bool:
    return isinstance(value, str) and _FULL_SHA_RE.match(value) is not None


@dataclass(frozen=True)
class VerdictRecord:
    """One validated record. ``as_dict`` is the serialised form the schema
    describes."""

    id: str
    gate: str
    head_sha: str
    tool: str
    exit: int
    started: str
    finished: str
    tree_sha: str | None = None
    repo: str | None = None
    spec_hash: str | None = None
    evidence_ref: str | None = None
    waiver: str | None = None

    @property
    def passed(self) -> bool:
        return self.exit == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "gate": self.gate,
            "head_sha": self.head_sha,
            "tree_sha": self.tree_sha,
            "repo": self.repo,
            "spec_hash": self.spec_hash,
            "tool": self.tool,
            "exit": self.exit,
            "started": self.started,
            "finished": self.finished,
            "evidence_ref": self.evidence_ref,
            "waiver": self.waiver,
        }


def validate_record_dict(data: object) -> VerdictRecord:
    """The record *data* describes. Raises RecordFormatError when it does not
    satisfy the published schema, or when it finished before it started."""
    if not isinstance(data, dict):
        raise RecordFormatError(f"a verdict record must be a JSON object, got {type(data).__name__}")
    errors = validate_against_schema(data, SCHEMA_PATH)
    if errors:
        raise RecordFormatError("; ".join(errors))
    if isinstance(data["exit"], bool):
        raise RecordFormatError("exit: a boolean is not an exit code")
    if parse_timestamp(data["finished"]) < parse_timestamp(data["started"]):
        raise RecordFormatError(
            f"finished {data['finished']!r} is earlier than started {data['started']!r}"
        )
    return VerdictRecord(
        id=data["id"],
        gate=data["gate"],
        head_sha=data["head_sha"],
        tool=data["tool"],
        exit=data["exit"],
        started=data["started"],
        finished=data["finished"],
        tree_sha=data.get("tree_sha"),
        repo=data.get("repo"),
        spec_hash=data.get("spec_hash"),
        evidence_ref=data.get("evidence_ref"),
        waiver=data.get("waiver"),
    )


def build_record(
    *,
    gate: str,
    head_sha: str,
    tool: str,
    exit: int,
    started: str,
    finished: str,
    tree_sha: str | None = None,
    repo: str | None = None,
    spec_hash: str | None = None,
    evidence_ref: str | None = None,
    waiver: str | None = None,
    record_id: str | None = None,
) -> VerdictRecord:
    """A new, validated record. Raises RecordFormatError for a value the schema
    refuses (a short sha, a gate name with a line break, a finish before the
    start)."""
    candidate = VerdictRecord(
        id=record_id if record_id is not None else new_record_id(),
        gate=gate,
        head_sha=head_sha,
        tool=tool,
        exit=exit,
        started=started,
        finished=finished,
        tree_sha=tree_sha,
        repo=repo,
        spec_hash=spec_hash,
        evidence_ref=evidence_ref,
        waiver=waiver,
    )
    return validate_record_dict(candidate.as_dict())


__all__ = [
    "GATE_CI",
    "KIND_PRE_CHECK",
    "KIND_PUSH_VERIFY",
    "KIND_REVIEWER",
    "RECORD_ID_RE",
    "SCHEMA_PATH",
    "SCHEMA_VERSION",
    "TOOL_FENCE",
    "RecordFormatError",
    "VerdictRecord",
    "build_record",
    "gate_name",
    "is_full_sha",
    "new_record_id",
    "parse_timestamp",
    "utc_timestamp",
    "validate_record_dict",
]
