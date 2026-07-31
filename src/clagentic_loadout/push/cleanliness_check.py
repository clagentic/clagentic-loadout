"""push.cleanliness_check — pre-push scratch-litter detection (lr-d7a8).

The push verb never runs `git add` (safe by design — see verb.py's module
docstring), but that also means it never notices when the working tree
carries unignored, untracked scratch litter (a stray PR-body staging file,
an ad-hoc HANDOFF.md, etc.) — a bug class this module exists to WARN on
before a push, without ever staging or removing anything itself.

TOOL-ALTITUDE (task lr-d7a8, explicit): this check is owned by the push VERB
itself, not a guard hook and not a merge gate. It never touches the working
tree, never mutates git state, and has no orchestration coupling — it only
reads `git ls-files --others --exclude-standard` (untracked-and-unignored,
gitignore-respecting) and matches basenames against a configurable pattern
list (push.cleanliness_config).

Detection semantics: `git ls-files --others --exclude-standard` already
gives exactly "untracked AND not excluded by .gitignore" — a gitignored
scratch file is never listed here, matching the task's explicit
gitignore-respecting requirement without needing to separately parse
`git status --porcelain` output.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from clagentic_loadout.push.cleanliness_config import match_scratch_pattern


class CleanlinessCheckError(Exception):
    """Raised when `git ls-files` itself fails (not a git repo, git not on
    PATH, etc.) — a check-execution failure, distinct from finding matches."""


class ScratchLitterFoundError(Exception):
    """Raised (--strict only) when one or more untracked-and-unignored files
    match a configured scratch pattern. Carries the matched files for the
    caller to report."""

    def __init__(self, matches: list[tuple[str, str]]) -> None:
        # matches: list of (file_path, matched_pattern)
        self.matches = matches
        files = ", ".join(f"{path!r} (pattern {pattern!r})" for path, pattern in matches)
        super().__init__(f"untracked scratch litter matched: {files}")


def find_scratch_matches(
    repo_root: str | Path,
    patterns: tuple[str, ...],
) -> list[tuple[str, str]]:
    """Return [(file_path, matched_pattern), ...] for every untracked,
    unignored file under *repo_root* whose basename matches one of
    *patterns*.

    Uses `git ls-files --others --exclude-standard` — untracked files only,
    with .gitignore (and other standard git exclude sources) already
    applied, so a gitignored scratch file is never returned here.

    Raises:
        CleanlinessCheckError: the underlying git command fails (not a git
            repo, git missing, etc.).
    """
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CleanlinessCheckError(
            f"git ls-files --others --exclude-standard failed (exit "
            f"{result.returncode}): {result.stderr.strip()}"
        )

    matches: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        file_path = line.strip()
        if not file_path:
            continue
        pattern = match_scratch_pattern(file_path, patterns)
        if pattern is not None:
            matches.append((file_path, pattern))
    return matches


def check_cleanliness(
    repo_root: str | Path,
    patterns: tuple[str, ...],
    *,
    strict: bool,
) -> list[tuple[str, str]]:
    """Run the pre-push cleanliness check.

    Always returns the list of (file_path, matched_pattern) matches found
    (possibly empty) so the caller can print a WARN-level message
    regardless of `strict`.

    Raises:
        CleanlinessCheckError: the underlying git command fails.
        ScratchLitterFoundError: `strict=True` and at least one match was
            found — carries the same matches list as `.matches`.
    """
    matches = find_scratch_matches(repo_root, patterns)
    if matches and strict:
        raise ScratchLitterFoundError(matches)
    return matches


__all__ = [
    "CleanlinessCheckError",
    "ScratchLitterFoundError",
    "check_cleanliness",
    "find_scratch_matches",
]
