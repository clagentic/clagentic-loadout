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
--repo-path <dir>` runs the repo-path/slug consistency check
(merge.repo_path_consistency, lr-4522a3 — --repo-path is REQUIRED here, so
this always has a tree to evaluate; refuses pre-mint on a mismatch, naming
both values, without flagging a merely differently-named checkout like the
'.github' org-profile shape), then the two links that matter for a re-run —
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
`known_merged_sha` when the platform reports one), running
`post_merge_steps` (via the SAME `merge.post_merge.run_post_merge_steps` +
`merge.post_merge_config.load_post_merge_steps` merge.verb's own step 10
already uses), and (lr-cd3644) landing `--repo-path` on `base_branch`
afterward via the SAME `merge.tree_sync.land_on_base_branch` merge.verb's own
step 10 calls. NO STEP OF THIS EXECUTION PATH IS RE-IMPLEMENTED — every gate,
every tree-sync call, every step-runner call is the identical function
merge.verb's step 10 calls; this verb differs from merge.verb only in WHICH
gates run before reaching that shared tail (no stale-SHA/verdict/diff-scope/
title/CI-status gate — those already did their job at merge time and this
verb never lands new code) and in NOT calling `backend.merge_pr` at all.

LANDING ON base_branch IS UNCONDITIONAL HERE (lr-cd3644, unlike merge.verb's
own step 10): merge.verb only checks `--repo-path` out (and only calls
`land_on_base_branch` afterward) when at least one `post_merge_steps` entry
will actually run this invocation (lr-173768 — a checkout that serves
nothing must never mutate a shared checkout). This verb has NO such
fetch-only branch: its step 5 ALWAYS performs a real, detached checkout via
`advance_repo_to_merged_sha`, because its entire purpose is to run (or
positively confirm there is nothing to run for) `post_merge_steps` against a
real, populated tree — there is no shape of this verb's invocation where
nothing will read the checked-out files. Before this fix, that unconditional
checkout was never followed by a land step: every standalone
`loadout-post-merge` invocation left `--repo-path` PERMANENTLY DETACHED at
the merged SHA, regardless of whether any steps ran, with no way for the
next dispatch into that tree to land anywhere useful (the observed incident:
a release-authority caller's `--repo-path` was found detached at the merged
SHA after invoking this verb standalone, while the tree's local `main`
branch still pointed at an EARLIER merge — see `merge.verb`'s own module
docstring, "STALE PRE-SYNC CONFIG DRIFT CHECK", for the sibling defect this
same incident surfaced).
`land_on_base_branch` now runs after step 6 regardless of `steps_run` — a
ref repoint (`git checkout -B`) onto the SAME `landed_sha` step 5 already
verified, never a merge/rebase, so it cannot diverge from what the server
already decided.

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
from clagentic_loadout.merge.repo_path_consistency import assert_repo_path_consistent
from clagentic_loadout.merge.tree_sync import (
    TreeSyncError,
    advance_repo_to_merged_sha,
    land_on_base_branch,
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
    resolve_bound_identity as _resolve_identity,
)
from clagentic_loadout.transport.caller_binding import (
    CallerBindingError,
    bind_caller,
    resolve_for_binding as _resolve_for_binding,
)
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
#: An OMITTED --role is ALSO bound to the attested identity now (lr-620837
#: operator ruling): this code also fires when NO attested identity can be
#: resolved at all for an omitted --role (transport.attestation.
#: AttestationError / BoundAttestationError propagating through
#: resolve_for_binding), not only on an explicit mismatch -- see
#: transport.caller_binding's own module docstring for the full behavior
#: change.
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
        f"resolved via the credential provider. The SAME authority seam "
        f"merge.verb/merge.close_verb consume -- a role authorized to "
        f"merge/close a PR is authorized to re-run its post-merge deploy. "
        f"Already-attested, opaque config key downstream. It must match "
        f"this process's own attested invoking identity (transport."
        f"attestation.resolve_bound_identity) or the call is refused "
        f"fail-closed before any I/O (transport.caller_binding.bind_caller)."
        f" OMITTED behaves as an IMPLICIT claim of 'act as my own attested "
        f"identity': the effective role becomes the resolved identity's "
        f"own subject, never {DEFAULT_ROLE!r} by itself -- a process with "
        f"no attested identity at all is refused the same way an explicit "
        f"mismatch is.",
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
    `transport.attestation.resolve_bound_identity`, the caller-BOUND
    resolver an operator ruling requires -- never falls through to the
    built-in OS-user layer) -- the injection point for the fail-closed
    --role/attested-invoker binding (transport.caller_binding.
    bind_caller), mirroring the identical parameter transport.git_host_api.main
    already carries for the same purpose. ALWAYS called now (lr-620837
    operator ruling): transport.caller_binding.resolve_for_binding no
    longer skips resolution on an omitted --role -- an omitted --role
    derives its effective role from the resolved identity's own subject,
    so a process with no attested identity at all refuses here too.
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

    # lr-4522a3: --repo-path is REQUIRED on this verb (unlike merge.verb's
    # optional override), so this check always has a tree to evaluate.
    # Refuse BEFORE any credential mint when that tree's own origin remote
    # names a different owner/repo than --repo -- see
    # merge.repo_path_consistency's module docstring for the full
    # rationale and the '.github' org-profile shape this never flags.
    assert_repo_path_consistent(args.repo, args.repo_path)

    # --role/attested-invoker fail-closed binding (lr-c75c9a, mirrors
    # transport.git_host_api's identical check; OMITTED-CALLER FIX,
    # lr-620837 operator ruling): checked BEFORE any
    # I/O -- before the namespace guard (step 1), before any token mint.
    # Resolution is now UNCONDITIONAL (resolve_for_binding no longer skips
    # it on an omitted --role -- see that function's own docstring for the
    # full ruling): an omitted --role derives its EFFECTIVE role from the
    # resolved attested identity's own subject, never DEFAULT_ROLE by
    # itself, so a process with no attested identity at all is refused here
    # exactly like an explicit mismatched --role always was.
    resolve_identity_fn = identity_provider if identity_provider is not None else _resolve_identity
    try:
        attested_identity = _resolve_for_binding(
            caller_explicit=args.role is not None,
            caller=args.role or DEFAULT_ROLE,
            resolve_identity_fn=resolve_identity_fn,
        )
    except AttestationError as exc:
        _fail(f"attested-identity resolution FAILED -- {exc}", code=EXIT_CALLER_INVOKER_MISMATCH)
    role = args.role if args.role is not None else attested_identity.subject
    bind_caller(role, caller_explicit=True, identity=attested_identity)

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
    #
    # lr-cd3644 fold-in #4 (PR #30 re-review finding): step 5 above has
    # ALREADY performed a real, verified checkout by this point (unlike
    # merge.verb's own step 10, this verb has no fetch-only branch -- see
    # step 7's own comment below) -- so EVERY
    # statement between that checkout and the land call must run inside the
    # SAME guard, including config load itself. The prior shape called
    # `load_post_merge_steps` in its own try/except BEFORE the try/except/
    # else that wrapped only the run step -- a `PostMergeConfigError` there
    # raised straight through `_fail()` and unwound past the land call
    # entirely, leaving a malformed-config invocation with --repo-path
    # PERMANENTLY DETACHED at landed_sha, the exact "no signal to the next
    # dispatch" defect class this task already closed for a step-run
    # failure. `load_post_merge_steps`, `resolve_post_merge_step_timeout_
    # seconds`, and `run_post_merge_steps` are therefore now ALL inside one
    # `try`, landed via a single `finally` clause (the task's own preferred
    # shape): `land_on_base_branch` runs exactly once, on EVERY exit path
    # from the try body -- success, a config-load failure, or a step
    # failure -- and a land failure is logged but never allowed to replace
    # an already-in-flight ORIGINAL exception (Python re-raises the
    # original on an uncaught exception from `finally` only if the finally
    # block itself doesn't raise; this catches TreeSyncError explicitly
    # inside `finally` so it can never mask or replace the original error).
    steps_run = 0
    original_exc: BaseException | None = None
    try:
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
        else:
            print(
                f"post-merge: running {len(steps)} post-merge step(s) in {args.repo_path}",
                file=sys.stderr,
            )
            deployment_env_overrides = resolve_env_overrides()
            # lr-d6e52b: SAME repo-tier default-timeout resolution merge.verb's
            # own step 10 uses -- a standalone re-run gets the identical bound a
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
            except (
                PostMergeStepFailedError,
                PostMergeStepTimeoutError,
                PostMergeLivenessError,
            ) as exc:
                _fail(str(exc), code=EXIT_POST_MERGE_FAILED)
            steps_run = len(steps)
            print(f"post-merge: PR #{args.pr_number} in {owner}/{repo} post-merge steps completed")
    except PostMergeVerbError as exc:
        original_exc = exc
        raise
    finally:
        # 7. Land --repo-path on base_branch (lr-cd3644, hardened fold-in
        # #4): step 5 above ALWAYS performs a real, detached checkout via
        # advance_repo_to_merged_sha -- unlike merge.verb's own step 10,
        # this verb has no fetch-only branch, since its WHOLE PURPOSE is to
        # run (or confirm there is nothing to run for) post_merge_steps
        # against a real, populated checkout. Before the original fix, that
        # checkout was never followed by a land step at all; before THIS
        # fix, a config-load failure specifically still skipped it (see the
        # comment above the outer `try`). Mirrors merge.verb's own step 10
        # land_on_base_branch call exactly: a ref repoint (`git checkout
        # -B`) onto the SAME landed_sha already verified above, never a
        # merge/rebase, so it cannot diverge from what the server already
        # decided. Runs UNCONDITIONALLY here (not gated on steps_run, the
        # way merge.verb's own call is gated on steps_will_run) because the
        # checkout above is itself unconditional on this verb -- there is
        # always a detached HEAD to move off of by the time this `finally`
        # runs, on EVERY exit path (return or raise) from the try body.
        try:
            landed_branch_sha = land_on_base_branch(
                git_tree_path,
                base_branch=base_branch,
                landed_sha=landed_sha,
            )
        except TreeSyncError as land_exc:
            if original_exc is not None:
                # A post-merge failure (config load or step run) already
                # occurred -- the ORIGINAL exception (and its ORIGINAL exit
                # code) is always what gets reported; a land failure here
                # is logged but deliberately swallowed rather than masking
                # the original error or replacing it via an exception
                # raised out of `finally`.
                print(
                    f"post-merge: WARNING -- working tree at {git_tree_path} "
                    f"could NOT be landed on {base_branch!r} after an "
                    f"earlier post-merge failure -- {land_exc}. The tree "
                    f"remains detached at the merged commit; the ORIGINAL "
                    f"post-merge failure (reported below) is still "
                    f"authoritative.",
                    file=sys.stderr,
                )
            else:
                _fail(
                    f"post-merge working-tree sync FAILED -- {land_exc}",
                    code=EXIT_POST_MERGE_FAILED,
                )
        else:
            if original_exc is not None:
                print(
                    f"post-merge: working tree at {git_tree_path} landed on "
                    f"{base_branch!r} at {landed_branch_sha!r} despite an "
                    f"earlier post-merge failure (reported below)",
                    file=sys.stderr,
                )
            else:
                print(
                    f"post-merge: working tree at {git_tree_path} landed on "
                    f"{base_branch!r} at {landed_branch_sha!r}",
                    file=sys.stderr,
                )

    print(
        json.dumps(
            {"pr_number": args.pr_number, "owner": owner, "repo": repo, "steps_run": steps_run}
        )
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
