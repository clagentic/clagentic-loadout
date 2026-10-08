"""merge.common_dir_guard -- detect and undo pre_check writes to the shared git state.

A pre_check runs PR-head code inside a linked worktree. A linked worktree shares
the repository's git common directory with the checkout the merger works from,
so that code can write there: a hook under `hooks/`, a `core.hooksPath` or
`core.fsmonitor` entry in `config`, or an `info/` file. Git would later run such
a hook or helper with the merger's full, unscrubbed environment (for example
during the post-merge tree sync).

This guard narrows that. Before the checks run it records, for the common
directory's `hooks/` and `info/` trees and its `config` and `config.worktree`
files, the set of entries with their kind, mode and content hash (and the
content itself, so it can be restored). After the checks, whatever their
outcome, it compares the live state to the record. On any difference it puts
the recorded state back exactly (contents and modes; timestamps are not
preserved) and raises `CommonDirModifiedError` naming the changed paths, so the
caller refuses the merge.

What this does and does not cover:

  * It covers only the four locations above. Other common-dir content (refs,
    objects, `packed-refs`, `worktrees/`, `modules/`, `HEAD`, ...) is not
    recorded, so a change there is neither detected nor undone.
  * It is a check made after the checks have finished, not a barrier: a hook
    planted and removed again, or any other effect that leaves the recorded
    locations as found, is not noticed, and nothing here stops the checks from
    reading the same files.
  * It does not isolate the check process. The child still runs as the
    merger's user, so it can read that user's files and the parent's
    `/proc/<pid>/environ`; process isolation needs a sandbox, which this module
    does not provide.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

#: Directories under the common dir that are recorded recursively.
_GUARDED_TREES = ("hooks", "info")
#: Single files directly under the common dir that are recorded.
_GUARDED_FILES = ("config", "config.worktree")
_MAX_NAMED_PATHS = 10
_STDERR_EXCERPT = 400


class CommonDirGuardError(Exception):
    """The common directory could not be recorded or compared."""


class CommonDirModifiedError(CommonDirGuardError):
    """The checks changed guarded common-dir state; it has been restored."""

    def __init__(self, changed: list[str], common_dir: Path, *, cause: str = "") -> None:
        self.changed = changed
        shown = ", ".join(changed[:_MAX_NAMED_PATHS])
        extra = len(changed) - _MAX_NAMED_PATHS
        if extra > 0:
            shown += f" (and {extra} more)"
        message = (
            f"the pre_checks changed the shared git directory {str(common_dir)!r}: {shown}. "
            f"The recorded state was restored"
        )
        if cause:
            message += f" (the checks had also ended with: {cause})"
        super().__init__(message)


@dataclass(frozen=True)
class _Entry:
    kind: str  # "file" | "dir" | "symlink"
    mode: int
    digest: str = ""
    data: bytes = b""


def _entry_for(path: Path) -> _Entry:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        return _Entry("symlink", 0, hashlib.sha256(target.encode("utf-8", "surrogateescape")).hexdigest(), target.encode("utf-8", "surrogateescape"))
    if stat.S_ISDIR(info.st_mode):
        return _Entry("dir", stat.S_IMODE(info.st_mode))
    if stat.S_ISREG(info.st_mode):
        data = path.read_bytes()
        return _Entry("file", stat.S_IMODE(info.st_mode), hashlib.sha256(data).hexdigest(), data)
    return _Entry("other", stat.S_IMODE(info.st_mode))


def _scan(common_dir: Path) -> dict[str, _Entry]:
    found: dict[str, _Entry] = {}
    try:
        for name in _GUARDED_FILES:
            if os.path.lexists(common_dir / name):
                found[name] = _entry_for(common_dir / name)
        for name in _GUARDED_TREES:
            root = common_dir / name
            if not os.path.lexists(root):
                continue
            found[name] = _entry_for(root)
            if found[name].kind != "dir":
                continue
            for current, dirs, files in os.walk(root, followlinks=False):
                for child in sorted(dirs) + sorted(files):
                    full = Path(current) / child
                    found[full.relative_to(common_dir).as_posix()] = _entry_for(full)
    except OSError as exc:
        raise CommonDirGuardError(
            f"the shared git directory {str(common_dir)!r} could not be read for comparison: {exc}"
        ) from exc
    return found


def _changed(before: dict[str, _Entry], after: dict[str, _Entry]) -> list[str]:
    return sorted(
        name
        for name in set(before) | set(after)
        if before.get(name) != after.get(name)
    )


def _remove(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def _restore(common_dir: Path, before: dict[str, _Entry]) -> None:
    after = _scan(common_dir)
    # Deepest first, so a directory is emptied before it is judged.
    for name in sorted(after, key=lambda n: n.count("/"), reverse=True):
        want = before.get(name)
        have = after[name]
        if want is None or want.kind != have.kind or (want.kind != "dir" and want != have):
            path = common_dir / name
            if os.path.lexists(path):
                _remove(path)
    for name in sorted(before, key=lambda n: n.count("/")):
        entry = before[name]
        path = common_dir / name
        if not os.path.lexists(path):
            if entry.kind == "dir":
                path.mkdir()
            elif entry.kind == "symlink":
                os.symlink(os.fsdecode(entry.data), path)
            elif entry.kind == "file":
                path.write_bytes(entry.data)
        if entry.kind in ("dir", "file"):
            os.chmod(path, entry.mode)
    # Directory modes last: a restored read-only directory must not block the
    # creation of its own children.
    for name in sorted(before, key=lambda n: n.count("/"), reverse=True):
        if before[name].kind == "dir":
            os.chmod(common_dir / name, before[name].mode)


def resolve_common_dir(git_tree: str | Path) -> Path:
    """The absolute git common directory of the repository at *git_tree*."""
    git_tree = Path(git_tree)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            errors="replace",
            cwd=str(git_tree),
        )
    except OSError as exc:
        raise CommonDirGuardError(f"git could not run in {str(git_tree)!r}: {exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise CommonDirGuardError(
            f"the git common directory of {str(git_tree)!r} could not be resolved "
            f"(exit {result.returncode}): {(result.stderr or result.stdout).strip()[:_STDERR_EXCERPT]}"
        )
    return (git_tree / result.stdout.strip()).resolve()


def _verify_and_restore(common_dir: Path, before: dict[str, _Entry]) -> list[str]:
    changed = _changed(before, _scan(common_dir))
    if changed:
        try:
            _restore(common_dir, before)
        except OSError as exc:
            raise CommonDirGuardError(
                f"the shared git directory {str(common_dir)!r} changed ({', '.join(changed[:_MAX_NAMED_PATHS])}) "
                f"and could not be restored: {exc}"
            ) from exc
    return changed


@contextmanager
def guard_common_dir(git_tree: str | Path) -> Iterator[Path]:
    """Record the guarded common-dir state of *git_tree*'s repository, run the
    body, then compare, restore and refuse on any difference.

    Raises:
        CommonDirModifiedError: the body changed guarded state (restored). When
            the body itself ended with an exception, that exception is chained
            and named in the message.
        CommonDirGuardError: the state could not be recorded, compared or
            restored.
    """
    common_dir = resolve_common_dir(git_tree)
    before = _scan(common_dir)
    try:
        yield common_dir
    except BaseException as body_error:
        changed = _verify_and_restore(common_dir, before)
        if changed and isinstance(body_error, Exception):
            raise CommonDirModifiedError(changed, common_dir, cause=str(body_error)[:_STDERR_EXCERPT]) from body_error
        raise
    changed = _verify_and_restore(common_dir, before)
    if changed:
        raise CommonDirModifiedError(changed, common_dir)


__all__ = [
    "CommonDirGuardError",
    "CommonDirModifiedError",
    "guard_common_dir",
    "resolve_common_dir",
]
