"""push.bounded_capture -- read a child's pipes incrementally, keeping only a
bounded tail, so memory stays flat however much the child writes.

`Popen.communicate` buffers a stream whole before returning; a verification
command that logs hundreds of megabytes would be held in memory just to have
all but its last few kilobytes discarded. Here each pipe is drained by a
reader thread into a `TailBuffer` that never holds more than a fixed number
of bytes.
"""

from __future__ import annotations

import subprocess
import threading

_READ_CHUNK = 64 * 1024


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
    proc: "subprocess.Popen[bytes]", limit: int
) -> tuple[TailBuffer, TailBuffer, list[threading.Thread]]:
    """Start draining proc.stdout and proc.stderr; returns (stdout, stderr,
    threads). Both pipes must have been opened with subprocess.PIPE."""
    out_buf, err_buf = TailBuffer(limit), TailBuffer(limit)
    threads = [
        threading.Thread(target=_drain, args=(proc.stdout, out_buf), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err_buf), daemon=True),
    ]
    for thread in threads:
        thread.start()
    return out_buf, err_buf, threads


def join_capture(threads: list[threading.Thread], timeout: float) -> None:
    """Wait for the readers to reach EOF, bounded: a descendant that left the
    process group can hold a pipe open indefinitely."""
    for thread in threads:
        thread.join(timeout)


__all__ = ["TailBuffer", "join_capture", "start_tail_capture"]
