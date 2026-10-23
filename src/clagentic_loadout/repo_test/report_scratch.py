"""repo_test.report_scratch — a per-run place for the test command to write its
JUnit report, reachable by whatever user the command ends up running as.

The run record under the reports root is owner-only, and a declared command may
re-execute itself as a different unprivileged user partway through, so the
runner cannot hand the report path inside that record to the command. Instead
each run gets a fresh directory outside the store:

* its name is random and the directory is mode 0o733 (others may enter and create
  files but cannot list it), so only a process that was handed the path can find
  the report file;
* the report file inside it is pre-created mode 0o666, so a command that opens the
  path for writing succeeds whichever user it is;
* once the command has ended the report is copied into the private run record
  (never followed through a symlink) and the scratch directory is removed, on
  success, failure and timeout alike.

Trade-off against the alternatives: a report path inside the repository's own
tree would touch the consumer's working tree and show up in its status; widening
the store itself would expose the other runs' records. The cost here is that the
report is briefly writable by any user who is told, or guesses, the path of an
unlisted random name. Nothing here needs root or knows a user name.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.repo_test.junit import MAX_JUNIT_BYTES, JunitError

# TMPDIR (via tempfile.gettempdir) is tried first. These are the POSIX/FHS
# standard temporary directories, the same fallback set the standard library's
# tempfile module tries; they are not a deployment- or host-specific path.
_FALLBACK_BASES = ("/tmp", "/var/tmp")
_COPY_CHUNK = 1024 * 1024
_SCRATCH_DIR_MODE = 0o733
_SCRATCH_FILE_MODE = 0o666
_REPORT_NAME = "junit.xml"


@dataclass(frozen=True)
class ReportScratch:
    directory: Path
    junit_path: Path


def _others_can_traverse(path: Path) -> bool:
    """True when every directory from the root down to *path* has the
    search bit for others."""
    current = path.resolve()
    while True:
        try:
            if not current.stat().st_mode & stat.S_IXOTH:
                return False
        except OSError:
            return False
        if current.parent == current:
            return True
        current = current.parent


def _scratch_base() -> Path:
    """The first temp location we can create in that an unrelated user can reach,
    else the process's own temp dir (which still serves a command that keeps the
    caller's user)."""
    default = Path(tempfile.gettempdir())
    for candidate in (default, *(Path(p) for p in _FALLBACK_BASES)):
        if candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK) and _others_can_traverse(candidate):
            return candidate
    return default


def create_scratch() -> ReportScratch:
    """Raises OSError when no scratch directory can be made."""
    directory = Path(tempfile.mkdtemp(prefix="loadout-junit-", dir=_scratch_base()))
    try:
        os.chmod(directory, _SCRATCH_DIR_MODE)
        junit_path = directory / _REPORT_NAME
        fd = os.open(junit_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _SCRATCH_FILE_MODE)
        os.close(fd)
        os.chmod(junit_path, _SCRATCH_FILE_MODE)
    except OSError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return ReportScratch(directory, junit_path)


def collect_report(scratch: ReportScratch, dest: Path) -> None:
    """Copy the report from *scratch* into the private record at *dest*. Raises
    JunitError, naming the path and the reason, when there is nothing usable."""
    source = scratch.junit_path
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise JunitError(
            f"the command left no readable report at {str(source)!r} (it may have removed or replaced it): {exc}"
        ) from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise JunitError(f"the report path {str(source)!r} is not a regular file")
        if info.st_size == 0:
            raise JunitError(
                f"the command never wrote its report to {str(source)!r}: the file is still empty "
                "(it may have ended early, or run as a user that could not open that path)"
            )
        if info.st_size > MAX_JUNIT_BYTES:
            raise JunitError(f"the JUnit report is {info.st_size} bytes, over the {MAX_JUNIT_BYTES} byte limit")
        try:
            out_fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except OSError as exc:
            raise JunitError(f"cannot store the report at {str(dest)!r}: {exc}") from exc
        # The size check above is only an early exit: the command may still be
        # appending, so the copy itself reads at most the cap plus one byte.
        copied = 0
        oversized = False
        failure: OSError | None = None
        try:
            with os.fdopen(out_fd, "wb") as out:
                while True:
                    chunk = handle.read(min(_COPY_CHUNK, MAX_JUNIT_BYTES + 1 - copied))
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > MAX_JUNIT_BYTES:
                        oversized = True
                        break
                    out.write(chunk)
        except OSError as exc:
            failure = exc
        if oversized or failure is not None:
            dest.unlink(missing_ok=True)
        if oversized:
            raise JunitError(
                f"the JUnit report at {str(source)!r} grew past the {MAX_JUNIT_BYTES} byte limit while it was being stored"
            )
        if failure is not None:
            raise JunitError(f"cannot store the report at {str(dest)!r}: {failure}") from failure


def remove_scratch(scratch: ReportScratch) -> str | None:
    """Remove the scratch directory; returns a description of what could not be
    removed, else None."""
    try:
        shutil.rmtree(scratch.directory)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"could not remove the report scratch directory {str(scratch.directory)!r}: {exc}"
    return None


__all__ = ["ReportScratch", "collect_report", "create_scratch", "remove_scratch"]
