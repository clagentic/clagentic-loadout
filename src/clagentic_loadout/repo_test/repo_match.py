"""repo_test.repo_match — whether a recorded repository is the one a caller names.

Shared by ``test run --expect-repo`` and ``test report --repo`` so both sides
apply one rule: a name matches a recorded path, name, ``owner/name`` or ``host/owner/name`` of the
remote, or a remote URL that parses to the same host, owner and name; a filesystem
path matches when it resolves to the recorded path. A record written before the
remote was recorded as parsed parts holds ``remote_url`` text and still matches.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clagentic_loadout.git_origin import parse_origin_url

SHA_ARG_RE = re.compile(r"\A[0-9a-fA-F]{7,40}\Z")


def recorded_remote(repo: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """The (host, owner, name) a recorded repository's remote names, or None.

    Reads the parsed ``remote`` object of a current record; a record written before
    that field existed carries ``remote_url`` text, which is parsed here so both
    forms yield one identity."""
    remote = repo.get("remote")
    if isinstance(remote, Mapping):
        parts = (remote.get("host"), remote.get("owner"), remote.get("name"))
        if all(isinstance(part, str) and part for part in parts):
            return parts  # type: ignore[return-value]
        return None
    legacy = repo.get("remote_url")
    if isinstance(legacy, str):
        target = parse_origin_url(legacy)
        if target is not None:
            return (target.host, target.owner, target.name)
    return None


def legacy_remote_text(repo: Mapping[str, Any]) -> str | None:
    """The ``remote_url`` text of a record written before remotes were recorded as
    parsed parts, or None."""
    legacy = repo.get("remote_url")
    return legacy if isinstance(legacy, str) and legacy else None


def repo_matches(repo: Mapping[str, Any], wanted: str) -> bool:
    """True when *wanted* names the recorded repository *repo*."""
    identities: set[Any] = {repo.get("path"), repo.get("name")}
    legacy = legacy_remote_text(repo)
    if legacy is not None:
        identities.add(legacy)
        identities.add(legacy.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1])
    parts = recorded_remote(repo)
    if parts is not None:
        host, owner, name = parts
        identities.update({name, f"{owner}/{name}", f"{host}/{owner}/{name}"})
        target = parse_origin_url(wanted)
        if target is not None and (target.host, target.owner, target.name) == parts:
            return True
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


__all__ = ["SHA_ARG_RE", "legacy_remote_text", "recorded_remote", "repo_label", "repo_matches"]
