"""Concurrent writers on one head must each leave their file.

The head directory is created by whichever writer gets there first; every other
writer must use it as it is. The interleave is forced by holding each writer
after it has seen the head directory absent, so both go on to create it.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

from clagentic_loadout import artifact_store
from clagentic_loadout.verdict_record.emit import emit_record, try_emit_record
from clagentic_loadout.verdict_record.store import StoreError, records_at_head, write_record
from clagentic_loadout.verdict_record.record import build_record

HEAD = "d" * 40
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-01T00:00:05Z"


def _emit(root: Path, gate: str):
    return emit_record(gate=gate, head_sha=HEAD, tool="t", exit=0, started=T0, finished=T1, root=root)


def _hold_head_dir_checks_at_barrier(monkeypatch, directory: Path, parties: int):
    """Hold every writer, once, after it has seen the head directory absent and
    again just before it creates the directory, so all of them race to create it
    whichever way the writer checks first."""
    barriers = {
        "exists": threading.Barrier(parties, timeout=10),
        "mkdir": threading.Barrier(parties, timeout=10),
    }
    waited: set[tuple[str, int]] = set()
    real_exists = Path.exists
    real_mkdir = Path.mkdir

    def wait(kind):
        key = (kind, threading.get_ident())
        if key not in waited:
            waited.add(key)
            barriers[kind].wait()

    def exists(self, *args, **kwargs):
        result = real_exists(self, *args, **kwargs)
        if self == directory and not result:
            wait("exists")
        return result

    def mkdir(self, *args, **kwargs):
        if self == directory:
            wait("mkdir")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(Path, "mkdir", mkdir)


def test_two_gates_writing_the_same_new_head_both_leave_a_record(tmp_path, monkeypatch):
    root = tmp_path / "records"
    root.mkdir(mode=0o755)
    _hold_head_dir_checks_at_barrier(monkeypatch, root / HEAD, parties=2)
    errors: list[BaseException] = []

    def run(gate):
        try:
            _emit(root, gate)
        except BaseException as exc:  # collected so the main thread can assert
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(g,)) for g in ("reviewer:agent-alpha", "reviewer:agent-beta")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert errors == []
    gates = sorted(r.gate for r in records_at_head(root, HEAD).records)
    assert gates == ["reviewer:agent-alpha", "reviewer:agent-beta"]


def test_a_head_directory_made_by_another_writer_is_used_and_not_chmodded(tmp_path, monkeypatch):
    root = tmp_path / "records"
    root.mkdir(mode=0o755)
    existing = root / HEAD
    existing.mkdir(mode=0o2775)
    os.chmod(existing, 0o2775)
    chmodded: list[str] = []
    real_chmod = os.chmod

    def spy(path, mode, *a, **kw):
        chmodded.append(str(path))
        return real_chmod(path, mode, *a, **kw)

    monkeypatch.setattr(os, "chmod", spy)
    real_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda self, *a, **kw: False if self == existing else real_exists(self, *a, **kw)
    )
    _emit(root, "reviewer:agent-alpha")

    assert str(existing) not in chmodded
    assert stat.S_IMODE(existing.stat().st_mode) == 0o2775
    assert len(records_at_head(root, HEAD).records) == 1


def test_a_symlink_at_the_head_path_is_still_refused(tmp_path):
    root = tmp_path / "records"
    root.mkdir(mode=0o755)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / HEAD).symlink_to(elsewhere)
    with pytest.raises(StoreError, match="symlink"):
        _emit(root, "reviewer:agent-alpha")
    assert list(elsewhere.iterdir()) == []


def test_a_symlink_swapped_in_before_the_creation_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "records"
    root.mkdir(mode=0o755)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    directory = root / HEAD
    real_mkdir = Path.mkdir

    def mkdir(self, *a, **kw):
        if self == directory:
            directory.symlink_to(elsewhere)
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(StoreError, match="symlink"):
        _emit(root, "reviewer:agent-alpha")
    assert list(elsewhere.iterdir()) == []


def test_the_shared_writer_covers_every_head_keyed_store(tmp_path, monkeypatch):
    # Push verification records and sealed specs reach the filesystem through
    # write_into_head_dir, so one forced interleave on it covers them.
    root = tmp_path / "store"
    root.mkdir(mode=0o755)
    kind = artifact_store.StoreKind("things", "X_ENV", "x", "y", "things")
    _hold_head_dir_checks_at_barrier(monkeypatch, root / HEAD, parties=2)
    errors: list[BaseException] = []

    def run(name):
        try:
            artifact_store.write_into_head_dir(root, HEAD, name, b"{}", kind=kind, description="a thing")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(n,)) for n in ("a.json", "b.json")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert errors == []
    assert sorted(p.name for p in (root / HEAD).iterdir()) == ["a.json", "b.json"]


def test_a_failure_that_survives_the_retry_warns_with_the_directory_and_errno(tmp_path, monkeypatch, capsys):
    root = tmp_path / "records"
    root.mkdir(mode=0o755)

    def always_exists(self, mode=0o777, parents=False, exist_ok=False):
        raise FileExistsError(17, "File exists", str(self))

    real_mkdir = Path.mkdir
    monkeypatch.setattr(
        Path, "mkdir", lambda self, *a, **kw: always_exists(self, *a, **kw) if self == root / HEAD else real_mkdir(self, *a, **kw)
    )
    result = try_emit_record(
        "prog", gate="reviewer:agent-alpha", head_sha=HEAD, tool="t", exit=0, started=T0, finished=T1, root=root
    )
    err = capsys.readouterr().err
    assert result is None
    assert "WARNING" in err and str(root / HEAD) in err and "errno 17" in err
