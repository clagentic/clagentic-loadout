"""spec.repo_roots -- the repository a specs root must stay out of.

A sealed spec is read back as the record of what was agreed, so a specs root
inside the repository under review is refused: the change under review could
write its own specification. Every verb that reads the store resolves that
repository here, so they refuse the same location.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_GIT_TIMEOUT_SECONDS = 10


def git_output(repo_path: str, *git_args: str) -> str | None:
    """Stripped standard output of ``git -C repo_path ...``, or None when git
    fails or cannot run."""
    try:
        done = subprocess.run(
            ["git", "-C", repo_path, *git_args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def untrusted_roots(repo_path: str | None) -> tuple[Path, ...]:
    """The repository the specs root must stay out of: the top level of the
    repository at *repo_path*, when it is one."""
    if repo_path is None:
        return ()
    toplevel = git_output(repo_path, "rev-parse", "--show-toplevel")
    return (Path(toplevel),) if toplevel else ()


__all__ = ["git_output", "untrusted_roots"]
