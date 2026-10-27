"""Shared helpers for the `clagentic-loadout test` suites: a throwaway repository
with a profile whose test commands run a small JUnit-writing stand-in runner, and
a direct writer for finished run directories."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

from clagentic_loadout.repo_test import store

FAKE_RUNNER = '''\
"""Stand-in test runner: writes a JUnit report to argv[1]. Every further argument
is a test file; a file whose name contains "bad" fails. A `--base=<value>` argument
is an option, never a file: its value is recorded as a passing `suite.base` test so
a test can read what the command received. With no files, the `fail.txt` marker in
the working directory makes one test fail."""
import sys

junit, args = sys.argv[1], sys.argv[2:]
bases = [a[len("--base="):] for a in args if a.startswith("--base=")]
files = [a for a in args if not a.startswith("--base=")]
cases = [("suite.alpha", "test_one", "pass"), ("suite.alpha", "test_two", "pass")]
cases.append(("suite.alpha", "test_skipped", "skip"))
cases.extend(("suite.base", value, "pass") for value in bases)
for name in files:
    cases.append(("suite.files", name, "fail" if "bad" in name else "pass"))
import os
if not files and os.path.exists("fail.txt"):
    cases.append(("suite.alpha", "test_broken", "fail"))
body = []
for classname, name, outcome in cases:
    inner = {"pass": "", "skip": "<skipped/>", "fail": "<failure message='x'/>"}[outcome]
    body.append(f"<testcase classname='{classname}' name='{name}'>{inner}</testcase>")
with open(junit, "w") as handle:
    handle.write("<testsuites><testsuite>" + "".join(body) + "</testsuite></testsuites>")
sys.exit(1 if any(c[2] == "fail" for c in cases) else 0)
'''

# Fixed so the seed commit, and the base SHA derived from it, is the same on every run.
_SEED_DATE = "2026-01-01T00:00:00+00:00"

_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def git(repo: Path, *args: str) -> str:
    done = subprocess.run([*_GIT, "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def profile_text(**test_keys: list[str] | str) -> str:
    runner = [sys.executable, "runner.py", "{junit}"]
    declared: dict[str, object] = {
        "full": runner,
        "impacted": [*runner, "--base={base}"],
        "files": runner,
    }
    declared.update(test_keys)
    declared = {key: value for key, value in declared.items() if value is not None}
    return "class: internal\ntest: " + json.dumps(declared) + "\n"


def make_repo(root: Path, *, profile: str | None = None, git_init: bool = True) -> Path:
    """A repository at *root* with the stand-in runner and a profile."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "runner.py").write_text(FAKE_RUNNER)
    (root / "test_good.py").write_text("")
    (root / "test_bad.py").write_text("")
    profile_path = root / ".clagentic" / "loadout" / "profile.yaml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(profile if profile is not None else profile_text())
    if git_init:
        git(root, "init", "-q", "-b", "main")
        git(root, "add", "runner.py", "test_good.py", "test_bad.py", ".clagentic/loadout/profile.yaml")
        subprocess.run(
            [*_GIT, "-C", str(root), "commit", "-q", "-m", "chore: seed"],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "GIT_AUTHOR_DATE": _SEED_DATE, "GIT_COMMITTER_DATE": _SEED_DATE},
        )
    return root


def write_run(
    reports: Path,
    run_id: str,
    *,
    failing: list[str] | None = None,
    state: str = store.STATE_COMPLETE,
    head_sha: str | None = None,
    dirty: bool | None = False,
    repo_path: str | None = "/repos/alpha",
    remote_url: str | None = None,
    finished: bool = True,
    mode: str = "full",
    tier: str | None = None,
    id_scheme: int | None = None,
    failed: int | None = None,
    junit_body: str | None = None,
) -> Path:
    """A finished run directory written directly, for the read side's tests."""
    run_dir = reports / run_id
    run_dir.mkdir(parents=True)
    repo = None
    if repo_path is not None:
        repo = {"path": repo_path, "name": Path(repo_path).name, "remote_url": remote_url, "is_git": True}
    provenance = {"repo": repo, "head_sha": head_sha, "dirty": dirty, "base_sha": None}
    (run_dir / store.PROVENANCE_FILE).write_text(json.dumps(provenance))
    if finished:
        ids = failing or []
        result = {
            "schema": 1,
            "run_id": run_id,
            "mode": mode,
            "state": state,
            "problem": None if state == store.STATE_COMPLETE else "no report",
            "command": ["runner"],
            "command_exit_code": 1 if ids else 0,
            "counts": {
                "collected": 3,
                "passed": 3 - len(ids),
                "failed": len(ids) if failed is None else failed,
                "skipped": 0,
            },
            "failing_ids": ids,
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:01+00:00",
        }
        if id_scheme is not None:
            # A record written before ids were made distinct has no such key.
            result["id_scheme"] = id_scheme
        if tier is not None:
            # A record written before tiers existed has no tier key at all.
            result["tier"] = tier
        (run_dir / store.RESULT_FILE).write_text(json.dumps(result))
    if junit_body is not None:
        (run_dir / store.JUNIT_FILE).write_text(f"<testsuites><testsuite>{junit_body}</testsuite></testsuites>")
    return run_dir


def recorded_bases(run_dir: Path) -> list[str]:
    """The base values the stand-in runner received, read from a run's junit report."""
    root = ElementTree.parse(run_dir / store.JUNIT_FILE).getroot()
    return [case.get("name", "") for case in root.iter("testcase") if case.get("classname") == "suite.base"]


def tree_snapshot(root: Path) -> dict[str, tuple[int, int, bytes]]:
    """Every path under *root* with its mtime, mode and content, to prove a
    command changed nothing."""
    snapshot: dict[str, tuple[int, int, bytes]] = {}
    for path in sorted(root.rglob("*")):
        stat = path.lstat()
        content = path.read_bytes() if path.is_file() else b""
        snapshot[str(path.relative_to(root))] = (stat.st_mtime_ns, stat.st_mode, content)
    return snapshot
