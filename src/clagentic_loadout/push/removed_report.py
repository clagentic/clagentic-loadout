"""push.removed_report -- the behaviour a branch removes, reported at push time.

When the branch being pushed deletes test cases, public symbols or command-line
verbs, the push verification step prints each one (see spec.removals for what an
item is and the languages covered). This is reporting only: it never fails a push
and is no gate input. A base that cannot be resolved prints nothing; a tree that
cannot be read is said in one line.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from clagentic_loadout.push.interface_report import merge_base
from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReadError
from clagentic_loadout.spec import removals

#: Most items listed in one push; the count of the rest is stated.
REPORT_CAP = 25


def notice_lines(project_root: Path, base_refs: Sequence[str]) -> list[str]:
    """The lines to print for the push from *project_root*: empty unless the
    branch removes items between its merge base with the first resolvable of
    *base_refs* and HEAD."""
    reader = GitPolicyReader(project_root)
    base = merge_base(reader, base_refs)
    if base is None:
        return []
    try:
        head = reader.resolve_commit("HEAD")
    except PolicyReadError as exc:
        return [f"push: removed behaviour not computed: {exc}"]
    report = removals.report_between(reader, base, head)
    if report["status"] != removals.STATUS_COMPUTED:
        return [f"push: removed behaviour not computed: {report['note']}"]
    items = report["items"]
    lines = [f"push: removed at {head}: {entry['item']} ({entry['kind']})" for entry in items[:REPORT_CAP]]
    if len(items) > REPORT_CAP:
        lines.append(f"push: and {len(items) - REPORT_CAP} more removed at {head}")
    return lines


def print_notice(
    project_root: Path, base_refs: Sequence[str], stream: TextIO | None = None
) -> None:
    """Print notice_lines on *stream* (default: standard error)."""
    out = stream if stream is not None else sys.stderr
    for line in notice_lines(project_root, base_refs):
        print(line, file=out)


__all__ = ["REPORT_CAP", "notice_lines", "print_notice"]
