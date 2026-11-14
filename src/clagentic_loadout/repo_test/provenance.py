"""repo_test.provenance — what a test run ran against.

Recorded with every run: the repository (top-level path, name and remote), the
HEAD commit, whether the tree had uncommitted changes, and the base commit when
one was given. Every field is best effort and never fails the run: a repository
outside a git work tree, an unreadable remote or a git failure leaves the
affected fields null. The remote is recorded as parsed parts (host, owner, name);
if it cannot be parsed it is recorded as null, never as URL text.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from clagentic_loadout.git_origin import parse_origin_url
from clagentic_loadout.sha import FULL_SHA_RE

# Version 2 records the remote as parsed parts (``repo.remote``) in place of
# ``repo.remote_url`` text. A record with no ``schema`` key, or 1, is the older
# form; readers accept both and old records are never rewritten.
PROVENANCE_SCHEMA = 2
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


def remote_parts(url: str | None) -> dict[str, str] | None:
    """The host, owner and name *url* names, or None when it is not an accepted
    remote shape. Only parsed parts leave this function, never any of the URL text,
    so a credential in the userinfo, query, fragment or path cannot be recorded."""
    target = parse_origin_url(url) if url else None
    if target is None:
        return None
    return {"host": target.host, "owner": target.owner, "name": target.name}


def resolve_commit(root: Path, revision: str) -> str | None:
    """The full commit SHA *revision* names in the repository at *root*, or None.
    A revision that starts with ``-`` is never passed to git."""
    if not revision or revision.startswith("-"):
        return None
    sha = _git(root, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
    return sha if sha and FULL_SHA_RE.match(sha) else None


def toplevel(root: Path) -> str | None:
    """The resolved top-level path of the git work tree holding *root*, or None
    when *root* is not inside one."""
    top = _git(root, "rev-parse", "--show-toplevel")
    return str(Path(top).resolve()) if top else None


def collect(root: Path, base_sha: str | None = None) -> dict[str, Any]:
    """The provenance record for a run in the directory *root*."""
    top = _git(root, "rev-parse", "--show-toplevel")
    repo_path = Path(top).resolve() if top else root.resolve()
    head = _git(root, "rev-parse", "HEAD") if top else None
    status = _git(root, "status", "--porcelain") if top else None
    remote = _git(root, "remote", "get-url", "origin") if top else None
    return {
        "schema": PROVENANCE_SCHEMA,
        "repo": {
            "path": str(repo_path),
            "name": repo_path.name,
            "remote": remote_parts(remote),
            "is_git": bool(top),
        },
        "head_sha": head if head and FULL_SHA_RE.match(head) else None,
        "dirty": (status != "") if status is not None else None,
        "base_sha": base_sha,
    }


__all__ = ["PROVENANCE_SCHEMA", "collect", "remote_parts", "resolve_commit", "toplevel"]
