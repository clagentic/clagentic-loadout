"""Real git repositories for the merge-gate tests.

The merge gate reads its declaration from the PR's BASE commit and runs
pre_checks against the merge result, so its tests need real commits: a base
holding (or lacking) the tracked gate file, a PR head built on it, and a
working tree whose state can be made to disagree with both."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from clagentic_loadout.repo_config import DEFAULT_CONFIG_RELATIVE_PATH, TRACKED_GATE_RELATIVE_PATH

_IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *_IDENTITY, *args], cwd=str(repo), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def commit_files(repo: Path, files: dict[str, str], message: str = "change") -> str:
    for relative, text in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        git(repo, "add", "--", relative)
    git(repo, "commit", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def write_deployment_config(repo: Path, merge_section: dict) -> None:
    path = repo / DEFAULT_CONFIG_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"merge": merge_section}), encoding="utf-8")


@dataclass
class GateRepo:
    """A repo whose base commit is `base_sha`; `head_sha` is the PR head."""

    path: Path
    base_sha: str
    head_sha: str

    def pr_info(self, *, title: str = "feat: a change") -> dict:
        return {
            "head": {"sha": self.head_sha},
            "title": title,
            "base": {"ref": "main", "sha": self.base_sha},
        }


def init_gate_repo(
    path: Path,
    *,
    tracked_gate: dict | None,
    head_files: dict[str, str] | None = None,
    deployment_merge: dict | None = None,
) -> GateRepo:
    """Create a repo at *path*: a base commit carrying *tracked_gate* (a `merge:`
    section; None commits no gate file), then a PR head one commit ahead.

    The working tree is left on the BASE commit. The gitignored deployment
    file carries only machine-local keys (sync off, so no real remote is
    needed)."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    base_files = {"README.md": "base\n", ".gitignore": f"{DEFAULT_CONFIG_RELATIVE_PATH}\n"}
    if tracked_gate is not None:
        base_files[TRACKED_GATE_RELATIVE_PATH] = yaml.safe_dump({"merge": tracked_gate})
    base_sha = commit_files(path, base_files, "base")
    git(path, "checkout", "-q", "-b", "pr")
    head_sha = commit_files(path, head_files or {"change.txt": "pr\n"}, "pr change")
    git(path, "checkout", "-q", "main")
    write_deployment_config(path, {"sync_tree_after_merge": False, **(deployment_merge or {})})
    return GateRepo(path=path, base_sha=base_sha, head_sha=head_sha)
