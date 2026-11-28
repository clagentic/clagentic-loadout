"""transcript.decisions -- permission decisions recorded in a session transcript.

A transcript is a JSONL file, one record per line. A decision is the
``permissionDecision`` object (``decision``, ``source``, ``reasonType``, and
optionally ``ruleSource``) on a tool-result record: ``type`` is ``user`` and
``message.content[]`` holds a ``tool_result`` block with a ``tool_use_id``. The tool
name and its full input come from the ``tool_use`` block (``id``, ``name``,
``input``) of the assistant record that issued the call.

Subagent transcripts sit at ``<transcript dir>/<session id>/subagents/agent-<id>.jsonl``
where ``<session id>`` is the transcript file's stem. Every record in them carries
``agentId``, which attributes the decision; the transcript given on the command
line is the main session.

Each decision is put in one class by CLASSIFICATION, a table over the raw
``decision``, ``source`` and ``reasonType`` values; the first matching row wins. A
combination no row matches is ``unclassified``: it is counted and listed with its raw
values, never guessed. A newly recorded value is added as one row. Reading never
writes, and nothing here refuses on a count.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAIN_AGENT = "main"
RULE_OR_CONFIG = "rule-or-config"
PERSON_ASKED = "person-asked"
UNCLASSIFIED = "unclassified"
CLASSES = (PERSON_ASKED, UNCLASSIFIED, RULE_OR_CONFIG)

#: (class, decision, source, reasonType); None matches any value. First match wins.
#: A saved allow rule (reasonType "rule") matches with no person asked even when its
#: source reads user_permanent; a shown card that timed out or was rejected records
#: user_reject.
CLASSIFICATION: tuple[tuple[str, str | None, str | None, str | None], ...] = (
    (RULE_OR_CONFIG, None, None, "rule"),
    (RULE_OR_CONFIG, None, "config", "subcommandResults"),
    (PERSON_ASKED, None, "user_reject", None),
    (PERSON_ASKED, "user_reject", None, None),
)


def classify(decision: str | None, source: str | None, reason_type: str | None) -> str:
    for cls, want_decision, want_source, want_reason in CLASSIFICATION:
        if (
            want_decision in (None, decision)
            and want_source in (None, source)
            and want_reason in (None, reason_type)
        ):
            return cls
    return UNCLASSIFIED


SUBAGENT_DIR = "subagents"
SUBAGENT_GLOB = "agent-*.jsonl"
SUBAGENT_FILE_PREFIX = "agent-"


@dataclass(frozen=True)
class Decision:
    agent: str
    tool: str | None
    input: Any
    decision: str | None
    source: str | None
    reason_type: str | None
    rule_source: str | None
    timestamp: str | None

    @property
    def cls(self) -> str:
        return classify(self.decision, self.source, self.reason_type)

    def to_json_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "class": self.cls,
            "agent": self.agent,
            "tool": self.tool,
            "input": self.input,
            "decision": self.decision,
            "source": self.source,
            "reasonType": self.reason_type,
            "timestamp": self.timestamp,
        }
        if self.rule_source is not None:
            out["ruleSource"] = self.rule_source
        return out


@dataclass
class Report:
    transcript: str
    decisions: list[Decision] = field(default_factory=list)
    subagent_transcripts: int = 0
    skipped_lines: int = 0
    unreadable: list[str] = field(default_factory=list)

    @property
    def counts_by_class(self) -> dict[str, int]:
        counts = {cls: 0 for cls in CLASSES}
        for item in self.decisions:
            counts[item.cls] += 1
        return counts

    @property
    def counts_by_combination(self) -> list[dict[str, Any]]:
        counts: dict[tuple[str | None, str | None, str | None], int] = {}
        for item in self.decisions:
            key = (item.decision, item.source, item.reason_type)
            counts[key] = counts.get(key, 0) + 1
        return [
            {"class": classify(*key), "decision": key[0], "source": key[1],
             "reasonType": key[2], "count": count}
            for key, count in sorted(counts.items(), key=lambda kv: tuple(map(str, kv[0])))
        ]

    @property
    def listed(self) -> list[Decision]:
        """Decisions a reader must see: person-asked and unclassified."""
        return [d for d in self.decisions if d.cls != RULE_OR_CONFIG]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "transcript": self.transcript,
            "decisions": len(self.decisions),
            "counts_by_class": self.counts_by_class,
            "counts_by_combination": self.counts_by_combination,
            "listed_decisions": [d.to_json_dict() for d in self.listed],
            "subagent_transcripts": self.subagent_transcripts,
            "skipped_lines": self.skipped_lines,
        }


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def subagent_paths(transcript: Path) -> list[Path]:
    """The subagent transcripts of *transcript*'s session, sorted. Raises OSError
    when the directory exists but cannot be listed."""
    directory = transcript.parent / transcript.stem / SUBAGENT_DIR
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob(SUBAGENT_GLOB) if p.is_file())


def _blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [b for b in content if isinstance(b, dict)]


def _read_records(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Parsed object records of one JSONL file and the number of lines skipped
    because they were not a JSON object. Raises OSError when unreadable."""
    records: list[dict[str, Any]] = []
    skipped = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
            else:
                skipped += 1
    return records, skipped


def _decisions_of(records: list[dict[str, Any]], agent_for: Any) -> list[Decision]:
    """Decisions in *records*; *agent_for(record)* names the agent of each."""
    uses: dict[str, tuple[Any, Any]] = {}
    for record in records:
        if record.get("type") != "assistant":
            continue
        for block in _blocks(record):
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                uses[block["id"]] = (block.get("name"), block.get("input"))
    found: list[Decision] = []
    for record in records:
        perm = record.get("permissionDecision")
        if record.get("type") != "user" or not isinstance(perm, dict):
            continue
        use_id = next(
            (b.get("tool_use_id") for b in _blocks(record) if b.get("type") == "tool_result"),
            None,
        )
        name, tool_input = uses.get(use_id, (None, None)) if isinstance(use_id, str) else (None, None)
        found.append(
            Decision(
                agent=agent_for(record),
                tool=_str_or_none(name),
                input=tool_input,
                decision=_str_or_none(perm.get("decision")),
                source=_str_or_none(perm.get("source")),
                reason_type=_str_or_none(perm.get("reasonType")),
                rule_source=_str_or_none(perm.get("ruleSource")),
                timestamp=_str_or_none(record.get("timestamp")),
            )
        )
    return found


def read_decisions(transcript: Path) -> Report:
    """Read *transcript* and its subagent transcripts. Every path that could not be
    read is listed in ``Report.unreadable``; the decisions are then incomplete and a
    caller must not present them as counts."""
    report = Report(transcript=str(transcript))
    try:
        subagents = subagent_paths(transcript)
    except OSError:
        report.unreadable.append(str(transcript.parent / transcript.stem / SUBAGENT_DIR))
        subagents = []
    sources: list[tuple[Path, Any]] = [(transcript, lambda _record: MAIN_AGENT)]
    for path in subagents:
        fallback = path.stem[len(SUBAGENT_FILE_PREFIX) :]
        sources.append((path, lambda record, fb=fallback: _str_or_none(record.get("agentId")) or fb))
    for path, agent_for in sources:
        try:
            records, skipped = _read_records(path)
        except OSError:
            report.unreadable.append(str(path))
            continue
        if path is not transcript:
            report.subagent_transcripts += 1
        report.skipped_lines += skipped
        report.decisions.extend(_decisions_of(records, agent_for))
    return report
