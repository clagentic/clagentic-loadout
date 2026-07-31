"""merge.diff_scope — refuse a PR whose changed-file count exceeds the
configured cap.

Wave B slice 4 (lr-885f, tome #688). Ported from the reference merge gate's
Forgejo diff-scope check (_assert_forgejo_diff_scope). The bound is CONFIG (a
constructor/call parameter, default DEFAULT_MAX_CHANGED_FILES), never hardcoded past that
default — a caller widening or narrowing the cap does so explicitly, and the
gate decision is always logged (via the returned file list) so it stays
auditable.
"""

from __future__ import annotations

from clagentic_loadout.merge.errors import DiffScopeExceededError

#: Default maximum changed-file count — passes unless the diff is very wide.
DEFAULT_MAX_CHANGED_FILES = 50


def check_diff_scope(
    changed_files: list[str],
    pr_number: int,
    owner: str,
    repo: str,
    *,
    max_changed_files: int = DEFAULT_MAX_CHANGED_FILES,
) -> None:
    """Refuse the merge if len(changed_files) exceeds max_changed_files.

    Takes the already-fetched file list rather than performing the fetch
    itself — the git-host API call is the merge verb's job (via
    merge.forgejo_backend), keeping this module a pure, easily-unit-tested
    policy check with no transport/credential coupling.

    Raises merge.errors.DiffScopeExceededError when the count exceeds the
    bound. A caller that could not fetch the file list at all must treat
    that as its own gate-fact-unavailable refusal (merge.errors.
    GateFactUnavailableError) BEFORE ever calling this function — an empty
    list is indistinguishable from "cannot determine" only from the outside;
    this function itself always fails closed if a caller ever pathologically
    passed a fetch failure through as an empty list.
    """
    count = len(changed_files)
    if count > max_changed_files:
        raise DiffScopeExceededError(
            f"merge gate FAILED — PR #{pr_number} in {owner}/{repo} touches "
            f"{count} file(s), exceeding the configured limit of "
            f"{max_changed_files}. Reduce the diff scope or raise the "
            f"configured max-changed-files bound if it is wrong for this "
            f"change."
        )


__all__ = ["DEFAULT_MAX_CHANGED_FILES", "check_diff_scope"]
