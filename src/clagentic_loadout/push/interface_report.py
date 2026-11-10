"""push.interface_report -- the consumers of a breaking interface change, reported
at push time.

When the branch being pushed changes the repository's declared interface file
in a breaking way, the push verification step prints who consumes what broke,
from the consumer map. This is reporting only: it never fails a push and is no
gate input. A base that cannot be resolved, a revision that cannot be read or a
consumer map that is not there is not an error here; the first two print
nothing and the last is said in one line.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from clagentic_loadout.git_origin import OriginBindingError, read_origin_target
from clagentic_loadout.interface.model import INTERFACE_PATH, InterfaceError
from clagentic_loadout.interface.report import breaking_report
from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReadError


def _producer(project_root: Path) -> str | None:
    """The producer repository named by the origin, or None when the origin is
    unreadable: a directory name is not a repository name, so it is not guessed."""
    try:
        return read_origin_target(project_root).name
    except OriginBindingError:
        return None


def _merge_base(reader: GitPolicyReader, base_refs: Sequence[str]) -> str | None:
    """The merge base of the first of *base_refs* that resolves and HEAD."""
    for ref in base_refs:
        try:
            return reader.merge_base(ref, "HEAD")
        except PolicyReadError:
            continue
    return None


def notice_lines(project_root: Path, base_refs: Sequence[str]) -> list[str]:
    """The lines to print for the push from *project_root*: empty unless the
    branch changes the interface file between its merge base with the first
    resolvable of *base_refs* and HEAD, and the change is breaking."""
    reader = GitPolicyReader(project_root)
    base = _merge_base(reader, base_refs)
    if base is None:
        return []
    try:
        head = reader.resolve_commit("HEAD")
        if INTERFACE_PATH not in {path for path, _modes in reader.changed_paths(base, head)}:
            return []
        producer = _producer(project_root)
        report = breaking_report([reader], base, head, producer or "")
    except PolicyReadError:
        return []
    except InterfaceError as exc:
        return [f"push: interface diff not computed: {exc}"]
    if not report.breaking:
        return []
    elements = ", ".join(report.changed_elements())
    if producer is None:
        return [
            f"push: BREAKING INTERFACE CHANGE ({elements}); the producer could not be "
            "determined from the origin, so its consumers are not listed"
        ]
    status = report.map_status
    if status is None or status.consumers is None:
        reason = status.unavailable if status is not None else "no map was read"
        return [
            f"push: BREAKING INTERFACE CHANGE ({elements}); the consumer map is unavailable "
            f"({reason}), so its consumers are not listed"
        ]
    if not report.hits:
        return [
            f"push: BREAKING INTERFACE CHANGE ({elements}); the consumer map lists no consumer of it"
        ]
    lines = [f"push: BREAKING INTERFACE CHANGE ({elements}); consumers per the consumer map:"]
    lines.extend(
        f"push:   {hit.consumer} consumes {hit.element} (confirmed {hit.confirmed_at})"
        for hit in report.hits
    )
    return lines


def print_notice(
    project_root: Path, base_refs: Sequence[str], stream: TextIO | None = None
) -> None:
    """Print notice_lines on *stream* (default: standard error)."""
    out = stream if stream is not None else sys.stderr
    for line in notice_lines(project_root, base_refs):
        print(line, file=out)


__all__ = ["notice_lines", "print_notice"]
