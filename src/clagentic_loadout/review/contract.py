"""review.contract — the shared review-post-and-verify seam.

Wave B slice 2 (lr-412f, tome #688). Ported from the reference two-caller
GitHub review transport (lr-c353/lr-622e/lr-8ea5) plus the already-landed
Forgejo post-and-verify
path (clagentic_loadout.transport.git_host_api, Wave B slice 1). The reference
copy stays primary until its separate CUT OVER + RETIRE + VERIFY-GONE task
per the migration plan.

ONE CONTRACT, TWO TRANSPORTS. ``ReviewBackend`` is the seam both the Forgejo
transport (transport.git_host_api's post-and-verify path, wrapped by
review.forgejo_backend) and the GitHub transport (review.github_backend,
the real new content in this slice) satisfy. review.verb — the CLI entry
point — depends only on this Protocol; it never branches on which platform
it is talking to beyond selecting *which* backend instance to call.

Every ``post_and_verify`` implementation:
  1. Posts exactly ONE review/comment.
  2. Performs a mandatory readback confirming a review/comment authored by
     the CALLER'S OWN resolved identity (never a hardcoded name) exists on
     the correct PR, anchored to the post that was just made (never a
     stale/pre-existing match).
  3. Raises ReviewPostError when the post itself never lands, and
     ReviewVerifyError when the post succeeded but the readback cannot
     confirm it — the two failure classes stay distinct across BOTH
     transports (lr-8ea5) so a caller's failure-handling code is uniform.
  4. Returns a VerifiedReview sourced from the READBACK, never from the
     POST response alone.

SCOPE BOUNDARY (task lr-412f, restated per the source module's own
NAMING TRADE-OFF section): this contract does NOT emit or parse the fenced
```review-result``` verdict block. That shape is asserted by the merge-gate
verb (a later loadout slice), not here. review-post's job ends at "posted
and verified" — the verdict content is caller-supplied prose, opaque to this
module.

ROLE, NOT AGENT NAME: nothing in this module or its callers hardcodes an
acting identity. The caller/role is always an input (a --caller flag or
config value resolved through transport.credential_provider.TokenProvider),
never a name baked into the code path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class VerifiedReview:
    """The confirmed result of a post_and_verify call, sourced from the
    READBACK step — never from the POST response alone. A 2xx POST response
    proves only that the HTTP transaction completed; `id`/`url`/`login` here
    are read back from the platform's own list/comments endpoint, which is
    the property that closes the "posted but never actually landed" gap
    (the source module's PR309/PR312 finding).

    `body` (lr-482c20) is the VERIFIED comment/review's own body text, as
    read back from the platform — never the locally-constructed string this
    process posted. A mandatory-verdict caller (review.verb's
    --verdict-review-status route) re-parses THIS field, not the pre-POST
    string, so a fence that landed truncated or mangled in transit is caught
    here rather than surfacing later as an opaque merge-gate refusal (the
    same mirror-verification property transport.git_host_api's
    --expect-verdict-block already established for the Forgejo-only path —
    see that module's verify_verdict_block)."""

    id: "int | str"
    url: str
    login: str
    body: str = ""


@runtime_checkable
class ReviewBackend(Protocol):
    """The one contract both transports satisfy. review.verb depends only on
    this signature — never on a transport-specific exception type, request
    shape, or identity-resolution mechanism."""

    def post_and_verify(
        self,
        *,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
    ) -> VerifiedReview:
        """Post exactly one review/comment to the given PR and confirm, via
        a mandatory readback, that it landed under the caller's own resolved
        identity.

        Raises:
            ReviewPostError: the post itself never landed.
            ReviewVerifyError: the post landed but the readback could not
                confirm it.
        """
        ...

    def delete_own_comment(
        self,
        *,
        owner: str,
        repo: str,
        comment_id: "int | str",
    ) -> None:
        """Belt-and-suspenders self-delete-own-comment (lr-e2ce66, platform
        parity closed by lr-f43c4b): delete a single already-posted comment
        IFF it was authored by the caller's own resolved identity AND its
        body carries no fenced ```review-result``` verdict block. Both
        backends (GitHub: review.github_backend.delete_own_comment; Forgejo:
        review.forgejo_backend delegating to transport.git_host_api.
        delete_own_comment) run the identical admissible-operation checks —
        see either implementation's own docstring for the full
        GET-then-assert-then-DELETE order of operations.

        Raises:
            DeleteOwnCommentRefusedError: the delete was refused before any
                DELETE was issued (unreadable comment, cross-author, or a
                verdict fence present).
            ReviewPostError: the belt-and-suspenders checks passed but the
                DELETE call itself returned a non-2xx response.
        """
        ...


def validate_review_body_stdin_content(raw_bytes: bytes) -> str:
    """Validate --body-stdin content and return the extracted review body
    string. Shared by both backends (and the verb CLI) so the pre-flight
    cannot drift between platforms — the staging contract on both platforms
    is the same ``{"body": "<prose>"}`` JSON shape.

    Raises ReviewBodyStdinEmptyError — BEFORE any network call is made — on:
      - zero-byte input
      - content that is not valid JSON
      - valid JSON that is not an object
      - a JSON object missing a 'body' key, or where 'body' is not a
        non-empty string (after stripping whitespace)

    On success, returns the extracted 'body' string — callers never need a
    second parse of the same bytes (no TOCTOU between validation and the
    value actually posted).
    """
    # Local import: keeps this module importable with zero cost when only
    # the Protocol/dataclass shapes are needed (e.g. by a type-checking-only
    # consumer), and avoids a module-level cycle with errors.py's own
    # from __future__ import annotations usage.
    from clagentic_loadout.review.errors import ReviewBodyStdinEmptyError
    import json

    if len(raw_bytes) == 0:
        raise ReviewBodyStdinEmptyError(
            "--body-stdin received empty input (0 bytes). Posting this would "
            "send an empty review body, which both platforms reject."
        )
    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReviewBodyStdinEmptyError(
            f"--body-stdin does not contain valid JSON: {exc}. Expected "
            f'{{"body": "<review text>"}}.'
        ) from exc
    if not isinstance(parsed, dict):
        raise ReviewBodyStdinEmptyError(
            f"--body-stdin must contain a JSON OBJECT with a 'body' key; got "
            f"{type(parsed).__name__}."
        )
    body_value = parsed.get("body")
    if not isinstance(body_value, str) or not body_value.strip():
        raise ReviewBodyStdinEmptyError(
            f"--body-stdin has no non-empty 'body' string field (got "
            f"{body_value!r}). A non-empty review body is required."
        )
    return body_value


def validate_review_verdict_body_stdin_content(raw_bytes: bytes) -> tuple[str, str]:
    """Validate --body-stdin content for the --verdict-review-status route
    (lr-482c20) and return (prose, review_status).

    The verdict-post route's stdin JSON carries the SAME 'body' field as an
    ordinary review post, PLUS a structured 'review_status' field ('clean'
    or 'blocking') — the one field this tool cannot derive on the caller's
    behalf. Mirrors transport.git_host_api.build_expected_verdict_body's
    validation shape exactly (that function is the Forgejo-only precedent
    this route generalizes to both platforms) rather than inventing a
    second validation contract.

    Raises ReviewBodyStdinEmptyError — BEFORE any network call — on every
    condition validate_review_body_stdin_content already raises on, plus a
    missing/invalid 'review_status' field.
    """
    from clagentic_loadout.review.errors import ReviewBodyStdinEmptyError
    import json

    prose = validate_review_body_stdin_content(raw_bytes)
    parsed = json.loads(raw_bytes.decode("utf-8"))
    review_status = parsed.get("review_status")
    if review_status not in ("clean", "blocking"):
        raise ReviewBodyStdinEmptyError(
            f"--verdict-review-status: --body-stdin 'review_status' must be "
            f"'clean' or 'blocking', got {review_status!r}."
        )
    return prose, review_status


def validate_review_findings_body_stdin_content(
    raw_bytes: bytes,
) -> tuple[str, list[dict]]:
    """Validate --body-stdin content for the --verdict-findings route
    (lr-c26110, PRIMARY structured-body-construction mechanism) and return
    (review_status, findings).

    UNLIKE validate_review_verdict_body_stdin_content, this route's stdin
    JSON carries NO 'body' field at all — only 'review_status' ('clean' or
    'blocking') and a 'findings' list (each entry: 'file', 'line',
    'rule_id', 'message'). There is nothing here for a reviewer to author
    free-form prose into: the entire comment body — header, bullets, fence
    — is constructed downstream by merge.verdict.build_findings_verdict_body
    from exactly these structured fields. This is deliberate: a caller
    cannot smuggle a foreign reviewer's narrative or fenced block through
    this route because it accepts no prose field for one to hide inside
    (the operator reframe this task shipped under — 'enforce good behavior
    over blocking bad behavior' — the good path is the only path).

    Raises ReviewBodyStdinEmptyError — BEFORE any network call — on:
      - zero-byte input, non-JSON, or non-object input (same as
        validate_review_body_stdin_content)
      - missing/invalid 'review_status' (must be 'clean' or 'blocking')
      - missing 'findings' key, or 'findings' not a list
      - any findings entry missing 'file', 'line', 'rule_id', or 'message',
        or not itself a JSON object
    """
    from clagentic_loadout.review.errors import ReviewBodyStdinEmptyError
    import json

    if len(raw_bytes) == 0:
        raise ReviewBodyStdinEmptyError(
            "--body-stdin received empty input (0 bytes). The "
            "--verdict-findings route requires a JSON object with "
            "'review_status' and 'findings' fields."
        )
    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReviewBodyStdinEmptyError(
            f"--body-stdin does not contain valid JSON: {exc}. Expected "
            f'{{"review_status": "clean"|"blocking", "findings": [...]}}.'
        ) from exc
    if not isinstance(parsed, dict):
        raise ReviewBodyStdinEmptyError(
            f"--body-stdin must contain a JSON OBJECT with 'review_status' "
            f"and 'findings' keys; got {type(parsed).__name__}."
        )

    review_status = parsed.get("review_status")
    if review_status not in ("clean", "blocking"):
        raise ReviewBodyStdinEmptyError(
            f"--verdict-findings: --body-stdin 'review_status' must be "
            f"'clean' or 'blocking', got {review_status!r}."
        )

    findings = parsed.get("findings")
    if not isinstance(findings, list):
        raise ReviewBodyStdinEmptyError(
            f"--verdict-findings: --body-stdin 'findings' must be a JSON "
            f"array, got {type(findings).__name__ if findings is not None else 'missing'}."
        )
    required_keys = ("file", "line", "rule_id", "message")
    for idx, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise ReviewBodyStdinEmptyError(
                f"--verdict-findings: findings[{idx}] must be a JSON "
                f"object, got {type(finding).__name__}."
            )
        missing = [k for k in required_keys if k not in finding]
        if missing:
            raise ReviewBodyStdinEmptyError(
                f"--verdict-findings: findings[{idx}] is missing required "
                f"field(s) {missing}: {finding!r}."
            )

    return review_status, findings


__all__ = [
    "ReviewBackend",
    "VerifiedReview",
    "validate_review_body_stdin_content",
    "validate_review_findings_body_stdin_content",
    "validate_review_verdict_body_stdin_content",
]
