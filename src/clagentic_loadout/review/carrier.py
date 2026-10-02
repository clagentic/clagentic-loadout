"""review.carrier — run one configured review command against one prompt.

A carrier is an argv (see review.profile_config) executed directly with the
prompt on stdin. This module classifies the outcome without interpreting the
reply: ok (exit 0), unavailable (the executable is absent, or exit 127),
timeout, or failed (any other non-zero exit). stderr and a bounded excerpt of
stdout are always kept, including on a timeout, so a stall can be diagnosed
from the per-chunk record instead of being a bare "timed out".
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

EXIT_COMMAND_NOT_FOUND = 127
EXCERPT_LIMIT = 2000
#: How long to wait for pipes to close after the process group is killed.
_DRAIN_SECONDS = 5

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
    #: True when the failure comes from the local setup (bad working
    #: directory, unexecutable file), so repeating the call cannot change it.
    deterministic: bool = False


def excerpt(data: bytes | str | None, limit: int = EXCERPT_LIMIT) -> str:
    """Bounded, decoded excerpt of captured output (None-safe)."""
    if data is None:
        return ""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    if len(text) > limit:
        return text[:limit] + f"... [{len(text) - limit} more characters]"
    return text


def _kill_group(proc: "subprocess.Popen[bytes]") -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # The group is already gone, or was never ours to signal; fall back
        # to the direct child so a timeout never leaves it running.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


def run_in_process_group(
    argv: Sequence[str],
    *,
    input: bytes,
    capture_output: bool,
    timeout: float,
    cwd: str,
) -> "subprocess.CompletedProcess[bytes]":
    """subprocess.run semantics, except the child leads its own process group
    and a timeout kills the whole group. subprocess.run kills only the direct
    child and then waits for pipe EOF, so a carrier whose grandchildren inherit
    stdout would hang past the timeout."""
    if not capture_output:
        raise ValueError("run_in_process_group always captures output")
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=_DRAIN_SECONDS)
        except subprocess.TimeoutExpired as drain:
            # A descendant that left the group can still hold a pipe open;
            # report what was captured rather than waiting on it.
            stdout, stderr = drain.stdout, drain.stderr
            # communicate() gave up before reaping the direct child; without
            # a wait it would stay a zombie for the life of this process.
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=_DRAIN_SECONDS)
        raise subprocess.TimeoutExpired(
            list(argv), timeout, output=stdout, stderr=stderr
        ) from None
    except BaseException:
        _kill_group(proc)
        proc.wait()
        raise
    return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)


def run_engine(
    argv: Sequence[str],
    prompt: str,
    timeout: float,
    *,
    cwd: Path,
    runner: Runner = run_in_process_group,
) -> EngineResult:
    """Execute *argv* once with *prompt* on stdin."""
    # subprocess raises FileNotFoundError for a missing cwd exactly as it does
    # for a missing executable; checking first keeps a bad run directory from
    # being reported as an absent engine and handed to the fallback.
    if not cwd.is_dir():
        return EngineResult(
            kind=KIND_FAILED,
            detail=f"cannot run {argv[0]!r}: working directory {str(cwd)!r} does not exist",
            deterministic=True,
        )
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
        return EngineResult(
            kind=KIND_FAILED, detail=f"cannot run {argv[0]!r}: {exc}", deterministic=True
        )

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
    runner: Runner = run_in_process_group,
) -> EngineResult:
    """run_engine, repeated once when the first call timed out or exited
    non-zero. An absent engine or a local-setup failure is never retried:
    nothing changes between two calls."""
    result = run_engine(argv, prompt, timeout, cwd=cwd, runner=runner)
    if result.kind in _TRANSIENT_KINDS and not result.deterministic:
        result = run_engine(argv, prompt, timeout, cwd=cwd, runner=runner)
    return result
