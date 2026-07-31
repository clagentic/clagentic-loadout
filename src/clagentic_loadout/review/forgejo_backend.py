"""review.forgejo_backend — Forgejo-side review-post-and-verify adapter.

Wave B slice 2 (lr-412f, tome #688). This backend does NOT reimplement
post-and-verify: the Forgejo comment-post-and-verify gate already exists in
clagentic_loadout.transport.git_host_api (Wave B slice 1, lr-3ba8) — this
module is a thin adapter from that module's function-level API onto the
review.contract.ReviewBackend Protocol, so review.verb can drive both
platforms through one shape without either backend knowing about the other.

No HTTP logic, no readback logic, no identity resolution lives here — all of
that is transport.git_host_api's (resolve_bot_login, check_pr_sha,
verify_comment_on_pr, request). This module only:
  1. Builds the issues/<pr>/comments POST path + JSON body git_host_api's
     request() already expects.
  2. Calls git_host_api's building blocks in the same order main() does for a
     --verify-comment POST.
  3. Translates GitHostApiError onto the shared review.errors vocabulary
     (ReviewPostError / ReviewVerifyError) so review.verb's exception
     handling never needs to know which backend raised it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from clagentic_loadout.review.contract import VerifiedReview
from clagentic_loadout.review.errors import (
    DeleteOwnCommentRefusedError,
    ReviewPostError,
    ReviewVerifyError,
)
from clagentic_loadout.transport import git_host_api

#: git_host_api exit codes that indicate the POST itself never landed (never a
#: verify-phase failure) — translated to ReviewPostError. EXIT_CURL_FAILED
#: covers both "the write method itself returned non-2xx" and "a redirect
#: was refused" (see git_host_api.request's docstring); either way, nothing was
#: confirmed to exist yet for a readback to have failed against.
_POST_PHASE_CODES = frozenset(
    {
        git_host_api.EXIT_CURL_FAILED,
        git_host_api.EXIT_BODY_STDIN_EMPTY,
        git_host_api.EXIT_STALE_PR,
        git_host_api.EXIT_OWNER_REPO_NOT_FOUND,
        git_host_api.EXIT_TOKEN_FETCH_FAILED,
    }
)


def post_and_verify_comment(
    git_host_base: str,
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    body: str,
    *,
    expected_pr_sha: str | None = None,
    known_bad_owners: frozenset[str] = frozenset(),
    opener=None,
) -> VerifiedReview:
    """Post exactly one issue/PR comment and confirm it landed, reusing
    transport.git_host_api's post-and-verify building blocks verbatim (no
    duplicated HTTP or readback logic).

    Mirrors git_host_api's own --verify-comment + --pr-sha CLI path:
      1. (optional) check_pr_sha — refuses on a stale PR head SHA.
      2. request(POST, issues/<pr>/comments) with the JSON body.
      3. resolve_bot_login — resolve the caller's own login from the token.
      4. verify_comment_on_pr — freshness-anchored readback confirming the
         caller's own comment landed on the correct PR.

    Raises:
        ReviewPostError: the POST itself never landed (translated from a
            git_host_api post-phase GitHostApiError — see _POST_PHASE_CODES).
        ReviewVerifyError: the POST succeeded but the readback could not
            confirm it (translated from git_host_api.EXIT_VERIFY_FAILED).
    """
    try:
        if expected_pr_sha:
            git_host_api.check_pr_sha(
                git_host_base, token, owner, repo, str(pr_number), expected_pr_sha,
                known_bad_owners=known_bad_owners, opener=opener,
            )

        body_bytes = json.dumps({"body": body}).encode("utf-8")
        pre_post_utc = datetime.now(timezone.utc)
        git_host_api.request(
            git_host_base,
            "POST",
            f"/api/v1/repos/{owner}/{repo}/issues/{pr_number}/comments",
            token,
            body_bytes=body_bytes,
            opener=opener,
        )

        bot_login = git_host_api.resolve_bot_login(git_host_base, token, opener=opener)
        verified = git_host_api.verify_comment_on_pr(
            git_host_base, token, owner, repo, str(pr_number), body, bot_login,
            not_before=pre_post_utc, opener=opener,
        )
    except git_host_api.GitHostApiError as exc:
        if exc.code in _POST_PHASE_CODES:
            raise ReviewPostError(str(exc)) from exc
        raise ReviewVerifyError(str(exc)) from exc

    return VerifiedReview(
        id=verified["id"],
        url=verified["html_url"],
        login=verified["login"],
        body=verified["body"],
    )


def delete_own_comment(
    git_host_base: str,
    token: str,
    owner: str,
    repo: str,
    comment_id: "int | str",
    *,
    opener=None,
) -> None:
    """Belt-and-suspenders self-delete-own-comment (lr-f43c4b, review-post
    CLI parity with the GitHub side's review.github_backend.
    delete_own_comment), reusing transport.git_host_api.delete_own_comment
    verbatim — no duplicated GET/assert/DELETE logic. This is the piece the
    lr-f43c4b root cause named: --delete-own-comment was previously wired
    ONLY through loadout-git-host-api's own Forgejo-shaped CLI (transport.
    git_host_api._run), with no route from review-post's shared,
    platform-selected entry point. review.verb's --delete-own-comment flag
    now reaches this adapter for a Forgejo PR exactly the same way it reaches
    review.github_backend.delete_own_comment for a GitHub one, so a single
    CLI surface handles self-delete on either platform.

    Mirrors post_and_verify_comment's own translation shape: a git_host_api
    GitHostApiError is translated onto this contract's shared vocabulary
    (review.errors) so review.verb's exception handling never needs a
    Forgejo-specific except clause. EXIT_DELETE_OWN_COMMENT_REFUSED (the
    only refusal-before-any-I/O code git_host_api.delete_own_comment raises)
    maps to DeleteOwnCommentRefusedError; any other GitHostApiError (e.g. the
    DELETE call itself failing after the belt-and-suspenders checks passed)
    maps to ReviewPostError, exactly like a post-phase failure on the
    ordinary post-and-verify path.

    Raises:
        DeleteOwnCommentRefusedError: refused before any DELETE was issued
            (unreadable comment, cross-author, or a verdict fence present).
        ReviewPostError: the belt-and-suspenders checks passed but the
            DELETE call itself failed (non-2xx / network error).
    """
    try:
        git_host_api.delete_own_comment(
            git_host_base, token, owner, repo, str(comment_id), opener=opener
        )
    except git_host_api.GitHostApiError as exc:
        if exc.code == git_host_api.EXIT_DELETE_OWN_COMMENT_REFUSED:
            raise DeleteOwnCommentRefusedError(str(exc)) from exc
        raise ReviewPostError(str(exc)) from exc


class ForgejoReviewBackend:
    """ReviewBackend Protocol implementation for Forgejo.

    Constructed with a resolved token and the Forgejo API base URL — token
    resolution stays the caller's responsibility (via
    transport.credential_provider), matching the GitHub backend's shape.
    """

    def __init__(
        self,
        token: str,
        *,
        git_host_base: str,
        expected_pr_sha: str | None = None,
        known_bad_owners: frozenset[str] = frozenset(),
        opener=None,
    ) -> None:
        self._token = token
        self._git_host_base = git_host_base
        self._expected_pr_sha = expected_pr_sha
        self._known_bad_owners = known_bad_owners
        self._opener = opener

    def post_and_verify(
        self,
        *,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> VerifiedReview:
        return post_and_verify_comment(
            self._git_host_base,
            self._token,
            owner,
            repo,
            pr_number,
            body,
            expected_pr_sha=self._expected_pr_sha,
            known_bad_owners=self._known_bad_owners,
            opener=self._opener,
        )

    def delete_own_comment(
        self,
        *,
        owner: str,
        repo: str,
        comment_id: "int | str",
    ) -> None:
        """Belt-and-suspenders self-delete-own-comment (lr-f43c4b) — see the
        module-level delete_own_comment() function for the full
        admissible-operation contract (delegates to transport.git_host_api.
        delete_own_comment verbatim)."""
        delete_own_comment(
            self._git_host_base,
            self._token,
            owner,
            repo,
            comment_id,
            opener=self._opener,
        )


__all__ = [
    "ForgejoReviewBackend",
    "delete_own_comment",
    "post_and_verify_comment",
]
