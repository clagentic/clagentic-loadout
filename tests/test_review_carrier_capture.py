"""review.carrier on the shared bounded-capture primitive: flat memory however
much an engine writes, a prompt larger than a pipe buffer handed to an engine
that never reads it, and owner-only saved stderr created at that mode."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
import time

import pytest

from clagentic_loadout import bounded_capture
from clagentic_loadout.review import carrier
from clagentic_loadout.review.atomic_io import ensure_private_dir, write_bytes_atomic
from clagentic_loadout.review.carrier import KIND_FAILED, KIND_OK, KIND_TIMEOUT, run_engine


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def open_umask():
    """A umask of 0, so a mode the code asks for is the mode the file gets and
    a too-wide default cannot hide behind the test environment's umask."""
    previous = os.umask(0)
    yield
    os.umask(previous)


def test_run_in_process_group_never_calls_communicate(monkeypatch, tmp_path):
    def forbidden(self, *args, **kwargs):
        raise AssertionError("communicate() buffers whole streams")

    monkeypatch.setattr(subprocess.Popen, "communicate", forbidden)

    result = run_engine([sys.executable, "-c", "print('[]')"], "p", 10, cwd=tmp_path)

    assert result.kind == KIND_OK
    assert result.text.strip() == "[]"


def test_a_large_prompt_reaches_the_engine_intact(tmp_path):
    prompt = "line of prompt text\n" * 200_000
    script = "import sys\ndata = sys.stdin.read()\nprint(len(data))\n"

    result = run_engine([sys.executable, "-c", script], prompt, 30, cwd=tmp_path)

    assert result.kind == KIND_OK
    assert int(result.text) == len(prompt)


def test_an_engine_that_never_reads_a_large_prompt_still_times_out(tmp_path):
    prompt = "x" * 4_000_000
    started = time.monotonic()

    result = run_engine(
        [sys.executable, "-c", "import time; time.sleep(60)"], prompt, 1, cwd=tmp_path
    )

    assert result.kind == KIND_TIMEOUT
    assert time.monotonic() - started < 30


def test_stderr_and_stdout_returned_to_the_caller_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(carrier, "STDOUT_CAPTURE_LIMIT", 1000)
    script = (
        "import sys\n"
        "sys.stdout.write('o' * 50000 + 'OUT-END')\n"
        "sys.stderr.write('e' * 2000000 + 'ERR-END')\n"
        "raise SystemExit(3)\n"
    )

    proc = carrier.run_in_process_group(
        [sys.executable, "-c", script], input=b"", capture_output=True, timeout=30, cwd=str(tmp_path)
    )

    assert proc.returncode == 3
    assert len(proc.stdout) == 1000 and proc.stdout.endswith(b"OUT-END")
    assert len(proc.stderr) == carrier.STDERR_FILE_LIMIT and proc.stderr.endswith(b"ERR-END")


def test_a_timeout_still_reports_the_bounded_tail_it_captured(tmp_path):
    script = (
        "import sys, time\n"
        "sys.stderr.write('z' * 600000 + 'STALL-NOTE'); sys.stderr.flush()\n"
        "time.sleep(60)\n"
    )

    result = run_engine(
        [sys.executable, "-c", script], "p", 2, cwd=tmp_path, log_dir=tmp_path / "logs"
    )

    assert result.kind == KIND_TIMEOUT
    assert "STALL-NOTE" in result.stderr_excerpt
    assert os.path.getsize(result.stderr_file) == carrier.STDERR_FILE_LIMIT


def test_the_saved_stderr_directory_and_file_are_owner_only(tmp_path, open_umask):
    result = run_engine(
        [sys.executable, "-c", "import sys; sys.stderr.write('echoed prompt'); sys.exit(2)"],
        "p", 10, cwd=tmp_path, log_dir=tmp_path / "carrier-logs",
    )

    assert result.kind == KIND_FAILED
    assert _mode(tmp_path / "carrier-logs") == 0o700
    assert _mode(result.stderr_file) == 0o600


def test_the_file_is_created_at_its_mode_not_narrowed_afterwards(tmp_path, open_umask, monkeypatch):
    observed: list[int] = []
    real_replace = os.replace

    def spying_replace(src, dst):
        observed.append(_mode(src))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spying_replace)

    write_bytes_atomic(tmp_path / "f", b"data", mode=0o600)

    # The temp file already had the narrow mode when it was moved into place.
    assert observed == [0o600]


def test_a_log_directory_left_wide_by_an_older_run_is_narrowed(tmp_path, open_umask):
    log_dir = tmp_path / "carrier-logs"
    log_dir.mkdir(mode=0o755)

    ensure_private_dir(log_dir)

    assert _mode(log_dir) == 0o700


def test_a_symlink_at_the_private_dir_path_is_refused_and_its_target_left_alone(tmp_path, open_umask):
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o755)
    link = tmp_path / "carrier-logs"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError):
        ensure_private_dir(link)

    assert _mode(target) == 0o755


def test_saved_stderr_is_not_written_through_a_symlinked_log_dir(tmp_path, open_umask):
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o755)
    link = tmp_path / "carrier-logs"
    link.symlink_to(target, target_is_directory=True)

    result = run_engine(
        [sys.executable, "-c", "import sys; sys.stderr.write('echoed prompt'); sys.exit(2)"],
        "p", 10, cwd=tmp_path, log_dir=link,
    )

    assert result.kind == KIND_FAILED
    assert result.stderr_file == ""
    assert list(target.iterdir()) == []
    assert _mode(target) == 0o755


def test_ensure_private_dir_creates_missing_parents(tmp_path, open_umask):
    ensure_private_dir(tmp_path / "a" / "b" / "carrier-logs")

    assert _mode(tmp_path / "a" / "b" / "carrier-logs") == 0o700


def test_the_default_write_mode_is_unchanged(tmp_path, open_umask):
    write_bytes_atomic(tmp_path / "f", b"data")

    assert _mode(tmp_path / "f") == 0o666


def test_stdin_feed_tolerates_a_child_that_closed_its_stdin(tmp_path, monkeypatch):
    # threading swallows an exception raised in a thread, so a BrokenPipeError
    # in the feeder would leave is_alive() False either way; record it instead.
    thread_errors: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(threading, "excepthook", thread_errors.append)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import os; os.close(0); import time; time.sleep(0.2)"],
        stdin=subprocess.PIPE,
    )

    feeder = bounded_capture.start_stdin_feed(proc, b"x" * 3_000_000)
    feeder.join(10)
    proc.wait(10)

    assert not feeder.is_alive()
    assert thread_errors == []
