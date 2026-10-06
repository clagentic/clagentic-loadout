"""review.chunk_review — review one chunk end to end and classify the outcome.

Policy, in one place:

  * The prompt carries the review-only preamble, the rulebook, the chunk, and
    (LAST) the output contract.
  * The carrier runs first. An absent carrier (MODEL_UNAVAILABLE) hands THIS
    chunk to the configured fallback; every chunk gets that treatment, so a
    host without the carrier still reviews every chunk.
  * A timeout or non-zero exit is retried once inside the call. If it still
    fails, the chunk is recorded as a RETRIABLE failure (CHUNK_TIMEOUT /
    CARRIER_FAILED): the caller persists it and a later invocation retries
    only that chunk, up to the profile's attempt bound.
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
        reply_excerpt=excerpt(reply) if reply else result.stdout_excerpt,
    )
    return record


def _transient_reason(result: EngineResult) -> str:
    return REASON_CHUNK_TIMEOUT if result.kind == KIND_TIMEOUT else REASON_CARRIER_FAILED


def _record_unavailable(record: dict[str, Any], engine: str, result: EngineResult) -> None:
    """Keep an absent engine's diagnostic on the record. Keyed per engine so a
    fallback's own absence never overwrites the carrier's."""
    record[f"{engine}_unavailable_detail"] = result.detail
    record[f"{engine}_unavailable_stderr_excerpt"] = result.stderr_excerpt


def _run_one_engine(
    engine: str,
    argv: tuple[str, ...],
    timeout: float,
    prompt: str,
    record: dict[str, Any],
    *,
    cwd: Path,
    runner: Runner,
) -> dict[str, Any] | None:
    """Run one engine for this chunk. Returns the finished record, or None
    when the engine is absent (the caller decides whether a fallback exists)."""
    result = run_engine_with_retry(argv, prompt, timeout, cwd=cwd, runner=runner)
    if result.kind == KIND_UNAVAILABLE:
        _record_unavailable(record, engine, result)
        return None
    if result.kind in (KIND_TIMEOUT, KIND_FAILED):
        return _failure(
            record, engine, _transient_reason(result), result, retriable=not result.deterministic
        )

    try:
        findings = parse_chunk_reply(result.text)
        reply_result = result
    except InvalidReplyError as first_error:
        retry = run_engine_with_retry(
            argv, prompt + FORMAT_REPROMPT, timeout, cwd=cwd, runner=runner
        )
        if retry.kind == KIND_UNAVAILABLE:
            # The engine vanished between the two calls: same as being absent
            # on the first, so the caller still hands the chunk to the fallback.
            _record_unavailable(record, engine, retry)
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


def review_chunk(
    chunk: Chunk,
    total: int,
    profile: ReviewProfile,
    *,
    attempts_before: int,
    cwd: Path,
    runner: Runner = run_in_process_group,
    delta_note: str = "",
) -> dict[str, Any]:
    """Review *chunk*; returns its persistable record. Never raises for an
    engine problem: every outcome is a record with a status."""
    prompt = build_prompt(chunk, total, profile.rulebook_text, delta_note)
    record: dict[str, Any] = {
        "index": chunk.index,
        "files": list(chunk.files),
        "nonce": uuid.uuid4().hex,
        "attempts": attempts_before + 1,
    }

    finished = _run_one_engine(
        ENGINE_CARRIER, profile.carrier, profile.timeout_seconds, prompt, record,
        cwd=cwd, runner=runner,
    )
    if finished is not None:
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
            ),
            retriable=False,
        )

    finished = _run_one_engine(
        ENGINE_FALLBACK, profile.fallback, profile.fallback_timeout_seconds, prompt, record,
        cwd=cwd, runner=runner,
    )
    if finished is not None:
        finished["carrier_unavailable"] = record.get(f"{ENGINE_CARRIER}_unavailable_detail", "")
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
        ),
        retriable=False,
    )
