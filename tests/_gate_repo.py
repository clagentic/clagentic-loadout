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


def commit_raw_gate_to_base(repo: "GateRepo", data: bytes) -> None:
    """Replace the base commit's tracked gate file with raw *data* (for content
    that is not valid text) and point `repo.base_sha` at the new commit."""
    target = repo.path / TRACKED_GATE_RELATIVE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    git(repo.path, "add", "--", TRACKED_GATE_RELATIVE_PATH)
    git(repo.path, "commit", "-m", "raw gate")
    repo.base_sha = git(repo.path, "rev-parse", "HEAD")


def seed_base_commit(path: Path) -> str:
    """Make *path* a git repo with one commit on `main` (when it is not one
    already) and return the commit SHA, for tests whose PR payload must carry a
    real `base.sha` but that exercise nothing in the tree itself."""
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        git(path, "init", "-q", "-b", "main")
        git(path, "commit", "-q", "--allow-empty", "-m", "base")
    return git(path, "rev-parse", "HEAD")


#: An origin the merge verbs confirm under their default Forgejo host.
CONFIRMABLE_ORIGIN = "http://127.0.0.1:3000/some-owner/some-repo.git"
#: The same for `--platform github`.
CONFIRMABLE_GITHUB_ORIGIN = "git@github.com:some-owner/some-repo.git"


def make_origin_confirmable(tree: Path, *, url: str = CONFIRMABLE_ORIGIN, branch: str = "main") -> None:
    """Give *tree* an `origin` the merge verbs can bind to (an https or ssh remote
    on the configured host) while real fetches keep working.

    A fixture's `origin` is often a local bare repository, which names no host.
    That remote is renamed `mirror` and made the base branch's tracking remote,
    which is the one the post-merge tree sync fetches from, and `origin` becomes
    *url*. Called again, it only replaces the URL."""
    remotes = git(tree, "remote").split()
    if "origin" in remotes and "mirror" not in remotes:
        git(tree, "remote", "rename", "origin", "mirror")
        git(tree, "config", f"branch.{branch}.remote", "mirror")
        git(tree, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")
        remotes = git(tree, "remote").split()
    if "origin" in remotes:
        git(tree, "remote", "set-url", "origin", url)
    else:
        git(tree, "remote", "add", "origin", url)


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
