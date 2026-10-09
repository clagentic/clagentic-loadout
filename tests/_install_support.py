"""Shared fixtures-as-functions for tests that run scripts/install.sh against a
copy of this checkout.

Installing straight from the live checkout makes a test depend on the checkout's
git state: install.sh gives a dirty tree a fresh nonce release id, so any
untracked or modified file there defeats release reuse. Tests that assert on
reuse install from a committed copy built here instead."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parent.parent

_COPY_IGNORE = shutil.ignore_patterns(
    ".git", "tests", "docs", "__pycache__", "*.egg-info", "build", "dist", ".pytest_cache"
)


def source_copy(tmp_path: Path, name: str = "src-copy") -> Path:
    """A copy of this checkout with no .git, so each install gets a fresh
    timestamp-pid release id."""
    dst = tmp_path / name
    shutil.copytree(CHECKOUT, dst, ignore=_COPY_IGNORE)
    return dst


def git_source(tmp_path: Path, name: str) -> Path:
    """A source copy committed as a clean git work tree, so release ids are
    sha-based and independent of the live checkout's state."""
    src = source_copy(tmp_path, name)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run(["git", "init", "-q"], cwd=src, check=True)
    shutil.copy(CHECKOUT / ".gitignore", src / ".gitignore")
    subprocess.run(["git", "add", "-A"], cwd=src, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=src, check=True)
    return src
