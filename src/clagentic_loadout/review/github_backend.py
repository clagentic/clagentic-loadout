"""review.github_backend — GitHub-side review-post-and-verify transport.

Wave B slice 2 (lr-412f, tome #688). Ported from the reference two-caller
GitHub review transport (lr-c353/lr-622e/lr-8ea5); the source module stays
primary until its
separate CUT OVER + RETIRE + VERIFY-GONE task per the migration plan. This
is the real new content of this slice — the Forgejo side already reuses
transport.git_host_api's post-and-verify path (see forgejo_backend.py).

VERDICT-TRANSPORT PARITY (lr-71f467, P1 fix): this backend posts a verdict
as an ISSUE COMMENT (POST/GET /repos/{owner}/{repo}/issues/{pr}/comments),
NOT a GitHub native PR review (POST /pulls/{pr}/reviews). Before this fix,
this module wrote to the native-review endpoint while merge.github_backend.
fetch_comments (the merge gate's read side) only ever read issue comments —
two backends inside this SAME package disagreed on the verdict transport, so
a clean verdict posted here was invisible to the merge gate (loadout-merge
refused every GitHub merge requiring a reviewer verdict, exit 24 "No PR
comment from reviewer login ... found"). Both Forgejo backends (review.
forgejo_backend, merge.forgejo_backend) already agreed on issue comments —
this fix makes GitHub match that existing three-backend contract rather than
carving out a fourth, divergent shape. merge.verdict.read_reviewer_verdict
verifies authorship purely via comment["user"]["login"] on an issue comment;
nothing in that contract is native-review-specific, so nothing is lost by
this switch — see post_and_verify_review below for the idempotent-post-and-
verify shape this module now uses, mirroring transport.git_host_api.
verify_comment_on_pr (the Forgejo-side precedent for the same
freshness-anchored readback) function-for-function.

DEDUPE (lr-39f8, structural, mirroring the lr-2451 lesson -- "prose is not a
fix until unskippable"): post_and_verify_review performs a READ-BACK-BEFORE-
POST idempotency check (see _find_existing_own_comment): before ever issuing
the POST, it lists the PR's existing issue comments and looks for one
already authored by the caller's own resolved login, with the SAME body. If
found, that existing comment is returned as the VerifiedReview and NO new
POST is issued -- a caller retried by an upstream dispatch loop (the PR#318
evidence: three POSTs 9-27s apart, identical body) becomes a no-op on the
second and third call, regardless of what triggered the retry. A CHANGED
body is never matched by this check (exact string equality, not substring)
-- a legitimate later re-review with different findings always posts fresh,
exactly as before this fix.

_resolve_own_login is TOKEN-TYPE-AWARE (lr-d31e, fixing the lr-8ea5 port's
JWT-only-GET-/app defect, root-caused per lr-e23a/lr-f193): GitHub
PAT/OAuth tokens resolve their own login via GET /user. GitHub App
INSTALLATION tokens CANNOT call GET /user (that endpoint requires
user-context auth; the documented, expected response is HTTP 403 — never a
"signature" or "permission" fault, lr-7482). GET /app CANNOT be used as a
fallback for an installation token either: /app is JWT-ONLY
(Authorization: Bearer <RS256 app JWT>), and this backend only ever holds
the installation token (Authorization: token <installation-token>) — a live
/app call 401s deterministically for this credential type, it can never
succeed. On a 403 from /user, the bot login is instead resolved
DETERMINISTICALLY from deployment config as '<app-slug>[bot]' (GitHub's own
documented App-bot-login convention) via
transport.github_app_config.resolve_github_app_slug — see that module's
docstring for the config precedence (env var, then user-level config file;
never repo-local) and the trade-off this implies (a slug rotation requires a
config update, traded against a fallback lookup that could never work).

CONFIG-FIRST SHORT-CIRCUIT (lr-b2d1c3): when a caller's own
github_app.slugs.<caller> entry IS configured, resolve_own_login resolves
the bot login from that slug FIRST, WITHOUT ever issuing the GET /user
probe — an installation token's /user call always 403s (see above), so for
a deployment that has declared its per-caller slug, that probe is a
guaranteed-403 wasted API call and deterministic error noise on every
reviewer post (lr-e41f). The GET /user probe remains the fallback for
token types where it can succeed (PAT/OAuth) or when no slug is configured
for the caller — this reorders, but does not change, the token-type-aware
resolution lr-d31e established.

assert_platform_is_github fires BEFORE any credential mint or API call —
callers (review.verb) MUST call it first; there is no code path here that
reaches the network without the platform having already been confirmed as
GitHub.

Post-and-verify (lr-622e freshness-anchor precedent generalized to issue
comments by lr-71f467, lr-8ea5 distinct exception classes): posts exactly
one issue comment, then reads back the PR's issue comments to confirm a
comment authored by the caller's OWN resolved login, with the posted body,
created at or after the moment immediately before this POST was issued
(freshness anchor, small clock-skew tolerance) exists on the correct PR. A
bare login+body substring match can in principle be satisfied by a stale
comment from an earlier invocation with overlapping body text; anchoring on
the freshness timestamp closes that gap — this is the SAME anchor shape
transport.git_host_api.verify_comment_on_pr already uses for the Forgejo
side (that function's `not_before` parameter), reused here rather than
reinvented.

_github_request routes through transport.github_client.request_json()
(post-Wave-B extraction, lr-e1f9) in "content_type" parse mode — this is the
one verb backend of the three whose GitHub calls (GET /user) can
legitimately return a non-JSON body on a success path, hence the
Content-Type-gated parse the other two backends never needed; see that
module's docstring "WHAT DID NOT MOVE" section. The User-Agent header this
module has always sent is passed through via request_json's extra_headers
seam — never dropped by the extraction. GET /app is NEVER called by this
backend (lr-d31e) — it is JWT-only and this backend never holds a JWT.

CLI-first / stdlib only, via transport.github_client (urllib, json under the
hood). No new dependencies.

delete_own_comment (lr-e2ce66, GitHub-side parity with transport.
git_host_api.delete_own_comment): self-delete-own-ISSUE-comment, a distinct
endpoint shape from this module's PR-review machinery above (DELETE
/repos/{owner}/{repo}/issues/comments/{comment_id}, not a review). Belt-and-
suspenders, mirroring the Forgejo side's admissible-operation rule
(operator-agreed): delete a comment IFF (author login == the caller's OWN
resolved bot identity, via resolve_own_login — the SAME token-type-aware
identity resolution post_and_verify_review already uses) AND (comment body
carries NO fenced ```review-result``` block, re-parsed via merge.verdict.
parse_verdict_block — the SAME single-source-of-truth parser the Forgejo
side and the merge gate itself use, never a bespoke regex). Before issuing
DELETE: (a) GET the comment, (b) assert author-login match (refuse
otherwise), (c) assert no verdict fence (refuse otherwise), (d) DELETE.
GitHub independently gates delete on authorship-or-admin at the API layer
(identity-of-token) — this tool-side check is defense-in-depth on top of
that platform enforcement, not a replacement for it.

comment_id digit-only constraint (lr-26f774, a pre-merge security review
finding on PR #53): both get_issue_comment and delete_own_comment interpolate
comment_id directly into a REST URL path. The Forgejo side
(transport.git_host_api._ISSUE_COMMENT_ID_RE) constrains the equivalent
path segment to digits-only at the CLI argv-parsing layer, before a
comment_id ever reaches transport.git_host_api.get_comment/
delete_own_comment. This module has no CLI entry point wiring the GitHub
delete into argv today (review.verb never exposes it), so this is
currently unreachable from any argv — but a caller could still pass a
non-numeric comment_id (e.g. sourced from unsanitized upstream data) once
such an entry point exists. _validate_comment_id mirrors
_ISSUE_COMMENT_ID_RE's digit-only shape exactly (accepts str or int;
rejects empty, non-digit, or signed/float-shaped input) and is called
BEFORE comment_id is interpolated into any URL in both functions below --
belt-and-suspenders defense-in-depth, not a live exploit fix.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from clagentic_loadout.merge.verdict import parse_verdict_block
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.review.contract import VerifiedReview
from clagentic_loadout.review.errors import (
    DeleteOwnCommentRefusedError,
    PlatformMismatchError,
    ReviewPostError,
    ReviewVerifyError,
)
from clagentic_loadout.transport.github_app_config import (
    GithubAppSlugNotConfiguredError,
    resolve_github_app_slug,
)
from clagentic_loadout.transport.github_client import GITHUB_API_BASE, request_json
from clagentic_loadout.transport.redirect_guard import no_redirect_opener

#: The real public GitHub API base URL — brand-neutral, not an operator
#: host; every GitHub deployment consuming this backend talks to this same
#: endpoint (GitHub Enterprise Server users pass their own base via a future
#: config seam if/when that becomes a real caller — not invented here
#: speculatively). Re-exports transport.github_client.GITHUB_API_BASE under
#: this module's pre-existing private name so no caller of this module
#: (including its own test suite, which references `_GITHUB_API` directly)
#: has to change.
_GITHUB_API = GITHUB_API_BASE

#: Freshness-anchor clock-skew tolerance for the post-and-verify readback
#: (lr-71f467), mirroring transport.git_host_api._FRESHNESS_SKEW_TOLERANCE_
#: SECONDS exactly -- absorbs ordinary clock drift between this process and
#: GitHub's API without opening a window wide enough to match a genuinely
#: stale/pre-existing comment.
_FRESHNESS_SKEW_TOLERANCE_SECONDS = 5

#: Digit-only comment_id constraint (lr-26f774), mirroring transport.
#: git_host_api._ISSUE_COMMENT_ID_RE's `\d+` shape exactly. GitHub's own
#: issue-comment ids are always positive decimal integers; this rejects any
#: non-digit content (including a leading sign, decimal point, or embedded
#: path separator) BEFORE comment_id is interpolated into a URL, matching
#: the Forgejo side's belt-and-suspenders posture on the parity path.
_ISSUE_COMMENT_ID_RE = re.compile(r"^\d+$")


def _parse_github_timestamp(raw: str) -> "datetime | None":
    """Parse a GitHub API timestamp (RFC 3339 / ISO 8601) into an aware UTC
    datetime, mirroring transport.git_host_api._parse_git_host_timestamp
    exactly (lr-71f467, freshness-anchor precedent for the issue-comment
    transport). Returns None if missing/unparseable -- callers must treat
    that as "cannot confirm freshness", not as a pass.
    """
    if not raw:
        return None
    try:
        normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_comment_id(comment_id: "int | str") -> str:
    """Reject a non-digit-only comment_id BEFORE it is interpolated into
    any GitHub API URL path (lr-26f774, defense-in-depth parity with
    transport.git_host_api._ISSUE_COMMENT_ID_RE's digit-only constraint).

    Accepts an int (always digit-shaped once stringified, since Python
    ints have no sign in `str()` for non-negative values -- a negative int
    fails the digit-only check below exactly like a non-numeric string
    would) or a str. Returns the validated value as a str for URL
    interpolation.

    Raises DeleteOwnCommentRefusedError -- this module's existing
    refused-before-any-I/O error class -- on any non-digit-only value, with
    a resolved-values message naming the rejected comment_id.
    """
    comment_id_str = str(comment_id)
    if not _ISSUE_COMMENT_ID_RE.match(comment_id_str):
        raise DeleteOwnCommentRefusedError(
            f"comment_id {comment_id!r} is not digit-only -- refusing to "
            f"interpolate it into a GitHub API URL path. GitHub issue "
            f"comment ids are always positive decimal integers; a "
            f"non-numeric value could inject an unexpected path segment."
        )
    return comment_id_str


def assert_platform_is_github(
    owner: str,
    repo: str,
    *,
    explicit_platform: str,
) -> None:
    """Host-keyed guard for the GitHub review transport.

    Callers MUST invoke this BEFORE minting any credential or making any API
    call. *explicit_platform* is a mandatory keyword argument — there is no
    optional/no-op form that could be silently skipped by omitting it; the
    caller (review.verb) resolves platform independently (e.g. via
    platform_detect.resolve_platform() against the target PR's own remote
    URL) and passes the result in.

    Owner/repo alone is NEVER an acceptable substitute signal for platform —
    the same owner/namespace value can exist on both GitHub and a
    self-hosted Forgejo instance.

    Raises PlatformMismatchError immediately when *explicit_platform* is not
    GitHub, so a wrong-platform call fails fast and locally instead of
    reaching the wrong host's API and returning an opaque 4xx/422.
    """
    if explicit_platform not in (PLATFORM_GITHUB, PLATFORM_FORGEJO):
        raise PlatformMismatchError(
            f"assert_platform_is_github: unrecognized platform "
            f"{explicit_platform!r} for {owner}/{repo}. Expected "
            f"{PLATFORM_GITHUB!r} or {PLATFORM_FORGEJO!r}."
        )
    if explicit_platform != PLATFORM_GITHUB:
        raise PlatformMismatchError(
            f"WRONG PLATFORM -- {owner}/{repo} PR resolves to platform "
            f"{explicit_platform!r}, but this backend only posts to GitHub. "
            f"Use the Forgejo backend instead. Refusing before minting any "
            f"credential or making any API call."
        )


def _github_request(
    method: str,
    url: str,
    token: str,
    payload: dict | None = None,
    *,
    accept: str = "application/vnd.github+json",
    user_agent: str = "clagentic-loadout-review/1.0",
    opener=None,
) -> tuple[int, dict | list | str]:
    """Make a GitHub API request with a bearer token.

    Thin wrapper over transport.github_client.request_json() in
    "content_type" parse mode — this backend's GET /user call can
    legitimately return a non-JSON body on a success path, so parsing is
    gated on the response's Content-Type header, matching this module's
    pre-extraction behavior exactly (the other two verb backends never
    needed this check — see transport.github_client's docstring).

    Redirect hardening (lr-412f pre-merge security review finding):
    request_json() defaults to transport.redirect_guard.no_redirect_opener()
    rather than bare urlopen — see that module's docstring. `opener_factory`
    is passed through as this module's own `no_redirect_opener` import so
    this name stays monkeypatchable at
    `clagentic_loadout.review.github_backend.no_redirect_opener`, the target
    test_review_github_backend.py's redirect-hardening coverage patches.

    `opener` injects a urllib opener's .open callable for tests — no real
    network call is ever made when a fake opener is supplied.
    """
    return request_json(
        method, url, token, payload,
        accept=accept,
        extra_headers={"User-Agent": user_agent},
        parse_mode="content_type",
        opener=opener,
        opener_factory=no_redirect_opener,
    )


def _resolve_own_login_from_configured_slug(caller: str | None) -> str | None:
    """Config-first short-circuit (lr-b2d1c3): when a `github_app.slugs`
    entry IS configured for *caller*, resolve the bot login
    DETERMINISTICALLY as '<slug>[bot]' WITHOUT issuing any request. Returns
    None when no slug resolves for *caller* (either *caller* is falsy, or
    resolve_github_app_slug has nothing configured for it) — the caller of
    this helper falls through to the GET /user probe in that case, exactly
    as before this change.

    Only consulted when *caller* is supplied: an omitted caller has no
    per-caller slug to short-circuit on, so the pre-existing GET /user-first
    behavior is unaffected for that call shape.
    """
    if not caller:
        return None
    try:
        slug = resolve_github_app_slug(caller=caller)
    except GithubAppSlugNotConfiguredError:
        return None
    return f"{slug}[bot]"


def resolve_own_login(token: str, *, caller: str | None = None, opener=None) -> str:
    """Resolve the caller's own bot login from the token, token-type-aware
    (lr-d31e, fixing the lr-8ea5 port's JWT-only-GET-/app defect).

    GitHub has two distinct token shapes this backend must support:
      - PAT / OAuth (user-context) tokens: GET /user returns the caller's
        own login directly.
      - GitHub App INSTALLATION tokens: GET /user returns 403 by design —
        that endpoint requires user-context auth, which an installation
        token is not. There is NO live-lookup fallback for this token type:
        GET /app is JWT-only (Authorization: Bearer <RS256 app JWT>), and
        this backend never holds anything but the installation token
        (Authorization: token <installation-token>) — a live /app call 401s
        deterministically for this credential type; it can never succeed.
        Instead, the bot login is resolved DETERMINISTICALLY from deployment
        config as '<app-slug>[bot]' (GitHub's own documented App-bot-login
        convention) via transport.github_app_config.resolve_github_app_slug.

    *caller* (lr-d72d) is the OPTIONAL role/caller string (e.g. the same
    value review.verb's --caller resolves) forwarded to
    resolve_github_app_slug so a role-scoped deployment running multiple
    GitHub Apps (builder/reviewer/security/merger) can resolve its OWN app
    slug via github_app.slugs.<caller> rather than the single global slug
    value serving only one of them correctly. Omitting *caller* (the default)
    is byte-identical to pre-lr-d72d behavior — the single global slug tier.

    CONFIG-FIRST SHORT-CIRCUIT (lr-b2d1c3): when *caller* is supplied AND a
    `github_app.slugs.<caller>` entry is configured, the bot login is
    resolved DETERMINISTICALLY from that slug BEFORE any GET /user call is
    ever made — GitHub App INSTALLATION tokens always 403 on GET /user (see
    above), so for a deployment that has already declared its slug, the
    probe is a guaranteed-403 wasted API call with deterministic error noise
    on every reviewer post (lr-e41f). The GET /user path is kept as the
    fallback ONLY for token types where it can succeed (PAT/OAuth
    user-context) or when no slug is configured for *caller* — this is a
    reordering of the existing token-type-aware behavior (lr-d31e), not a
    change to which credential types resolve which way.

    When no configured slug short-circuits the call (no *caller* supplied,
    or *caller* has no matching slugs entry), falls through to GET /user
    first (the PAT/OAuth path). On HTTP 403 specifically — the documented
    response when an App installation token calls an endpoint that requires
    user-context auth — resolves the configured app slug (the single-global
    tier, since a per-caller entry would already have short-circuited above)
    instead of attempting any further live lookup. Any other /user failure
    (401, 5xx, network) is NOT assumed to be an App-token/endpoint-mismatch
    case and is reported as-is; only 403 triggers the config-based
    resolution, since that is the specific, documented condition of "this
    token cannot use this endpoint" rather than "this token is invalid or
    absent".

    When /user 403s and no app slug is configured, this fails closed with a
    resolved-values error naming BOTH config seams that would fix it — a
    credential-type-determined 403 is reported as exactly that, never
    implied to be reachable via a further live call.
    """
    configured_login = _resolve_own_login_from_configured_slug(caller)
    if configured_login is not None:
        return configured_login

    status, body = _github_request("GET", f"{_GITHUB_API}/user", token, opener=opener)
    if status == 200 and isinstance(body, dict) and body.get("login"):
        return body["login"]

    if status == 403:
        try:
            # caller is forwarded ONLY when supplied (lr-d72d): every
            # pre-lr-d72d test/call site monkeypatches or invokes
            # resolve_github_app_slug with ZERO arguments, so an
            # unconditional caller=caller kwarg here would break that
            # existing zero-arg call shape even when caller is None. A
            # per-caller slug would already have short-circuited above
            # (_resolve_own_login_from_configured_slug), so reaching here
            # with a non-empty caller means only the single-global-slug
            # tier (or nothing) is left to resolve.
            slug = resolve_github_app_slug(caller=caller) if caller else resolve_github_app_slug()
        except GithubAppSlugNotConfiguredError as exc:
            body_snippet = str(body)[:200] if body else "<empty>"
            raise ReviewVerifyError(
                f"post_and_verify FAILED -- identity resolution could not "
                f"determine the caller's own bot login: GET /user returned "
                f"HTTP 403, body: {body_snippet!r}. This is the documented, "
                f"expected response when the token in use is a GitHub App "
                f"INSTALLATION token -- that credential type cannot call "
                f"GET /user, and it cannot use GET /app either (that "
                f"endpoint is JWT-only; an installation token 401s there "
                f"deterministically, so no live fallback lookup exists for "
                f"this credential type). The bot login must instead be "
                f"resolved from a configured app slug, but none is "
                f"configured: {exc}"
            ) from exc
        return f"{slug}[bot]"

    raise ReviewVerifyError(
        f"post_and_verify FAILED -- identity resolution could not determine "
        f"the caller's own bot login: GET /user returned HTTP {status} "
        f"(neither a 200 success nor the expected App-token 403). Cannot "
        f"verify review authorship without a resolved login."
    )


def _find_existing_own_comment(
    comments: list,
    *,
    own_login: str,
    body: str,
) -> "VerifiedReview | None":
    """Read-back-before-post idempotency check (lr-39f8, structural dedupe;
    ported onto the issue-comment transport by lr-71f467).

    Scans an already-fetched *comments* list (GET issues/{pr}/comments) for a
    comment authored by *own_login* whose body is EXACTLY *body* (equality,
    never substring -- a substring match would let a shorter retried body
    spuriously match a longer, unrelated comment, and would let a
    changed/expanded re-review spuriously match a stale one). Returns the
    newest such match as a VerifiedReview, or None if no match exists.

    Gate is body-identity: a caller posting a genuinely CHANGED body (e.g. a
    later re-review with different findings) never matches here and always
    proceeds to a fresh POST -- this check only suppresses an exact repost of
    a comment this same caller already landed.

    Scans newest-first so a caller retried multiple times (the PR#318
    evidence: three identical POSTs 9-27s apart) converges on the FIRST
    comment that ever landed being returned consistently, once dedupe takes
    effect from the second call onward.
    """
    for comment in reversed(comments):
        comment_login = comment.get("user", {}).get("login", "")
        comment_body = comment.get("body", "")
        if comment_login == own_login and comment_body == body:
            return VerifiedReview(
                id=comment.get("id"),
                url=comment.get("html_url", ""),
                login=own_login,
                body=comment_body,
            )
    return None


def post_and_verify_review(
    owner: str,
    repo: str,
    pr_number: int,
    body: str,
    token: str,
    *,
    caller: str | None = None,
    opener=None,
) -> VerifiedReview:
    """Read-back-before-post idempotency (lr-39f8), then POST an ISSUE
    COMMENT (lr-71f467 -- NOT a GitHub native PR review; see this module's
    docstring "VERDICT-TRANSPORT PARITY" section) and READ BACK to confirm it
    landed under the caller's own bot login on the correct PR, at or after
    the moment immediately before the POST was issued (freshness anchor).

    DEDUPE (lr-39f8): BEFORE ever issuing the POST, this lists the PR's
    existing issue comments and checks for one already authored by the
    caller's own resolved login, with the EXACT same body (see
    _find_existing_own_comment). If found, that existing comment is returned
    immediately and NO new POST is issued -- this is the structural fix for
    the triple-post defect a retried reviewer invocation produced on a live
    PR (three identical 'blocking' posts 9-27s apart): neither transport
    (native review nor issue comment) carries a server-assigned
    invocation_id, so an AGENT.md prose convention alone cannot cover this
    path; idempotency has to live in the tool. A body CHANGE is never
    matched (exact equality, not substring) -- a legitimate later re-review
    with different findings still posts fresh, unaffected by this check.

    *caller* (lr-d72d, optional, keyword-only, defaults to None) is forwarded
    to resolve_own_login's app-slug resolution — see that function's
    docstring. Omitting it is byte-identical to pre-lr-d72d behavior.

    Freshness anchor (lr-622e precedent, generalized to issue comments by
    lr-71f467): the readback match requires the listed comment's created_at
    to be at or after the timestamp captured immediately before this POST was
    issued (minus a small clock-skew tolerance), in addition to login/body.
    A bare login+body substring match can in principle be satisfied by a
    stale comment from an earlier invocation with overlapping body text;
    anchoring on freshness closes that gap -- the SAME anchor shape
    transport.git_host_api.verify_comment_on_pr already uses for the Forgejo
    side.

    Returns a VerifiedReview sourced from a READBACK (either the pre-post
    dedupe check or the post-POST readback), never from the POST response
    alone. `VerifiedReview.body` (lr-482c20) carries the READBACK's own body
    text — a mandatory-verdict caller (review.verb) re-parses this field,
    never the pre-POST string, to confirm a fence landed byte-identical.

    Raises:
        ReviewPostError: the POST itself never landed — a non-2xx response.
            Callers MUST treat this as `blocked`, never as a partial
            success.
        ReviewVerifyError: the POST succeeded but identity resolution or the
            readback cannot confirm a comment with the posted body landed
            under the caller's own login on the correct PR, fresh enough to
            be this invocation's own post. Distinguishing this from
            ReviewPostError matters: a verify-phase failure must never be
            reported as a post failure — the post already succeeded, so
            retrying the POST is not the fix.
    """
    full_repo = f"{owner}/{repo}"
    comments_url = f"{_GITHUB_API}/repos/{full_repo}/issues/{pr_number}/comments"

    # Identity resolution runs BEFORE the dedupe readback, since the dedupe
    # check itself needs own_login to know which existing comment (if any) is
    # "ours". This is a pure GET-driven identity lookup -- no POST has
    # happened yet at this point, so a failure here is a ReviewVerifyError
    # exactly like the post-POST identity failure this function already
    # raised before the dedupe check existed (no new failure mode).
    own_login = resolve_own_login(token, caller=caller, opener=opener)

    dedupe_status, dedupe_comments = _github_request("GET", comments_url, token, opener=opener)
    if dedupe_status == 200 and isinstance(dedupe_comments, list):
        existing = _find_existing_own_comment(dedupe_comments, own_login=own_login, body=body)
        if existing is not None:
            return existing
    # A non-200/non-list readback here is NOT fatal to the dedupe check --
    # falling through to a normal POST is safe (worst case: a duplicate that
    # the mandatory post-POST readback below still verifies as landed). The
    # dedupe check is an optimization on top of the mandatory post-and-verify
    # contract, never a replacement for it.

    pre_post_utc = datetime.now(timezone.utc)
    payload = {"body": body}
    status, resp = _github_request("POST", comments_url, token, payload=payload, opener=opener)
    if not (200 <= status < 300):
        msg = resp.get("message", "") if isinstance(resp, dict) else ""
        raise ReviewPostError(
            f"POST issue comment FAILED -- HTTP {status}"
            + (f": {msg}" if msg else "")
            + f" ({full_repo}#{pr_number})."
        )

    # own_login was already resolved above for the pre-post dedupe check --
    # never re-resolved here, so a caller's opener only ever sees ONE /user
    # call per post_and_verify_review invocation, on both the dedupe-hit and
    # dedupe-miss paths.
    status, comments = _github_request("GET", comments_url, token, opener=opener)
    if status != 200 or not isinstance(comments, list):
        raise ReviewVerifyError(
            f"post_and_verify FAILED -- GET {comments_url} returned HTTP "
            f"{status} (or non-list body) during readback. Cannot confirm "
            f"the comment landed on the correct PR."
        )

    not_before_with_tolerance = pre_post_utc.timestamp() - _FRESHNESS_SKEW_TOLERANCE_SECONDS
    stale_candidates: list = []
    for comment in reversed(comments):
        comment_login = comment.get("user", {}).get("login", "")
        comment_body = comment.get("body", "")
        if not (comment_login == own_login and body in comment_body):
            continue
        created_at = _parse_github_timestamp(comment.get("created_at", ""))
        if created_at is None or created_at.timestamp() < not_before_with_tolerance:
            stale_candidates.append(comment.get("id"))
            continue
        return VerifiedReview(
            id=comment.get("id"),
            url=comment.get("html_url", ""),
            login=own_login,
            body=comment_body,
        )

    if stale_candidates:
        raise ReviewVerifyError(
            f"post_and_verify MISMATCH -- {len(stale_candidates)} comment(s) "
            f"by {own_login!r} matched the posted body but FAILED the "
            f"freshness anchor (created_at older than "
            f"not_before={pre_post_utc.isoformat()!r}, stale comment "
            f"ids={stale_candidates}). A stale/pre-existing comment with "
            f"overlapping body text cannot satisfy this post's verify. "
            f"Gate-pass REFUSED."
        )

    raise ReviewVerifyError(
        f"post_and_verify MISMATCH -- no comment by {own_login!r} with the "
        f"posted body was found in GET {comments_url}. The comment may have "
        f"landed on the wrong PR, failed silently, or posted under a "
        f"different identity. Gate-pass REFUSED."
    )


def get_issue_comment(
    owner: str,
    repo: str,
    comment_id: "int | str",
    token: str,
    *,
    opener=None,
) -> dict:
    """GET the single issue comment at issues/comments/<comment_id>
    (lr-e2ce66). Belt-and-suspenders step (a) for delete_own_comment: reads
    the comment's current author login and body BEFORE any DELETE is
    issued, so the checks below are evaluated against live comment state,
    never a caller-supplied guess.

    Raises DeleteOwnCommentRefusedError if comment_id is not digit-only
    (lr-26f774, checked BEFORE any I/O -- see _validate_comment_id) or if
    the GET does not return HTTP 200 with a JSON object body — a comment
    that cannot be read cannot be safely deleted under this contract.
    """
    comment_id = _validate_comment_id(comment_id)
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/issues/comments/{comment_id}"
    status, body = _github_request("GET", url, token, opener=opener)
    if status != 200 or not isinstance(body, dict):
        body_snippet = str(body)[:200] if body else "<empty>"
        raise DeleteOwnCommentRefusedError(
            f"delete-own-comment REFUSED -- GET {url} returned HTTP {status}, "
            f"body: {body_snippet!r}. Cannot confirm authorship/verdict-fence "
            f"status of a comment that cannot be read."
        )
    return body


def delete_own_comment(
    owner: str,
    repo: str,
    comment_id: "int | str",
    token: str,
    *,
    caller: str | None = None,
    opener=None,
) -> None:
    """Belt-and-suspenders self-delete-own-ISSUE-comment (lr-e2ce66),
    GitHub-side parity with transport.git_host_api.delete_own_comment.

    ADMISSIBLE OPERATION (operator-agreed): delete a comment IFF (author
    login == the caller's OWN bot identity, resolved via resolve_own_login)
    AND (comment body contains NO fenced ```review-result``` block, via
    merge.verdict.parse_verdict_block). No crew agent may delete another
    author's comment (cross-author delete is an audit-tampering/censorship
    surface, refused unconditionally — human-comment removal stays an
    operator action outside this tool entirely). Even a self-authored
    comment carrying a landed verdict fence is refused, so a caller can
    never game the merge-gate re-read by posting clean, letting it be read,
    deleting it, and reposting.

    Order of operations, BEFORE issuing DELETE:
      (a) GET the comment (get_issue_comment) -- resolves its live author
          login and body.
      (b) Resolve the caller's OWN bot login (resolve_own_login, the SAME
          token-type-aware identity resolution post_and_verify_review
          already uses) and assert the comment's author login matches it
          exactly. Refuse otherwise.
      (c) Re-parse the comment body for a fenced ```review-result``` block
          via merge.verdict.parse_verdict_block. Refuse if a fence is
          found.
      (d) Only then issue DELETE.

    *caller* (optional, keyword-only) is forwarded to resolve_own_login's
    app-slug resolution exactly like post_and_verify_review's own *caller*
    parameter — see that function's docstring.

    GitHub independently gates delete on authorship-or-admin at the API
    layer (identity-of-token) -- this tool-side check is defense-in-depth
    on top of that platform enforcement, not a replacement for it.

    Raises DeleteOwnCommentRefusedError on any refusal: comment_id is not
    digit-only (lr-26f774, checked BEFORE any I/O -- see
    _validate_comment_id), unreadable comment, cross-author, or
    verdict-fence-present. A non-2xx DELETE response is raised as
    ReviewPostError -- distinct from a refused-before-any-I/O case, since
    the belt-and-suspenders checks already passed at that point.
    """
    comment_id = _validate_comment_id(comment_id)
    comment = get_issue_comment(owner, repo, comment_id, token, opener=opener)
    comment_login = comment.get("user", {}).get("login", "")
    comment_body = comment.get("body", "")

    own_login = resolve_own_login(token, caller=caller, opener=opener)
    if comment_login != own_login:
        raise DeleteOwnCommentRefusedError(
            f"delete-own-comment REFUSED -- comment {comment_id!r} on "
            f"{owner}/{repo} is authored by {comment_login!r}, not the "
            f"caller's own bot login {own_login!r}. No crew agent may "
            f"delete another author's comment (cross-author delete is an "
            f"audit-tampering/censorship surface). DELETE not issued."
        )

    if parse_verdict_block(comment_body) is not None:
        raise DeleteOwnCommentRefusedError(
            f"delete-own-comment REFUSED -- comment {comment_id!r} on "
            f"{owner}/{repo} carries a fenced ```review-result``` block. "
            f"Deleting a landed verdict could game the merge-gate re-read "
            f"(post clean, get read, delete, repost) -- even a caller's own "
            f"verdict comment is never eligible for delete. DELETE not "
            f"issued."
        )

    url = f"{_GITHUB_API}/repos/{owner}/{repo}/issues/comments/{comment_id}"
    status, resp = _github_request("DELETE", url, token, opener=opener)
    if not (200 <= status < 300):
        msg = resp.get("message", "") if isinstance(resp, dict) else ""
        raise ReviewPostError(
            f"DELETE {url} FAILED -- HTTP {status}"
            + (f": {msg}" if msg else "")
            + f" (comment {comment_id!r}, {owner}/{repo})."
        )


class GithubReviewBackend:
    """ReviewBackend Protocol implementation for GitHub.

    Constructed with a resolved token (the caller mints/resolves it through
    its own credential seam before constructing this backend — this class
    does not own token minting, mirroring the Forgejo backend's use of
    transport.credential_provider). `caller` (lr-d72d, optional) is the
    role/caller string used ONLY for the app-slug identity-resolution seam
    (transport.github_app_config's per-caller `slugs` map) — never plumbed
    into the ReviewBackend.post_and_verify Protocol signature itself, since
    it is already known at construction time (review.verb.build_backend
    resolves --caller before building the backend). `opener` injects a
    urllib opener's .open callable for tests.
    """

    def __init__(self, token: str, *, caller: str | None = None, opener=None) -> None:
        self._token = token
        self._caller = caller
        self._opener = opener

    def post_and_verify(
        self,
        *,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> VerifiedReview:
        return post_and_verify_review(
            owner, repo, pr_number, body, self._token,
            caller=self._caller, opener=self._opener,
        )

    def delete_own_comment(
        self,
        *,
        owner: str,
        repo: str,
        comment_id: "int | str",
    ) -> None:
        """Belt-and-suspenders self-delete-own-ISSUE-comment (lr-e2ce66) --
        see the module-level delete_own_comment() function for the full
        admissible-operation contract."""
        delete_own_comment(
            owner, repo, comment_id, self._token,
            caller=self._caller, opener=self._opener,
        )


__all__ = [
    "GithubReviewBackend",
    "assert_platform_is_github",
    "delete_own_comment",
    "get_issue_comment",
    "post_and_verify_review",
    "resolve_own_login",
]
