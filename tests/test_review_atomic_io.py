"""Every state or record file under review/ is written atomically."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest import mock

import pytest

from clagentic_loadout.review import atomic_io
from clagentic_loadout.review.engine_breaker import EngineBreaker

_REVIEW_SRC = Path(atomic_io.__file__).parent


def test_failed_replace_keeps_previous_file_and_leaves_no_temp(tmp_path):
    target = tmp_path / "state.json"
    atomic_io.write_json_atomic(target, {"v": 1})
    with mock.patch("os.replace", side_effect=OSError("simulated mid-write failure")):
        with pytest.raises(OSError):
            atomic_io.write_json_atomic(target, {"v": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_breaker_persist_failure_keeps_previous_trip_file(tmp_path):
    path = tmp_path / "engine-breaker.json"
    breaker = EngineBreaker(path)
    breaker.trip("carrier", "usage_limit", {"stderr_last_line": "limit"})
    before = path.read_text(encoding="utf-8")
    with mock.patch("os.replace", side_effect=OSError("simulated mid-write failure")):
        breaker.trip("fallback", "usage_limit")
    assert path.read_text(encoding="utf-8") == before
    assert EngineBreaker(path).reason("carrier") == "usage_limit"


def test_no_review_module_writes_files_outside_the_atomic_writer():
    """A new direct write under review/ would bypass the atomic writer."""
    direct = re.compile(r"\.write_text\(|\.write_bytes\(|\bopen\([^)]*[\"'][wax]")
    offenders = [
        path.name
        for path in sorted(_REVIEW_SRC.glob("*.py"))
        if path.name != "atomic_io.py" and direct.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
