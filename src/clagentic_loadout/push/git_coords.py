"""push.git_coords — git branch/remote resolution + Forgejo URL parsing.

Wave B slice 3 (lr-09ca, tome #688). Ported from the reference push
transport's git-coordinate helpers (current_branch / tracking_remote /
remote_url / parse_forgejo_coords, lr-e247). No operator host is baked in
anywhere here — every example in
this module's docstrings uses a neutral placeholder host
(git-host.example.com), and the real host is always read from the actual git
remote at runtime.
"""

from __future__ import annotations

import subprocess
import urllib.parse
from pathlib import Path

from clagentic_loadout.push.errors import RemoteResolutionError

#: Branches a push must never target directly.
PROTECTED_BRANCHES = frozenset({"HEAD", "main", "master"})


def _run(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd is not None else None,
    )


def current_branch(git_cwd: Path | None = None) -> str:
    """Return the current git branch name."""
    r = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=git_cwd)
    return r.stdout.strip()


def tracking_remote(branch: str, git_cwd: Path | None = None) -> str:
    """Return the remote name for *branch*'s tracking ref, defaulting to
    'origin' when no tracking remote is configured."""
    r = _run(["git", "config", f"branch.{branch}.remote"], cwd=git_cwd)
    name = r.stdout.strip()
    return name if name else "origin"


def remote_url(remote: str, git_cwd: Path | None = None) -> str:
    """Return the raw remote URL for *remote*.

    Raises RemoteResolutionError if the remote is unset or git fails.
    """
    r = _run(["git", "remote", "get-url", remote], cwd=git_cwd)
    if r.returncode != 0 or not r.stdout.strip():
        raise RemoteResolutionError(f"cannot get URL for remote {remote!r}")
    return r.stdout.strip()


def read_remote_url_best_effort(git_cwd: Path | None) -> str:
    """Best-effort read of the git remote URL, for platform auto-detection
    only. Never raises — returns "" when cwd is not a git repo, has no
    tracking remote, or any git command fails (the isolated-spawn case where
    the caller must pass an explicit platform)."""
    try:
        branch = current_branch(git_cwd)
        if not branch:
            return ""
    except Exception:
        return ""
    try:
        remote_name = tracking_remote(branch, git_cwd)
        return remote_url(remote_name, git_cwd)
    except Exception:
        return ""


def parse_forgejo_coords(url: str) -> tuple[str, str, str]:
    """Parse a Forgejo HTTP(S) remote URL into (api_base, owner, repo_name).

    Accepts:
        http://git-host.example.com:3000/some-owner/some-repo.git
        https://git-host.example.com/some-owner/some-repo
        http://x-access-token:<token>@git-host.example.com:3000/some-owner/some-repo.git

    The returned api_base NEVER contains userinfo (user:pass@) — embedded
    credentials are stripped before constructing api_base. Auth for API
    calls comes from the credential-provider seam (Authorization header),
    never from the remote URL — dropping userinfo here does not affect auth.

    Raises RemoteResolutionError on any parse failure.
    """
    clean = url.rstrip("/")
    if clean.endswith(".git"):
        clean = clean[:-4]

    parsed = urllib.parse.urlsplit(clean)
    if parsed.scheme not in ("http", "https"):
        raise RemoteResolutionError(
            "remote URL does not match expected Forgejo shape: "
            "<scheme>://<host>/<owner>/<repo>[.git]"
        )

    host = parsed.hostname
    if not host:
        raise RemoteResolutionError(
            "remote URL does not match expected Forgejo shape: "
            "<scheme>://<host>/<owner>/<repo>[.git]"
        )
    netloc_clean = host if parsed.port is None else f"{host}:{parsed.port}"
    api_base = f"{parsed.scheme}://{netloc_clean}"

    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) != 2:
        raise RemoteResolutionError(
            "remote URL does not match expected Forgejo shape: "
            "<scheme>://<host>/<owner>/<repo>[.git]"
        )

    owner, repo_name = path_parts[0], path_parts[1]
    return api_base, owner, repo_name


def parse_owner_repo(owner_repo: str) -> tuple[str, str]:
    """Split an explicit 'owner/repo' string. Raises RemoteResolutionError
    on malformed input."""
    parts = owner_repo.strip().split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise RemoteResolutionError(f"owner_repo must be 'owner/repo', got: {owner_repo!r}")
    return parts[0], parts[1]


__all__ = [
    "PROTECTED_BRANCHES",
    "current_branch",
    "parse_forgejo_coords",
    "parse_owner_repo",
    "read_remote_url_best_effort",
    "remote_url",
    "tracking_remote",
]
