"""repo_test.report_scratch — a per-run place for the test command to write its
JUnit report, reachable by whatever user the command ends up running as, and the
checks that the report found there was written by that command.

The run record under the reports root is owner-only, and a declared command may
re-execute itself as a different unprivileged user partway through, so the
runner cannot hand the report path inside that record to the command. Instead
each run gets a fresh directory outside the store:

* its name is random and the directory is mode 0o1733 (others may enter and create
  files but cannot list it; the sticky bit stops anyone but a file's owner removing
  or renaming it), so only a process that was handed the path can find the name;
* the runner does NOT create the report. The command creates it, so the file is
  owned by, and has the permissions of, the user the command wrote it as, and a
  stranger cannot write into it afterwards;
* once the command has ended the report is verified and copied into the private
  run record (never followed through a symlink) and the scratch directory is
  removed, on success, failure and timeout alike.

Verification at copy-back refuses a report that is not a regular file with one
link, that is writable by others, or whose owner is neither the caller nor a user
the command's process tree was seen running as. The refusal names the reason and
nothing is stored; the stored run record carries what was verified.

Residual risk: a stranger who learns the path from the process list can still
create the report name before the command does. That is caught by the owner check
unless the stranger runs as the same user as the caller or as a user the command
itself switched to. A user the command switched to for less than the sampling
interval is not seen, which refuses a genuine report rather than trusting a
forged one. Group-writable reports are accepted, because a default umask of 002 is
common; members of the report's group could rewrite it in place. Nothing here
needs root or knows a user name.

Trade-off against the alternatives: a report path inside the repository's own
tree would touch the consumer's working tree and show up in its status; widening
the store itself would expose the other runs' records; an inherited descriptor in
place of a path needs every declared command to accept one and to keep it across a
privilege-dropping re-execution.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.repo_test.junit import MAX_JUNIT_BYTES, JunitError

VERIFIED = "verified"
REFUSED = "refused"

# TMPDIR (via tempfile.gettempdir) is tried first. These are the POSIX/FHS
# standard temporary directories, the same fallback set the standard library's
# tempfile module tries; they are not a deployment- or host-specific path.
_FALLBACK_BASES = ("/tmp", "/var/tmp")
_COPY_CHUNK = 1024 * 1024
_SCRATCH_DIR_MODE = 0o1733
_REPORT_NAME = "junit.xml"


class ReportUntrusted(JunitError):
    """The report exists but was not shown to come from the test command."""


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
    except OSError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return ReportScratch(directory, directory / _REPORT_NAME)


def _check_origin(info: os.stat_result, source: Path, allowed_uids: frozenset[int]) -> None:
    """Refuse a report that cannot have been written only by the command's tree."""
    if info.st_nlink != 1:
        raise ReportUntrusted(
            f"the report {str(source)!r} has {info.st_nlink} links: it may be another file linked in"
        )
    if info.st_mode & stat.S_IWOTH:
        raise ReportUntrusted(
            f"the report {str(source)!r} is writable by any user (mode {stat.S_IMODE(info.st_mode):04o}), "
            "so it cannot be shown to be the command's own output"
        )
    if info.st_uid not in allowed_uids:
        seen = ", ".join(str(uid) for uid in sorted(allowed_uids))
        raise ReportUntrusted(
            f"the report {str(source)!r} is owned by user id {info.st_uid}, which the command was not seen "
            f"running as (seen: {seen}); another user may have created it"
        )


def collect_report(scratch: ReportScratch, dest: Path, allowed_uids: frozenset[int]) -> dict[str, object]:
    """Verify the report in *scratch* and copy it into the private record at
    *dest*; returns what was verified. *allowed_uids* are the users the command
    may legitimately have written it as. Raises JunitError, naming the path and
    the reason, when there is nothing usable, and ReportUntrusted when the report
    is not shown to come from the command."""
    source = scratch.junit_path
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise JunitError(
            f"the command never wrote its report to {str(source)!r}: no file exists there "
            "(it may have ended early, run as a user that could not create it, or removed the file)"
        ) from exc
    except OSError as exc:
        raise JunitError(
            f"the command left no readable report at {str(source)!r} (it may have removed or replaced it): {exc}"
        ) from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ReportUntrusted(f"the report path {str(source)!r} is not a regular file")
        _check_origin(info, source, allowed_uids)
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
    return {
        "status": VERIFIED,
        "owner_uid": info.st_uid,
        "single_link": True,
        "writable_by_others": False,
        "command_uids": sorted(allowed_uids),
    }


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


__all__ = [
    "REFUSED",
    "VERIFIED",
    "ReportScratch",
    "ReportUntrusted",
    "collect_report",
    "create_scratch",
    "remove_scratch",
]
