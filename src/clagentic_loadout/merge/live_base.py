"""merge.live_base -- where the PR's base branch points at merge time.

A host's pull request record carries a base commit (``base.sha``) that is the
base branch's tip as of the PR's last head update. Policy merged to the base
branch after that push (a new disposition) or removed from it (a withdrawn or
edited one) is invisible there, so reading policy at the recorded commit both
ignores new dispositions and keeps applying withdrawn ones. The merge gate
reads policy at the commit this module resolves instead; the recorded
``base.sha`` stays provenance only and is never a fallback.
"""

from __future__ import annotations

from collections.abc import Callable

from clagentic_loadout.merge.errors import GateFactUnavailableError
from clagentic_loadout.merge.tree_sync import resolve_base_branch
from clagentic_loadout.sha import FULL_SHA_RE


def resolve_live_base_tip(
    read_branch_tip: Callable[[str], str], pr_info: dict
) -> str:
    """The full commit SHA the PR's base branch points at now.

    *read_branch_tip* is the host seam: it takes a branch name and returns the
    branch's tip, raising GateFactUnavailableError when it cannot. Raises
    GateFactUnavailableError (fail closed) when the PR names no base branch,
    the tip cannot be read, or what the host returned is not a full commit SHA.
    """
    branch = resolve_base_branch(pr_info)
    if not branch:
        raise GateFactUnavailableError(
            "the pull request record names no base branch, so the policy base "
            "cannot be resolved"
        )
    tip = read_branch_tip(branch)
    if not isinstance(tip, str) or not FULL_SHA_RE.match(tip):
        raise GateFactUnavailableError(
            f"the host did not return a full commit SHA for the tip of base branch {branch!r}"
        )
    return tip
