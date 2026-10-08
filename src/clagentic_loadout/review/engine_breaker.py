"""review.engine_breaker — per-run record that an engine is out of service.

Once the carrier reports it is unavailable for a reason that outlasts the run
(an exhausted usage quota), every remaining chunk of that run goes straight to
the fallback instead of spending calls on an engine that will refuse them.
Chunks are reviewed on worker threads, so the state is lock-protected.

With a *path*, the state is also kept on disk so a resumed invocation of the
same run does not re-hammer an engine already known to be down. It is scoped to
the run's state directory, expires after *ttl_seconds* (a quota resets), and is
removed by clear() once the run reaches a terminal outcome, so a fresh run
always starts with every engine presumed available.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clagentic_loadout.review.atomic_io import write_json_atomic

DEFAULT_TTL_SECONDS = 900.0
BREAKER_FILENAME = "engine-breaker.json"


class EngineBreaker:
    def __init__(
        self,
        path: Path | None = None,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._clock = clock
        self._ttl = ttl_seconds
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            stored = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(stored, dict):
            return
        now = self._clock()
        for engine, entry in stored.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("reason"), str):
                continue
            tripped_at = entry.get("tripped_at")
            if not isinstance(tripped_at, (int, float)) or now - tripped_at > self._ttl:
                continue
            self._entries[engine] = entry

    def _persist(self) -> None:
        if self._path is None:
            return
        try:
            write_json_atomic(self._path, self._entries)
        except OSError:
            # Persistence only saves a later resume some calls; failing to
            # write it must not fail the review that is in progress, and the
            # atomic writer leaves the previous file intact.
            return

    def trip(self, engine: str, reason: str, evidence: dict[str, Any] | None = None) -> None:
        """Mark *engine* out of service for the rest of the run; the first
        reason recorded wins. *evidence* is what the engine said, kept so a
        chunk that skips the engine can still show why."""
        with self._lock:
            if engine in self._entries:
                return
            self._entries[engine] = {
                "reason": reason,
                "evidence": dict(evidence or {}),
                "tripped_at": self._clock(),
            }
            self._persist()

    def reason(self, engine: str) -> str:
        """Why *engine* is out of service, or empty when it is not."""
        with self._lock:
            return self._entries.get(engine, {}).get("reason", "")

    def evidence(self, engine: str) -> dict[str, Any]:
        """What *engine* said when it was tripped; empty when unknown."""
        with self._lock:
            found = self._entries.get(engine, {}).get("evidence")
            return dict(found) if isinstance(found, dict) else {}

    def clear(self) -> None:
        """Forget every trip, in memory and on disk."""
        with self._lock:
            self._entries.clear()
            if self._path is not None:
                try:
                    self._path.unlink(missing_ok=True)
                except OSError:
                    return
