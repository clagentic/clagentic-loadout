"""Synthetic fixtures for the roles, brief and rules tests: a groups file with
invented repository, group and rule names, a repo whose profile is committed,
and a user-level config root. No name here belongs to any real deployment."""

from __future__ import annotations

from pathlib import Path

from clagentic_loadout.review.policy_source import PROFILE_PATH
from tests._gate_repo import git
from tests._review_evaluate_support import commit_files, make_repo

GROUPS_TEXT = """\
schema_version: 1
classes:
  released-tool:
    repos: [some-repo, other-repo]
    source: registry-one
  future-class:
    repos: [future-repo]
    note: not yet in the profile schema
groups:
  - id: shipped-tools
    description: Rules for shipped tools
    jurisdiction_class: released-tool
    rules:
      - {id: shipped-1, statement: Ship no private notes., source: doc-a}
  - id: some-group
    description: Named repositories
    jurisdiction: ["some-*", "other-repo"]
    rules:
      - {id: group-1, statement: Keep a changelog., source: doc-b}
      - {id: group-2, statement: Pin the toolchain., source: doc-c}
  - id: noted
    jurisdiction: [some-repo]
    rules: []
    note: Rules are pending.
  - id: future
    jurisdiction_class: future-class
    rules:
      - {id: future-1, statement: A future rule., source: doc-d}
"""

PROFILE_BUILD = """\
class: released-tool
paths:
  ".state/**": {class: internal}
deny:
  names: [invented-name]
  hosts: [host.example.test]
tool_packages: [tool_one]
test:
  full: [python3, -m, pytest, "--junitxml={junit}"]
"""


def write_groups(tmp_path: Path, text: str = GROUPS_TEXT, name: str = "groups.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def profile_repo(tmp_path: Path, profile_text: str | None = PROFILE_BUILD, *, name: str = "some-repo") -> Path:
    """A git repo whose origin names *name*, with *profile_text* committed as its
    profile (none when None)."""
    repo = make_repo(tmp_path)
    commit_files(repo, {"README.md": "x\n", **({PROFILE_PATH: profile_text} if profile_text else {})})
    git(repo, "remote", "add", "origin", f"https://git-host.example.test/some-owner/{name}.git")
    return repo


def write_user_config(root: Path, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(text, encoding="utf-8")
    return root
