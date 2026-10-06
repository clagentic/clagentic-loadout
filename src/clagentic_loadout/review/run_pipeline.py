"""review.run_pipeline — chunk, review each chunk, persist, merge.

Resumable by construction. Every chunk's record is persisted under a state
directory keyed on everything that shapes the chunk (its text, the carrier and
fallback argv, the rulebook), so re-running the identical command skips
finished chunks and retries only the unfinished ones. A failed chunk is
persisted, not fatal: a retriable failure (timeout, carrier exit) makes the
run return RESUME so the next invocation retries just that chunk, with its
attempt count carried forward. Only once its attempts are exhausted — or the
engine answered badly twice, or no engine exists — does the run return
BLOCKED naming the chunk. Only ok chunks are ever reused: BLOCKED discards
the failed records, so the next invocation gives every non-ok chunk a fresh
attempt budget.

All writes stay inside the run directory.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clagentic_loadout.acquire.contract import AcquiredPr
from clagentic_loadout.review.carrier import Runner, run_in_process_group
from clagentic_loadout.review.chunk_review import (
    ENGINE_FALLBACK,
    STATUS_FAILED,
    STATUS_OK,
    review_chunk,
)
from clagentic_loadout.review.chunking import Chunk, plan_chunks
from clagentic_loadout.review.delta import (
    MODE_DELTA,
    MODE_FULL,
    STATUS_DELTA,
    STATUS_FALLBACK,
    DeltaContext,
    carried_findings,
    render_delta_note,
)
from clagentic_loadout.review.engine_breaker import BREAKER_FILENAME, EngineBreaker
from clagentic_loadout.review.findings_contract import merge_findings
from clagentic_loadout.review.profile_config import ReviewProfile
from clagentic_loadout.sha import FULL_SHA_RE

EXIT_COMPLETE = 0
EXIT_RESUME = 10
EXIT_BLOCKED = 20

RESULT_COMPLETE = "complete"
RESULT_RESUME = "resume"
RESULT_BLOCKED = "blocked"

FINDINGS_SCHEMA = "loadout.review-findings/1"
FINDINGS_FILENAME = "findings.json"
BINDING_FILENAME = "run-binding.json"

#: Bump when chunk planning, prompting, or merging changes: part of the
#: resume key, so a state directory built by older logic is never reused.
PIPELINE_VERSION = "3"

REASON_ACQUIRE_INVALID = "ACQUIRE_INVALID"
REASON_DIFF_EMPTY = "DIFF_EMPTY"
REASON_RUN_DIR_UNWRITABLE = "RUN_DIR_UNWRITABLE"

_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]")

StageEmitter = Callable[..., None]


@dataclass
class RunOutcome:
    exit_code: int
    result: str
    payload: dict[str, Any]
    stages: list[dict[str, Any]] = field(default_factory=list)


def default_run_dir(
    run_root: Path,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    since_head: str | None = None,
) -> Path:
    """Stable run directory for (repo, pr, head_sha) under *run_root*; a delta
    review since *since_head* gets its own directory beside the full one.

    Raises ValueError for a head_sha (or since_head) that is not 40 lowercase
    hex characters: it becomes a path segment, so an unvalidated value could
    escape *run_root*."""
    for sha in (head_sha, since_head):
        if sha is not None and not FULL_SHA_RE.match(sha):
            raise ValueError(f"head sha must be 40 lowercase hex characters, got {sha!r}")
    owner_repo = _SAFE_SEGMENT_RE.sub("_", f"{owner}__{repo}")
    leaf = head_sha[:12] if since_head is None else f"{head_sha[:12]}-since-{since_head[:12]}"
    return run_root / owner_repo / f"pr-{pr_number}" / leaf


def bind_run_dir(
    run_dir: Path,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    since_head: str | None = None,
) -> None:
    """Tie *run_dir* to (repo, pr, head_sha, since_head). Chunk state is already
    keyed on the head, so an old head's chunks are never resumed; this
    additionally drops a merged findings file left by any other binding, so a
    caller cannot pick up findings for a head that has since moved, or a
    delta's findings for a full review. Raises OSError when the directory
    cannot be written."""
    binding = {
        "repo": f"{owner}/{repo}".lower(),
        "pr_number": pr_number,
        "head_sha": head_sha,
        "since_head": since_head,
    }
    path = run_dir / BINDING_FILENAME
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        recorded = None
    if recorded == binding:
        return
    (run_dir / FINDINGS_FILENAME).unlink(missing_ok=True)
    _write_json(path, binding)


def _write_json(path: Path, data: Any) -> None:
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # Leave no half-written temp file in the state directory.
        tmp.unlink(missing_ok=True)
        raise


def _resume_key(
    chunks: list[Chunk], profile: ReviewProfile, head_sha: str, delta_note: str = ""
) -> str:
    digest = hashlib.sha256()
    for part in (
        PIPELINE_VERSION,
        head_sha,
        delta_note,
        json.dumps(profile.carrier),
        json.dumps(profile.fallback),
        profile.rulebook_text,
        str(profile.chunk_lines),
    ):
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    for chunk in chunks:
        digest.update(chunk.text.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _result_path(state_dir: Path, index: int) -> Path:
    return state_dir / f"result-{index:04d}.json"


def _load_record(state_dir: Path, index: int) -> dict[str, Any] | None:
    try:
        record = json.loads(_result_path(state_dir, index).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _attempts(record: dict[str, Any]) -> int:
    """Attempt count of a persisted record; a corrupt value counts as zero
    (the chunk is simply retried) rather than crashing the run."""
    value = record.get("attempts", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _is_exhausted(record: dict[str, Any], max_attempts: int) -> bool:
    return (
        record.get("status") == STATUS_FAILED
        and record.get("retriable", False)
        and _attempts(record) >= max_attempts
    )


def _is_terminal(record: dict[str, Any], max_attempts: int) -> bool:
    """A failure that a re-run must not silently repeat: a non-retriable
    failure (engine answered badly twice, no engine) or exhausted retries."""
    return record.get("status") == STATUS_FAILED and (
        not record.get("retriable", False) or _is_exhausted(record, max_attempts)
    )


def _discard_unfinished(state_dir: Path, records: dict[int, dict[str, Any]]) -> None:
    """Drop every non-ok chunk record when a run reports blocked, so failure
    state never outlives the invocation that reported it. A reviewer role
    cannot delete state itself; re-invoking is its deliberate retry. A record
    that cannot be removed is still ignored on load (see fresh_budget)."""
    for index, record in records.items():
        if record.get("status") != STATUS_OK:
            try:
                _result_path(state_dir, index).unlink(missing_ok=True)
            except OSError:
                continue


def _stage_status(record: dict[str, Any]) -> str:
    if record.get("status") == STATUS_FAILED:
        return "failed"
    return "fallback" if record.get("engine") == ENGINE_FALLBACK else "ok"


def _stage_note(record: dict[str, Any]) -> str | None:
    """Why this chunk was answered by, or failed on, the engine it names: the
    last stderr line is where an engine says what went wrong."""
    unavailable = record.get("carrier_unavailable_reason")
    if unavailable and record.get("engine") == ENGINE_FALLBACK:
        return f"carrier unavailable: {unavailable}"
    failure = record.get("carrier_failure")
    if failure:
        return f"carrier failed: {failure.get('stderr_last_line') or failure.get('detail')}"
    if record.get("status") == STATUS_FAILED:
        return record.get("stderr_last_line") or None
    return None


def _engine_summary(records: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Which engine answered how many chunks, and why the carrier did not."""
    summary: dict[str, Any] = {"engines": {}}
    for record in records.values():
        engine = record.get("engine")
        summary["engines"][engine] = summary["engines"].get(engine, 0) + 1
        reason = record.get("carrier_unavailable_reason")
        if reason:
            summary["carrier_unavailable_reason"] = reason
        if record.get("carrier_failure"):
            summary["carrier_failed_chunks"] = summary.get("carrier_failed_chunks", 0) + 1
    return summary


def _blocked(stage: str, reason: str, detail: str, stages: list[dict[str, Any]], **extra: Any) -> RunOutcome:
    payload = {"result": RESULT_BLOCKED, "stage": stage, "reason": reason, "detail": detail}
    payload.update({k: v for k, v in extra.items() if v not in (None, "")})
    return RunOutcome(EXIT_BLOCKED, RESULT_BLOCKED, payload, stages)


def unusable_acquire_outcome(acquired: AcquiredPr, *, emit: StageEmitter) -> RunOutcome | None:
    """A blocked outcome when the host returned a base/head SHA that is not 40
    lowercase hex characters, else None. The head becomes a path segment, so
    callers check this before touching any run directory."""
    if FULL_SHA_RE.match(acquired.head_sha) and FULL_SHA_RE.match(acquired.base_sha):
        return None
    emit("acquired", "failed", reason=REASON_ACQUIRE_INVALID)
    return _blocked(
        "acquired",
        REASON_ACQUIRE_INVALID,
        f"the host API returned a base/head SHA that is not 40 lowercase hex "
        f"characters (base={acquired.base_sha!r}, head={acquired.head_sha!r})",
        [{"stage": "acquired", "status": "failed", "reason": REASON_ACQUIRE_INVALID}],
    )


def run_review(
    acquired: AcquiredPr,
    profile: ReviewProfile,
    run_dir: Path,
    *,
    emit: StageEmitter,
    runner: Runner = run_in_process_group,
    delta: DeltaContext | None = None,
    delta_stage: dict[str, str] | None = None,
) -> RunOutcome:
    """Drive one invocation of the review pipeline for *acquired*. With
    *delta*, *acquired* already holds the delta diff and each chunk is framed
    by review.delta; *delta_stage* is the decision between delta and full,
    reported once right after "acquired"."""
    stages: list[dict[str, Any]] = []

    def stage(name: str, status: str, **fields: Any) -> None:
        stages.append({"stage": name, "status": status, **fields})
        emit(name, status, **fields)

    refused = unusable_acquire_outcome(acquired, emit=emit)
    if refused is not None:
        return refused
    stage("acquired", "ok", head_sha=acquired.head_sha, base_sha=acquired.base_sha)
    if delta_stage is not None:
        stage("delta", STATUS_DELTA if delta is not None else STATUS_FALLBACK, **delta_stage)
    delta_note = render_delta_note(delta) if delta is not None else ""

    chunks = plan_chunks(acquired.diff_text, profile.chunk_lines)
    if not chunks:
        stage("chunked", "failed", reason=REASON_DIFF_EMPTY)
        return _blocked(
            "chunked",
            REASON_DIFF_EMPTY,
            "the acquired diff has no reviewable content; refusing to report a "
            "clean review for a diff that was never read",
            stages,
        )

    state_dir = run_dir / f"state-{_resume_key(chunks, profile, acquired.head_sha, delta_note)}"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Reported once, as the stage's only status: "chunked" is not
        # announced ok until its state directory exists.
        stage("chunked", "failed", reason=REASON_RUN_DIR_UNWRITABLE)
        return _blocked(
            "chunked", REASON_RUN_DIR_UNWRITABLE, f"cannot create {str(state_dir)!r}: {exc}", stages
        )
    stage("chunked", "ok", chunk_count=len(chunks))

    records: dict[int, dict[str, Any]] = {}
    pending: list[Chunk] = []
    # Chunks whose persisted failure is terminal or exhausted start over with
    # a fresh attempt budget: only an exit-10 sequence carries counts forward.
    fresh_budget: set[int] = set()
    for chunk in chunks:
        existing = _load_record(state_dir, chunk.index)
        if existing is not None and existing.get("status") == STATUS_OK:
            records[chunk.index] = existing
            stage(f"chunk-{chunk.index}", _stage_status(existing), resumed="yes", nonce=existing.get("nonce"))
        else:
            if existing is not None and _is_terminal(existing, profile.max_attempts):
                fresh_budget.add(chunk.index)
            pending.append(chunk)

    # Kept beside the chunk results so a resume of this exact run reuses it;
    # every terminal outcome below clears it so a fresh run starts clean.
    breaker = EngineBreaker(state_dir / BREAKER_FILENAME)

    def work(chunk: Chunk) -> dict[str, Any]:
        previous = _load_record(state_dir, chunk.index) or {}
        record = review_chunk(
            chunk,
            len(chunks),
            profile,
            attempts_before=0 if chunk.index in fresh_budget else _attempts(previous),
            cwd=run_dir,
            runner=runner,
            delta_note=delta_note,
            breaker=breaker,
        )
        try:
            _write_json(_result_path(state_dir, chunk.index), record)
        except OSError as exc:
            return {
                **record,
                "status": STATUS_FAILED,
                "retriable": False,
                "reason": REASON_RUN_DIR_UNWRITABLE,
                "detail": f"cannot persist the chunk result: {exc}",
            }
        return record

    if pending:
        workers = max(1, min(profile.parallel, len(pending)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for chunk, record in zip(pending, pool.map(work, pending)):
                records[chunk.index] = record
                stage(
                    f"chunk-{chunk.index}",
                    _stage_status(record),
                    nonce=record.get("nonce"),
                    attempts=record.get("attempts"),
                    reason=record.get("reason"),
                    engine=record.get("engine"),
                    note=_stage_note(record),
                )

    blocking = [
        (index, r) for index, r in sorted(records.items())
        if _is_terminal(r, profile.max_attempts)
    ]
    unfinished = sorted(
        index for index in (c.index for c in chunks)
        if records.get(index, {}).get("status") != STATUS_OK
    )
    if blocking or not unfinished:
        breaker.clear()
    if blocking:
        first_index, first = blocking[0]
        exhausted = _is_exhausted(first, profile.max_attempts)
        detail = first.get("detail", "")
        if exhausted:
            detail = f"retries exhausted after {first.get('attempts')} attempts: {detail}"
        _discard_unfinished(state_dir, records)
        return _blocked(
            f"chunk-{first_index}",
            first.get("reason", "CHUNK_FAILED"),
            detail,
            stages,
            engine=first.get("engine"),
            reply_excerpt=first.get("reply_excerpt"),
            stderr_excerpt=first.get("stderr_excerpt"),
            stderr_last_line=first.get("stderr_last_line"),
            stderr_file=first.get("stderr_file"),
            run_dir=str(run_dir),
        )

    if unfinished:
        return RunOutcome(
            EXIT_RESUME,
            RESULT_RESUME,
            {
                "result": RESULT_RESUME,
                "pending_chunks": unfinished,
                "action": "run the identical command again; finished chunks are cached",
                "run_dir": str(run_dir),
            },
            stages,
        )

    per_chunk = [(index, record.get("findings", [])) for index, record in sorted(records.items())]
    carried: list[dict[str, Any]] = []
    if delta is not None:
        touched = {name for chunk in chunks for name in chunk.files}
        carried = carried_findings(delta, touched)
        per_chunk.append((0, carried))
    findings = merge_findings(per_chunk)
    findings_path = run_dir / FINDINGS_FILENAME
    document = {
        "schema": FINDINGS_SCHEMA,
        "owner": acquired.owner,
        "repo": acquired.repo,
        "pr_number": acquired.pr_number,
        "head_sha": acquired.head_sha,
        "base_sha": acquired.base_sha,
        "mode": MODE_DELTA if delta is not None else MODE_FULL,
        "since_head": delta.since_head if delta is not None else None,
        "carried_count": len(carried),
        "chunk_count": len(chunks),
        "chunks": [
            {
                key: records[chunk.index].get(key)
                for key in (
                    "index", "engine", "nonce", "attempts", "exit_code", "files",
                    "carrier_unavailable_reason", "carrier_failure",
                )
            }
            for chunk in chunks
        ],
        "findings": findings,
    }
    try:
        _write_json(findings_path, document)
    except OSError as exc:
        stage("merged", "failed", reason=REASON_RUN_DIR_UNWRITABLE)
        return _blocked("merged", REASON_RUN_DIR_UNWRITABLE, f"cannot write {str(findings_path)!r}: {exc}", stages)
    stage("merged", "ok", findings=len(findings))
    return RunOutcome(
        EXIT_COMPLETE,
        RESULT_COMPLETE,
        {
            "result": RESULT_COMPLETE,
            "findings_file": str(findings_path),
            "run_dir": str(run_dir),
            "head_sha": acquired.head_sha,
            "base_sha": acquired.base_sha,
            "mode": MODE_DELTA if delta is not None else MODE_FULL,
            "chunk_count": len(chunks),
            "finding_count": len(findings),
            "carried_count": len(carried),
            **_engine_summary({i: records[i] for i in (c.index for c in chunks)}),
        },
        stages,
    )
