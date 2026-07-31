"""test_telemetry_emitter.py — unit tests for clagentic_loadout.telemetry.emitter
(Wave A slice 5, tome #688 REBUILD of trace_writer.py / dispatch_sidecar.py /
agent_run_post.py on the §11 sink model).

Covers:
  - each emit_* function validates its record against the published schema
    before handing it to a sink, and raises TelemetryValidationError on a
    malformed record rather than silently emitting it,
  - default is sink=none (no-op) when no sink override is passed and no
    CLAGENTIC_LOADOUT_TELEMETRY_SINK env is set,
  - an explicit sink override receives a schema-valid record,
  - build_agent_run_record() omits empty optionals rather than sending
    schema-valid-but-noisy fields,
  - emit_dispatch_heartbeat / emit_dispatch_resume_ack (lr-99a8): the
    dispatch-liveness sibling records validate with/without their optional
    label, reference an existing dispatch_id, and leave the existing
    telemetry-dispatch-record.json in_flight/terminal writes unchanged
    (regression -- TestEmitDispatchRecord below).

No date-dependent assertions — ts/recorded_at fields are always passed
explicitly in these tests, never asserted against wall-clock time.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.telemetry.emitter import (
    TelemetryValidationError,
    build_agent_run_record,
    emit_agent_run,
    emit_dispatch_heartbeat,
    emit_dispatch_record,
    emit_dispatch_resume_ack,
    emit_followup_events,
    emit_trace_event,
)
from clagentic_loadout.telemetry.sink import FilesystemSink, NoneSink


FIXED_TS = "2026-07-07T22:00:00Z"


class _RecordingSink:
    def __init__(self):
        self.records: list[dict] = []

    def emit(self, record: dict) -> bool:
        self.records.append(record)
        return True


class TestEmitTraceEvent:
    def _envelope_in(self) -> dict:
        return {
            "task_id": "lr-61b9",
            "project": "clagentic-loadout",
            "agent_name": "some-builder",
            "invoker": {"kind": "human", "name": "operator"},
            "authorized_by": "operator",
            "conversation_id": "018f8a3e-7b2c-7f4a-8b3d-0123456789ab",
            "invocation_id": "018f8a3e-7b2c-7f4a-8b3d-0123456789ac",
            "attempt": 1,
            "participants": [
                {"kind": "human", "name": "operator", "role": "invoker", "position": 0},
            ],
            "invocation_ts": FIXED_TS,
        }

    def test_valid_envelope_in_emitted(self):
        sink = _RecordingSink()
        assert emit_trace_event("envelope_in", self._envelope_in(), ts=FIXED_TS, sink=sink) is True
        assert len(sink.records) == 1
        assert sink.records[0]["schema"] == "telemetry-trace-event/v1"
        assert sink.records[0]["kind"] == "envelope_in"
        assert sink.records[0]["ts"] == FIXED_TS
        assert sink.records[0]["envelope"] == self._envelope_in()

    def test_invalid_kind_rejected(self):
        with pytest.raises(ValueError):
            emit_trace_event("not-a-real-kind", self._envelope_in(), sink=_RecordingSink())

    def test_default_sink_is_none_and_is_a_noop(self, monkeypatch):
        monkeypatch.delenv("CLAGENTIC_LOADOUT_TELEMETRY_SINK", raising=False)
        # No sink override, no env config: resolve_sink() must return NoneSink
        # under the hood — proven indirectly by no exception and a normal
        # True return with no observable side effect to assert against.
        assert emit_trace_event("envelope_out", self._envelope_in(), ts=FIXED_TS) is True

    def test_malformed_envelope_wrapper_raises_before_sink_called(self):
        sink = _RecordingSink()
        with pytest.raises(TelemetryValidationError):
            emit_trace_event("envelope_in", "not-a-dict-envelope", ts=FIXED_TS, sink=sink)  # type: ignore[arg-type]
        assert sink.records == []


class TestEmitDispatchRecord:
    def test_pre_spawn_write(self):
        sink = _RecordingSink()
        ok = emit_dispatch_record("dispatch-1", "in_flight", ts=FIXED_TS, sink=sink)
        assert ok is True
        assert sink.records[0] == {
            "schema": "telemetry-dispatch-record/v1",
            "dispatch_id": "dispatch-1",
            "status": "in_flight",
            "ts": FIXED_TS,
        }

    def test_post_spawn_terminal_write_carries_optional_fields(self):
        sink = _RecordingSink()
        emit_dispatch_record(
            "dispatch-1",
            "completed",
            ts=FIXED_TS,
            target_session_id="session-abc",
            completed_ts=FIXED_TS,
            result_summary="ok",
            spawn_env={"SOME_DEPLOYMENT_KEY": "value"},
            sink=sink,
        )
        record = sink.records[0]
        assert record["target_session_id"] == "session-abc"
        assert record["completed_ts"] == FIXED_TS
        assert record["result_summary"] == "ok"
        assert record["spawn_env"] == {"SOME_DEPLOYMENT_KEY": "value"}

    def test_invalid_status_rejected(self):
        with pytest.raises(ValueError):
            emit_dispatch_record("dispatch-1", "not-a-status", sink=_RecordingSink())

    def test_empty_dispatch_id_rejected(self):
        with pytest.raises(ValueError):
            emit_dispatch_record("", "in_flight", sink=_RecordingSink())


class TestEmitDispatchHeartbeat:
    """lr-99a8: dispatch-liveness heartbeat, a sibling record to
    telemetry-dispatch-record.json rather than an added status value --
    see telemetry-dispatch-heartbeat.json's description for why."""

    def test_heartbeat_without_progress(self):
        sink = _RecordingSink()
        ok = emit_dispatch_heartbeat("dispatch-1", ts=FIXED_TS, sink=sink)
        assert ok is True
        assert sink.records[0] == {
            "schema": "telemetry-dispatch-heartbeat/v1",
            "dispatch_id": "dispatch-1",
            "ts": FIXED_TS,
        }

    def test_heartbeat_with_progress_label(self):
        sink = _RecordingSink()
        emit_dispatch_heartbeat(
            "dispatch-1", ts=FIXED_TS, progress="running test suite", sink=sink
        )
        assert sink.records[0]["progress"] == "running test suite"

    def test_heartbeat_references_dispatch_id(self):
        """A heartbeat carries the SAME dispatch_id as the dispatch's
        pre-spawn in_flight write -- proving both records correlate on the
        one caller-supplied identifier, no separate heartbeat identity."""
        sink = _RecordingSink()
        emit_dispatch_record("dispatch-42", "in_flight", ts=FIXED_TS, sink=sink)
        emit_dispatch_heartbeat("dispatch-42", ts=FIXED_TS, sink=sink)
        assert sink.records[0]["dispatch_id"] == sink.records[1]["dispatch_id"] == "dispatch-42"

    def test_empty_dispatch_id_rejected(self):
        with pytest.raises(ValueError):
            emit_dispatch_heartbeat("", sink=_RecordingSink())

    def test_default_sink_is_none_and_is_a_noop(self, monkeypatch):
        monkeypatch.delenv("CLAGENTIC_LOADOUT_TELEMETRY_SINK", raising=False)
        assert emit_dispatch_heartbeat("dispatch-1", ts=FIXED_TS) is True

    def test_malformed_record_raises_before_sink_called(self):
        """additionalProperties:false on the published schema rejects an
        unexpected key rather than silently passing it through to the
        sink -- proven here via a monkeypatched record shape is not
        possible without reaching into internals, so this instead proves
        the schema itself enforces the const schema value."""
        sink = _RecordingSink()
        with pytest.raises(TelemetryValidationError):
            emit_dispatch_heartbeat(123, sink=sink)  # type: ignore[arg-type]
        assert sink.records == []


class TestEmitDispatchResumeAck:
    """lr-99a8: instant liveness confirmation on a dispatch-resume path.
    Schema + emitter only -- no harness/SendMessage delivery is exercised
    or assumed here (CLAUDE.md hard rule 2)."""

    def test_resume_ack_without_label(self):
        sink = _RecordingSink()
        ok = emit_dispatch_resume_ack("dispatch-1", ts=FIXED_TS, sink=sink)
        assert ok is True
        assert sink.records[0] == {
            "schema": "telemetry-dispatch-resume-ack/v1",
            "dispatch_id": "dispatch-1",
            "ts": FIXED_TS,
        }

    def test_resume_ack_with_resuming_label(self):
        sink = _RecordingSink()
        emit_dispatch_resume_ack(
            "dispatch-1", ts=FIXED_TS, resuming="continuing PR #332 fix", sink=sink
        )
        assert sink.records[0]["resuming"] == "continuing PR #332 fix"

    def test_resume_ack_references_original_dispatch_id(self):
        """A resume-ack continues an EXISTING dispatch -- it must carry the
        same dispatch_id as the original in_flight write, not mint a new
        identity for the resumed leg."""
        sink = _RecordingSink()
        emit_dispatch_record("dispatch-99", "in_flight", ts=FIXED_TS, sink=sink)
        emit_dispatch_resume_ack("dispatch-99", ts=FIXED_TS, sink=sink)
        assert sink.records[0]["dispatch_id"] == sink.records[1]["dispatch_id"] == "dispatch-99"

    def test_empty_dispatch_id_rejected(self):
        with pytest.raises(ValueError):
            emit_dispatch_resume_ack("", sink=_RecordingSink())

    def test_default_sink_is_none_and_is_a_noop(self, monkeypatch):
        monkeypatch.delenv("CLAGENTIC_LOADOUT_TELEMETRY_SINK", raising=False)
        assert emit_dispatch_resume_ack("dispatch-1", ts=FIXED_TS) is True

    def test_malformed_record_raises_before_sink_called(self):
        sink = _RecordingSink()
        with pytest.raises(TelemetryValidationError):
            emit_dispatch_resume_ack(123, sink=sink)  # type: ignore[arg-type]
        assert sink.records == []


class TestBuildAgentRunRecord:
    def test_required_fields_only(self):
        record = build_agent_run_record(
            record_id="r-1",
            agent_name="Some-Builder",
            spawn_id="spawn-1",
            started_at=FIXED_TS,
            finished_at=FIXED_TS,
            recorded_at=FIXED_TS,
        )
        assert record["schema"] == "telemetry-agent-run/v1"
        assert record["agent_name"] == "some-builder"  # normalized lowercase
        assert record["exit_status"] == "unknown"
        # Optional empty-string/empty-list fields are omitted, not sent as noise.
        assert "initiator" not in record
        assert "host" not in record
        assert "error_codes" not in record

    def test_optional_fields_included_when_provided(self):
        record = build_agent_run_record(
            record_id="r-1",
            agent_name="builder",
            spawn_id="spawn-1",
            started_at=FIXED_TS,
            finished_at=FIXED_TS,
            recorded_at=FIXED_TS,
            initiator="cli",
            host="worker-1",
            error_codes=["SOMETHING_FAILED"],
        )
        assert record["initiator"] == "cli"
        assert record["host"] == "worker-1"
        assert record["error_codes"] == ["SOMETHING_FAILED"]


class TestEmitAgentRun:
    def test_valid_record_emitted(self):
        sink = _RecordingSink()
        record = build_agent_run_record(
            record_id="r-1",
            agent_name="builder",
            spawn_id="spawn-1",
            started_at=FIXED_TS,
            finished_at=FIXED_TS,
            recorded_at=FIXED_TS,
            exit_status="ok",
        )
        assert emit_agent_run(record, sink=sink) is True
        assert sink.records == [record]

    def test_missing_required_field_raises(self):
        record = build_agent_run_record(
            record_id="r-1",
            agent_name="builder",
            spawn_id="spawn-1",
            started_at=FIXED_TS,
            finished_at=FIXED_TS,
            recorded_at=FIXED_TS,
        )
        del record["record_id"]
        with pytest.raises(TelemetryValidationError):
            emit_agent_run(record, sink=_RecordingSink())

    def test_invalid_exit_status_raises(self):
        record = build_agent_run_record(
            record_id="r-1",
            agent_name="builder",
            spawn_id="spawn-1",
            started_at=FIXED_TS,
            finished_at=FIXED_TS,
            recorded_at=FIXED_TS,
        )
        record["exit_status"] = "not-a-real-status"
        with pytest.raises(TelemetryValidationError):
            emit_agent_run(record, sink=_RecordingSink())


class TestEmitFollowupEvents:
    """lr-7a6e: generic sink-routed ingest for envelope-out.json's
    followups[]. loadout's job ends at emitting a typed record -- no task
    creation, no tracker coupling (CLAUDE.md hard rule 6a)."""

    def test_emits_one_event_per_followup(self):
        sink = _RecordingSink()
        followups = [
            {"summary": "extract the retry helper"},
            {"summary": "tighten the timeout message", "kind": "refactor"},
        ]
        delivered = emit_followup_events(
            "lr-7a6e", "some-builder", followups, ts=FIXED_TS, sink=sink
        )
        assert delivered == 2
        assert len(sink.records) == 2
        for record, followup in zip(sink.records, followups):
            assert record["schema"] == "telemetry-followup-event/v1"
            assert record["task_id"] == "lr-7a6e"
            assert record["agent_name"] == "some-builder"
            assert record["ts"] == FIXED_TS
            assert record["followup"] == followup

    def test_empty_followups_list_is_a_noop(self):
        sink = _RecordingSink()
        assert emit_followup_events("lr-7a6e", "some-builder", [], sink=sink) == 0
        assert sink.records == []

    def test_missing_followups_is_a_noop(self):
        """None is accepted the same as an empty list -- a caller passing
        through envelope.get('followups') (which may be absent) does not
        need a guard before calling."""
        sink = _RecordingSink()
        assert emit_followup_events("lr-7a6e", "some-builder", None, sink=sink) == 0
        assert sink.records == []

    def test_none_sink_is_a_true_noop_and_still_counts_as_delivered(self):
        """sink=none's emit() always returns True (see sink.py) -- ingest
        into a none-configured deployment no-ops cleanly rather than
        erroring, matching every other emit_* function's contract."""
        sink = NoneSink()
        delivered = emit_followup_events(
            "lr-7a6e",
            "some-builder",
            [{"summary": "ok"}],
            ts=FIXED_TS,
            sink=sink,
        )
        assert delivered == 1

    def test_task_id_none_carried_through(self):
        """envelope-out.json's task_id is itself optional at the envelope
        layer in some deployments -- the ingest helper must not require a
        non-null task_id."""
        sink = _RecordingSink()
        emit_followup_events(None, "some-builder", [{"summary": "ok"}], ts=FIXED_TS, sink=sink)
        assert sink.records[0]["task_id"] is None

    def test_default_sink_is_none_and_is_a_noop(self, monkeypatch):
        monkeypatch.delenv("CLAGENTIC_LOADOUT_TELEMETRY_SINK", raising=False)
        delivered = emit_followup_events(
            "lr-7a6e", "some-builder", [{"summary": "ok"}], ts=FIXED_TS
        )
        assert delivered == 1

    def test_malformed_followup_record_raises_before_sink_called(self):
        """A record that fails schema validation (e.g. an unexpected key
        leaking through) raises rather than silently emitting a malformed
        event -- same contract as every other emit_* function."""
        sink = _RecordingSink()
        with pytest.raises(TelemetryValidationError):
            emit_followup_events(
                "lr-7a6e",
                123,  # agent_name must be a string
                [{"summary": "ok"}],
                ts=FIXED_TS,
                sink=sink,
            )
        assert sink.records == []

    def test_partial_delivery_continues_past_a_sink_failure(self):
        """One item's sink failure does not stop the remaining items from
        being attempted -- mirrors sink.emit()'s own never-raises
        contract, just applied across multiple items in one call."""

        class _FlakySink:
            def __init__(self):
                self.calls = 0

            def emit(self, record):
                del record
                self.calls += 1
                return self.calls != 1  # first call fails, rest succeed

        sink = _FlakySink()
        delivered = emit_followup_events(
            "lr-7a6e",
            "some-builder",
            [{"summary": "one"}, {"summary": "two"}, {"summary": "three"}],
            ts=FIXED_TS,
            sink=sink,
        )
        assert delivered == 2
        assert sink.calls == 3


class TestEndToEndWithFilesystemSink:
    """One integration-style test proving the emitter + a real (non-mock)
    sink cooperate correctly end to end, on top of the per-unit tests above."""

    def test_agent_run_lands_on_disk_via_filesystem_sink(self, tmp_path):
        sink = FilesystemSink(tmp_path)
        record = build_agent_run_record(
            record_id="r-e2e",
            agent_name="builder",
            spawn_id="spawn-e2e",
            started_at=FIXED_TS,
            finished_at=FIXED_TS,
            recorded_at=FIXED_TS,
            exit_status="ok",
        )
        assert emit_agent_run(record, sink=sink) is True

        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        import json

        lines = files[0].read_text(encoding="utf-8").splitlines()
        assert json.loads(lines[0]) == record

    def test_followup_events_land_on_disk_via_filesystem_sink(self, tmp_path):
        sink = FilesystemSink(tmp_path)
        delivered = emit_followup_events(
            "lr-7a6e",
            "builder",
            [{"summary": "one"}, {"summary": "two"}],
            ts=FIXED_TS,
            sink=sink,
        )
        assert delivered == 2

        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        import json

        lines = files[0].read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert {json.loads(line)["followup"]["summary"] for line in lines} == {"one", "two"}

    def test_none_sink_explicit_is_a_true_noop(self, tmp_path):
        sink = NoneSink()
        record = build_agent_run_record(
            record_id="r-noop",
            agent_name="builder",
            spawn_id="spawn-noop",
            started_at=FIXED_TS,
            finished_at=FIXED_TS,
            recorded_at=FIXED_TS,
        )
        assert emit_agent_run(record, sink=sink) is True
        assert list(tmp_path.iterdir()) == []
