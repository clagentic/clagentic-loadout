"""merge.stale_sha — refuse a merge whose gate-time SHA has gone stale.

Wave B slice 4 (lr-885f, tome #688). Ported from the reference merge gate's
stale-SHA check (_check_stale_sha / _check_stale_sha_if_provided). Reuses
clagentic_loadout.sha.compare_sha_values
(Wave A slice 1) rather than a bare string compare — see that module's
docstring for the abbreviated-SHA and stray-whitespace false-mismatch classes
a naive `!=` compare is prone to.

LOAD-BEARING: when a lead/gate evaluates a PR at one HEAD and the branch is
pushed to again before the merge executes, the new commits were never
reviewed. Merging at that point would silently land unreviewed code. This
check is a no-op ONLY when the caller supplies no expected SHA at all — it
never invents one.
"""

from __future__ import annotations

from clagentic_loadout.merge.errors import StaleHeadShaError
from clagentic_loadout.sha import compare_sha_values


def check_stale_head_sha(
    expected_head_sha: str,
    actual_head_sha: str,
    pr_number: int,
    owner: str,
    repo: str,
) -> None:
    """Refuse the merge if *actual_head_sha* (the PR's CURRENT head, read
    live from the git-host API) does not match *expected_head_sha* (the SHA
    the caller evaluated at gate time).

    No-op when *expected_head_sha* is empty — the caller did not supply one.
    This function never invents an expected SHA; absence of the flag is
    always treated as "no staleness check requested," not as an implicit
    pass against whatever the current HEAD happens to be.

    Raises merge.errors.StaleHeadShaError on mismatch.
    """
    if not expected_head_sha:
        return
    if not compare_sha_values(expected_head_sha, actual_head_sha):
        raise StaleHeadShaError(
            f"STALE GATE DATA — refusing merge of PR #{pr_number} in "
            f"{owner}/{repo}. Branch advanced after the gate evaluated it: "
            f"expected head SHA {expected_head_sha!r} but current head is "
            f"{actual_head_sha!r}. Re-run the gate against the current HEAD, "
            f"then retry with the new --expected-head-sha."
        )


__all__ = ["check_stale_head_sha"]
