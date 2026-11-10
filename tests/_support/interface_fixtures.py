"""Synthetic fixtures for the interface tests: an interface file at two shapes, a
consumer map with invented repository names, and a git repository that commits
them. No name here belongs to any real deployment."""

from __future__ import annotations

from pathlib import Path

from clagentic_loadout.interface.model import INTERFACE_PATH
from tests._gate_repo import commit_files, git
from tests._review_evaluate_support import make_repo

BASE_INTERFACE = """\
schema_version: 1
verbs:
  - name: tool-one
    subcommands: [run]
    exit_codes: {ok: 0, usage: 1, failed: 5}
  - name: tool-two
schemas: [thing.schema.json]
env: [TOOL_HOME]
"""

#: tool-one renamed to tool-uno: a removal of the old name and an addition.
RENAMED_INTERFACE = BASE_INTERFACE.replace("name: tool-one", "name: tool-uno")

#: Additions only: a verb, an exit code, a schema and an env name.
ADDED_INTERFACE = BASE_INTERFACE.replace(
    "env: [TOOL_HOME]", "env: [TOOL_HOME, TOOL_NEW]"
).replace("exit_codes: {ok: 0, usage: 1, failed: 5}", "exit_codes: {ok: 0, usage: 1, failed: 5, late: 9}")

CONSUMERS_TEXT = """\
schema_version: 1
producers:
  some-repo:
    - element: tool-one
      kind: verb
      consumers:
        - {repo: app-one, confirmed_at: "2026-10-01", source: "src/a.py"}
        - {repo: app-two, confirmed_at: "2026-09-01", source: "src/b.py"}
    - element: tool-one run --fast
      kind: flag
      consumers:
        - {repo: app-one, confirmed_at: "2026-10-02", source: "src/a.py"}
    - element: config/settings.yaml (keys)
      kind: file
      consumers:
        - {repo: app-three, confirmed_at: "2026-08-01", source: "cfg"}
  other-producer:
    - element: tool-one
      kind: verb
      consumers:
        - {repo: app-nine, confirmed_at: "2026-10-01", source: "y"}
unconfirmed:
  - {producer: some-repo, element: tool-two, consumers: [app-four], seed_source: s, reason: r}
"""


def write_consumers(tmp_path: Path, text: str = CONSUMERS_TEXT, name: str = "consumers.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def interface_repo(tmp_path: Path, base: str | None, head: str | None) -> tuple[Path, str, str]:
    """A git repo with one commit holding *base* as the interface file (none when
    None) and a second holding *head*; returns the repo and both commit SHAs.
    A None *head* removes the file in the second commit."""
    repo = make_repo(tmp_path)
    first_files = {"README.md": "x\n"}
    if base is not None:
        first_files[INTERFACE_PATH] = base
    base_sha = commit_files(repo, first_files, "chore: base")
    if head is None:
        if base is not None:
            git(repo, "rm", "-q", "--", INTERFACE_PATH)
            git(repo, "commit", "-q", "-m", "chore: drop interface")
        else:
            commit_files(repo, {"other.txt": "y\n"}, "chore: other")
    elif head == base:
        git(repo, "commit", "--allow-empty", "-q", "-m", "chore: head")
    else:
        commit_files(repo, {INTERFACE_PATH: head}, "chore: head")
    return repo, base_sha, git(repo, "rev-parse", "HEAD")
