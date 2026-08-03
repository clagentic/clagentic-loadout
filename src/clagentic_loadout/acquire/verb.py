"""acquire.verb — the PR content-acquisition CLI: fetch a PR's diff/changed-
file list (and, optionally, scannable file content staged to scratch) from
the HOST API, on either transport, behind one contract.

lr-c17040 (tome #687 EPIC E). Mirrors review.verb's dispatch shape:

  1. Resolve platform (--platform, mandatory).
  2. Platform guard fires BEFORE any credential mint or API call.
  3. Resolve the caller's token via transport.credential_provider.
  4. Build the AcquireBackend for the resolved platform.
  5. backend.fetch_pr_content() — never touches a local working tree; the
     base/head SHAs are read from the PR's own metadata via the host API,
     never a locally-resolved git ref (this is what makes wrapper-cwd and
     stale-local-main non-issues by construction — see acquire.contract's
     module docstring).
  6. Optionally (--stage-scratch) writes the fetched diff + changed-file
     content to a per-spawn TMPDIR scratch directory (acquire.scratch) a
     security scanner can be pointed at without any local checkout.

This is a READ-ONLY verb: it fetches and optionally stages content, it never
posts anything. It has no relationship to review.verb's --body-stdin/
--verdict-* machinery, and does not construct or parse the fenced
```review-result``` block (merge.verdict's job).
"""

from __future__ import annotations

import argparse
import json
import sys

from clagentic_loadout._version import get_version
from clagentic_loadout.acquire.contract import AcquireBackend
from clagentic_loadout.acquire.errors import AcquireFetchError, PlatformMismatchError, ScratchWriteError
from clagentic_loadout.acquire.forgejo_backend import ForgejoAcquireBackend
from clagentic_loadout.acquire.github_backend import GithubAcquireBackend
from clagentic_loadout.acquire.scratch import write_scratch_content
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
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
# Exit codes -- one reserved range for this verb, distinct from review.verb's
# and transport.git_host_api's own tables.
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_TOKEN_FETCH_FAILED = 2
EXIT_WRONG_PLATFORM = 4
EXIT_FETCH_FAILED = 5
EXIT_SCRATCH_WRITE_FAILED = 6
#: An EXPLICIT --caller value does not match the ATTESTED invoking identity
#: this process's own attestation-provider chain resolved
#: (transport.caller_binding.bind_caller, lr-c75c9a -- the same fail-closed
#: binding transport.git_host_api's EXIT_CALLER_INVOKER_MISMATCH already
#: enforced; this verb now enforces it too). FAILS CLOSED BEFORE ANY I/O --
#: no token mint, no fetch is ever attempted. An OMITTED --caller never
#: triggers this (see bind_caller's own docstring) -- it is unchanged,
#: existing behavior.
EXIT_CALLER_INVOKER_MISMATCH = 7


class AcquireVerbError(Exception):
    """Raised for any acquire-verb failure that should terminate the process
    with a specific exit code. Carries the intended exit code as `.code`."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _fail(message: str, code: int) -> None:
    raise AcquireVerbError(message, code)


def assert_platform_is_forgejo(owner: str, repo: str, *, explicit_platform: str) -> None:
    """Mirror-image guard for the Forgejo backend (mirrors review.verb's
    function of the same name exactly): fires BEFORE any credential mint or
    API call when the caller's own --platform value says the target is NOT
    Forgejo."""
    if explicit_platform not in (PLATFORM_GITHUB, PLATFORM_FORGEJO):
        raise PlatformMismatchError(
            f"assert_platform_is_forgejo: unrecognized platform "
            f"{explicit_platform!r} for {owner}/{repo}. Expected "
            f"{PLATFORM_GITHUB!r} or {PLATFORM_FORGEJO!r}."
        )
    if explicit_platform != PLATFORM_FORGEJO:
        raise PlatformMismatchError(
            f"WRONG PLATFORM -- {owner}/{repo} PR resolves to platform "
            f"{explicit_platform!r}, but this backend only fetches from "
            f"Forgejo. Use the GitHub backend instead. Refusing before "
            f"minting any credential or making any API call."
        )


def assert_platform_is_github(owner: str, repo: str, *, explicit_platform: str) -> None:
    """Mirror-image guard for the GitHub backend."""
    if explicit_platform not in (PLATFORM_GITHUB, PLATFORM_FORGEJO):
        raise PlatformMismatchError(
            f"assert_platform_is_github: unrecognized platform "
            f"{explicit_platform!r} for {owner}/{repo}. Expected "
            f"{PLATFORM_GITHUB!r} or {PLATFORM_FORGEJO!r}."
        )
    if explicit_platform != PLATFORM_GITHUB:
        raise PlatformMismatchError(
            f"WRONG PLATFORM -- {owner}/{repo} PR resolves to platform "
            f"{explicit_platform!r}, but this backend only fetches from "
            f"GitHub. Use the Forgejo backend instead. Refusing before "
            f"minting any credential or making any API call."
        )


def build_backend(
    platform: str,
    *,
    owner: str,
    repo: str,
    caller: str,
    git_host_base: str,
    token_provider: TokenProvider | None,
    opener,
) -> AcquireBackend:
    """Resolve platform guard -> mint/resolve token -> construct the
    matching AcquireBackend. The platform guard ALWAYS runs before token
    resolution, mirroring review.verb.build_backend's ordering exactly."""
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
        return GithubAcquireBackend(token, opener=opener)
    return ForgejoAcquireBackend(token, git_host_base=git_host_base, opener=opener)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acquire",
        description=(
            "acquire -- fetch a PR's diff/changed-file list from the HOST "
            "API (never a local working tree), on either Forgejo or "
            "GitHub, behind one contract. base_sha/head_sha are always "
            "read from the PR's own metadata, never a locally-resolved git "
            "ref -- wrapper-cwd and stale-local-main are non-issues by "
            "construction. --stage-scratch additionally writes the fetched "
            "content to a per-spawn TMPDIR scratch directory a security "
            "scanner can be pointed at without a local checkout."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  acquire --caller reviewer --platform github "
            "some-owner/some-repo 42\n"
            "\n"
            "Scanner-staging route:\n"
            "  acquire --caller security --platform forgejo "
            "--git-host-base-url http://git-host.example.com "
            "--stage-scratch some-owner/some-repo 42\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"acquire {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    parser.add_argument(
        "--caller",
        default=None,
        help=f"Role/name whose token is resolved via the credential "
        f"provider (default: {DEFAULT_ROLE!r}). A role/caller, never a "
        f"hardcoded agent name. Already-attested, opaque config key "
        f"downstream (the credential provider never re-authenticates it "
        f"itself -- see transport.credential_provider's module docstring). "
        f"When EXPLICITLY supplied, it must ALSO match this process's own "
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
        "independently via platform_detect.resolve_platform, e.g. from a "
        "dispatch envelope's pr_url).",
    )
    parser.add_argument(
        "--git-host-base-url",
        default=None,
        help=f"Forgejo API base URL (default: ${GIT_HOST_BASE_URL_ENV_VAR} "
        f"env var, falling back to a configurable compat-alias env var if "
        f"that is unset, or {DEFAULT_GIT_HOST_BASE_URL!r} if neither is "
        f"set). Ignored for the GitHub platform.",
    )
    parser.add_argument(
        "--stage-scratch",
        action="store_true",
        help="Fetch each changed file's post-change content (in addition "
        "to the diff/file-list) and stage it, along with the whole-PR "
        "diff text, to a per-spawn TMPDIR scratch directory (acquire."
        "scratch.write_scratch_content) -- for a security scanner that "
        "needs real files on disk rather than a diff blob. Never writes "
        "anywhere in the repo tree (CLAUDE.md rule 7). Without this flag, "
        "only the diff text and changed-filename list are fetched.",
    )
    parser.add_argument("owner_repo", help="owner/repo of the target PR.")
    parser.add_argument("pr_number", help="PR number.")
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
    sys.exit itself so it stays testable).

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
    except AcquireVerbError as exc:
        print(f"acquire: {exc}", file=sys.stderr)
        return exc.code
    except CallerBindingError as exc:
        print(f"acquire: {exc}", file=sys.stderr)
        return EXIT_CALLER_INVOKER_MISMATCH


def _run(
    args: argparse.Namespace,
    *,
    token_provider: TokenProvider | None,
    opener,
    identity_provider=None,
) -> int:
    owner, repo = _parse_owner_repo(args.owner_repo)

    try:
        pr_number = int(args.pr_number)
        if pr_number <= 0:
            raise ValueError("non-positive")
    except (TypeError, ValueError):
        _fail(f"pr_number must be a positive integer, got: {args.pr_number!r}", code=EXIT_USAGE)

    caller = args.caller or DEFAULT_ROLE

    # --caller/attested-invoker fail-closed binding (lr-c75c9a, mirrors
    # transport.git_host_api's identical check): checked BEFORE any I/O --
    # before the platform guard, before any token mint.
    resolve_identity_fn = identity_provider if identity_provider is not None else _resolve_identity
    try:
        attested_identity = resolve_identity_fn()
    except AttestationError as exc:
        _fail(f"attested-identity resolution FAILED -- {exc}", code=EXIT_CALLER_INVOKER_MISMATCH)
    bind_caller(caller, caller_explicit=args.caller is not None, identity=attested_identity)

    git_host_base = _resolve_git_host_base(args.git_host_base_url)

    print(
        f"acquire: platform={args.platform!r} caller={caller!r} "
        f"target={owner}/{repo}#{pr_number}",
        file=sys.stderr,
    )

    try:
        backend = build_backend(
            args.platform,
            owner=owner,
            repo=repo,
            caller=caller,
            git_host_base=git_host_base,
            token_provider=token_provider,
            opener=opener,
        )
    except PlatformMismatchError as exc:
        _fail(str(exc), code=EXIT_WRONG_PLATFORM)

    try:
        acquired = backend.fetch_pr_content(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            include_file_contents=args.stage_scratch,
        )
    except AcquireFetchError as exc:
        _fail(str(exc), code=EXIT_FETCH_FAILED)
        return EXIT_FETCH_FAILED  # unreachable; _fail raises

    result = {
        "owner": owner,
        "repo": repo,
        "pr_number": pr_number,
        "base_sha": acquired.base_sha,
        "head_sha": acquired.head_sha,
        "changed_files": acquired.changed_filenames,
        "diff_text": acquired.diff_text,
    }

    if args.stage_scratch:
        try:
            staged = write_scratch_content(acquired)
        except ScratchWriteError as exc:
            _fail(str(exc), code=EXIT_SCRATCH_WRITE_FAILED)
            return EXIT_SCRATCH_WRITE_FAILED  # unreachable; _fail raises
        result["scratch_root"] = str(staged.root)
        result["scratch_diff_path"] = str(staged.diff_path) if staged.diff_path else None
        result["scratch_files_dir"] = str(staged.files_dir)
        result["scratch_written_files"] = list(staged.written_files)

    print(json.dumps(result))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
