"""review.engine_breaker — per-run record that an engine is out of service.

Once the carrier reports it is unavailable for a reason that outlasts the run
(an exhausted usage quota), every remaining chunk of that run goes straight to
the fallback instead of spending calls on an engine that will refuse them.
Chunks are reviewed on worker threads, so the state is lock-protected.
"""

from __future__ import annotations

import threading


class EngineBreaker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reasons: dict[str, str] = {}

    def trip(self, engine: str, reason: str) -> None:
        """Mark *engine* out of service for the rest of the run; the first
        reason recorded wins."""
        with self._lock:
            self._reasons.setdefault(engine, reason)

    def reason(self, engine: str) -> str:
        """Why *engine* is out of service, or empty when it is not."""
        with self._lock:
            return self._reasons.get(engine, "")
