"""review.chunk_review — review one chunk end to end and classify the outcome.

Policy, in one place:

  * The prompt carries the review-only preamble, the rulebook, the chunk, and
    (LAST) the output contract.
  * The carrier runs first. An absent carrier (MODEL_UNAVAILABLE) hands THIS
    chunk to the configured fallback; every chunk gets that treatment, so a
    host without the carrier still reviews every chunk. A carrier that is up
    but out of quota counts as absent, is not retried, and trips the run's
    EngineBreaker so the remaining chunks skip it.
  * A timeout or non-zero exit is retried once inside the call. If it still
    fails, the chunk is recorded as a RETRIABLE failure (CHUNK_TIMEOUT /
    CARRIER_FAILED): the caller persists it and a later invocation retries
    only that chunk, up to the profile's attempt bound. On the last permitted
    attempt a CARRIER_FAILED chunk goes to the fallback, when one exists, and
    the record keeps what the carrier said (carrier_failure).
  * A reply that is not a findings array gets one format-only re-prompt. A
    second bad reply is a TERMINAL OUTPUT_INVALID with a bounded
    excerpt of what the engine actually said; prose is never treated as "no
    findings".
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from clagentic_loadout.review.carrier import (
    KIND_FAILED,
    KIND_TIMEOUT,
    KIND_UNAVAILABLE,
    EngineResult,
    Runner,
    excerpt,
    run_engine_with_retry,
    run_in_process_group,
)
from clagentic_loadout.review.chunking import Chunk
from clagentic_loadout.review.engine_breaker import EngineBreaker
from clagentic_loadout.review.findings_contract import (
    FORMAT_REPROMPT,
    OUTPUT_CONTRACT,
    InvalidReplyError,
    parse_chunk_reply,
)
from clagentic_loadout.review.profile_config import ReviewProfile

REASON_MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
REASON_CHUNK_TIMEOUT = "CHUNK_TIMEOUT"
REASON_CARRIER_FAILED = "CARRIER_FAILED"
REASON_OUTPUT_INVALID = "OUTPUT_INVALID"

STATUS_OK = "ok"
STATUS_FAILED = "failed"

ENGINE_CARRIER = "carrier"
ENGINE_FALLBACK = "fallback"

#: Run-directory subfolder holding the bounded full stderr of failed calls.
CARRIER_LOG_DIRNAME = "carrier-logs"

_REVIEW_ONLY_PREFIX = (
    "You are reviewing a diff for a pull request. This is a READ-ONLY code review. "
    "Do not apply, patch, or modify any file. Follow the output contract exactly.\n\n"
)


def build_prompt(chunk: Chunk, total: int, rulebook_text: str, delta_note: str = "") -> str:
    """Chunk prompt with the output contract placed LAST, where a model
    weighs it most. *delta_note*, when given, frames the chunk as part of an
    incremental review (see review.delta)."""
    parts = [_REVIEW_ONLY_PREFIX]
    if rulebook_text.strip():
        parts.append("## Rulebook\n\n" + rulebook_text.strip() + "\n\n")
    if delta_note.strip():
        parts.append(delta_note.strip() + "\n\n")
    parts.append(f"## Diff chunk {chunk.index} of {total}\n\n" + chunk.text + "\n")
    parts.append(OUTPUT_CONTRACT)
    return "".join(parts)


def _failure(
    record: dict[str, Any],
    engine: str,
    reason: str,
    result: EngineResult,
    *,
    retriable: bool,
    reply: str = "",
) -> dict[str, Any]:
    record.update(
        status=STATUS_FAILED,
        engine=engine,
        reason=reason,
        detail=result.detail,
        retriable=retriable,
        exit_code=result.exit_code,
        stderr_excerpt=result.stderr_excerpt,
        stderr_last_line=result.stderr_last_line,
        stderr_file=result.stderr_file,
        reply_excerpt=excerpt(reply) if reply else result.stdout_excerpt,
    )
    return record


def _transient_reason(result: EngineResult) -> str:
    return REASON_CHUNK_TIMEOUT if result.kind == KIND_TIMEOUT else REASON_CARRIER_FAILED


def _record_unavailable(
    record: dict[str, Any],
    engine: str,
    result: EngineResult,
    breaker: EngineBreaker | None = None,
) -> None:
    """Keep an unavailable engine's diagnostic on the record. Keyed per engine
    so a fallback's own absence never overwrites the carrier's. A reason that
    outlasts the run also opens the breaker for the rest of it."""
    record[f"{engine}_unavailable_detail"] = result.detail
    record[f"{engine}_unavailable_stderr_excerpt"] = result.stderr_excerpt
    record[f"{engine}_unavailable_stderr_last_line"] = result.stderr_last_line
    if result.unavailable_reason:
        record[f"{engine}_unavailable_reason"] = result.unavailable_reason
        if breaker is not None:
            breaker.trip(engine, result.unavailable_reason)


def _run_one_engine(
    engine: str,
    argv: tuple[str, ...],
    timeout: float,
    prompt: str,
    record: dict[str, Any],
    *,
    cwd: Path,
    runner: Runner,
    breaker: EngineBreaker | None = None,
) -> dict[str, Any] | None:
    """Run one engine for this chunk. Returns the finished record, or None
    when the engine is unavailable (the caller decides whether a fallback
    exists)."""
    call = {
        "cwd": cwd,
        "runner": runner,
        "log_dir": cwd / CARRIER_LOG_DIRNAME,
        "label": f"chunk-{record['index']}-{engine}",
    }
    result = run_engine_with_retry(argv, prompt, timeout, **call)
    if result.kind == KIND_UNAVAILABLE:
        _record_unavailable(record, engine, result, breaker)
        return None
    if result.kind in (KIND_TIMEOUT, KIND_FAILED):
        return _failure(
            record, engine, _transient_reason(result), result, retriable=not result.deterministic
        )

    try:
        findings = parse_chunk_reply(result.text)
        reply_result = result
    except InvalidReplyError as first_error:
        retry = run_engine_with_retry(argv, prompt + FORMAT_REPROMPT, timeout, **call)
        if retry.kind == KIND_UNAVAILABLE:
            # The engine vanished between the two calls: same as being absent
            # on the first, so the caller still hands the chunk to the fallback.
            _record_unavailable(record, engine, retry, breaker)
            return None
        if retry.kind in (KIND_TIMEOUT, KIND_FAILED):
            return _failure(
                record, engine, _transient_reason(retry), retry, retriable=not retry.deterministic
            )
        try:
            findings = parse_chunk_reply(retry.text)
            reply_result = retry
        except InvalidReplyError as second_error:
            failed = EngineResult(
                kind=KIND_FAILED,
                text=retry.text,
                exit_code=retry.exit_code,
                stderr_excerpt=retry.stderr_excerpt,
                stdout_excerpt=retry.stdout_excerpt,
                stderr_last_line=retry.stderr_last_line,
                stderr_file=retry.stderr_file,
                detail=(
                    f"{engine} reply was not a findings array twice "
                    f"(first: {first_error}; second: {second_error})"
                ),
            )
            return _failure(
                record,
                engine,
                REASON_OUTPUT_INVALID,
                failed,
                retriable=False,
                reply=retry.text,
            )

    record.update(
        status=STATUS_OK,
        engine=engine,
        exit_code=reply_result.exit_code,
        stderr_excerpt=reply_result.stderr_excerpt,
        findings=findings,
    )
    return record


def _new_record(chunk: Chunk, attempts: int) -> dict[str, Any]:
    return {
        "index": chunk.index,
        "files": list(chunk.files),
        "nonce": uuid.uuid4().hex,
        "attempts": attempts,
    }


def _carrier_attempts_exhausted(record: dict[str, Any], profile: ReviewProfile) -> bool:
    """True when this chunk's last permitted carrier attempt just failed with
    an ordinary non-zero exit and a fallback exists to take over."""
    return (
        profile.fallback is not None
        and record.get("status") == STATUS_FAILED
        and record.get("engine") == ENGINE_CARRIER
        and record.get("reason") == REASON_CARRIER_FAILED
        and record.get("retriable", False)
        and record.get("attempts", 0) >= profile.max_attempts
    )


def _fallback_after_carrier_failure(
    chunk: Chunk,
    failed: dict[str, Any],
    profile: ReviewProfile,
    prompt: str,
    *,
    cwd: Path,
    runner: Runner,
    breaker: EngineBreaker | None,
) -> dict[str, Any]:
    """Run the fallback for a chunk whose carrier attempts are used up. The
    substitution is never silent: the record names the fallback as the engine
    and keeps what the carrier said under carrier_failure."""
    fallback = profile.fallback or ()
    carrier_failure = {
        key: failed.get(key)
        for key in ("exit_code", "detail", "stderr_last_line", "stderr_excerpt", "stderr_file")
    }
    record = _new_record(chunk, failed["attempts"])
    finished = _run_one_engine(
        ENGINE_FALLBACK, fallback, profile.fallback_timeout_seconds, prompt, record,
        cwd=cwd, runner=runner, breaker=breaker,
    )
    if finished is None:
        # The fallback is absent too: report the carrier's own failure, which
        # is the more useful diagnosis, with the fallback's absence beside it.
        failed["fallback_unavailable_detail"] = record.get(f"{ENGINE_FALLBACK}_unavailable_detail", "")
        return failed
    finished["carrier_failure"] = carrier_failure
    return finished


def review_chunk(
    chunk: Chunk,
    total: int,
    profile: ReviewProfile,
    *,
    attempts_before: int,
    cwd: Path,
    runner: Runner = run_in_process_group,
    delta_note: str = "",
    breaker: EngineBreaker | None = None,
) -> dict[str, Any]:
    """Review *chunk*; returns its persistable record. Never raises for an
    engine problem: every outcome is a record with a status. *breaker* is the
    run-wide record of engines known to be out of service."""
    prompt = build_prompt(chunk, total, profile.rulebook_text, delta_note)
    record = _new_record(chunk, attempts_before + 1)

    skipped = breaker.reason(ENGINE_CARRIER) if breaker is not None else ""
    if skipped:
        record[f"{ENGINE_CARRIER}_unavailable_detail"] = (
            f"carrier not called: unavailable earlier in this run ({skipped})"
        )
        record[f"{ENGINE_CARRIER}_unavailable_reason"] = skipped
        finished = None
    else:
        finished = _run_one_engine(
            ENGINE_CARRIER, profile.carrier, profile.timeout_seconds, prompt, record,
            cwd=cwd, runner=runner, breaker=breaker,
        )
    if finished is not None:
        if _carrier_attempts_exhausted(finished, profile):
            return _fallback_after_carrier_failure(
                chunk, finished, profile, prompt, cwd=cwd, runner=runner, breaker=breaker
            )
        return finished

    if profile.fallback is None:
        return _failure(
            record,
            ENGINE_CARRIER,
            REASON_MODEL_UNAVAILABLE,
            EngineResult(
                kind=KIND_UNAVAILABLE,
                detail=record.get(f"{ENGINE_CARRIER}_unavailable_detail", "carrier unavailable")
                + "; no fallback is configured for this profile",
                stderr_excerpt=record.get(f"{ENGINE_CARRIER}_unavailable_stderr_excerpt", ""),
                stderr_last_line=record.get(f"{ENGINE_CARRIER}_unavailable_stderr_last_line", ""),
            ),
            retriable=False,
        )

    finished = _run_one_engine(
        ENGINE_FALLBACK, profile.fallback, profile.fallback_timeout_seconds, prompt, record,
        cwd=cwd, runner=runner, breaker=breaker,
    )
    if finished is not None:
        finished["carrier_unavailable"] = record.get(f"{ENGINE_CARRIER}_unavailable_detail", "")
        reason = record.get(f"{ENGINE_CARRIER}_unavailable_reason", "")
        if reason:
            finished["carrier_unavailable_reason"] = reason
        return finished
    return _failure(
        record,
        ENGINE_FALLBACK,
        REASON_MODEL_UNAVAILABLE,
        EngineResult(
            kind=KIND_UNAVAILABLE,
            detail=(
                "carrier and fallback are both unavailable: carrier: "
                f"{record.get(f'{ENGINE_CARRIER}_unavailable_detail', '')}; fallback: "
                f"{record.get(f'{ENGINE_FALLBACK}_unavailable_detail', '')}"
            ),
            stderr_excerpt=(
                record.get(f"{ENGINE_FALLBACK}_unavailable_stderr_excerpt", "")
                or record.get(f"{ENGINE_CARRIER}_unavailable_stderr_excerpt", "")
            ),
            stderr_last_line=(
                record.get(f"{ENGINE_FALLBACK}_unavailable_stderr_last_line", "")
                or record.get(f"{ENGINE_CARRIER}_unavailable_stderr_last_line", "")
            ),
        ),
        retriable=False,
    )
