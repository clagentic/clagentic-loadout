"""review.carrier — run one configured review command against one prompt.

A carrier is an argv (see review.profile_config) executed directly with the
prompt on stdin. This module classifies the outcome without interpreting the
reply: ok (exit 0), unavailable (the executable is absent, or exit 127),
timeout, or failed (any other non-zero exit). stderr and a bounded excerpt of
stdout are always kept, including on a timeout, so a stall can be diagnosed
from the per-chunk record instead of being a bare "timed out".
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

EXIT_COMMAND_NOT_FOUND = 127
EXCERPT_LIMIT = 2000

KIND_OK = "ok"
KIND_UNAVAILABLE = "unavailable"
KIND_TIMEOUT = "timeout"
KIND_FAILED = "failed"

#: A call is repeated once when it times out or exits non-zero.
_TRANSIENT_KINDS = (KIND_TIMEOUT, KIND_FAILED)

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


@dataclass(frozen=True)
class EngineResult:
    kind: str
    text: str = ""
    exit_code: int | None = None
    stderr_excerpt: str = ""
    stdout_excerpt: str = ""
    detail: str = ""


def excerpt(data: bytes | str | None, limit: int = EXCERPT_LIMIT) -> str:
    """Bounded, decoded excerpt of captured output (None-safe)."""
    if data is None:
        return ""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    if len(text) > limit:
        return text[:limit] + f"... [{len(text) - limit} more characters]"
    return text


def run_engine(
    argv: Sequence[str],
    prompt: str,
    timeout: float,
    *,
    cwd: Path,
    runner: Runner = subprocess.run,
) -> EngineResult:
    """Execute *argv* once with *prompt* on stdin."""
    try:
        proc = runner(
            list(argv),
            input=prompt.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired as exc:
        return EngineResult(
            kind=KIND_TIMEOUT,
            stderr_excerpt=excerpt(exc.stderr),
            stdout_excerpt=excerpt(exc.stdout),
            detail=f"no reply within {timeout:g}s",
        )
    except FileNotFoundError as exc:
        # Only a missing executable means "engine absent". PermissionError (a
        # present but non-executable file) is a misconfiguration and falls to
        # the OSError branch below, so it is not silently handed to a fallback.
        return EngineResult(
            kind=KIND_UNAVAILABLE,
            detail=f"cannot execute {argv[0]!r}: {exc}",
        )
    except OSError as exc:
        return EngineResult(kind=KIND_FAILED, detail=f"cannot run {argv[0]!r}: {exc}")

    stderr = excerpt(proc.stderr)
    stdout = excerpt(proc.stdout)
    if proc.returncode == EXIT_COMMAND_NOT_FOUND:
        return EngineResult(
            kind=KIND_UNAVAILABLE,
            exit_code=proc.returncode,
            stderr_excerpt=stderr,
            stdout_excerpt=stdout,
            detail=f"{argv[0]!r} exited {EXIT_COMMAND_NOT_FOUND} (engine absent)",
        )
    if proc.returncode != 0:
        return EngineResult(
            kind=KIND_FAILED,
            exit_code=proc.returncode,
            stderr_excerpt=stderr,
            stdout_excerpt=stdout,
            detail=f"{argv[0]!r} exited {proc.returncode}",
        )
    return EngineResult(
        kind=KIND_OK,
        text=proc.stdout.decode("utf-8", "replace"),
        exit_code=0,
        stderr_excerpt=stderr,
        stdout_excerpt=stdout,
    )


def run_engine_with_retry(
    argv: Sequence[str],
    prompt: str,
    timeout: float,
    *,
    cwd: Path,
    runner: Runner = subprocess.run,
) -> EngineResult:
    """run_engine, repeated once when the first call timed out or failed. An
    absent engine is never retried: nothing changes between two calls."""
    result = run_engine(argv, prompt, timeout, cwd=cwd, runner=runner)
    if result.kind in _TRANSIENT_KINDS:
        result = run_engine(argv, prompt, timeout, cwd=cwd, runner=runner)
    return result
