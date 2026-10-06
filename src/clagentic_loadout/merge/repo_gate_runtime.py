"""merge.repo_gate_runtime — the runtime consumer of a repo's declared merge
gate keys.

`merge.gate_config` reads and validates the repo's `merge:` gate declarations.
This module is the ONLY place `loadout-merge` consumes them, and it owns the
bootstrap-safety policy that makes consuming them safe:

  - `required_reviewer_roles` is a FLOOR beneath `--required-reviewer`: the
    roles actually required are the union of the two.
  - `required_scanners` maps a reviewer role to scanner names a clean verdict
    from that role must record, and must not report as failed.
  - FALLBACK GRANULARITY IS THE WHOLE CONFIG. If any gate key cannot be
    loaded, none of the repo's gate keys is enforced: the merge degrades to
    the flags-only behaviour with a warning naming the file and the error, so
    the merge that lands the corrected config is always possible. A config
    that is half-trusted is harder to reason about than one that is either
    enforced or visibly not.

A config that loads cleanly but cannot be satisfied is NOT handled here: that
is a real refusal, overridable only by the verb's explicit, logged
`--ignore-repo-gate`, which covers every repo gate key.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.merge.gate_config import (
    InvalidMergeGateConfigError,
    load_required_reviewer_roles,
    load_required_scanners,
)


@dataclass(frozen=True)
class RepoGate:
    """What the repo's config requires of a merge, plus any load warnings."""

    reviewer_roles: tuple[str, ...] = ()
    required_scanners: dict[str, tuple[str, ...]] | None = None
    warnings: tuple[str, ...] = ()

    def scanners_for(self, reviewer_name: str) -> tuple[str, ...]:
        return (self.required_scanners or {}).get(reviewer_name, ())


def load_repo_gate(repo_path: str | Path | None) -> RepoGate:
    """Load the repo's declared gate; any unloadable key drops the whole gate.

    *repo_path* None (no local tree) declares nothing, matching every other
    repo-tier key in the merge verb.
    """
    if repo_path is None:
        return RepoGate()

    try:
        roles = load_required_reviewer_roles(repo_path)
        scanners = load_required_scanners(repo_path)
    except InvalidMergeGateConfigError as exc:
        return RepoGate(
            warnings=(
                f"merge.required_reviewer_roles NOT ENFORCED and merge.required_scanners "
                f"NOT ENFORCED -- the repo gate config could not be loaded, so only "
                f"--required-reviewer applies: {exc}",
            )
        )
    return RepoGate(reviewer_roles=roles, required_scanners=scanners)


__all__ = ["RepoGate", "load_repo_gate"]
