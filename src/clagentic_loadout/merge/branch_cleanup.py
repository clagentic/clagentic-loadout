"""merge.branch_cleanup — delete a closed PR's head branch, guarded.

Opt-in step of loadout-close-pr (--delete-branch). It runs only after the
close has been confirmed by readback, and it never changes the close result:
every outcome is reported as a separate BranchDeletion record.

A branch is deleted only when ALL of these hold, each read through the host
API under the same credential as the close:
  - the head repository is the PR's own repository (never a fork);
  - the branch is neither the PR's base branch nor the repository default;
  - the branch is not protected;
  - the branch tip still equals the expected head SHA.
Any failed condition keeps the branch and names the reason.

The expected head SHA is the PR head as read BEFORE the close call and passed
in by the caller; it is never re-read after the close, so a push landing
between the close and the cleanup is compared against the closed head, not
against itself. Without an expected SHA nothing is deleted.

REMAINING RACE: neither host offers a delete conditional on an expected SHA.
The tip is read immediately before the delete call, so a push landing in the
window between that read and the delete is still lost. The window is one HTTP
round trip; it cannot be closed from the client side. The earlier window (PR
read to close) is covered by the pre-close expected SHA.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from clagentic_loadout.merge.errors import GateFactUnavailableError, MergeExecutionError

OUTCOME_DELETED = "deleted"
OUTCOME_KEPT = "kept"
OUTCOME_ERROR = "error"

REASON_HEAD_REPO_UNAVAILABLE = "head-repo-unavailable"
REASON_FORK_HEAD = "fork-head"
REASON_BASE_BRANCH = "base-branch"
REASON_DEFAULT_BRANCH = "default-branch"
REASON_PROTECTED = "protected"
REASON_HEAD_ADVANCED = "head-advanced"
REASON_ALREADY_ABSENT = "branch-absent"
REASON_HEAD_UNKNOWN = "head-unknown"


@dataclass(frozen=True)
class BranchDeletion:
    """Result of one head-branch deletion attempt. `outcome` is one of
    deleted | kept | error; `reason` is a stable token for kept and a
    human-readable message for error; `detail` carries resolved values."""

    outcome: str
    branch: str
    reason: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        out = {"outcome": self.outcome, "branch": self.branch}
        if self.reason:
            out["reason"] = self.reason
        if self.detail:
            out["detail"] = self.detail
        return out

    def describe(self, owner: str, repo: str) -> str:
        where = f"branch {self.branch!r} in {owner}/{repo}" if self.branch else f"in {owner}/{repo}"
        text = f"branch deletion {self.outcome} -- {where}"
        if self.reason:
            text += f" ({self.reason})"
        if self.detail:
            text += f": {self.detail}"
        return text


def _full_name(repo_obj: Any) -> str:
    name = repo_obj.get("full_name") if isinstance(repo_obj, dict) else None
    return name.lower() if isinstance(name, str) else ""


def delete_head_branch(
    owner: str,
    repo: str,
    *,
    expected_head_sha: str,
    get_pr_info: Callable[[], dict[str, Any]],
    get_default_branch: Callable[[], str],
    get_branch_state: Callable[[str], tuple[str, bool] | None],
    delete_branch: Callable[[str], None],
) -> BranchDeletion:
    """Apply the guard chain and delete the PR's head branch when it passes.
    `expected_head_sha` is the PR head SHA read before the close; empty means
    that read failed, and the branch is kept. Never raises for a host failure:
    that is reported as outcome=error."""
    branch = ""
    try:
        pr = get_pr_info()
        head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
        branch = head.get("ref") or ""
        post_close_sha = head.get("sha") or ""
        if not branch:
            return BranchDeletion(OUTCOME_ERROR, branch, "the PR record carries no head branch")
        if not expected_head_sha:
            return BranchDeletion(
                OUTCOME_KEPT, branch, REASON_HEAD_UNKNOWN,
                "the PR head SHA could not be read before the close",
            )
        pr_sha = expected_head_sha
        if post_close_sha and post_close_sha != pr_sha:
            return BranchDeletion(
                OUTCOME_KEPT, branch, REASON_HEAD_ADVANCED,
                f"PR head {post_close_sha} differs from head at close {pr_sha}",
            )

        head_repo = _full_name(head.get("repo"))
        if not head_repo:
            return BranchDeletion(
                OUTCOME_KEPT, branch, REASON_HEAD_REPO_UNAVAILABLE,
                "the PR record names no head repository",
            )
        base_repo = _full_name(base.get("repo")) or f"{owner}/{repo}".lower()
        if head_repo != base_repo or head_repo != f"{owner}/{repo}".lower():
            return BranchDeletion(
                OUTCOME_KEPT, branch, REASON_FORK_HEAD,
                f"head repository {head_repo!r} is not {owner}/{repo}",
            )

        if branch == base.get("ref"):
            return BranchDeletion(OUTCOME_KEPT, branch, REASON_BASE_BRANCH)
        default_branch = get_default_branch()
        if branch == default_branch:
            return BranchDeletion(OUTCOME_KEPT, branch, REASON_DEFAULT_BRANCH)

        state = get_branch_state(branch)
        if state is None:
            return BranchDeletion(OUTCOME_KEPT, branch, REASON_ALREADY_ABSENT)
        tip, protected = state
        if protected:
            return BranchDeletion(OUTCOME_KEPT, branch, REASON_PROTECTED)
        if tip != pr_sha:
            return BranchDeletion(
                OUTCOME_KEPT, branch, REASON_HEAD_ADVANCED,
                f"branch tip {tip} differs from PR head {pr_sha}",
            )

        delete_branch(branch)
    except (GateFactUnavailableError, MergeExecutionError) as exc:
        return BranchDeletion(OUTCOME_ERROR, branch, str(exc))
    return BranchDeletion(OUTCOME_DELETED, branch, detail=f"head {pr_sha}")


__all__ = [
    "BranchDeletion",
    "OUTCOME_DELETED",
    "OUTCOME_ERROR",
    "OUTCOME_KEPT",
    "delete_head_branch",
]
