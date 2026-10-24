"""verdict_record.emit — the write path every measuring step goes through.

``emit_record`` builds a validated record and stores it. ``try_emit_record`` is
the form for a step whose own work has already happened (a push that landed, a
verdict comment that posted): it never raises, and says on stderr, with the
reason, that no record was left. The consumer then reads that gate as not
measured, which is the true state of the world.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from clagentic_loadout.verdict_record.record import (
    RecordFormatError,
    VerdictRecord,
    build_record,
)
from clagentic_loadout.verdict_record.store import StoreError, records_root, write_record


def emit_record(
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
    docs_only: dict[str, Any] | None = None,
    root: Path | None = None,
    untrusted_roots: Sequence[Path] = (),
) -> VerdictRecord:
    """Build and store one record under *root* (default: the configured records
    root). Raises RecordFormatError for a value the schema refuses and
    StoreError when the record cannot be stored."""
    record = build_record(
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
        docs_only=docs_only,
    )
    write_record(
        root if root is not None else records_root(), record, untrusted_roots=untrusted_roots
    )
    return record


def try_emit_record(prog: str, **kwargs) -> VerdictRecord | None:
    """``emit_record`` that reports a failure on stderr, prefixed with *prog*,
    and returns None instead of raising."""
    try:
        return emit_record(**kwargs)
    except (RecordFormatError, StoreError) as exc:
        print(
            f"{prog}: WARNING -- no verdict record was written for gate "
            f"{kwargs.get('gate')!r}: {exc}. The gate will read as not measured at this head.",
            file=sys.stderr,
        )
        return None


__all__ = ["emit_record", "try_emit_record"]
