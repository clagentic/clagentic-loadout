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
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.review.atomic_io import write_bytes_atomic

EXIT_COMMAND_NOT_FOUND = 127
EXCERPT_LIMIT = 2000
#: Head kept ahead of the elision marker in a tail excerpt.
HEAD_PREFIX = 300
LAST_LINE_LIMIT = 300
#: Upper bound on the full-stderr file kept per failed call.
STDERR_FILE_LIMIT = 256 * 1024
_CLASSIFY_WINDOW = 4096
#: How many trailing non-empty stderr lines are searched for an engine error.
_CLASSIFY_LINES = 3

UNAVAILABLE_REASON_USAGE_LIMIT = "usage_limit"
#: Lowercase phrases an engine prints when its account quota is spent.
_USAGE_LIMIT_MARKERS = ("usage_limit_exceeded", "hit your usage limit")
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
    #: Why an engine that ran is nevertheless unavailable (see
    #: UNAVAILABLE_REASON_USAGE_LIMIT); empty for an ordinary absence.
    unavailable_reason: str = ""
    #: Last non-empty stderr line: where an engine reports why it failed.
    stderr_last_line: str = ""
    #: Path of the bounded full-stderr file kept in the run directory, if any.
    stderr_file: str = ""


def _decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else data


def excerpt(data: bytes | str | None, limit: int = EXCERPT_LIMIT) -> str:
    """Bounded, decoded head excerpt of captured output (None-safe). Right for
    a model reply; for diagnostics use tail_excerpt."""
    text = _decode(data)
    if len(text) > limit:
        return text[:limit] + f"... [{len(text) - limit} more characters]"
    return text


def tail_excerpt(
    data: bytes | str | None, limit: int = EXCERPT_LIMIT, head: int = HEAD_PREFIX
) -> str:
    """Bounded excerpt that keeps the END of the output, with a short head
    prefix and an explicit elision marker between. An engine that echoes its
    prompt to stderr puts the actual failure last, so a head-only excerpt would
    be all prompt."""
    text = _decode(data)
    if len(text) <= limit:
        return text
    head = min(head, limit // 2)
    elided = len(text) - limit
    return text[:head] + f"\n... [{elided} characters elided] ...\n" + text[len(text) - (limit - head):]


def last_line(data: bytes | str | None) -> str:
    """Last non-empty line of *data*, bounded; empty when there is none."""
    for line in reversed(_decode(data).splitlines()):
        if line.strip():
            return line.strip()[:LAST_LINE_LIMIT]
    return ""


def classify_unavailable(stderr: bytes | str | None) -> str:
    """Reason an engine that ran is out of service, read from the last few
    non-empty stderr lines only: the engine echoes the prompt first and its own
    error comes after, and a reviewed diff may legitimately contain these
    phrases. The echoed prompt ends with the output contract, so a phrase from
    the diff never sits in the final lines."""
    lines = [line for line in _decode(stderr)[-_CLASSIFY_WINDOW:].splitlines() if line.strip()]
    window = "\n".join(lines[-_CLASSIFY_LINES:]).lower()
    if any(marker in window for marker in _USAGE_LIMIT_MARKERS):
        return UNAVAILABLE_REASON_USAGE_LIMIT
    return ""


def _save_stderr(stderr: bytes | str | None, log_dir: Path | None, label: str) -> str:
    """Keep the tail of the full stderr in *log_dir* (the run directory) and
    return its path; empty when nothing was kept. Never raises: losing the
    file must not turn a diagnosable failure into a crash."""
    data = stderr.encode("utf-8") if isinstance(stderr, str) else stderr
    if log_dir is None or not data:
        return ""
    path = log_dir / f"{label}-{uuid.uuid4().hex[:12]}.stderr"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(path, data[-STDERR_FILE_LIMIT:])
    except OSError:
        return ""
    return str(path)


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
    log_dir: Path | None = None,
    label: str = "engine",
) -> EngineResult:
    """Execute *argv* once with *prompt* on stdin. With *log_dir*, a failed
    call's stderr is also kept there (bounded) and referenced by the result."""
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
            stderr_excerpt=tail_excerpt(exc.stderr),
            stdout_excerpt=tail_excerpt(exc.stdout),
            detail=f"no reply within {timeout:g}s",
            stderr_last_line=last_line(exc.stderr),
            stderr_file=_save_stderr(exc.stderr, log_dir, label),
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

    stderr = tail_excerpt(proc.stderr)
    stdout = tail_excerpt(proc.stdout)
    if proc.returncode == 0:
        return EngineResult(
            kind=KIND_OK,
            text=proc.stdout.decode("utf-8", "replace"),
            exit_code=0,
            stderr_excerpt=stderr,
            stdout_excerpt=stdout,
        )

    diagnostics = {
        "exit_code": proc.returncode,
        "stderr_excerpt": stderr,
        "stdout_excerpt": stdout,
        "stderr_last_line": last_line(proc.stderr),
        "stderr_file": _save_stderr(proc.stderr, log_dir, label),
    }
    if proc.returncode == EXIT_COMMAND_NOT_FOUND:
        return EngineResult(
            kind=KIND_UNAVAILABLE,
            detail=f"{argv[0]!r} exited {EXIT_COMMAND_NOT_FOUND} (engine absent)",
            **diagnostics,
        )
    reason = classify_unavailable(proc.stderr)
    if reason:
        # The engine is up but will refuse every call until its quota resets,
        # so it is treated like an absent one: no retry, straight to fallback.
        return EngineResult(
            kind=KIND_UNAVAILABLE,
            detail=f"{argv[0]!r} exited {proc.returncode}: engine unavailable ({reason})",
            unavailable_reason=reason,
            **diagnostics,
        )
    return EngineResult(
        kind=KIND_FAILED,
        detail=f"{argv[0]!r} exited {proc.returncode}",
        **diagnostics,
    )


def run_engine_with_retry(
    argv: Sequence[str],
    prompt: str,
    timeout: float,
    *,
    cwd: Path,
    runner: Runner = run_in_process_group,
    log_dir: Path | None = None,
    label: str = "engine",
) -> EngineResult:
    """run_engine, repeated once when the first call timed out or exited
    non-zero. An absent or quota-exhausted engine and a local-setup failure
    are never retried: nothing changes between two calls."""
    kwargs = {"cwd": cwd, "runner": runner, "log_dir": log_dir, "label": label}
    result = run_engine(argv, prompt, timeout, **kwargs)
    if result.kind in _TRANSIENT_KINDS and not result.deterministic:
        result = run_engine(argv, prompt, timeout, **kwargs)
    return result
