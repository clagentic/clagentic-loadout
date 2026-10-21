"""repo_test.provenance — what a test run ran against.

Recorded with every run: the repository (top-level path, name and remote), the
HEAD commit, whether the tree had uncommitted changes, and the base commit when
one was given. Every field is best effort and never fails the run: a repository
outside a git work tree, an unreadable remote or a git failure leaves the
affected fields null. A remote URL is recorded only with any credentials
removed; if it cannot be parsed it is recorded as null, never as the raw string.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from clagentic_loadout.sha import FULL_SHA_RE

_GIT_TIMEOUT_SECONDS = 30


def _git(root: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def strip_userinfo(url: str) -> str | None:
    """*url* without any user or password part, or None when it cannot be parsed.
    A form with no scheme (``host:path``) carries no secret field and is kept."""
    if "://" not in url:
        return url
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = f"{host}:{parts.port}" if parts.port is not None else host
    except ValueError:
        return None
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def resolve_commit(root: Path, revision: str) -> str | None:
    """The full commit SHA *revision* names in the repository at *root*, or None.
    A revision that starts with ``-`` is never passed to git."""
    if not revision or revision.startswith("-"):
        return None
    sha = _git(root, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
    return sha if sha and FULL_SHA_RE.match(sha) else None


def collect(root: Path, base_sha: str | None = None) -> dict[str, Any]:
    """The provenance record for a run in the directory *root*."""
    top = _git(root, "rev-parse", "--show-toplevel")
    repo_path = Path(top).resolve() if top else root.resolve()
    head = _git(root, "rev-parse", "HEAD") if top else None
    status = _git(root, "status", "--porcelain") if top else None
    remote = _git(root, "remote", "get-url", "origin") if top else None
    return {
        "repo": {
            "path": str(repo_path),
            "name": repo_path.name,
            "remote_url": strip_userinfo(remote) if remote else None,
            "is_git": bool(top),
        },
        "head_sha": head if head and FULL_SHA_RE.match(head) else None,
        "dirty": (status != "") if status is not None else None,
        "base_sha": base_sha,
    }


__all__ = ["collect", "resolve_commit", "strip_userinfo"]
