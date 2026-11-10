"""interface.report -- the interface diff of two revisions with the consumers of
what broke, for the callers that only report it (push and merge).

Reporting only. Nothing here fails a push or a merge: a revision that cannot be
read or an interface file that is not valid is raised for the caller to print as
a note, and a consumer map that is not available is carried in the result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.interface.consumers import ConsumerHit, MapStatus, locate_consumers
from clagentic_loadout.interface.diff import (
    InterfaceDiff,
    diff_documents,
    read_interface_with_fallback,
)
from clagentic_loadout.interface.notice import consumers_of_changes
from clagentic_loadout.review.policy_reader import PolicyReader


@dataclass(frozen=True)
class BreakingReport:
    diff: InterfaceDiff
    #: None when the diff is not breaking, since the map is not read then.
    map_status: MapStatus | None
    hits: tuple[ConsumerHit, ...]

    @property
    def breaking(self) -> bool:
        return self.diff.breaking

    def changed_elements(self) -> list[str]:
        return [c.element for c in self.diff.breaking_changes()]


def breaking_report(
    readers: Sequence[PolicyReader],
    base_rev: str,
    head_rev: str,
    producer: str,
    *,
    consumers_flag: str | None = None,
    env: Mapping[str, str] | None = None,
    config_root: str | Path | None = None,
) -> BreakingReport:
    """Diff the interface of *producer* between *base_rev* and *head_rev* and,
    when the change is breaking, list who consumes what broke. Raises
    PolicyReadError when a revision cannot be read through any reader and
    InterfaceError when an interface file is not valid."""
    diff = diff_documents(
        read_interface_with_fallback(readers, base_rev),
        read_interface_with_fallback(readers, head_rev),
    )
    if not diff.breaking:
        return BreakingReport(diff, None, ())
    status = locate_consumers(consumers_flag, env, config_root=config_root)
    if status.consumers is None:
        return BreakingReport(diff, status, ())
    hits = consumers_of_changes(status.consumers, producer, diff.breaking_changes())
    return BreakingReport(diff, status, tuple(hits))


__all__ = ["BreakingReport", "breaking_report"]
