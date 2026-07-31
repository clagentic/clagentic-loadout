"""poll.py — file-poll wait primitive.

Ported from the reference implementation's poll-wait (Wave A slice 4, tome
#688). The source copy stays primary until that project's separate CUT
OVER + RETIRE + VERIFY-GONE task for this slice.

Some Bash tool-call classifiers reject command substitution (`$(...)`), which
rules out the conventional poll/wait shell idiom
(`until $(wc -l < file) -gt N; do sleep 1; done`). This module implements the
same poll loop in pure Python — the caller's tool call only ever invokes one
clean function/command, with no command substitution anywhere in it.

Public surface:
    poll_wait(file_path, *, min_lines=1, grep_pattern=None, timeout=120.0,
              interval=2.0) -> PollWaitResult

Zero third-party dependencies.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MIN_LINES = 1
DEFAULT_TIMEOUT = 120.0
DEFAULT_INTERVAL = 2.0


class InvalidGrepPatternError(ValueError):
    """Raised when the supplied grep_pattern is not a valid Python regex."""


@dataclass(frozen=True)
class PollWaitResult:
    """Outcome of a poll_wait() call.

    met: True when the file satisfied min_lines (and grep_pattern, if given)
        before the timeout elapsed.
    elapsed_seconds: wall-clock time spent polling.
    """

    met: bool
    elapsed_seconds: float


def _condition_met(file_path: str, min_lines: int, grep_pattern: str | None) -> bool:
    """Return True when the file at file_path meets all conditions."""
    p = Path(file_path)
    if not p.exists():
        return False
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    lines = [ln for ln in content.splitlines() if ln.strip()]
    if len(lines) < min_lines:
        return False

    if grep_pattern is not None and not re.search(grep_pattern, content):
        return False

    return True


def poll_wait(
    file_path: str,
    *,
    min_lines: int = DEFAULT_MIN_LINES,
    grep_pattern: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
) -> PollWaitResult:
    """Poll `file_path` until it has at least `min_lines` non-empty lines
    (and, if given, contains a match for `grep_pattern`), or `timeout`
    seconds elapse.

    Args:
        file_path: path to the file to poll.
        min_lines: minimum number of non-empty lines required. Defaults to 1
            (file must be non-empty).
        grep_pattern: Python regex that must appear in the file content.
            When None (default), only min_lines is checked.
        timeout: maximum wall-clock seconds to wait. Defaults to 120.
        interval: poll interval in seconds. Defaults to 2.

    Returns:
        PollWaitResult(met=True, ...) once the condition is satisfied, or
        PollWaitResult(met=False, ...) once the timeout elapses without the
        condition being met. Never raises on timeout — callers that need a
        process exit code translate `met` themselves (see cli.py).

    Raises:
        InvalidGrepPatternError: grep_pattern is not a valid regex.
        ValueError: min_lines < 0, timeout <= 0, or interval <= 0.
    """
    if min_lines < 0:
        raise ValueError("min_lines must be >= 0.")
    if timeout <= 0:
        raise ValueError("timeout must be > 0.")
    if interval <= 0:
        raise ValueError("interval must be > 0.")
    if grep_pattern is not None:
        try:
            re.compile(grep_pattern)
        except re.error as exc:
            raise InvalidGrepPatternError(
                f"grep_pattern {grep_pattern!r} is not a valid regex: {exc}."
            ) from exc

    start = time.monotonic()
    deadline = start + timeout
    while True:
        if _condition_met(file_path, min_lines, grep_pattern):
            return PollWaitResult(met=True, elapsed_seconds=time.monotonic() - start)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return PollWaitResult(met=False, elapsed_seconds=time.monotonic() - start)
        time.sleep(min(interval, max(remaining, 0.1)))
