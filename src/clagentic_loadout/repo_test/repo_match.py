"""repo_test.repo_match — whether a recorded repository is the one a caller names.

Shared by ``test run --expect-repo`` and ``test report --repo`` so both sides
apply one rule: a name matches a recorded path, name, remote URL, or the last
segment of the remote URL (without ``.git``); a filesystem path matches when it
resolves to the recorded path.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SHA_ARG_RE = re.compile(r"\A[0-9a-fA-F]{7,40}\Z")


def repo_matches(repo: Mapping[str, Any], wanted: str) -> bool:
    """True when *wanted* names the recorded repository *repo*."""
    identities = {repo.get("path"), repo.get("name"), repo.get("remote_url")}
    remote = repo.get("remote_url")
    if isinstance(remote, str):
        identities.add(remote.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1])
    if wanted in identities:
        return True
    try:
        return str(Path(wanted).resolve()) == repo.get("path")
    except OSError:
        return False


def repo_label(repo: Mapping[str, Any] | None) -> str:
    """A short identity of a recorded repository for messages: name and path."""
    if not repo:
        return "unknown repository"
    name, path = repo.get("name"), repo.get("path")
    if name and path:
        return f"{name} ({path})"
    return str(name or path or "unknown repository")


__all__ = ["SHA_ARG_RE", "repo_label", "repo_matches"]
