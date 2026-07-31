"""review.errors — shared exception classes for the review-post contract.

Wave B slice 2 (lr-412f, tome #688). Ported from the reference two-caller
GitHub review transport's exception shape; the source module stays primary
until its separate CUT OVER + RETIRE + VERIFY-GONE task per the migration
plan.

Both transports behind the review-post contract (see
clagentic_loadout.review.contract) raise these same three exception classes,
so a caller (verb.py) never needs a transport-specific except clause. The
FORGEJO backend adapts transport.git_host_api.GitHostApiError onto this same
vocabulary rather than leaking a git_host_api-specific exception type across the
contract boundary.
"""

from __future__ import annotations


class PlatformMismatchError(Exception):
    """Raised when the caller's own explicit platform selection does not
    match the transport a backend implements (e.g. a Forgejo PR routed
    through the GitHub backend, or vice versa). This is a caller-input-shape
    error, not a network error: it fires BEFORE any credential is minted or
    API call is made, so a wrong-platform call fails fast and locally
    instead of reaching the wrong host's API and returning an opaque
    4xx/422."""


class ReviewPostError(Exception):
    """Raised when the review post itself never lands: a non-2xx response,
    or a 2xx response with no usable identifier to anchor the readback to.
    Distinct from ReviewVerifyError — the post never succeeded, so there is
    nothing for a readback to confirm. Callers MUST treat this as `blocked`,
    never as a partial success, and must never conflate it with a
    verify-phase failure: the remediation differs (retry/inspect the post vs.
    investigate the identity/readback step)."""


class ReviewVerifyError(Exception):
    """Raised when the review post succeeded (2xx, with a usable id) but the
    mandatory readback cannot confirm the review/comment landed under the
    caller's own resolved identity on the correct PR. Mirrors the Forgejo
    git_host_api transport's EXIT_VERIFY_FAILED contract: a reviewer MUST NOT
    report success unless the readback confirms authorship. Reachable ONLY
    after a successful post — a failure at this stage must never be reported
    as a post/token-permission failure."""


class ReviewBodyStdinEmptyError(Exception):
    """Raised when a --body-stdin source is empty, not valid JSON, not a
    JSON object, or has no non-empty 'body' string field. Fires BEFORE any
    network call is made, mirroring transport.git_host_api's
    EXIT_BODY_STDIN_EMPTY contract — a truncated/malformed stdin source
    never reaches either platform as a silent empty post."""


class DeleteOwnCommentRefusedError(Exception):
    """Raised when a self-delete-own-comment request (lr-e2ce66) is refused
    BEFORE the DELETE is issued: the comment could not be read, its author
    login does not match the caller's own resolved bot identity
    (cross-author delete — an audit-tampering/censorship surface, refused
    unconditionally, no override), or its body carries a fenced
    ```review-result``` block (deleting a landed verdict could game the
    merge gate's re-read: post clean, get read, delete, repost — refused
    even for the caller's own comment). Mirrors
    transport.git_host_api.EXIT_DELETE_OWN_COMMENT_REFUSED's contract on the
    GitHub side of the same admissible-operation rule."""


__all__ = [
    "DeleteOwnCommentRefusedError",
    "PlatformMismatchError",
    "ReviewBodyStdinEmptyError",
    "ReviewPostError",
    "ReviewVerifyError",
]
