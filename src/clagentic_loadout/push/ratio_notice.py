"""push.ratio_notice — the net test:code ratio of the branch just pushed.

Advice for the builder, never a gate: the line is printed as a notice and the
object is added to the push result, and nothing here changes what the push
exits with. The caller (the builder) puts the line in the PR body; push does
not write the body.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clagentic_loadout.review.changeset_ratio_source import from_local
from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReadError


def ratio_for_push(project_root: Path, base_branch: str) -> dict[str, Any]:
    """The ratio object for the checked-out head against *base_branch*, taking
    the remote's copy of the branch when the checkout has one: a stale local
    branch would count commits that are already merged."""
    reader = GitPolicyReader(project_root)
    base = base_branch
    try:
        base = reader.resolve_commit(f"origin/{base_branch}")
    except PolicyReadError:
        pass
    return from_local(project_root, base, "HEAD")


def notice_line(ratio: dict[str, Any]) -> str:
    """The one-line notice for *ratio*."""
    return f"push: {ratio['line']}"


__all__ = ["notice_line", "ratio_for_push"]
