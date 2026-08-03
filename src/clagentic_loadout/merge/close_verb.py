"""merge.close_verb — loadout-close-pr: close a PR WITHOUT merging it.

Task lr-2ba5e1: a consuming deployment's release-gate close_pr action
(abandoning a superseded/dead PR) had no sanctioned loadout write path. The
nearest existing surface, transport.git_host_api's --body-stdin, runs
validate_body_stdin_content() UNCONDITIONALLY on every write-method body and
hardcodes the comment-post {"body": ...} shape — a {"state": "closed"} PATCH
carries no 'body' key and is refused before any I/O
(EXIT_BODY_STDIN_EMPTY), with no dedicated close verb, alternate validator,
or bypass anywhere in the package.

WHY A DEDICATED VERB, NOT A RELAXED VALIDATOR (design_direction, lr-2ba5e1):
validate_body_stdin_content is the fail-closed gate for the comment-post
emit-and-verify contract (lr-482c20) — every --body-stdin caller in that
contract posts exactly {"body": "<comment text>"}. Relaxing it to also admit
a bodyless state-change shape would quietly widen what --body-stdin accepts
for EVERY caller of that flag, coupling an unrelated abandonment action to
the comment-posting validator's own hardening history. A dedicated verb
instead earns its own CLI-hygiene surface (--help/--version, a reserved
exit-code range, tested error messages — CLAUDE.md/CLI-NAMING-STANDARD rule
4) with NO shared validator to reason about across two different body
shapes. This mirrors merge.verb's own platform-aware backend-dispatch shape
(_resolve_backend below is a smaller, close-scoped analog of merge.verb.
_resolve_backend) rather than introducing a second dispatch pattern.

SCOPE — deliberately NARROWER than merge.verb's full gate chain: closing a
PR abandons it; it never lands code on the target branch, so the gate chain
that exists to protect what lands on main (stale-SHA, reviewer verdicts,
diff-scope, PR-title, CI-status) does not apply here. What DOES carry over,
unchanged, from merge.verb (an abandonment action still needs the same
authorization posture a landing action does — both are PR-terminal actions
this package's own AGENT.md contract reserves to a merge-authority role):
  1. Namespace guard (push.namespace_guard, reused verbatim) — runs first,
     before any credential or network call.
  2. Merge-authority check (merge.authority) — the SAME AuthorityProvider
     seam merge.verb consumes; a close is refused for the identical reason
     a merge would be, if the calling role has no configured authority over
     this owner/repo/PR.
  3. Platform guard (assert_platform_is_forgejo / assert_platform_is_github,
     BOTH directions, fail-closed) + credential resolution — the SAME
     credential seam every other loadout verb resolves a git-host token
     through. Runs only after 1-2 pass.
  4. Execute the close via the resolved backend's close_pr (Forgejo or
     GitHub — merge.forgejo_backend.close_pr / merge.github_backend.
     close_pr, both new in this task, mirroring each backend's existing
     merge_pr contract: MergeExecutionError on any non-2xx response or
     network failure, never a silent partial success).

The state-change body ({"state": "closed"}) is constructed by the two
close_pr backend functions themselves, in-process — it never touches
--body-stdin/--body-env or transport.git_host_api.validate_body_stdin_content
at all. There is no shell-visible body payload anywhere in this verb's own
invocation.
"""

from __future__ import annotations

import argparse
import json
import sys

from clagentic_loadout._version import get_version
from clagentic_loadout.merge import forgejo_backend, github_backend
from clagentic_loadout.merge.authority import (
    AuthorityProvider,
    StaticRoleAuthorityProvider,
    check_authority,
)
from clagentic_loadout.merge.errors import (
    AuthorityDeniedError,
    MergeExecutionError,
    MergeUsageError,
    PlatformMismatchError,
)
from clagentic_loadout.merge.merge_readback import verify_pr_closed
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.push.errors import NamespaceDeniedError, RemoteResolutionError
from clagentic_loadout.push.git_coords import parse_owner_repo
from clagentic_loadout.push.namespace_guard import (
    ALLOWED_NAMESPACES_ENV_VAR,
    check_namespace_allowed,
    resolve_allowed_namespaces,
)
from clagentic_loadout.transport.attestation import (
    AttestationError,
    resolve_identity as _resolve_identity,
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
from clagentic_loadout.transport.readback_envelope import READBACK_ENVELOPE_KEY

# ---------------------------------------------------------------------------
# Exit codes — reserved range for this verb, distinct from merge.verb's own
# EXIT_* constants (a caller dispatching both verbs must be able to tell them
# apart by code without inspecting stderr text).
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_TOKEN_FETCH_FAILED = 2
EXIT_WRONG_PLATFORM = 4
EXIT_NAMESPACE_DENIED = 20
EXIT_AUTHORITY_DENIED = 21
EXIT_CLOSE_FAILED = 26
#: A fresh post-close readback (merge.merge_readback.verify_pr_closed) did
#: NOT confirm the PR's state is "closed" (lr-361de3): the close PATCH
#: itself returned success, but a SEPARATE GET re-reading the PR afterward
#: did not find state=="closed". Distinct from EXIT_CLOSE_FAILED (the close
#: PATCH call itself failed) so a caller can tell "the close call succeeded
#: but could not be independently confirmed" apart from "the close call
#: itself refused." FAIL-CLOSED: a caller MUST NOT report success when this
#: fires.
EXIT_CLOSE_READBACK_FAILED = 27
#: An EXPLICIT --role value does not match the ATTESTED invoking identity
#: this process's own attestation-provider chain resolved
#: (transport.caller_binding.bind_caller, lr-c75c9a -- the same fail-closed
#: binding transport.git_host_api's EXIT_CALLER_INVOKER_MISMATCH already
#: enforced; this verb now enforces it too). FAILS CLOSED BEFORE ANY I/O --
#: no token mint, no authority check, no close is ever attempted. An
#: OMITTED --role never triggers this (see bind_caller's own docstring) --
#: it is unchanged, existing behavior.
EXIT_CALLER_INVOKER_MISMATCH = 28


class CloseVerbError(Exception):
    """Raised for any close-PR-verb failure that should terminate the
    process with a specific exit code. Carries the intended exit code as
    `.code`."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _fail(message: str, code: int) -> None:
    raise CloseVerbError(message, code)


def _resolve_backend(
    platform: str,
    *,
    owner: str,
    repo: str,
    role: str,
    git_host_base: str,
    token_provider: TokenProvider | None,
    opener,
):
    """Resolve platform guard -> mint/resolve token -> return
    (call_close_pr, get_pr_info). Mirrors merge.verb._resolve_backend's shape
    (lr-9c69 precedent): the platform guard ALWAYS runs before token
    resolution, for both platforms.

    `get_pr_info` (lr-361de3) is a zero-arg callable bound to the resolved
    backend/owner/repo/pr_number-free PR read -- actually still needs
    pr_number, so it is a one-arg callable of pr_number -- used by _run's
    post-close readback (merge.merge_readback.verify_pr_closed) to re-read
    the PR's state AFTER the close PATCH, via the SAME get_pr_info() each
    backend already provides for merge.verb's gate chain (no second PR-read
    endpoint added).

    Raises CloseVerbError(code=EXIT_USAGE) for an unrecognized --platform
    value, PlatformMismatchError for a recognized-but-wrong platform (the
    caller translates that to EXIT_WRONG_PLATFORM), and CloseVerbError(code=
    EXIT_TOKEN_FETCH_FAILED) on credential resolution failure.
    """
    if platform == PLATFORM_GITHUB:
        github_backend.assert_platform_is_github(owner, repo, explicit_platform=platform)
    elif platform == PLATFORM_FORGEJO:
        forgejo_backend.assert_platform_is_forgejo(owner, repo, explicit_platform=platform)
    else:
        _fail(
            f"--platform {platform!r} not recognized. Expected "
            f"{PLATFORM_GITHUB!r} or {PLATFORM_FORGEJO!r}.",
            code=EXIT_USAGE,
        )

    print(f"close-pr: resolving token for role={role!r}", file=sys.stderr)
    active_provider = (
        token_provider if token_provider is not None else resolve_platform_provider(platform)
    )
    try:
        token = _resolve_token(role, active_provider, repo=f"{owner}/{repo}")
    except CredentialProviderError as exc:
        _fail(f"token resolution FAILED -- {exc}", code=EXIT_TOKEN_FETCH_FAILED)

    if platform == PLATFORM_GITHUB:
        def _close(pr_number: int) -> None:
            github_backend.close_pr(owner, repo, pr_number, token=token, opener=opener)

        def _get_pr_info(pr_number: int) -> dict:
            return github_backend.get_pr_info(owner, repo, pr_number, token=token, opener=opener)
    else:
        def _close(pr_number: int) -> None:
            forgejo_backend.close_pr(
                git_host_base, owner, repo, pr_number, token=token, opener=opener
            )

        def _get_pr_info(pr_number: int) -> dict:
            return forgejo_backend.get_pr_info(
                git_host_base, owner, repo, pr_number, token=token, opener=opener
            )
    return _close, _get_pr_info


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadout-close-pr",
        description=(
            "loadout-close-pr -- close a PR WITHOUT merging it (abandon a "
            "superseded/dead PR). Runs the same namespace + merge-authority "
            "gate a merge would (a close is a PR-terminal action too), then "
            "issues a platform-aware state=closed PATCH via either the "
            "Forgejo or GitHub API. Never merges; never lands a diff."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  loadout-close-pr --role merger --platform forgejo \\\n"
            "      --repo some-owner/some-repo --pr 42 \\\n"
            "      --authorized-role merger\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"loadout-close-pr {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    parser.add_argument(
        "--platform",
        required=True,
        choices=(PLATFORM_GITHUB, PLATFORM_FORGEJO),
        help="Target platform for the PR (mandatory -- resolved "
        "independently, e.g. from a dispatch envelope's pr_url).",
    )
    parser.add_argument(
        "--role",
        default=None,
        help=f"Role whose merge authority is checked and whose token is "
        f"resolved via the credential provider (default: {DEFAULT_ROLE!r}). "
        f"Which role may authorize a close is config (see "
        f"--authorized-role / an AuthorityProvider), never a hardcoded "
        f"identity -- the SAME authority seam merge.verb consumes for a "
        f"merge. When EXPLICITLY supplied, it must match this process's "
        f"own attested invoking identity (transport.attestation."
        f"resolve_identity) or the call is refused fail-closed before any "
        f"I/O (transport.caller_binding.bind_caller); omitted, this check "
        f"does not apply.",
    )
    parser.add_argument(
        "--authorized-role",
        action="append",
        dest="authorized_roles",
        default=None,
        help="A role permitted to hold merge/close authority (repeatable). "
        "Used to build the standalone StaticRoleAuthorityProvider when no "
        "external AuthorityProvider is injected.",
    )
    parser.add_argument("--repo", required=True, help="owner/repo the PR lives in.")
    parser.add_argument("--pr", type=int, required=True, dest="pr_number", help="PR number to close.")
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
        "--allowed-namespace",
        action="append",
        dest="allowed_namespaces",
        default=None,
        help="Restrict the close target owner to this namespace "
        "(repeatable). When omitted, falls back to "
        f"{ALLOWED_NAMESPACES_ENV_VAR} (comma-separated); when neither is "
        f"set, no namespace restriction is enforced.",
    )
    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(
    argv: list[str] | None = None,
    *,
    token_provider: TokenProvider | None = None,
    authority_provider: AuthorityProvider | None = None,
    opener=None,
    identity_provider=None,
) -> int:
    """CLI entrypoint. Returns the process exit code (does not call
    sys.exit itself so it stays testable).

    `token_provider`, `authority_provider`, and `opener` are injection
    points for tests and for a deployment wiring its own reference
    providers; all default to the standalone/real path in production use.

    `identity_provider` (lr-c75c9a): a zero-arg callable returning a
    `transport.attestation.Identity` (defaults to
    `transport.attestation.resolve_identity`) -- the injection point for the
    fail-closed --role/attested-invoker binding (transport.caller_binding.
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
            args,
            token_provider=token_provider,
            authority_provider=authority_provider,
            opener=opener,
            identity_provider=identity_provider,
        )
    except CloseVerbError as exc:
        print(f"close-pr: {exc}", file=sys.stderr)
        return exc.code
    except MergeUsageError as exc:
        print(f"close-pr: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except CallerBindingError as exc:
        print(f"close-pr: {exc}", file=sys.stderr)
        return EXIT_CALLER_INVOKER_MISMATCH


def _run(
    args: argparse.Namespace,
    *,
    token_provider: TokenProvider | None,
    authority_provider: AuthorityProvider | None,
    opener,
    identity_provider=None,
) -> int:
    try:
        owner, repo = parse_owner_repo(args.repo)
    except RemoteResolutionError as exc:
        raise MergeUsageError(str(exc)) from exc

    role = args.role or DEFAULT_ROLE

    # --role/attested-invoker fail-closed binding (lr-c75c9a, mirrors
    # transport.git_host_api's identical check): checked BEFORE any I/O --
    # before the namespace guard (step 1), before any token mint.
    resolve_identity_fn = identity_provider if identity_provider is not None else _resolve_identity
    try:
        attested_identity = resolve_identity_fn()
    except AttestationError as exc:
        _fail(f"attested-identity resolution FAILED -- {exc}", code=EXIT_CALLER_INVOKER_MISMATCH)
    bind_caller(role, caller_explicit=args.role is not None, identity=attested_identity)

    git_host_base = _resolve_git_host_base(args.git_host_base_url)

    # 1. Namespace guard -- runs FIRST, before any credential or network call.
    allowed_namespaces = resolve_allowed_namespaces(
        frozenset(args.allowed_namespaces) if args.allowed_namespaces else None
    )
    try:
        check_namespace_allowed(owner, repo, allowed_namespaces=allowed_namespaces)
    except NamespaceDeniedError as exc:
        _fail(str(exc), code=EXIT_NAMESPACE_DENIED)

    # 2. Merge-authority check -- FAIL-CLOSED provider seam. A close is a
    # PR-terminal action, gated by the SAME authority seam a merge is.
    provider = authority_provider or StaticRoleAuthorityProvider(
        frozenset(args.authorized_roles) if args.authorized_roles else frozenset()
    )
    try:
        check_authority(role, owner, repo, args.pr_number, provider)
    except AuthorityDeniedError as exc:
        _fail(str(exc), code=EXIT_AUTHORITY_DENIED)

    # 3. Platform guard (BOTH directions, fail-closed, BEFORE any credential
    # mint or API call) -> credential resolution -> resolved close callable.
    try:
        close_callable, get_pr_info_callable = _resolve_backend(
            args.platform,
            owner=owner,
            repo=repo,
            role=role,
            git_host_base=git_host_base,
            token_provider=token_provider,
            opener=opener,
        )
    except PlatformMismatchError as exc:
        _fail(str(exc), code=EXIT_WRONG_PLATFORM)

    # 4. Execute the close.
    print(
        f"close-pr: closing PR #{args.pr_number} in {owner}/{repo} "
        f"(platform={args.platform!r})",
        file=sys.stderr,
    )
    try:
        close_callable(args.pr_number)
    except MergeExecutionError as exc:
        _fail(str(exc), code=EXIT_CLOSE_FAILED)

    print(f"close-pr: PR #{args.pr_number} in {owner}/{repo} closed")

    # Post-close authoritative readback (lr-361de3): the close PATCH's own
    # response is trusted no further than the HTTP status code by itself --
    # a FRESH GET, issued now, re-reads the PR and confirms state=="closed",
    # the same predicate seq 2 of this task's research pass specified.
    # FAIL-CLOSED: a readback that does not confirm the close produces a
    # distinct, non-zero exit code rather than reporting EXIT_OK for a
    # mutation this verb cannot independently confirm landed.
    close_readback = verify_pr_closed(lambda: get_pr_info_callable(args.pr_number))
    if not close_readback.verified:
        _fail(
            f"post-close readback FAILED for PR #{args.pr_number} in "
            f"{owner}/{repo} -- {close_readback.detail.get('reason', '')} "
            f"The close call itself reported success; this independent "
            f"re-read could not confirm it landed.",
            code=EXIT_CLOSE_READBACK_FAILED,
        )
    print(
        f"close-pr: post-close readback CONFIRMED -- state="
        f"{close_readback.detail.get('state')!r}",
        file=sys.stderr,
    )
    print(json.dumps({
        "pr_number": args.pr_number, "owner": owner, "repo": repo,
        READBACK_ENVELOPE_KEY: close_readback.to_dict(),
    }))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
