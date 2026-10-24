"""Shared builders for the review evaluator tests: findings with facts, a
throwaway git repository with a base commit, and a no-policy Stakes."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from pathlib import Path

from clagentic_loadout.review import dispositions as disp
from clagentic_loadout.review import stakes as stk

HEAD = "b" * 40
OTHER_HEAD = "c" * 40
TODAY = dt.date(2026, 10, 1)


def facts(
    *,
    category: str = "correctness",
    reachable: str = "yes",
    precondition: str = "none",
    confidentiality: str = "none",
    integrity: str = "none",
    availability: str = "none",
    durability: str = "durable",
    **extra,
) -> dict:
    return {
        "category": category,
        "reachable": reachable,
        "attacker_precondition": precondition,
        "impact": {
            "confidentiality": confidentiality,
            "integrity": integrity,
            "availability": availability,
        },
        "durability": durability,
        **extra,
    }


def finding(
    file: str = "src/a.py",
    line: int = 10,
    rule_id: str = "R1",
    message: str = "something is wrong",
    fact: dict | None = None,
    **extra,
) -> dict:
    item = {"file": file, "line": line, "rule_id": rule_id, "message": message, "severity": None}
    if fact is not None:
        item["facts"] = fact
    item.update(extra)
    return item


def internal_stakes(data_class: str = "internal", merge_control: str = stk.CONTROLLED) -> stk.Stakes:
    """Stakes that state everything: internal, no worst-case defaults."""
    return stk.Stakes(
        profile=stk.StakesProfile(
            name="p", exposure=stk.INTERNAL, data_class=data_class, merge_control=merge_control
        )
    )


def exposed_stakes(data_class: str = "internal") -> stk.Stakes:
    return stk.Stakes(
        profile=stk.StakesProfile(
            name="p", exposure=stk.EXPOSED, data_class=data_class, merge_control=stk.CONTROLLED
        )
    )


def disposition(**overrides) -> disp.Disposition:
    base = {
        "path": "src/**",
        "rule_id": "R1",
        "kind": disp.FALSE_POSITIVE,
        "rationale": "checked by hand",
        "by": "a-maintainer",
        "at": dt.date(2026, 9, 20),
    }
    base.update(overrides)
    return disp.Disposition(**base)


def run_git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        # Fixed so two fixtures built the same way get the same commit SHA.
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env
    )
    return done.stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    return repo


def utc_today_text() -> str:
    """Today's UTC date as ISO text: a confirmation that is always fresh for a
    verb that reads the real clock."""
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def posture_profile(drop: tuple[str, ...] = (), **overrides: str) -> str:
    """The text of a profile whose posture claims an internal, reviewed,
    private repository confirmed today. *overrides* replace a key's value and
    *drop* removes keys."""
    values = {
        "exposure": "internal_authenticated",
        "data": "internal",
        "visibility": "private",
        "merge_control": "review_required",
        "confirmed_at": utc_today_text(),
    }
    values.update(overrides)
    lines = [f"{key}: {value}" for key, value in values.items() if key not in drop]
    return "class: internal\n" + "\n".join(lines) + "\n"


def commit_files(repo: Path, files: dict[str, str]) -> str:
    """Write *files* (path -> text), commit them, return the commit SHA."""
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        run_git(repo, "add", "--", rel)
    run_git(repo, "commit", "-q", "-m", "chore: fixture")
    return run_git(repo, "rev-parse", "HEAD")
