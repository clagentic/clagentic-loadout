"""clagentic_loadout.telemetry — generic, sink-based structured telemetry
emission (Wave A slice 5, tome #688).

REBUILD (not a port) of the source trace-writer / dispatch-sidecar /
agent-run-post capability on the §11 sink model (tome #687): a telemetry
emitter that builds loadout's own event records and hands them to a
pluggable TelemetrySink — none (default, no-op), filesystem (configured
directory), or webhook (configured URL). The emitter and sinks carry zero
internal identity: no hardcoded paths, no named external collectors. Sink
selection and parameters come from CLAGENTIC_LOADOUT_* environment
variables (see sink.py). Mapping a sink to a specific collector service is
entirely a deployment concern, layered on top of this package.
"""

from __future__ import annotations

from clagentic_loadout.telemetry.emitter import (
    TelemetryValidationError,
    build_agent_run_record,
    emit_agent_run,
    emit_dispatch_record,
    emit_followup_events,
    emit_trace_event,
)
from clagentic_loadout.telemetry.sink import (
    ENV_FILESYSTEM_DIR,
    ENV_SINK,
    ENV_WEBHOOK_TIMEOUT,
    ENV_WEBHOOK_TOKEN,
    ENV_WEBHOOK_URL,
    FilesystemSink,
    NoneSink,
    PathEscapeError,
    SINK_FILESYSTEM,
    SINK_NONE,
    SINK_WEBHOOK,
    SinkConfigError,
    TelemetrySink,
    WebhookSink,
    resolve_sink,
)

__all__ = [
    "ENV_FILESYSTEM_DIR",
    "ENV_SINK",
    "ENV_WEBHOOK_TIMEOUT",
    "ENV_WEBHOOK_TOKEN",
    "ENV_WEBHOOK_URL",
    "FilesystemSink",
    "NoneSink",
    "PathEscapeError",
    "SINK_FILESYSTEM",
    "SINK_NONE",
    "SINK_WEBHOOK",
    "SinkConfigError",
    "TelemetrySink",
    "TelemetryValidationError",
    "WebhookSink",
    "build_agent_run_record",
    "emit_agent_run",
    "emit_dispatch_record",
    "emit_followup_events",
    "emit_trace_event",
    "resolve_sink",
]
