"""review.verb — the review-post CLI: post exactly one review comment and
verify it landed, on either transport, behind one contract.

Wave B slice 2 (lr-412f, tome #688). This is the generic, role-parameterized
review-post verb: WHICH role/caller is acting is CLI input (--caller), never
a hardcoded name. Loadout does not ship agent-named wrapper scripts —
role-to-agent mapping is the consuming project's concern (cast registry),
not loadout's.

Dispatch shape:
  1. Resolve platform (--platform, mandatory — see _build_arg_parser).
  2. Platform guard fires BEFORE any credential mint or API call regardless
     of which platform was selected — for the GitHub path this is
     github_backend.assert_platform_is_github(); for the Forgejo path the
     mirror-image check is inline below (fail fast, same ordering
     guarantee, no gap where a token could be minted for the wrong
     platform).
  3. Resolve the caller's token via transport.credential_provider — the
     SAME seam Wave B slice 1 established; a GitHub App-token minting
     service is a provider implementation of that seam, not something this
     module bakes in.
  4. Build the ReviewBackend for the resolved platform.
  5. --body-stdin is the sole body path (validated via
     review.contract.validate_review_body_stdin_content before any network
     call) — no --body-file back-compat; this verb is born correct per the
     task's --body-stdin-only instruction.
  6. backend.post_and_verify() — exactly one post, mandatory readback.

Verdict-post route (lr-482c20, --verdict-review-status): MANDATORY,
fail-closed emit-and-verify for a reviewer's merge-gate verdict, on EITHER
platform. Sibling of transport.git_host_api's Forgejo-only
--expect-verdict-block (lr-30c0d0) — a security-review pass already used
that path cleanly; this is NOT a rebuild of it, and that flag is UNCHANGED.
This route closes the two gaps that flag leaves open: (a) GitHub has no
tool-owned verdict-fence
emit-and-verify at all (review.github_backend's native PR-review transport
never touched merge.verdict), and (b) a Forgejo reviewer role that goes
through THIS shared verb rather than git-host-api directly had no fence
enforcement either — a plain --body-stdin post with hand-typed prose was
indistinguishable from a real verdict.

When --verdict-review-status is supplied (--verdict-head-sha becomes
mandatory alongside it; --caller supplies the fence's `reviewer` field), this
verb:
  1. CONSTRUCTS the fenced ```review-result``` block internally, in-process,
     via merge.verdict.build_verdict_block (the SAME function the merge
     gate's own re-parse — merge.verdict.read_reviewer_verdict — treats as
     the fence's one authoring source, and the SAME function
     transport.git_host_api's --expect-verdict-block already reuses) — the
     caller's --body-stdin JSON carries only ordinary prose plus the
     structured review_status field; zero backticks ever cross the shell.
     `pr_number` comes from the CLI's own pr_number positional, never
     re-declared in stdin, so the fenced pr_number can never disagree with
     the PR this is actually posted to.
  2. Posts through the ordinary ReviewBackend.post_and_verify() path for the
     resolved platform — no second post/transport implementation; this is
     the same one-contract, two-transport seam every other review-post call
     already goes through.
  3. Re-fetches the LANDED body from the VERIFIED comment/review
     (VerifiedReview.body, sourced from the backend's own mandatory
     readback — never the locally-constructed string) and re-parses it via
     merge.verdict.parse_verdict_block, the IDENTICAL parser the merge gate
     itself uses.
  4. Asserts every field (reviewer, review_status, head_sha, pr_number)
     matches what was requested.
  5. Returns EXIT_OK ONLY when the landed fence verified byte-identical;
     ANY mismatch, or no fence found at all in the readback body, fails
     closed with EXIT_VERDICT_BLOCK_MISMATCH — never an implicit pass. No
     model-side retry (lr-1ce1/lr-fe04, locked): a mismatch is reported to
     the caller, never silently re-attempted by this tool.

There is no optional/skippable lane once --verdict-review-status is present:
--verdict-head-sha is mandatory alongside it (usage error otherwise), and
steps 3-4 above are not gated by any further flag — the emit-and-verify
readback always runs for a verdict post. A caller that wants an ordinary,
non-verdict review comment simply omits --verdict-review-status entirely,
exactly as before this feature.

Structured-findings route (lr-c26110, --verdict-findings): PRIMARY
mechanism, superseding --verdict-review-status's caller-supplied-prose
shape for a reviewer that wants the strongest guarantee. THE REVIEWER NEVER
HANDS THIS TOOL A FREE-FORM BODY: --body-stdin's JSON carries only
'review_status' and a structured 'findings' list (each: file, line,
rule_id, message) — no 'body'/prose field exists on this route at all. This
verb CONSTRUCTS THE ENTIRE comment body — header, one bullet per finding,
then the tool-owned fence — via merge.verdict.build_findings_verdict_body.
A foreign reviewer's narrative CANNOT appear in the posted body because
there is no prose input for one to hide inside: the good path is the only
path (operator reframe, lr-c26110: "enforce good behavior over blocking
bad behavior", same shape as an earlier fix, lr-3b11ab). After the ordinary
post_and_verify readback, the SAME emit-and-verify re-parse
--verdict-review-status already performs runs here too, PLUS
merge.verdict.assert_single_own_verdict_block (the fail-closed backstop):
the landed body must carry EXACTLY ONE fenced block, tagged with this
caller's own reviewer id — a body with zero, more than one, or a
wrongly-tagged block all fail closed with EXIT_VERDICT_BLOCK_MISMATCH.
--verdict-review-status and --verdict-findings are mutually exclusive
(usage error if both given); --verdict-head-sha is mandatory alongside
either.

FOREIGN-BLOCK BACKSTOP (lr-c26110, applies to BOTH verdict routes): the
SECONDARY, fail-closed guard beneath the primary structured-body
mechanism above. Even on the --verdict-review-status route (which still
accepts caller-supplied prose, kept for transition/back-compat), the
emit-and-verify re-parse now also calls assert_single_own_verdict_block on
the landed body — so a free-form prose body that happens to carry a
second, foreign reviewer's block is rejected the same way the structured
route's own body-shape prevents in the first place. This backstop should
shrink/retire as --verdict-findings becomes the sole route.

SCOPE BOUNDARY: outside the --verdict-review-status / --verdict-findings
routes above, this verb does not touch the fenced ```review-result```
verdict block — see review.contract's module docstring.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from clagentic_loadout._version import get_version
from clagentic_loadout.merge.errors import VerdictMalformedError
from clagentic_loadout.merge.verdict import (
    VERDICT_FENCE,
    assert_single_own_verdict_block,
    build_findings_verdict_body,
    build_verdict_block,
    find_all_verdict_blocks,
    parse_verdict_block,
)
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.review.contract import (
    ReviewBackend,
    validate_review_body_stdin_content,
    validate_review_findings_body_stdin_content,
    validate_review_verdict_body_stdin_content,
)
from clagentic_loadout.review.errors import (
    DeleteOwnCommentRefusedError,
    PlatformMismatchError,
    ReviewBodyStdinEmptyError,
    ReviewPostError,
    ReviewVerifyError,
)
from clagentic_loadout.review.forgejo_backend import ForgejoReviewBackend
from clagentic_loadout.review.github_backend import (
    GithubReviewBackend,
    assert_platform_is_github,
)
from clagentic_loadout.transport.attestation import (
    AttestationError,
    resolve_identity as _resolve_identity,
)
from clagentic_loadout.transport.body_env import (
    BODY_ENV_NOT_EPHEMERAL_NOTE,
    BodyEnvError,
    read_body_bytes,
    resolve_caller_body_path,
)
from clagentic_loadout.transport.caller_binding import CallerBindingError, bind_caller
from clagentic_loadout.transport.credential_provider import (
    CredentialProviderError,
    DEFAULT_ROLE,
    TokenProvider,
    resolve_token as _resolve_token,
)
from clagentic_loadout.transport.git_host_api import (
    DEFAULT_GIT_HOST_BASE_URL,
    GIT_HOST_BASE_URL_ENV_VAR,
    _resolve_git_host_base,
)
from clagentic_loadout.transport.provider_config import resolve_platform_provider

# Digit-only comment_id constraint (lr-f43c4b security-review hardening
# finding, same finding class as lr-26f774): --delete-own-comment's value is
# interpolated
# into a REST URL path by BOTH backends (review.github_backend.
# get_issue_comment/delete_own_comment, transport.git_host_api.get_comment/
# delete_own_comment via review.forgejo_backend). The sibling CLI,
# loadout-git-host-api, already anchors its own --delete-own-comment target
# against transport.git_host_api._ISSUE_COMMENT_ID_RE's `\d+` shape at the
# argv layer, before any I/O -- this verb had no equivalent CLI-layer guard,
# relying only on each backend's own defense-in-depth check
# (_validate_comment_id / _ISSUE_COMMENT_ID_RE), which is real but leaves
# review-post inconsistent with its sibling entry point's fail-fast posture.
# Mirrors both backends' own digit-only pattern exactly (accepts only a bare
# positive-decimal-integer string).
_DELETE_COMMENT_ID_RE = re.compile(r"^\d+$")

# ---------------------------------------------------------------------------
# Exit codes -- one reserved range for the review-post verb, distinct from
# transport.git_host_api's own table (this verb wraps git_host_api, it does not
# reuse its exit-code space).
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_TOKEN_FETCH_FAILED = 2
EXIT_WRONG_PLATFORM = 4
EXIT_VERIFY_FAILED = 5
EXIT_POST_FAILED = 6
EXIT_BODY_STDIN_EMPTY = 7
#: --verdict-review-status was supplied but a precondition for constructing
#: the fence is missing/invalid BEFORE any I/O: missing --verdict-head-sha,
#: or --body-stdin JSON missing/malformed review_status. Distinct from
#: EXIT_USAGE so a caller can tell "the verdict-post contract was violated"
#: apart from an ordinary argument-shape mistake (mirrors transport.
#: git_host_api's EXIT_VERDICT_BLOCK_USAGE precedent, lr-30c0d0).
EXIT_VERDICT_BLOCK_USAGE = 8
#: The comment/review landed and passed the ordinary post_and_verify
#: readback, but re-parsing the fenced ```review-result``` block from the
#: VERIFIED body (never the locally-constructed string) did not match what
#: --verdict-review-status requested -- the fence was lost, truncated, or
#: mangled in transit, or is simply absent. The gate is not verified when
#: this fires. No model-side retry (lr-1ce1/lr-fe04, locked): this is a
#: terminal failure this invocation reports, never one it silently
#: re-attempts.
EXIT_VERDICT_BLOCK_MISMATCH = 9
#: --body-env was supplied but the caller-namespaced staged-body path
#: (transport.body_env.resolve_caller_body_path, lr-3a7ae8) is missing, not
#: a regular file, or unreadable -- the caller's harness never staged a
#: body under this --caller's own namespace before invoking this verb
#: (lr-10a996 BODY-TRANSPORT half, mirrors transport.git_host_api's
#: EXIT_BODY_ENV_UNREADABLE).
EXIT_BODY_ENV_UNREADABLE = 10
#: --delete-own-comment was refused BEFORE the DELETE was issued (lr-f43c4b,
#: platform-aware CLI parity with transport.git_host_api's own
#: EXIT_DELETE_OWN_COMMENT_REFUSED): either the belt-and-suspenders GET could
#: not resolve the comment / the caller's own identity, the comment's author
#: does not match the caller's own resolved identity (cross-author delete --
#: an audit-tampering/censorship surface, refused unconditionally), or the
#: comment body carries a fenced ```review-result``` verdict block (deleting
#: a landed verdict could game the merge gate: post clean, get read, delete,
#: repost). Distinct from EXIT_POST_FAILED so a caller can tell "the
#: delete-own-comment authorship/verdict contract was violated" apart from
#: an ordinary transport failure.
EXIT_DELETE_OWN_COMMENT_REFUSED = 11
#: An EXPLICIT --caller value does not match the ATTESTED invoking identity
#: this process's own attestation-provider chain resolved
#: (transport.caller_binding.bind_caller, lr-c75c9a -- the same fail-closed
#: binding transport.git_host_api's EXIT_CALLER_INVOKER_MISMATCH already
#: enforced; this verb now enforces it too). FAILS CLOSED BEFORE ANY I/O --
#: no token mint, no post, no verify readback is ever attempted. An OMITTED
#: --caller never triggers this (see bind_caller's own docstring) -- it is
#: unchanged, existing behavior.
EXIT_CALLER_INVOKER_MISMATCH = 12


class ReviewPostVerbError(Exception):
    """Raised for any review-post failure that should terminate the process
    with a specific exit code. Carries the intended exit code as `.code`."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _fail(message: str, code: int) -> None:
    raise ReviewPostVerbError(message, code)


def assert_platform_is_forgejo(owner: str, repo: str, *, explicit_platform: str) -> None:
    """Mirror-image guard for the Forgejo backend: fires BEFORE any
    credential mint or API call when the caller's own --platform value says
    the target is NOT Forgejo. Kept alongside github_backend's
    assert_platform_is_github so BOTH directions of the wrong-platform
    failure class fail fast and locally rather than reaching the wrong
    host's API — the GitHub guard alone would leave a Forgejo PR posted
    through a (hypothetical) code path that assumed GitHub was the only
    transport needing the check.
    """
    if explicit_platform not in (PLATFORM_GITHUB, PLATFORM_FORGEJO):
        raise PlatformMismatchError(
            f"assert_platform_is_forgejo: unrecognized platform "
            f"{explicit_platform!r} for {owner}/{repo}. Expected "
            f"{PLATFORM_GITHUB!r} or {PLATFORM_FORGEJO!r}."
        )
    if explicit_platform != PLATFORM_FORGEJO:
        raise PlatformMismatchError(
            f"WRONG PLATFORM -- {owner}/{repo} PR resolves to platform "
            f"{explicit_platform!r}, but this backend only posts to Forgejo. "
            f"Use the GitHub backend instead. Refusing before minting any "
            f"credential or making any API call."
        )


def build_backend(
    platform: str,
    *,
    owner: str,
    repo: str,
    caller: str,
    git_host_base: str,
    expected_pr_sha: str | None,
    token_provider: TokenProvider | None,
    opener,
) -> ReviewBackend:
    """Resolve platform guard -> mint/resolve token -> construct the
    matching ReviewBackend. The platform guard ALWAYS runs before token
    resolution, for both platforms -- there is no call path here that
    reaches _resolve_token before the platform has been confirmed to match
    the selected backend (lr-622e part 1 ordering, preserved for both
    directions).
    """
    if platform == PLATFORM_GITHUB:
        assert_platform_is_github(owner, repo, explicit_platform=platform)
    elif platform == PLATFORM_FORGEJO:
        assert_platform_is_forgejo(owner, repo, explicit_platform=platform)
    else:
        _fail(
            f"--platform {platform!r} not recognized. Expected "
            f"{PLATFORM_GITHUB!r} or {PLATFORM_FORGEJO!r}.",
            code=EXIT_USAGE,
        )

    active_provider = (
        token_provider if token_provider is not None else resolve_platform_provider(platform)
    )
    try:
        token = _resolve_token(caller, active_provider, repo=f"{owner}/{repo}")
    except CredentialProviderError as exc:
        _fail(f"token resolution FAILED -- {exc}", code=EXIT_TOKEN_FETCH_FAILED)

    if platform == PLATFORM_GITHUB:
        return GithubReviewBackend(token, caller=caller, opener=opener)
    return ForgejoReviewBackend(
        token,
        git_host_base=git_host_base,
        expected_pr_sha=expected_pr_sha,
        opener=opener,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-post",
        description=(
            "review-post -- post exactly one review comment and verify it "
            "landed, on either Forgejo or GitHub, behind one contract. "
            "stdin is the default body path; --body-env reads a "
            "FIXED, statically-analyzable path instead, for a caller whose "
            "invoking command line must carry zero per-invocation body data. "
            "--delete-own-comment COMMENT_ID routes a "
            "belt-and-suspenders self-delete to the resolved --platform's "
            "own backend instead of posting."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  echo '{\"body\":\"LGTM\"}' | review-post --caller reviewer "
            "--platform github --pr-sha abc123 some-owner/some-repo 42\n"
            "\n"
            "Verdict route -- tool-owned fence, NO backticks cross the shell, "
            "GitHub AND Forgejo parity:\n"
            "  echo '{\"body\":\"No issues found.\",\"review_status\":\"clean\"}' | \\\n"
            "    review-post --caller reviewer --platform github \\\n"
            "      --verdict-review-status clean --verdict-head-sha abc123 \\\n"
            "      some-owner/some-repo 42\n"
            "\n"
            "Structured-findings route -- PRIMARY: the reviewer supplies\n"
            "NO free-form prose at all, only review_status + findings; the tool\n"
            "constructs the entire comment body (header, bullets, fence):\n"
            "  echo '{\"review_status\":\"blocking\",\"findings\":[{\"file\":\"a.py\",\\\n"
            "    \"line\":10,\"rule_id\":\"E501\",\"message\":\"line too long\"}]}' | \\\n"
            "    review-post --caller reviewer --platform github \\\n"
            "      --verdict-findings --verdict-head-sha abc123 \\\n"
            "      some-owner/some-repo 42\n"
            "\n"
            "Body-off-argv route -- a harness stages the body at the\n"
            "CALLER-NAMESPACED fixed path first (e.g. its own Write tool:\n"
            "$TMPDIR/clagentic-loadout/body.<caller>.json), then invokes with a\n"
            "CONSTANT argv -- no per-invocation body substring, no pipe, no producer:\n"
            "    review-post --caller reviewer --platform github --body-env \\\n"
            "      --pr-sha abc123 some-owner/some-repo 42\n"
            "\n"
            "Self-delete-own-comment route -- platform-aware, belt-and-\n"
            "suspenders: refuses unless the caller's own identity authored the\n"
            "comment and it carries no review-result verdict fence. Works on EITHER\n"
            "platform through this ONE entry point (no separate Forgejo-only tool\n"
            "needed for a review-post caller):\n"
            "  review-post --caller reviewer --platform github \\\n"
            "    --delete-own-comment 123456 some-owner/some-repo\n"
            "  review-post --caller reviewer --platform forgejo \\\n"
            "    --git-host-base-url http://git-host.example.com \\\n"
            "    --delete-own-comment 42 some-owner/some-repo\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"review-post {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    parser.add_argument(
        "--body-env",
        action="store_true",
        help="BODY-OFF-ARGV-AND-PIPE route: read the review "
        "body from a FIXED, statically-analyzable, CALLER-NAMESPACED path "
        "($TMPDIR/clagentic-loadout/body.<caller>.json, transport.body_env."
        "resolve_caller_body_path) that the caller's own harness "
        "stages BEFORE invoking this verb, instead of stdin -- see "
        "docs/integration.md. Takes NO value, so the invoking command line "
        "is a CONSTANT string with no per-invocation body substring. "
        "Namespaced by --caller so two concurrent same-TMPDIR callers can "
        "never collide on one staged file. Content is validated "
        "identically to the stdin path. " + BODY_ENV_NOT_EPHEMERAL_NOTE,
    )
    parser.add_argument(
        "--caller",
        default=None,
        help=f"Role/name whose token is resolved via the credential provider "
        f"(default: {DEFAULT_ROLE!r}). A role/caller, never a hardcoded "
        f"agent name. Already-attested, opaque config key downstream (the "
        f"credential provider never re-authenticates it itself -- see "
        f"transport.credential_provider's module docstring). When "
        f"EXPLICITLY supplied, it must ALSO match this process's own "
        f"already attested invoking identity (transport.attestation."
        f"resolve_identity) or the call is refused fail-closed before any "
        f"I/O (transport.caller_binding.bind_caller); omitted, this check "
        f"does not apply.",
    )
    parser.add_argument(
        "--platform",
        required=True,
        choices=(PLATFORM_GITHUB, PLATFORM_FORGEJO),
        help="Target platform for the PR (mandatory -- resolved "
        "independently, e.g. from a dispatch envelope's pr_url).",
    )
    parser.add_argument(
        "--pr-sha",
        default=None,
        help="Confirm the PR head SHA matches before posting (Forgejo "
        "backend only; GitHub's native review API has no equivalent "
        "pre-check).",
    )
    parser.add_argument(
        "--git-host-base-url",
        default=None,
        help=f"Forgejo API base URL (default: ${GIT_HOST_BASE_URL_ENV_VAR} env "
        f"var, falling back to a configurable compat-alias env var if that "
        f"is unset, or {DEFAULT_GIT_HOST_BASE_URL!r} if neither is set -- see "
        f"transport.git_host_api._resolve_git_host_base). Ignored for the "
        f"GitHub platform.",
    )
    parser.add_argument(
        "--verdict-review-status",
        metavar="STATUS",
        default=None,
        choices=("clean", "blocking"),
        help="MANDATORY, fail-closed emit-and-verify route for a reviewer's "
        "merge-gate verdict, on EITHER platform -- the sibling "
        "of transport.git_host_api's Forgejo-only --expect-verdict-block. "
        "TOOL-OWNED fence construction: builds and appends a fenced "
        "```review-result``` block (clagentic_loadout.merge.verdict."
        "build_verdict_block) to the --body-stdin 'body' prose before "
        "posting, using --caller as the fence's 'reviewer' field and this "
        "flag's value as review_status. No backtick ever needs to appear "
        "in --body-stdin's JSON or in any argv. Requires "
        "--verdict-head-sha (supplies head_sha; pr_number is taken from "
        "the pr_number positional). After the ordinary post_and_verify "
        "readback, the VERIFIED comment/review's own body is re-parsed "
        "and checked field-for-field against what was requested -- a "
        "fence that landed truncated or mangled in transit fails closed "
        "rather than silently reporting success.",
    )
    parser.add_argument(
        "--verdict-findings",
        action="store_true",
        help="PRIMARY structured-body-construction route: the "
        "reviewer NEVER supplies free-form prose. --body-stdin's JSON "
        "carries 'review_status' ('clean'|'blocking') and a structured "
        "'findings' list (each: file, line, rule_id, message) -- NO "
        "'body' field. This verb constructs the ENTIRE comment body "
        "(header, one bullet per finding, then the tool-owned fence via "
        "merge.verdict.build_findings_verdict_body) -- there is nothing "
        "for a foreign reviewer's narrative or block to hide inside. "
        "Requires --verdict-head-sha. Mutually exclusive with "
        "--verdict-review-status. After the ordinary post_and_verify "
        "readback, the landed body is re-parsed field-for-field AND "
        "checked to carry exactly one fenced block tagged with this "
        "caller's own reviewer id (merge.verdict."
        "assert_single_own_verdict_block) -- any mismatch fails closed.",
    )
    parser.add_argument(
        "--verdict-head-sha",
        metavar="SHA",
        default=None,
        help="Required alongside --verdict-review-status or "
        "--verdict-findings: supplies the fenced verdict block's head_sha "
        "field -- the caller's own evaluated SHA, not a second value to "
        "keep in sync.",
    )
    parser.add_argument(
        "--delete-own-comment",
        metavar="COMMENT_ID",
        default=None,
        help="Platform-aware self-delete: belt-and-suspenders "
        "delete of ONE already-posted comment, routed to the resolved "
        "--platform's own backend (review.github_backend.delete_own_comment "
        "for GitHub; review.forgejo_backend, delegating to transport."
        "git_host_api.delete_own_comment, for Forgejo) -- the SAME "
        "admissible-operation checks on both: refuses unless the comment's "
        "author matches the caller's own resolved identity, and refuses if "
        "the comment body carries a fenced ```review-result``` verdict "
        "block (even the caller's own). Not PR-scoped -- takes a bare "
        "comment id, never the pr_number positional (which is not required "
        "when this flag is supplied). A DELETE that omits this flag never "
        "happens -- review-post has no other delete path.",
    )
    parser.add_argument("owner_repo", help="owner/repo of the target PR.")
    parser.add_argument(
        "pr_number",
        nargs="?",
        default=None,
        help="PR number. Not required when --delete-own-comment is supplied "
        "(comment delete is scoped by comment id, not PR).",
    )
    return parser


def _parse_owner_repo(owner_repo: str) -> tuple[str, str]:
    parts = owner_repo.strip().split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        _fail(
            f"owner_repo must be in 'owner/repo' format, got: {owner_repo!r}",
            code=EXIT_USAGE,
        )
    return parts[0], parts[1]


def main(
    argv: list[str] | None = None,
    *,
    token_provider: TokenProvider | None = None,
    opener=None,
    identity_provider=None,
) -> int:
    """CLI entrypoint. Returns the process exit code (does not call
    sys.exit itself so it stays testable) -- the `if __name__` guard below
    is the one place that translates the return value into a real exit.

    `identity_provider` (lr-c75c9a): a zero-arg callable returning a
    `transport.attestation.Identity` (defaults to
    `transport.attestation.resolve_identity`) -- the injection point for the
    fail-closed --caller/attested-invoker binding (transport.caller_binding.
    bind_caller), mirroring the identical parameter transport.git_host_api.main
    already carries for the same purpose.
    """
    if argv is None:
        argv = sys.argv[1:]

    if any(arg in ("--help", "-h") for arg in argv):
        _build_arg_parser().print_help()
        return EXIT_OK

    parser = _build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    try:
        return _run(
            args, token_provider=token_provider, opener=opener,
            identity_provider=identity_provider,
        )
    except ReviewPostVerbError as exc:
        print(f"review-post: {exc}", file=sys.stderr)
        return exc.code
    except CallerBindingError as exc:
        print(f"review-post: {exc}", file=sys.stderr)
        return EXIT_CALLER_INVOKER_MISMATCH


def _run_delete_own_comment(
    args: argparse.Namespace,
    *,
    owner: str,
    repo: str,
    token_provider: TokenProvider | None,
    opener,
) -> int:
    """--delete-own-comment dispatch (lr-f43c4b): platform-aware routing to
    whichever backend's own delete_own_comment implementation matches
    --platform, mirroring build_backend's own platform-guard-before-mint
    ordering. Not PR-scoped -- comment_id alone identifies the target, so
    this never touches pr_number, body ingestion, or the verdict routes.

    COMMENT_ID digit-only constraint (lr-f43c4b security-review hardening
    finding, lr-26f774 finding class): validated BEFORE build_backend -- and
    therefore before any credential mint, platform guard, or I/O of any
    kind -- mirroring loadout-git-host-api's own --delete-own-comment
    argv-layer guard (transport.git_host_api._ISSUE_COMMENT_ID_RE). Both
    backends already re-validate this independently as defense-in-depth
    (review.github_backend._validate_comment_id, transport.git_host_api.
    _validate_comment_id via review.forgejo_backend) -- this is an
    additional, earlier fail-fast check at the CLI layer, not a
    replacement for either.
    """
    if not _DELETE_COMMENT_ID_RE.match(args.delete_own_comment):
        _fail(
            f"--delete-own-comment {args.delete_own_comment!r} is not "
            f"digit-only -- refusing before any credential mint or I/O. "
            f"Both git-host REST APIs use positive-decimal-integer comment "
            f"ids; a non-numeric value could inject an unexpected path "
            f"segment.",
            code=EXIT_DELETE_OWN_COMMENT_REFUSED,
        )

    try:
        backend = build_backend(
            args.platform,
            owner=owner,
            repo=repo,
            caller=args.caller or DEFAULT_ROLE,
            git_host_base=_resolve_git_host_base(args.git_host_base_url),
            expected_pr_sha=None,
            token_provider=token_provider,
            opener=opener,
        )
    except PlatformMismatchError as exc:
        _fail(str(exc), code=EXIT_WRONG_PLATFORM)

    try:
        backend.delete_own_comment(owner=owner, repo=repo, comment_id=args.delete_own_comment)
    except DeleteOwnCommentRefusedError as exc:
        _fail(str(exc), code=EXIT_DELETE_OWN_COMMENT_REFUSED)
    except ReviewPostError as exc:
        _fail(str(exc), code=EXIT_POST_FAILED)

    print(json.dumps({"deleted_comment_id": args.delete_own_comment, "owner": owner, "repo": repo}))
    return EXIT_OK


def _run(
    args: argparse.Namespace,
    *,
    token_provider: TokenProvider | None,
    opener,
    identity_provider=None,
) -> int:
    owner, repo = _parse_owner_repo(args.owner_repo)

    # --caller/attested-invoker fail-closed binding (lr-c75c9a, mirrors
    # transport.git_host_api's identical check): checked BEFORE any I/O --
    # before the --delete-own-comment short-circuit, before any body
    # ingestion, before any token mint. An OMITTED --caller (args.caller is
    # None) is never checked here -- see transport.caller_binding.bind_caller's
    # own docstring for why (this preserves the pre-existing "omitted
    # --caller behaves exactly as before" contract unchanged).
    resolve_identity_fn = identity_provider if identity_provider is not None else _resolve_identity
    try:
        attested_identity = resolve_identity_fn()
    except AttestationError as exc:
        _fail(f"attested-identity resolution FAILED -- {exc}", code=EXIT_CALLER_INVOKER_MISMATCH)
    bind_caller(
        args.caller or DEFAULT_ROLE, caller_explicit=args.caller is not None,
        identity=attested_identity,
    )

    # --delete-own-comment (lr-f43c4b) short-circuits here, BEFORE pr_number
    # is parsed/required and BEFORE any body-ingestion/verdict-route
    # machinery below runs -- a comment delete carries no request body and
    # is not PR-scoped, so none of that machinery should even be aware this
    # flag exists (mirrors transport.git_host_api._run's own
    # --delete-own-comment short-circuit ordering).
    if args.delete_own_comment is not None:
        return _run_delete_own_comment(
            args, owner=owner, repo=repo, token_provider=token_provider, opener=opener
        )

    try:
        pr_number = int(args.pr_number)
        if pr_number <= 0:
            raise ValueError("non-positive")
    except (TypeError, ValueError):
        _fail(f"pr_number must be a positive integer, got: {args.pr_number!r}", code=EXIT_USAGE)

    # --verdict-review-status and --verdict-findings are mutually exclusive
    # (lr-c26110): each constructs the fenced block a different way, and
    # accepting both would leave it ambiguous which construction wins.
    if args.verdict_review_status is not None and args.verdict_findings:
        _fail(
            "--verdict-review-status and --verdict-findings are mutually "
            "exclusive -- --verdict-findings is the PRIMARY "
            "structured-body route; --verdict-review-status is kept for "
            "transition/back-compat with caller-supplied prose. Pass "
            "exactly one.",
            code=EXIT_VERDICT_BLOCK_USAGE,
        )

    # --verdict-review-status / --verdict-findings preconditions, checked
    # BEFORE any I/O (same fail-fast posture as transport.git_host_api's
    # --expect-verdict-block usage guards): --verdict-head-sha is mandatory
    # alongside either -- there is no partial/optional lane once a verdict
    # post is requested.
    if args.verdict_review_status is not None and not args.verdict_head_sha:
        _fail(
            "--verdict-review-status requires --verdict-head-sha (supplies "
            "the fence's head_sha field -- the caller's own evaluated SHA, "
            "not a second value to keep in sync).",
            code=EXIT_VERDICT_BLOCK_USAGE,
        )
    if args.verdict_findings and not args.verdict_head_sha:
        _fail(
            "--verdict-findings requires --verdict-head-sha (supplies "
            "the fence's head_sha field -- the caller's own evaluated SHA, "
            "not a second value to keep in sync).",
            code=EXIT_VERDICT_BLOCK_USAGE,
        )

    # Platform guard + token mint BEFORE --body-env's CONSUMING read
    # (security re-audit follow-up, same class of fix as push.verb's
    # detached-HEAD reorder): build_backend() runs the platform guard
    # (assert_platform_is_github/forgejo) and mints the caller's token --
    # NEITHER depends on the body content, and both are computable purely
    # from args.platform/owner/repo/caller, all already resolved above. The
    # PRIOR ordering called build_backend() only at post time, AFTER
    # --body-env's read had already CONSUMED the staged body+stamp
    # (transport.body_env.read_body_bytes unlinks both on success) -- a
    # caller on the wrong --platform, or whose token mint failed, would have
    # its staged body destroyed by the read and then hit a refusal it had
    # no way to anticipate before staging, forcing a re-stage-from-scratch
    # retry for a failure entirely unrelated to what was staged.
    #
    # GATED ON "a NON-EMPTY body is ACTUALLY staged" (checked via a
    # non-consuming stat -- exists() AND st_size > 0, never a second read),
    # not merely on args.body_env: tests/test_review_verb.py::
    # TestBodyEnvUsageAndEndToEnd locks TWO pre-existing, deliberate
    # contracts that a MISSING stage, and an EMPTY staged file, must both
    # fail BEFORE any credential mint (no sense minting a token to post a
    # body that either doesn't exist or is empty/whitespace-only -- content
    # that will fail validation regardless of platform/token). Pre-minting
    # whenever --body-env is passed, unconditionally, would break both
    # guarantees. This stat-based check costs nothing (no read, no consume)
    # and correctly distinguishes "nothing worth protecting" (missing or
    # empty -- the read below fails the SAME way whether or not a token was
    # minted first) from "a real staged body exists" (worth protecting from
    # being destroyed by a platform/token failure downstream). Skipped
    # identically for --body-stdin (no staged file exists in that mode at
    # all -- TestBodyStdinIsSoleBodyPath's own "no mint before a content-
    # validation failure" contract is the sibling of this same principle).
    body_env_caller = args.caller or DEFAULT_ROLE
    git_host_base = _resolve_git_host_base(args.git_host_base_url)
    backend: ReviewBackend | None = None
    _staged_body_path = resolve_caller_body_path(caller=body_env_caller)
    if args.body_env and _staged_body_path.exists() and _staged_body_path.stat().st_size > 0:
        try:
            backend = build_backend(
                args.platform,
                owner=owner,
                repo=repo,
                caller=body_env_caller,
                git_host_base=git_host_base,
                expected_pr_sha=args.pr_sha,
                token_provider=token_provider,
                opener=opener,
            )
        except PlatformMismatchError as exc:
            _fail(str(exc), code=EXIT_WRONG_PLATFORM)

    # Body ingestion: --body-env reads a CALLER-NAMESPACED staged path
    # (lr-3a7ae8: <TMPDIR>/clagentic-loadout/body.<caller>.json, not the
    # single shared fixed path -- two concurrent same-TMPDIR callers with
    # different --caller values can never collide on one physical file, and
    # a caller that never staged its own body fails closed rather than
    # risking a foreign caller's staged content). --caller is resolved here,
    # BEFORE the body-ingestion block, specifically so this namespacing can
    # happen -- see the `caller = args.caller or DEFAULT_ROLE` assignment
    # this used to do further down; it is now duplicated to this earlier
    # point intentionally, not accidentally advanced. Its absence
    # (--body-env not supplied) keeps the existing stdin-only behavior
    # byte-for-byte.
    #
    # The read is bound to THIS invocation's own pr_number (already resolved
    # above, before any body ingestion) and, when supplied, its
    # --verdict-head-sha, and consumes the staged file on success
    # (lr-becdef): a leftover body staged for a prior, unrelated PR/review
    # can no longer be silently re-read and re-posted under this PR's
    # identity -- see transport.body_env's module docstring.
    if args.body_env:
        try:
            raw_bytes = read_body_bytes(
                caller=body_env_caller,
                expect_target_pr=pr_number,
                expect_head_sha=args.verdict_head_sha,
            )
        except BodyEnvError as exc:
            _fail(str(exc), code=EXIT_BODY_ENV_UNREADABLE)
    else:
        raw_bytes = sys.stdin.buffer.read()
    verdict_review_status: str | None = None
    verdict_findings: list[dict] | None = None
    if args.verdict_findings:
        # PRIMARY route (lr-c26110): stdin carries NO 'body'/prose field at
        # all -- only review_status + a structured findings list. There is
        # nothing here for a reviewer to author free-form content into.
        try:
            verdict_review_status, verdict_findings = validate_review_findings_body_stdin_content(
                raw_bytes
            )
        except ReviewBodyStdinEmptyError as exc:
            _fail(str(exc), code=EXIT_VERDICT_BLOCK_USAGE)
        body = None  # constructed below, entirely from structured fields
    elif args.verdict_review_status is not None:
        try:
            body, verdict_review_status = validate_review_verdict_body_stdin_content(raw_bytes)
        except ReviewBodyStdinEmptyError as exc:
            _fail(str(exc), code=EXIT_VERDICT_BLOCK_USAGE)
        if verdict_review_status != args.verdict_review_status:
            _fail(
                f"--verdict-review-status {args.verdict_review_status!r} "
                f"does not match --body-stdin's own 'review_status' field "
                f"{verdict_review_status!r} -- these must agree; pass the "
                f"same value to both or omit --verdict-review-status and "
                f"rely on --body-stdin alone.",
                code=EXIT_VERDICT_BLOCK_USAGE,
            )
    else:
        try:
            body = validate_review_body_stdin_content(raw_bytes)
        except ReviewBodyStdinEmptyError as exc:
            _fail(str(exc), code=EXIT_BODY_STDIN_EMPTY)

    caller = body_env_caller

    print(
        f"review-post: platform={args.platform!r} caller={caller!r} "
        f"target={owner}/{repo}#{pr_number}",
        file=sys.stderr,
    )

    # --verdict-findings: construct the ENTIRE body (header, bullets, fence)
    # IN PROCESS from structured fields alone -- merge.verdict.
    # build_findings_verdict_body, never re-implemented here. No caller
    # prose is ever consulted; pr_number comes from this verb's own
    # positional argument, so the fenced pr_number can never disagree with
    # the PR actually posted to.
    if verdict_findings is not None:
        try:
            body = build_findings_verdict_body(
                caller, verdict_review_status, args.verdict_head_sha, pr_number, verdict_findings
            )
        except ValueError as exc:
            _fail(f"--verdict-findings: {exc}", code=EXIT_VERDICT_BLOCK_USAGE)
    # --verdict-review-status: build the combined (prose + fence) body IN
    # PROCESS, exactly like transport.git_host_api.build_expected_verdict_body
    # -- reusing merge.verdict.build_verdict_block (the single fence-authoring
    # source), never re-implementing fence construction here. pr_number comes
    # from this verb's own positional argument, so the fenced pr_number can
    # never disagree with the PR actually posted to.
    #
    # PRE-EMBEDDED-FENCE REFUSAL (lr-5260f9, mirrors transport.git_host_api.
    # build_expected_verdict_body's identical check exactly -- same defect,
    # same design choice: reject, don't silently respect or silently skip
    # the append): 'body' (still caller-supplied prose on THIS route, unlike
    # --verdict-findings) must not already carry a fenced ```review-result```
    # block, or the comment posted below would carry two -- the exact
    # shape observed against a Forgejo deployment.
    elif verdict_review_status is not None:
        pre_embedded = find_all_verdict_blocks(body)
        if pre_embedded:
            _fail(
                f"--verdict-review-status: --body-stdin 'body' already "
                f"contains {len(pre_embedded)} fenced ```{VERDICT_FENCE}``` "
                f"block(s). This route CONSTRUCTS the verdict fence itself "
                f"-- 'body' must be plain prose with no pre-embedded fence, "
                f"or the posted comment would carry two. Remove the "
                f"hand-authored fence from 'body', or use --verdict-findings "
                f"(which accepts no prose field at all).",
                code=EXIT_VERDICT_BLOCK_USAGE,
            )
        try:
            fence = build_verdict_block(caller, verdict_review_status, args.verdict_head_sha, pr_number)
        except ValueError as exc:
            _fail(f"--verdict-review-status: {exc}", code=EXIT_VERDICT_BLOCK_USAGE)
        body = f"{body}\n{fence}"

    # build_backend() was already called earlier (see comment there) when
    # --body-env was used, BEFORE the read that consumes the staged body --
    # `backend` is non-None in that case, and is NOT rebuilt here. On the
    # --body-stdin path (no staged artifact to protect), this is the
    # ORIGINAL post-content-validation position -- unchanged, so
    # TestBodyStdinIsSoleBodyPath's "no credential minted before a content-
    # validation failure" contract still holds exactly as before.
    if backend is None:
        try:
            backend = build_backend(
                args.platform,
                owner=owner,
                repo=repo,
                caller=caller,
                git_host_base=git_host_base,
                expected_pr_sha=args.pr_sha,
                token_provider=token_provider,
                opener=opener,
            )
        except PlatformMismatchError as exc:
            _fail(str(exc), code=EXIT_WRONG_PLATFORM)

    try:
        verified = backend.post_and_verify(owner=owner, repo=repo, pr_number=pr_number, body=body)
    except ReviewPostError as exc:
        _fail(str(exc), code=EXIT_POST_FAILED)
        return EXIT_POST_FAILED  # unreachable; _fail raises
    except ReviewVerifyError as exc:
        _fail(str(exc), code=EXIT_VERIFY_FAILED)
        return EXIT_VERIFY_FAILED  # unreachable; _fail raises

    result = {
        "verified_id": verified.id,
        "verified_url": verified.url,
        "verified_by_login": verified.login,
        "owner": owner,
        "repo": repo,
        "pr_number": pr_number,
    }

    if verdict_review_status is not None:
        # Emit-and-verify (lr-482c20, extended by lr-c26110's foreign-block
        # backstop below for BOTH the --verdict-findings and
        # --verdict-review-status routes): re-parse the fence from the
        # VERIFIED comment/review's OWN body (the readback, never the
        # locally-constructed `body` string above) via the SAME
        # merge.verdict.parse_verdict_block the merge gate itself uses --
        # confirms the fence this verb constructed and posted landed
        # byte-identical. ANY mismatch, or no fence at all, fails closed --
        # no model-side retry (lr-1ce1/lr-fe04, locked).
        route_label = "--verdict-findings" if verdict_findings is not None else "--verdict-review-status"
        parsed = parse_verdict_block(verified.body)
        mismatches = []
        if parsed is None:
            _fail(
                f"{route_label}: post_and_verify confirmed the comment/"
                f"review landed, but re-parsing its own body found NO "
                f"fenced ```review-result``` block. Gate-pass REFUSED.",
                code=EXIT_VERDICT_BLOCK_MISMATCH,
            )
        if parsed.get("reviewer") != caller:
            mismatches.append(f"reviewer: expected {caller!r}, got {parsed.get('reviewer')!r}")
        if parsed.get("review_status") != verdict_review_status:
            mismatches.append(
                f"review_status: expected {verdict_review_status!r}, got "
                f"{parsed.get('review_status')!r}"
            )
        if parsed.get("head_sha") != args.verdict_head_sha:
            mismatches.append(
                f"head_sha: expected {args.verdict_head_sha!r}, got "
                f"{parsed.get('head_sha')!r}"
            )
        if parsed.get("pr_number") != pr_number:
            mismatches.append(
                f"pr_number: expected {pr_number!r}, got {parsed.get('pr_number')!r}"
            )
        if mismatches:
            _fail(
                f"{route_label} MISMATCH -- the verified comment/review's "
                f"own fenced ```review-result``` block does not match what "
                f"was requested: " + "; ".join(mismatches) +
                ". Gate-pass REFUSED.",
                code=EXIT_VERDICT_BLOCK_MISMATCH,
            )

        # FOREIGN-BLOCK BACKSTOP (lr-c26110, secondary/fail-closed guard
        # beneath the primary structured-body-construction path): the check
        # above only inspects parse_verdict_block's LAST-match result, so a
        # body carrying a SECOND, foreign reviewer's block earlier in the
        # text would still pass it (observed against a Forgejo deployment,
        # lr-f89f6f evidence: a structural self-verify pass while a foreign
        # block rode along). assert_single_own_verdict_block requires the
        # landed body
        # carry EXACTLY ONE fenced block, tagged with this caller's own
        # reviewer id.
        try:
            assert_single_own_verdict_block(verified.body, caller)
        except VerdictMalformedError as exc:
            _fail(
                f"{route_label} MISMATCH -- foreign-block backstop refused: "
                f"{exc}. Gate-pass REFUSED.",
                code=EXIT_VERDICT_BLOCK_MISMATCH,
            )

        result["verdict_block_verified"] = True

    print(json.dumps(result))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
