"""merge.tracked_file -- is a file tracked by the git repository at a given root?

Shared by the merge gate (which must not read a deployment-tier setting from a
file a pull request can change) and `loadout-doctor` (which warns about the
same condition), so both ask git the same question the same way.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from clagentic_loadout.merge.pre_check_env import without_git_location

_PROBE_TIMEOUT_SECONDS = 10.0
_NOT_TRACKED_EXIT = 1


def git_tracks_path(
    repo_root: str | Path, path: str | Path, *, timeout: float = _PROBE_TIMEOUT_SECONDS
) -> bool | None:
    """True when git tracks *path* in the work tree at *repo_root*, False when
    it does not, None when git could not say (git missing, not a repository,
    probe timed out).

    The repository is resolved from *repo_root*, never from `GIT_DIR`-style
    variables in the caller's environment."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--error-unmatch", "--", str(path)],
            shell=False,
            capture_output=True,
            timeout=timeout,
            env=without_git_location(os.environ),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode == 0:
        return True
    return False if completed.returncode == _NOT_TRACKED_EXIT else None


__all__ = ["git_tracks_path"]
