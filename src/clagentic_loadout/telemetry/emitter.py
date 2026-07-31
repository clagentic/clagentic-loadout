"""emitter.py — telemetry event construction + emission (Wave A slice 5,
tome #688).

REBUILD of trace_writer.py, dispatch_sidecar.py, and agent_run_post.py on
the generic sink model (tome #687 §11). Where the source modules each
picked their own destination (a fixed filesystem path, a fixed remote
collector endpoint), this module builds one of loadout's own event records —
validated against clagentic_loadout's own published schemas — and hands it
to whichever TelemetrySink resolve_sink() returns. The emitter never knows
what the sink does with the record; that is entirely the sink's and the
deployment's concern (CLAUDE.md hard rule 2, transport-agnostic).

Public surface:
    emit_trace_event(kind, envelope, *, sink=None) -> bool
    emit_dispatch_record(dispatch_id, status, *, ts=None,
                          target_session_id=None, completed_ts=None,
                          result_summary=None, spawn_env=None,
                          sink=None) -> bool
    emit_dispatch_heartbeat(dispatch_id, *, ts=None, progress=None,
                             sink=None) -> bool
        # lr-99a8: dispatch-liveness convention. A running dispatch may
        # emit this between long steps to give a consumer (lead/monitor)
        # a repeatable, non-terminal liveness pulse distinguishing
        # still-working from stalled, without polling git HEAD or an
        # output-file mtime. Sibling schema to telemetry-dispatch-record
        # (see telemetry-dispatch-heartbeat.json for why it is not an
        # added dispatch-record status).
    emit_dispatch_resume_ack(dispatch_id, *, ts=None, resuming=None,
                              sink=None) -> bool
        # lr-99a8: dispatch-liveness convention. A resumed dispatch may
        # emit this immediately on resume, before starting a long task,
        # for instant liveness confirmation on the resume path. Schema +
        # emitter only -- how/whether a resume is actually delivered to a
        # running vs completed process is harness/transport territory,
        # outside loadout (CLAUDE.md hard rule 2).
    emit_agent_run(record, *, sink=None) -> bool
    build_agent_run_record(...) -> dict   # construction helper, no I/O
    emit_followup_events(task_id, agent_name, followups, *, ts=None,
                          sink=None) -> int
        # lr-7a6e: generic ingest for envelope-out.json's followups[].
        # Emits one telemetry-followup-event/v1 record per item to the
        # resolved sink and returns the count of items that were durably
        # delivered. loadout's job ends here -- it emits a typed event,
        # nothing more. It does NOT create tasks, does NOT import a
        # tracker client, and does NOT know what (if anything) a
        # deployment's own consumer does with the event.

Every emit_* function validates the record against its published schema
BEFORE handing it to the sink — an invalid record is a programmer error and
is raised as TelemetryValidationError, never silently sent. Sink-level
failures (a stopped collector, a network blip) are a separate concern:
sink.emit() itself never raises (see sink.py), so a delivery failure never
propagates out of these functions — they return the sink's success bool.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clagentic_loadout.telemetry.sink import TelemetrySink, resolve_sink

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"

TRACE_EVENT_SCHEMA = "telemetry-trace-event.json"
DISPATCH_RECORD_SCHEMA = "telemetry-dispatch-record.json"
DISPATCH_HEARTBEAT_SCHEMA = "telemetry-dispatch-heartbeat.json"
DISPATCH_RESUME_ACK_SCHEMA = "telemetry-dispatch-resume-ack.json"
AGENT_RUN_SCHEMA = "telemetry-agent-run.json"
FOLLOWUP_EVENT_SCHEMA = "telemetry-followup-event.json"

TRACE_EVENT_KINDS = ("envelope_in", "envelope_out")
DISPATCH_STATUSES = ("in_flight", "completed", "failed", "refused", "timed_out")


class TelemetryValidationError(ValueError):
    """A telemetry record failed schema validation before it reached a sink."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(record: dict[str, Any], schema_name: str) -> None:
    """Validate `record` against the named published schema. Lazy-imports
    jsonschema/referencing so a caller that never emits telemetry does not
    pay the import cost or require the dependency to be present."""
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    resources = []
    for json_path in SCHEMAS_DIR.glob("*.json"):
        with json_path.open(encoding="utf-8") as f:
            doc = json.load(f)
        if isinstance(doc, dict) and "$id" in doc:
            resources.append((doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012)))
    registry = Registry().with_resources(resources)

    schema_path = SCHEMAS_DIR / schema_name
    with schema_path.open(encoding="utf-8") as f:
        schema = json.load(f)

    validator = Draft202012Validator(schema, registry=registry)
    errors = sorted(validator.iter_errors(record), key=lambda e: list(e.absolute_path))
    if errors:
        details = "; ".join(
            f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errors
        )
        raise TelemetryValidationError(f"{schema_name}: {details}")


def _emit(record: dict[str, Any], schema_name: str, sink: TelemetrySink | None) -> bool:
    _validate(record, schema_name)
    target = sink if sink is not None else resolve_sink()
    return target.emit(record)


def emit_trace_event(
    kind: str,
    envelope: dict[str, Any],
    *,
    ts: str | None = None,
    sink: TelemetrySink | None = None,
) -> bool:
    """Emit one telemetry-trace-event/v1 record wrapping a dispatch envelope.

    Args:
        kind: "envelope_in" (dispatch start) or "envelope_out" (dispatch
            exit). Callers validate the envelope itself against
            envelope-in.json / envelope-out.json (clagentic_loadout.envelope)
            before calling this — this function trusts the payload it is
            given and only validates the wrapping trace-event shape.
        envelope: the already-validated envelope payload dict.
        ts: emission timestamp override (tests). Defaults to now (UTC).
        sink: sink override (tests / explicit routing). Defaults to
            resolve_sink() (env-driven, defaults to NoneSink).

    Returns:
        True if the sink durably delivered the record, False otherwise.
        Never raises for a sink-level failure — only for a malformed
        record (TelemetryValidationError) or an unrecognized kind.
    """
    if kind not in TRACE_EVENT_KINDS:
        raise ValueError(f"kind must be one of {TRACE_EVENT_KINDS}, got {kind!r}.")

    record = {
        "schema": "telemetry-trace-event/v1",
        "kind": kind,
        "ts": ts or _now_iso(),
        "envelope": envelope,
    }
    return _emit(record, TRACE_EVENT_SCHEMA, sink)


def emit_dispatch_record(
    dispatch_id: str,
    status: str,
    *,
    ts: str | None = None,
    target_session_id: str | None = None,
    completed_ts: str | None = None,
    result_summary: str | None = None,
    spawn_env: dict[str, str] | None = None,
    sink: TelemetrySink | None = None,
) -> bool:
    """Emit one telemetry-dispatch-record/v1 record.

    Consolidates the source dispatch-record + spawn-meta pair into a single
    write: the same dispatch_id is written once pre-spawn (status=
    "in_flight") and once post-spawn (a terminal status), each write
    carrying whatever fields are known at that point. spawn_env carries the
    deployment's own identity/attribution env keys opaquely — this function
    does not interpret or require any specific key names.

    Returns:
        True if the sink durably delivered the record, False otherwise.
    """
    if status not in DISPATCH_STATUSES:
        raise ValueError(f"status must be one of {DISPATCH_STATUSES}, got {status!r}.")
    if not dispatch_id:
        raise ValueError("dispatch_id must be a non-empty string.")

    record: dict[str, Any] = {
        "schema": "telemetry-dispatch-record/v1",
        "dispatch_id": dispatch_id,
        "status": status,
        "ts": ts or _now_iso(),
    }
    if target_session_id is not None:
        record["target_session_id"] = target_session_id
    if completed_ts is not None:
        record["completed_ts"] = completed_ts
    if result_summary is not None:
        record["result_summary"] = result_summary
    if spawn_env is not None:
        record["spawn_env"] = spawn_env

    return _emit(record, DISPATCH_RECORD_SCHEMA, sink)


def emit_dispatch_heartbeat(
    dispatch_id: str,
    *,
    ts: str | None = None,
    progress: str | None = None,
    sink: TelemetrySink | None = None,
) -> bool:
    """Emit one telemetry-dispatch-heartbeat/v1 record (lr-99a8).

    A liveness pulse a running dispatch may emit between long steps,
    referencing the same dispatch_id as the dispatch's pre-spawn
    telemetry-dispatch-record.json in_flight write. Non-terminal and
    repeatable -- a dispatch may emit any number of heartbeats before its
    terminal dispatch-record write. A consumer reading the configured sink
    treats the most recent heartbeat's ts as its liveness signal, so it can
    tell still-working from stalled without polling git HEAD or an
    output-file mtime (the observability gap this convention closes).

    loadout defines the record shape and emission only -- it does not
    schedule heartbeat cadence and does not itself judge staleness; that
    consumption logic is a deployment/harness concern (CLAUDE.md hard rule
    2, see docs/integration.md's dispatch-liveness section for a worked
    example).

    Returns:
        True if the sink durably delivered the record, False otherwise.
    """
    if not dispatch_id:
        raise ValueError("dispatch_id must be a non-empty string.")

    record: dict[str, Any] = {
        "schema": "telemetry-dispatch-heartbeat/v1",
        "dispatch_id": dispatch_id,
        "ts": ts or _now_iso(),
    }
    if progress is not None:
        record["progress"] = progress

    return _emit(record, DISPATCH_HEARTBEAT_SCHEMA, sink)


def emit_dispatch_resume_ack(
    dispatch_id: str,
    *,
    ts: str | None = None,
    resuming: str | None = None,
    sink: TelemetrySink | None = None,
) -> bool:
    """Emit one telemetry-dispatch-resume-ack/v1 record (lr-99a8).

    An immediate "received, working on X" acknowledgement a resumed
    dispatch may emit before starting a long task, referencing the same
    dispatch_id as the original in_flight write -- a resume continues an
    existing dispatch, it does not mint a new one. Gives a caller instant
    liveness confirmation on the resume path without waiting for the first
    heartbeat or the terminal dispatch-record write.

    Schema + emitter only: this function says nothing about HOW or WHEN a
    resume is actually delivered to a running vs completed process -- that
    delivery mechanism lives entirely in whatever harness/transport a
    deployment uses to resume a dispatch, outside loadout's tree (CLAUDE.md
    hard rule 2).

    Returns:
        True if the sink durably delivered the record, False otherwise.
    """
    if not dispatch_id:
        raise ValueError("dispatch_id must be a non-empty string.")

    record: dict[str, Any] = {
        "schema": "telemetry-dispatch-resume-ack/v1",
        "dispatch_id": dispatch_id,
        "ts": ts or _now_iso(),
    }
    if resuming is not None:
        record["resuming"] = resuming

    return _emit(record, DISPATCH_RESUME_ACK_SCHEMA, sink)


def build_agent_run_record(
    *,
    record_id: str,
    agent_name: str,
    spawn_id: str,
    started_at: str,
    finished_at: str,
    initiator: str = "",
    host: str = "",
    conversation_id: str = "",
    task_id: str = "",
    duration_ms: int = 0,
    model_used: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    tokens_cache_read: int = 0,
    tokens_cache_creation: int = 0,
    cost_estimate_usd: float = 0.0,
    billing_mode: str = "",
    exit_status: str = "unknown",
    error_codes: list[str] | None = None,
    num_turns: int = 0,
    pr_url: str = "",
    project: str = "",
    attempt: int = 1,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Build a telemetry-agent-run/v1 record dict (no I/O, no validation —
    call emit_agent_run() to validate + deliver). Pure construction helper
    so a caller can inspect/log the record before deciding to emit it."""
    record: dict[str, Any] = {
        "schema": "telemetry-agent-run/v1",
        "record_id": record_id,
        "agent_name": agent_name.strip().lower(),
        "spawn_id": spawn_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_status": exit_status,
        "recorded_at": recorded_at or _now_iso(),
    }
    optional: dict[str, Any] = {
        "initiator": initiator,
        "host": host,
        "conversation_id": conversation_id,
        "task_id": task_id,
        "duration_ms": max(0, int(duration_ms)),
        "model_used": model_used,
        "tokens_in": max(0, int(tokens_in)),
        "tokens_out": max(0, int(tokens_out)),
        "tokens_cache_read": max(0, int(tokens_cache_read)),
        "tokens_cache_creation": max(0, int(tokens_cache_creation)),
        "cost_estimate_usd": float(cost_estimate_usd),
        "billing_mode": billing_mode,
        "error_codes": list(error_codes) if error_codes else [],
        "num_turns": max(0, int(num_turns)),
        "pr_url": pr_url,
        "project": project,
        "attempt": max(1, int(attempt)),
    }
    for key, value in optional.items():
        # Omit empty-string / empty-list optionals rather than sending
        # noise — every field the schema declares is still optional there.
        if value == "" or value == []:
            continue
        record[key] = value
    return record


def emit_agent_run(record: dict[str, Any], *, sink: TelemetrySink | None = None) -> bool:
    """Validate and emit a telemetry-agent-run/v1 record (see
    build_agent_run_record() to construct one).

    Returns:
        True if the sink durably delivered the record, False otherwise.
    """
    return _emit(record, AGENT_RUN_SCHEMA, sink)


def emit_followup_events(
    task_id: str | None,
    agent_name: str,
    followups: list[dict[str, Any]] | None,
    *,
    ts: str | None = None,
    sink: TelemetrySink | None = None,
) -> int:
    """Emit one telemetry-followup-event/v1 record per item in `followups`
    (an envelope-out.json `followups[]` list, see
    common.json#/$defs/followup).

    Generic ingest, sink-routed (CLAUDE.md hard rule 2/6a): this function
    validates and hands each event to whichever TelemetrySink resolve_sink()
    returns; it never creates a task, never imports a tracker client, and
    never assumes what (if anything) the sink's consumer does with the
    event. A deployment that wants a followup to become a tracked task
    writes its own consumer against the emitted record shape (see
    docs/integration.md's "Followup ingest" section for a worked example).

    Args:
        task_id: the source invocation's task_id (envelope-out.json), or
            None if the envelope omitted it. Carried through unchanged.
        agent_name: the source invocation's agent_name, carried through
            unchanged.
        followups: the envelope-out.json `followups[]` list. An empty or
            missing list is a no-op (returns 0) -- this is intentionally
            NOT an error, since most invocations have no followups.
        ts: emission timestamp override (tests). Defaults to now (UTC),
            applied identically to every emitted event in this call.
        sink: sink override (tests / explicit routing). Defaults to
            resolve_sink() (env-driven, defaults to NoneSink).

    Returns:
        The number of followup events the sink durably delivered. A
        per-item sink failure does not stop the remaining items from being
        attempted -- matches emit()'s own never-raises-for-sink-failure
        contract (see sink.py).
    """
    if not followups:
        return 0

    emitted_ts = ts or _now_iso()
    target = sink if sink is not None else resolve_sink()

    delivered = 0
    for followup in followups:
        record = {
            "schema": "telemetry-followup-event/v1",
            "task_id": task_id,
            "agent_name": agent_name,
            "ts": emitted_ts,
            "followup": followup,
        }
        if _emit(record, FOLLOWUP_EVENT_SCHEMA, target):
            delivered += 1
    return delivered


__all__ = [
    "AGENT_RUN_SCHEMA",
    "DISPATCH_HEARTBEAT_SCHEMA",
    "DISPATCH_RECORD_SCHEMA",
    "DISPATCH_RESUME_ACK_SCHEMA",
    "DISPATCH_STATUSES",
    "FOLLOWUP_EVENT_SCHEMA",
    "TRACE_EVENT_KINDS",
    "TRACE_EVENT_SCHEMA",
    "TelemetryValidationError",
    "build_agent_run_record",
    "emit_agent_run",
    "emit_dispatch_heartbeat",
    "emit_dispatch_record",
    "emit_dispatch_resume_ack",
    "emit_followup_events",
    "emit_trace_event",
]
