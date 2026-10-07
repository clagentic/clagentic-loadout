"""bounded_capture -- read a child's pipes incrementally, keeping only a
bounded tail, so memory stays flat however much the child writes.

`Popen.communicate` buffers a stream whole before returning; a verification
command that logs hundreds of megabytes would be held in memory just to have
all but its last few kilobytes discarded. Here each pipe is drained by a
reader thread into a `TailBuffer` that never holds more than a fixed number
of bytes. The one primitive shared by every verb that runs a child process
(push verification commands, review carriers).
"""

from __future__ import annotations

import subprocess
import threading

_READ_CHUNK = 64 * 1024
_WRITE_CHUNK = 64 * 1024


class TailBuffer:
    """The last `limit` bytes appended, plus whether anything was dropped."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._data = bytearray()
        self.dropped = False

    def append(self, chunk: bytes) -> None:
        self._data += chunk
        # Trim lazily at twice the limit so a stream of small chunks does not
        # copy the whole buffer on every append.
        if len(self._data) > 2 * self._limit:
            self._trim()

    def _trim(self) -> None:
        if len(self._data) > self._limit:
            del self._data[: len(self._data) - self._limit]
            self.dropped = True

    def value(self) -> bytes:
        self._trim()
        return bytes(self._data)


def _drain(stream, buffer: TailBuffer) -> None:
    try:
        while True:
            chunk = stream.read(_READ_CHUNK)
            if not chunk:
                return
            buffer.append(chunk)
    except (OSError, ValueError):
        # The pipe was closed under us (e.g. after a kill); what was read
        # so far is the result.
        return


def start_tail_capture(
    proc: "subprocess.Popen[bytes]", limit: int, *, stderr_limit: int | None = None
) -> tuple[TailBuffer, TailBuffer, list[threading.Thread]]:
    """Start draining proc.stdout and proc.stderr; returns (stdout, stderr,
    threads). Both pipes must have been opened with subprocess.PIPE. *limit*
    bounds stdout, and stderr too unless *stderr_limit* gives it its own."""
    out_buf = TailBuffer(limit)
    err_buf = TailBuffer(limit if stderr_limit is None else stderr_limit)
    threads = [
        threading.Thread(target=_drain, args=(proc.stdout, out_buf), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err_buf), daemon=True),
    ]
    for thread in threads:
        thread.start()
    return out_buf, err_buf, threads


def _feed(stream, data: bytes) -> None:
    try:
        for start in range(0, len(data), _WRITE_CHUNK):
            stream.write(data[start : start + _WRITE_CHUNK])
        stream.flush()
    except (OSError, ValueError):
        # The child closed its stdin or exited without reading it all; that is
        # the child's answer, reported through its exit status and output.
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def start_stdin_feed(proc: "subprocess.Popen[bytes]", data: bytes) -> threading.Thread:
    """Write *data* to proc.stdin from a thread and close it. A caller that
    wrote it inline would block on a child that stops reading, and never get
    to enforce its timeout."""
    thread = threading.Thread(target=_feed, args=(proc.stdin, data), daemon=True)
    thread.start()
    return thread


def join_capture(threads: list[threading.Thread], timeout: float) -> None:
    """Wait for the readers to reach EOF, bounded: a descendant that left the
    process group can hold a pipe open indefinitely."""
    for thread in threads:
        thread.join(timeout)


__all__ = ["TailBuffer", "join_capture", "start_stdin_feed", "start_tail_capture"]
