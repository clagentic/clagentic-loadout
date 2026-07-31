"""review — review-post verb: post exactly one review comment and verify it
landed, on either transport, behind one contract.

Wave B slice 2 (lr-412f, tome #688). Ported from the reference two-caller
GitHub review transport plus the already-landed Forgejo post-and-verify path
(clagentic_loadout.transport.git_host_api, Wave B slice 1). Source copies
stay primary until their separate CUT OVER + RETIRE + VERIFY-GONE tasks per
the migration plan.

    contract         — the shared ReviewBackend Protocol + VerifiedReview
                        result shape + --body-stdin content validation.
    errors           — PlatformMismatchError / ReviewPostError /
                        ReviewVerifyError / ReviewBodyStdinEmptyError,
                        shared by both backends.
    forgejo_backend   — thin adapter over transport.git_host_api's existing
                        post-and-verify path (no duplicated HTTP/readback
                        logic).
    github_backend    — the GitHub-side post-and-verify transport, including
                        token-type-aware own-login resolution (PAT/OAuth via
                        GET /user; App installation tokens resolve
                        '<configured-app-slug>[bot]' on a 403 from /user --
                        GET /app is JWT-only and NEVER called by this
                        backend, lr-d31e).
    verb              — the review-post CLI: role-parameterized (--caller),
                        platform-parameterized (--platform, mandatory,
                        checked before any credential mint).

SCOPE BOUNDARY: this package does not emit or parse the fenced
```review-result``` verdict block — that is the merge-gate verb's job (a
later loadout slice). See contract.py's module docstring.
"""

from __future__ import annotations

from clagentic_loadout.review.contract import (
    ReviewBackend,
    VerifiedReview,
    validate_review_body_stdin_content,
    validate_review_findings_body_stdin_content,
)
from clagentic_loadout.review.errors import (
    PlatformMismatchError,
    ReviewBodyStdinEmptyError,
    ReviewPostError,
    ReviewVerifyError,
)
from clagentic_loadout.review.forgejo_backend import ForgejoReviewBackend
from clagentic_loadout.review.github_backend import (
    GithubReviewBackend,
    assert_platform_is_github,
    resolve_own_login,
)
from clagentic_loadout.review.verb import (
    EXIT_BODY_STDIN_EMPTY,
    EXIT_OK,
    EXIT_POST_FAILED,
    EXIT_TOKEN_FETCH_FAILED,
    EXIT_USAGE,
    EXIT_VERIFY_FAILED,
    EXIT_WRONG_PLATFORM,
    assert_platform_is_forgejo,
    build_backend,
    main,
)

__all__ = [
    "EXIT_BODY_STDIN_EMPTY",
    "EXIT_OK",
    "EXIT_POST_FAILED",
    "EXIT_TOKEN_FETCH_FAILED",
    "EXIT_USAGE",
    "EXIT_VERIFY_FAILED",
    "EXIT_WRONG_PLATFORM",
    "ForgejoReviewBackend",
    "GithubReviewBackend",
    "PlatformMismatchError",
    "ReviewBackend",
    "ReviewBodyStdinEmptyError",
    "ReviewPostError",
    "ReviewVerifyError",
    "VerifiedReview",
    "assert_platform_is_forgejo",
    "assert_platform_is_github",
    "build_backend",
    "main",
    "resolve_own_login",
    "validate_review_body_stdin_content",
    "validate_review_findings_body_stdin_content",
]
