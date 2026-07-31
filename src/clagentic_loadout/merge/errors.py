"""merge.errors — shared exception classes for the merge-gate verb.

Wave B slice 4 (lr-885f, tome #688). Ported from the reference merge gate;
the source module stays primary until its separate CUT OVER + RETIRE +
VERIFY-GONE task per the migration plan.

THIS IS THE LOAD-BEARING RELEASE GATE. Every exception class here corresponds
to a fail-closed refusal link in the gate chain (merge.verb's docstring lists
the full chain). A caller catching one of these and silently continuing is a
critical defect — main() always translates each of these to a distinct exit
code and a refusal, never a partial success.
"""

from __future__ import annotations


class MergeUsageError(Exception):
    """Raised for a caller-input-shape error (bad flags, malformed owner/repo,
    missing required argument combination). Fires before any credential
    resolution, gate check, or network call."""


class AuthorityDeniedError(Exception):
    """Raised when the merge-authority provider seam (merge.authority) does
    not affirm that the caller's role may authorize a merge on the target
    repo/PR. FAIL-CLOSED: an unreachable provider, an empty role, or an
    explicit deny are all refusals — never a silent allow."""


class NamespaceDeniedError(Exception):
    """Raised when the target owner/namespace is not present in the
    caller-supplied allowed-namespace set (see push.namespace_guard, reused
    here — not reimplemented). Fires BEFORE any credential is resolved or
    merge attempted."""


class StaleHeadShaError(Exception):
    """Raised when --expected-head-sha was supplied and the PR's CURRENT head
    SHA differs from it. The branch advanced after the gate ran and the new
    commits were never reviewed — merging them would silently land unreviewed
    code. A no-op when --expected-head-sha is absent (see merge.stale_sha)."""


class VerdictMissingError(Exception):
    """Raised when no PR comment authored by the expected reviewer login can
    be found, or the comment carries no fenced ```review-result``` verdict
    block. A missing verdict is a refusal, never treated as an implicit
    pass."""


class VerdictMalformedError(Exception):
    """Raised when a reviewer's verdict comment carries a
    ```review-result``` block but its JSON payload is malformed, is not an
    object, is missing a required field, or has an invalid review_status /
    a head_sha that fails 40-char hex validation. A malformed verdict is a
    caller (reviewer) bug, not a stale-gate condition, and refuses exactly
    like a missing verdict."""


class VerdictStaleError(Exception):
    """Raised when a reviewer's verdict block's head_sha does not match the
    PR's current head SHA (the branch advanced after the reviewer ran). The
    verdict does not cover the current HEAD and cannot authorize a merge of
    it."""


class VerdictBlockingError(Exception):
    """Raised when a reviewer's verdict block's review_status is
    'blocking'. The reviewer found an issue that must be resolved before
    merge; this is the gate doing exactly its job, not an error condition to
    route around."""


class VerdictRoleMismatchError(Exception):
    """Raised when a verdict comment's platform authorship (user.login) is
    correct for the required reviewer slot, but the fenced block's own
    self-declared ``reviewer`` field does NOT match that reviewer's
    tool-authoritative name. This is a GATE-INTEGRITY failure, not a
    missing/stale/malformed verdict: the right App posted, but the CONTENT
    riding in the comment belongs to a different reviewer (e.g. a
    security-audit body posted under the code-review App's login after a
    shared staging path was clobbered — console PR #332, lr-f00c6f Fault 3,
    re-scoped by lr-23fe19). DEFENSE-IN-DEPTH ON TOP OF, NOT INSTEAD OF, the
    user.login authorship binding in merge.verdict.read_reviewer_verdict —
    this check never substitutes body text for the login check; it adds a
    second, independent assertion that the AUTHENTICATED comment's own
    content-claim is self-consistent with who was authenticated."""


class DiffScopeExceededError(Exception):
    """Raised when the PR's changed-file count exceeds the configured
    maximum. A wide diff cannot be safely gated by an automated reviewer at
    the same confidence as a narrow one; the cap is config-driven, not
    hardcoded."""


class TitleInvalidError(Exception):
    """Raised when the PR title does not conform to Conventional Commits
    grammar and --skip-title-check was not passed. The title is promoted
    verbatim into the merge commit message; a non-conformant title produces
    permanent, un-fixable history."""


class CommitSubjectInvalidError(Exception):
    """Raised when a branch commit subject introduced by the PR (base..head)
    does not conform to Conventional Commits grammar and
    --skip-commit-check was not passed. Only fires on a resolved
    merge_method='merge' (real, non-squash) repo — semantic-release (Angular
    preset) parses INDIVIDUAL BRANCH COMMIT SUBJECTS on a real merge, not the
    promoted PR title, so a non-conformant subject here yields next-release
    =none and silently stops beta cuts even when the PR title itself passed
    TitleInvalidError's check. This is a BLOCK, never a rewrite: the offending
    subject stays exactly as authored — normalizing it here would change
    history reviewers already anchored to (see merge.commit_subjects'
    module docstring)."""


class GateFactUnavailableError(Exception):
    """Raised when a gate fact (PR metadata, comment list, file list) cannot
    be read from the git-host API. FAIL-CLOSED: an unreadable gate fact can
    never be treated as a passing gate — the merge is refused pending a
    successful re-read, never silently skipped."""


class CiStatusFailedError(Exception):
    """Raised when the CI-status gate finds a non-empty status set at the
    PR's HEAD whose combined state is not a clean success (failure, error,
    or pending). An EMPTY status set (no runner wired to this repo, by
    design — see merge.ci_status's module docstring) is NEVER routed
    through this exception; only a real, observed red/pending state is a
    refusal."""


class MergeExecutionError(Exception):
    """Raised when the merge API call itself fails (non-2xx/204 response,
    genuine 405 refusal, or a network error). All gates already passed by
    the time this can fire; the merge attempt itself did not complete."""


class PlatformMismatchError(Exception):
    """Raised when the caller's own explicit --platform selection does not
    match the backend a --platform value would select (e.g. a Forgejo PR
    routed through the GitHub backend, or vice versa). Mirrors
    review.errors.PlatformMismatchError exactly (lr-9c69) — same fail-fast
    contract: this fires BEFORE any credential is minted or API call is
    made, so a wrong-platform call refuses locally instead of reaching the
    wrong host's API and returning an opaque 4xx/422."""


__all__ = [
    "AuthorityDeniedError",
    "CiStatusFailedError",
    "CommitSubjectInvalidError",
    "DiffScopeExceededError",
    "GateFactUnavailableError",
    "MergeExecutionError",
    "MergeUsageError",
    "NamespaceDeniedError",
    "PlatformMismatchError",
    "StaleHeadShaError",
    "TitleInvalidError",
    "VerdictBlockingError",
    "VerdictMalformedError",
    "VerdictMissingError",
    "VerdictRoleMismatchError",
    "VerdictStaleError",
]
