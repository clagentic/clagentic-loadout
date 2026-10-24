"""review.policy_reader — where the evaluator's policy files are read from.

Policy is read at the revision a pull request merges into, from one of two
sources behind the same two-method seam:

  HostPolicyReader  the host API's tree and contents endpoints, with the same
                    reader credential the acquisition uses. No local checkout
                    is needed, which is what a reviewer run needs: its working
                    directory is a per-spawn scratch directory, not a clone.
  GitPolicyReader   ``git show`` / ``git ls-tree`` in a local repository, used
                    only when the caller names one explicitly.

Both raise PolicyReadError for anything that is not a clean read, with a message
naming what was being read; the caller turns that into a DEGRADED verdict. An
absent file is not an error here: callers learn which files exist from
``list_paths`` and read only those.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Protocol

from clagentic_loadout.acquire.contract import PolicyFileBackend
from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.merge.pre_check_env import without_git_location

MAX_POLICY_BYTES = 512 * 1024
_COMMIT_SHA_RE = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_GIT_TIMEOUT_SECONDS = 30


class PolicyReadError(Exception):
    """A policy read failed; the message names what was being read."""


class PolicyReader(Protocol):
    def list_paths(self, base: str) -> list[str]:
        """Every file path in the tree at *base*."""
        ...

    def read_text(self, base: str, path: str) -> str:
        """The UTF-8 text of *path* at *base*, bounded in size."""
        ...


def decode_policy_bytes(raw: bytes, path: str, base: str) -> str:
    if len(raw) > MAX_POLICY_BYTES:
        raise PolicyReadError(f"{path} at {base[:12]} is larger than {MAX_POLICY_BYTES} bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyReadError(f"{path} at {base[:12]} is not valid UTF-8: {exc}") from exc


class GitPolicyReader:
    """Reads from a local repository that holds the base revision."""

    def __init__(self, repo_path: str | Path) -> None:
        self._repo_path = Path(repo_path)

    def _git(self, args: list[str], what: str) -> bytes:
        try:
            completed = subprocess.run(
                ["git", "-C", str(self._repo_path), *args],
                shell=False,
                capture_output=True,
                timeout=_GIT_TIMEOUT_SECONDS,
                env=without_git_location(os.environ),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PolicyReadError(f"cannot read {what}: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
            raise PolicyReadError(f"cannot read {what}: {detail[-1] if detail else 'git failed'}")
        return completed.stdout

    def resolve_commit(self, ref: str) -> str:
        """The full commit SHA *ref* names right now. A caller that reads more
        than once at a mutable ref resolves it here first and reads at the SHA,
        so every read sees the same commit."""
        out = self._git(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}"],
            f"the commit {ref[:40]!r} names",
        )
        sha = out.decode("ascii", "replace").strip()
        if not _COMMIT_SHA_RE.match(sha):
            raise PolicyReadError(f"cannot read the commit {ref[:40]!r} names: git printed {sha[:20]!r}")
        return sha

    def list_paths(self, base: str) -> list[str]:
        out = self._git(
            ["ls-tree", "-r", "-z", "--name-only", base], f"the file list at {base[:12]}"
        )
        return [p for p in out.decode("utf-8", "replace").split("\0") if p]

    def read_text(self, base: str, path: str) -> str:
        raw = self._git(["show", f"{base}:{path}"], f"{path} at {base[:12]}")
        return decode_policy_bytes(raw, path, base)


class HostPolicyReader:
    """Reads from the host API through an acquisition backend, which already
    holds the reader credential."""

    def __init__(self, backend: PolicyFileBackend, *, owner: str, repo: str) -> None:
        self._backend = backend
        self._owner = owner
        self._repo = repo

    def list_paths(self, base: str) -> list[str]:
        try:
            return self._backend.fetch_tree_paths(owner=self._owner, repo=self._repo, ref=base)
        except AcquireFetchError as exc:
            raise PolicyReadError(f"cannot read the file list at {base[:12]}: {exc}") from exc

    def read_text(self, base: str, path: str) -> str:
        try:
            raw = self._backend.fetch_file_bytes(
                owner=self._owner, repo=self._repo, path=path, ref=base
            )
        except AcquireFetchError as exc:
            raise PolicyReadError(f"cannot read {path} at {base[:12]}: {exc}") from exc
        return decode_policy_bytes(raw, path, base)


__all__ = [
    "MAX_POLICY_BYTES",
    "GitPolicyReader",
    "HostPolicyReader",
    "PolicyReadError",
    "PolicyReader",
    "decode_policy_bytes",
]
