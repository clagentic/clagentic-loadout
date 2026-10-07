"""Shared fixtures for review.chunk_review tests: a review profile, and a
scripted runner so no subprocess or real engine is involved."""

from __future__ import annotations

import json
import subprocess

from clagentic_loadout.review.chunk_review import review_chunk
from clagentic_loadout.review.chunking import plan_chunks
from clagentic_loadout.review.profile_config import ReviewProfile
from tests._review_cli_support import make_diff

CARRIER = "carrier-engine"
FALLBACK = "fallback-engine"
ARRAY = json.dumps(
    [{"file": "a.py", "line": 1, "rule_id": "R1", "severity": "nit", "message": "m"}]
)

__all__ = ["ARRAY", "CARRIER", "FALLBACK", "profile", "review", "scripted_runner"]


def profile(*, fallback: bool, **overrides) -> ReviewProfile:
    fields = {
        "name": "reviewer",
        "carrier": (CARRIER,),
        "fallback": (FALLBACK,) if fallback else None,
        "rulebook_text": "",
        "chunk_lines": 600,
        "timeout_seconds": 5.0,
        "fallback_timeout_seconds": 5.0,
        "max_attempts": 3,
        "parallel": 1,
    }
    fields.update(overrides)
    return ReviewProfile(**fields)


def scripted_runner(scripts: dict[str, list]):
    """Each call to an engine pops its next scripted step: an exception
    instance is raised, a (code, stdout, stderr) tuple is returned."""
    calls: list[str] = []

    def run(argv, *, input, capture_output, timeout, cwd):
        name = argv[0]
        calls.append(name)
        step = scripts[name].pop(0)
        if isinstance(step, BaseException):
            raise step
        code, stdout, stderr = step
        return subprocess.CompletedProcess(argv, code, stdout.encode(), stderr.encode())

    run.calls = calls
    return run


def review(tmp_path, scripts, *, fallback: bool, **profile_overrides):
    """Review one small chunk against *scripts*; returns (record, runner)."""
    chunk = plan_chunks(make_diff({"a.py": 3}), 600)[0]
    runner = scripted_runner(scripts)
    record = review_chunk(
        chunk, 1, profile(fallback=fallback, **profile_overrides),
        attempts_before=0, cwd=tmp_path, runner=runner,
    )
    return record, runner
