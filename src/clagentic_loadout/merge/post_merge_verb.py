"""merge.post_merge_verb — loadout-post-merge: standalone post-merge
re-run entrypoint for an ALREADY-MERGED PR (lr-dd99e7).

THE GAP THIS CLOSES: `merge.verb` (loadout-merge) only ever runs
`post_merge_steps` inside the SAME invocation that just executed the merge
(step 10 of `merge.verb._run`, immediately after step 9's `backend.merge_pr`
succeeds). There is no standalone entrypoint to re-fire `post_merge_steps`
for a PR that merged successfully but whose post-merge deploy failed, hung,
or was never attempted (`--no-post-merge-tree` / `--skip-post-merge`, or a
`--repo-path`-less bare API-only merge). `loadout-merge` re-invoked against
an already-merged PR correctly no-ops the merge itself (`backend.merge_pr` on
an already-merged PR is a deliberate idempotent success on both platforms —
see forgejo_backend/github_backend's own merge_pr docstrings) but that path
still requires re-running the FULL gate chain (namespace, authority,
stale-SHA, verdict fences, diff-scope, title, CI-status) to reach step 10 —
gates that make sense before a merge decision, not after one has already been
made and recorded. A full `loadout-merge --help` review (lr-2ce122) confirmed
that no flag or sibling verb re-triggers post-merge standalone.

THIS VERB: `loadout-post-merge --platform <p> --repo <owner/repo> --pr <n>
--repo-path <dir>` runs ONLY the two links that matter for a re-run —
namespace guard and merge-authority (a re-deploy is still a PR-terminal-
adjacent action reserved to the SAME merge-authority role a merge/close
already requires — mirrors `merge.close_verb`'s own scope-narrowing
precedent, see that module's docstring "SCOPE — deliberately NARROWER than
merge.verb's full gate chain") — then POSITIVELY CONFIRMS the target PR is
actually merged (`pr_info["merged"] is True`, read fresh from the resolved
platform's own API; a PR that is open or closed-without-merging is refused,
never silently treated as a no-op success) before advancing `--repo-path` to
the MERGED SHA (via the SAME `merge.tree_sync.advance_repo_to_merged_sha`
merge.verb's own step 10 already uses, given `pr_info["merge_commit_sha"]` as
`known_merged_sha` when the platform reports one) and running
`post_merge_steps` (via the SAME `merge.post_merge.run_post_merge_steps` +
`merge.post_merge_config.load_post_merge_steps` merge.verb's own step 10
already uses). NO STEP OF THIS EXECUTION PATH IS RE-IMPLEMENTED — every gate,
every tree-sync call, every step-runner call is the identical function
merge.verb's step 10 calls; this verb differs from merge.verb only in WHICH
gates run before reaching that shared tail (no stale-SHA/verdict/diff-scope/
title/CI-status gate — those already did their job at merge time and this
verb never lands new code) and in NOT calling `backend.merge_pr` at all.

GATED TO A MERGE-AUTHORITY ROLE (task's explicit requirement): the SAME
`merge.authority.check_authority` / `AuthorityProvider` seam and
`--authorized-role` flag `merge.verb` and `merge.close_verb` both already
consume — a deployment that authorizes a role to merge/close a PR authorizes
that same role to re-run its post-merge deploy; there is no separate
authority tier invented here.

WHY NOT A `loadout-merge --rerun-post-merge` FLAG INSTEAD (the task's other
enumerated option; naming the trade-off per this repo's CLAUDE.md "Principle
conflict" rule): `merge.verb._run`'s gate chain (steps 1-8) and its post-merge
tail (step 10) are coupled through local variables computed ACROSS that gate
chain (`current_head_sha`, `merged_sha`, `pr_info`, `ci_disposition`) that a
re-run path would need to either re-derive independently (duplicating
`get_pr_info` + `resolve_base_branch` calls merge.verb already makes) or
route around via a threadbare early-return inside `_run` guarded by yet
another flag combination -- a shape this repo's existing `--repo-path`/
`--no-post-merge-tree`/`--skip-post-merge` three-way branch in that same
function already shows gets harder to reason about with each additional flag.
A SEPARATE verb, mirroring `merge.close_verb`'s own precedent for a
PR-terminal action that intentionally runs a narrower gate subset than a full
merge, keeps `merge.verb`'s own gate chain and exit-code range completely
unchanged (no new flag threading through 700+ lines of the load-bearing
release gate) and earns its own CLI-hygiene surface (--help/--version, a
reserved exit-code range, resolved-value error messages) exactly like
`loadout-close-pr` did for the same reason.
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
    GateFactUnavailableError,
    MergeUsageError,
    PlatformMismatchError,
)
from clagentic_loadout.merge.post_merge import (
    EXIT_POST_MERGE_FAILED as _EXIT_POST_MERGE_FAILED,
    PostMergeConfigError,
    PostMergeLivenessError,
    PostMergeStepFailedError,
    PostMergeStepTimeoutError,
    run_post_merge_steps,
)
from clagentic_loadout.merge.post_merge_config import (
    DEFAULT_CONFIG_RELATIVE_PATH as DEFAULT_POST_MERGE_CONFIG_RELATIVE_PATH,
    load_post_merge_steps,
    resolve_env_overrides,
    resolve_git_working_tree,
    resolve_post_merge_step_timeout_seconds,
)
from clagentic_loadout.merge.tree_sync import (
    TreeSyncError,
    advance_repo_to_merged_sha,
    resolve_base_branch,
)
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

# ---------------------------------------------------------------------------
# Exit codes — reserved range for this verb, distinct from merge.verb's own
# EXIT_* constants and merge.close_verb's own range (a caller dispatching
# more than one of these verbs must be able to tell the codes apart without
# inspecting stderr text).
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_TOKEN_FETCH_FAILED = 2
EXIT_WRONG_PLATFORM = 4
EXIT_NAMESPACE_DENIED = 20
EXIT_AUTHORITY_DENIED = 21
EXIT_GATE_FACT_UNAVAILABLE = 27
EXIT_POST_MERGE_FAILED = _EXIT_POST_MERGE_FAILED
EXIT_PR_NOT_MERGED = 31
#: An EXPLICIT --role value does not match the ATTESTED invoking identity
#: this process's own attestation-provider chain resolved
#: (transport.caller_binding.bind_caller, lr-c75c9a -- the same fail-closed
#: binding transport.git_host_api's EXIT_CALLER_INVOKER_MISMATCH already
#: enforced; this verb now enforces it too). FAILS CLOSED BEFORE ANY I/O --
#: no token mint, no authority check, no post-merge step is ever attempted.
#: An OMITTED --role never triggers this (see bind_caller's own docstring)
#: -- it is unchanged, existing behavior.
EXIT_CALLER_INVOKER_MISMATCH = 32


class PostMergeVerbError(Exception):
    """Raised for any loadout-post-merge failure that should terminate the
    process with a specific exit code. Carries the intended exit code as
    `.code`."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _fail(message: str, code: int) -> None:
    raise PostMergeVerbError(message, code)


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
    """Resolve platform guard -> mint/resolve token -> return a uniform
    ``get_pr_info(pr_number) -> dict`` callable. Mirrors merge.verb.
    _resolve_backend / merge.close_verb._resolve_backend's shape exactly
    (lr-9c69 precedent): the platform guard ALWAYS runs before token
    resolution, for both platforms.

    Raises PostMergeVerbError(code=EXIT_USAGE) for an unrecognized
    --platform value, PlatformMismatchError for a recognized-but-wrong
    platform (the caller translates that to EXIT_WRONG_PLATFORM), and
    PostMergeVerbError(code=EXIT_TOKEN_FETCH_FAILED) on credential
    resolution failure.
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

    print(f"post-merge: resolving token for role={role!r}", file=sys.stderr)
    active_provider = (
        token_provider if token_provider is not None else resolve_platform_provider(platform)
    )
    try:
        token = _resolve_token(role, active_provider, repo=f"{owner}/{repo}")
    except CredentialProviderError as exc:
        _fail(f"token resolution FAILED -- {exc}", code=EXIT_TOKEN_FETCH_FAILED)

    if platform == PLATFORM_GITHUB:
        def _get_pr_info(pr_number: int) -> dict:
            return github_backend.get_pr_info(owner, repo, pr_number, token=token, opener=opener)
    else:
        def _get_pr_info(pr_number: int) -> dict:
            return forgejo_backend.get_pr_info(
                git_host_base, owner, repo, pr_number, token=token, opener=opener
            )
    return _get_pr_info


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadout-post-merge",
        description=(
            "loadout-post-merge -- re-run post_merge_steps for an "
            "ALREADY-MERGED PR, standalone, without re-merging. For when a "
            "merge succeeded but its post-merge deploy failed, hung, or "
            "was never attempted (--no-post-merge-tree / --skip-post-merge, "
            "or a --repo-path-less bare API-only merge). Refuses unless the "
            "target PR is POSITIVELY confirmed merged, read fresh from the "
            "resolved platform's own API -- never re-merges, never lands a "
            "diff."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  loadout-post-merge --role merger --platform forgejo \\\n"
            "      --repo some-owner/some-repo --pr 42 \\\n"
            "      --repo-path /path/to/checkout --authorized-role merger\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"loadout-post-merge {get_version()}",
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
        f"The SAME authority seam merge.verb/merge.close_verb consume -- a "
        f"role authorized to merge/close a PR is authorized to re-run its "
        f"post-merge deploy. Already-attested, opaque config key "
        f"downstream. When EXPLICITLY supplied, it must ALSO match this "
        f"process's own already attested invoking identity (transport."
        f"attestation.resolve_identity) or the call is refused fail-closed "
        f"before any I/O (transport.caller_binding.bind_caller); omitted, "
        f"this check does not apply.",
    )
    parser.add_argument(
        "--authorized-role",
        action="append",
        dest="authorized_roles",
        default=None,
        help="A role permitted to hold merge authority (repeatable). Used "
        "to build the standalone StaticRoleAuthorityProvider when no "
        "external AuthorityProvider is injected.",
    )
    parser.add_argument("--repo", required=True, help="owner/repo the PR was merged in.")
    parser.add_argument(
        "--pr", type=int, required=True, dest="pr_number",
        help="Already-merged PR number to re-run post_merge_steps for.",
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
        "--allowed-namespace",
        action="append",
        dest="allowed_namespaces",
        default=None,
        help="Restrict the target owner to this namespace (repeatable). "
        f"When omitted, falls back to {ALLOWED_NAMESPACES_ENV_VAR} "
        f"(comma-separated); when neither is set, no namespace restriction "
        f"is enforced.",
    )
    parser.add_argument(
        "--repo-path",
        required=True,
        dest="repo_path",
        help="Local working-tree root to advance to the merged SHA and run "
        f"post_merge_steps in (see merge.post_merge_config) -- read from "
        f"<repo-path>/{DEFAULT_POST_MERGE_CONFIG_RELATIVE_PATH}. REQUIRED: "
        "unlike loadout-merge's --repo-path (an optional override guarded "
        "by --no-post-merge-tree/--skip-post-merge), this verb exists "
        "SOLELY to run post_merge_steps against a local tree -- there is no "
        "'skip' shape for it, since skipping is simply not invoking this "
        "verb at all.",
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
    except PostMergeVerbError as exc:
        print(f"post-merge: {exc}", file=sys.stderr)
        return exc.code
    except MergeUsageError as exc:
        print(f"post-merge: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except CallerBindingError as exc:
        print(f"post-merge: {exc}", file=sys.stderr)
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

    # 2. Merge-authority check -- FAIL-CLOSED provider seam. Re-running a
    # post-merge deploy is gated to the SAME authority a merge/close is.
    provider = authority_provider or StaticRoleAuthorityProvider(
        frozenset(args.authorized_roles) if args.authorized_roles else frozenset()
    )
    try:
        check_authority(role, owner, repo, args.pr_number, provider)
    except AuthorityDeniedError as exc:
        _fail(str(exc), code=EXIT_AUTHORITY_DENIED)

    # 3. Platform guard (BOTH directions, fail-closed, BEFORE any credential
    # mint or API call) -> credential resolution -> resolved PR-info reader.
    try:
        get_pr_info = _resolve_backend(
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

    # 4. Read the PR's LIVE current state -- POSITIVELY confirm it is
    # actually merged before touching anything. A PR that is open, or
    # closed-without-merging, has no merged SHA to advance --repo-path to and
    # is refused here, never silently treated as an already-satisfied no-op.
    try:
        pr_info = get_pr_info(args.pr_number)
    except GateFactUnavailableError as exc:
        _fail(str(exc), code=EXIT_GATE_FACT_UNAVAILABLE)

    if pr_info.get("merged") is not True:
        _fail(
            f"PR #{args.pr_number} in {owner}/{repo} is NOT merged "
            f"(merged={pr_info.get('merged')!r}) -- refusing to run "
            f"post_merge_steps for a PR that was never merged. This verb "
            f"only re-runs post-merge automation for an ALREADY-MERGED PR; "
            f"a PR still open or closed-without-merging needs "
            f"loadout-merge (to merge it) instead.",
            code=EXIT_PR_NOT_MERGED,
        )

    known_merged_sha = pr_info.get("merge_commit_sha") or None
    print(
        f"post-merge: PR #{args.pr_number} in {owner}/{repo} confirmed "
        f"merged (merge_commit_sha={known_merged_sha!r})",
        file=sys.stderr,
    )

    # 5. Advance --repo-path to the merged SHA -- the SAME tree_sync call
    # merge.verb's own step 10 makes, so a re-run step sees exactly what
    # landed on the base branch, never a stale local ref.
    base_branch = resolve_base_branch(pr_info)
    try:
        declared_working_tree = resolve_git_working_tree(args.repo_path)
    except PostMergeConfigError as exc:
        _fail(
            f"post-merge config FAILED to load -- {exc}",
            code=EXIT_POST_MERGE_FAILED,
        )
    git_tree_path = (
        str(declared_working_tree) if declared_working_tree is not None else args.repo_path
    )
    try:
        landed_sha = advance_repo_to_merged_sha(
            git_tree_path,
            base_branch=base_branch,
            known_merged_sha=known_merged_sha,
        )
    except TreeSyncError as exc:
        _fail(
            f"post-merge working-tree sync FAILED -- {exc}",
            code=EXIT_POST_MERGE_FAILED,
        )
    print(
        f"post-merge: working tree at {git_tree_path} advanced to merged "
        f"SHA {landed_sha!r}",
        file=sys.stderr,
    )

    # 6. Load + run post_merge_steps -- the SAME loader/runner merge.verb's
    # own step 10 already calls. A repo with no declared steps is a no-op,
    # exactly like an ordinary merge that never configured any.
    try:
        steps = load_post_merge_steps(args.repo_path)
    except PostMergeConfigError as exc:
        _fail(
            f"post-merge config FAILED to load -- {exc}",
            code=EXIT_POST_MERGE_FAILED,
        )
    if not steps:
        print(
            f"post-merge: no post_merge_steps configured for {args.repo_path} "
            f"-- nothing to run",
            file=sys.stderr,
        )
        print(json.dumps({"pr_number": args.pr_number, "owner": owner, "repo": repo, "steps_run": 0}))
        return EXIT_OK

    print(
        f"post-merge: running {len(steps)} post-merge step(s) in {args.repo_path}",
        file=sys.stderr,
    )
    deployment_env_overrides = resolve_env_overrides()
    # lr-d6e52b: SAME repo-tier default-timeout resolution merge.verb's own
    # step 10 uses -- a standalone re-run gets the identical bound a
    # merge-embedded run would have.
    try:
        default_step_timeout = resolve_post_merge_step_timeout_seconds(args.repo_path)
    except PostMergeConfigError as exc:
        _fail(
            f"post-merge config FAILED to load -- {exc}",
            code=EXIT_POST_MERGE_FAILED,
        )
    try:
        run_post_merge_steps(
            steps,
            args.repo_path,
            deployment_env_overrides=deployment_env_overrides,
            default_timeout_seconds=default_step_timeout,
        )
    except (PostMergeStepFailedError, PostMergeStepTimeoutError, PostMergeLivenessError) as exc:
        _fail(str(exc), code=EXIT_POST_MERGE_FAILED)

    print(f"post-merge: PR #{args.pr_number} in {owner}/{repo} post-merge steps completed")
    print(
        json.dumps(
            {"pr_number": args.pr_number, "owner": owner, "repo": repo, "steps_run": len(steps)}
        )
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
