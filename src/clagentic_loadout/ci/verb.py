"""ci.verb -- `clagentic-loadout ci rerun` and `clagentic-loadout ci cancel`.

Re-run the whole of one named CI run, re-run only its failed jobs, or cancel it,
on one repository, with the credential of the calling role. It adds no other
write capability: no push, no comment, no workflow dispatch, no setting change.

  clagentic-loadout ci rerun  [--failed-jobs] --caller ROLE --platform P --repo OWNER/NAME --run ID
  clagentic-loadout ci cancel                 --caller ROLE --platform P --repo OWNER/NAME --run ID

The caller names the operation; the verb never chooses between them. It makes
one request and does not retry. It does not refuse because the run's commit has
moved, because the run was re-run before, or for any reason of its own: a host
answer is reported as the host gave it. How often an agent uses the verb is the
agent's own discipline, not a check in here.

A run stuck queued or in progress cannot be re-run until it is cancelled:
``ci cancel`` it, then ``ci rerun`` it once it reads completed. Those are two
calls and two records.

Every call that reaches the host (and every one that is refused before it, for a
missing operation or credential) is recorded through the telemetry sink: the
verb, the operation, the caller, the platform, the repository, the run, the
commit the run was started for (read before the call), the HTTP status and the
UTC time. The default sink is none, which writes nothing; the call is the same.

Which platform offers which operation is data in each backend. GitHub offers all
three. Forgejo offers cancel only; ``rerun`` and ``rerun --failed-jobs`` there
exit with ``not supported`` rather than guessing at an endpoint.

Exit codes are in ``ci.exits``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

from clagentic_loadout._version import get_version
from clagentic_loadout.ci.backend import (
    OPERATION_CANCEL,
    OPERATION_RERUN,
    OPERATION_RERUN_FAILED_JOBS,
    CiBackend,
    HostAnswer,
)
from clagentic_loadout.ci.exits import (
    EXIT_CALLER_INVOKER_MISMATCH,
    EXIT_FORBIDDEN,
    EXIT_HOST_ERROR,
    EXIT_NOT_SUPPORTED,
    EXIT_OK,
    EXIT_RUN_NOT_FOUND,
    EXIT_STATE_REFUSED,
    EXIT_TOKEN_FETCH_FAILED,
    EXIT_USAGE,
)
from clagentic_loadout.ci.forgejo_backend import ForgejoCiBackend
from clagentic_loadout.ci.github_backend import GithubCiBackend
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.push.errors import RemoteResolutionError
from clagentic_loadout.push.git_coords import parse_owner_repo
from clagentic_loadout.telemetry.emitter import emit_ci_call
from clagentic_loadout.telemetry.sink import SinkConfigError, TelemetrySink
from clagentic_loadout.transport.attestation import AttestationError
from clagentic_loadout.transport.attestation import resolve_bound_identity as _resolve_identity
from clagentic_loadout.transport.caller_binding import (
    CallerBindingError,
    bind_caller,
    describe_omitted_caller_behavior,
    resolve_for_binding,
)
from clagentic_loadout.transport.credential_provider import (
    DEFAULT_ROLE,
    CredentialProviderError,
    TokenProvider,
)
from clagentic_loadout.transport.credential_provider import resolve_token as _resolve_token
from clagentic_loadout.transport.git_host_api import (
    DEFAULT_GIT_HOST_BASE_URL,
    GIT_HOST_BASE_URL_ENV_VAR,
    _resolve_git_host_base,
)
from clagentic_loadout.transport.provider_config import resolve_platform_provider
from clagentic_loadout.transport.seam_binding import provider_fields

_PROG = "clagentic-loadout ci"

#: Operations each platform's host offers. Read from the backend classes, so the
#: check before any credential or network call and the backend cannot disagree.
_SUPPORTED: dict[str, frozenset[str]] = {
    PLATFORM_GITHUB: GithubCiBackend.supported,
    PLATFORM_FORGEJO: ForgejoCiBackend.supported,
}

_STUCK_RUN_HINT = (
    "A run stuck queued or in progress cannot be re-run: cancel it with `ci cancel`, "
    "then re-run it with `ci rerun` once it reads completed."
)


class CiUsageError(Exception):
    """A usage failure that ends the process with EXIT_USAGE."""


@dataclass(frozen=True)
class Outcome:
    """The result of one call, before it is recorded and printed."""

    code: int
    name: str  # a telemetry-ci-call outcome value
    http_status: int
    head_sha: str | None
    message: str


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}")
    return value


def _build_arg_parser(operation: str) -> argparse.ArgumentParser:
    rerun = operation == OPERATION_RERUN
    if rerun:
        summary = (
            "re-run one CI run: the whole run, or with --failed-jobs only its failed jobs."
        )
    else:
        summary = "cancel one CI run that is queued or running."
    parser = argparse.ArgumentParser(
        prog=f"{_PROG} {'rerun' if rerun else 'cancel'}",
        description=(
            f"{_PROG} {'rerun' if rerun else 'cancel'} -- {summary} It makes one request "
            f"with the --caller role's credential, does not retry, and reports the host's "
            f"answer as it is. {_STUCK_RUN_HINT}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            + (
                f"  {_PROG} rerun --caller some-role --platform github \\\n"
                "      --repo some-owner/some-repo --run 1234 --failed-jobs\n"
                f"  {_PROG} rerun --caller some-role --platform github \\\n"
                "      --repo some-owner/some-repo --run 1234\n"
                if rerun
                else f"  {_PROG} cancel --caller some-role --platform forgejo \\\n"
                "      --repo some-owner/some-repo --run 1234\n"
            )
            + "\nForgejo offers cancel only; a re-run there exits with 'not supported'.\n"
            "Each call is recorded through the configured telemetry sink (default: none).\n"
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"{_PROG} {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    parser.add_argument(
        "--caller",
        default=None,
        help="Role whose credential is used. It must match this process's own attested "
        "invoking identity or the call is refused before any request. "
        f"{describe_omitted_caller_behavior()}",
    )
    parser.add_argument(
        "--platform", required=True, choices=(PLATFORM_GITHUB, PLATFORM_FORGEJO),
        help="Git host the repository is on.",
    )
    parser.add_argument("--repo", required=True, help="owner/name of the repository.")
    parser.add_argument(
        "--run", required=True, type=_positive_int, dest="run_id",
        help="Id of the CI run to act on.",
    )
    if rerun:
        parser.add_argument(
            "--failed-jobs", action="store_true", dest="failed_jobs",
            help="Re-run only the failed jobs of the run instead of the whole run.",
        )
    parser.add_argument(
        "--git-host-base-url", default=None,
        help=f"Forgejo API base URL (default: ${GIT_HOST_BASE_URL_ENV_VAR}, then the "
        f"user-level config, then {DEFAULT_GIT_HOST_BASE_URL!r}). Ignored for GitHub.",
    )
    return parser


def _classify(answer: HostAnswer) -> tuple[int, str]:
    if answer.accepted:
        return EXIT_OK, "ok"
    if answer.status == 404:
        return EXIT_RUN_NOT_FOUND, "not_found"
    if answer.status == 403:
        return EXIT_FORBIDDEN, "forbidden"
    if answer.status in (409, 422):
        return EXIT_STATE_REFUSED, "state_refused"
    return EXIT_HOST_ERROR, "host_error"


def _describe_failure(
    outcome_name: str, *, operation: str, platform: str, repo: str, run_id: int,
    caller: str, answer: HostAnswer,
) -> str:
    where = f"{operation} of run {run_id} in {repo} on {platform}"
    host = f"the host answered {answer.status}" if answer.status else "the host gave no answer"
    detail = f": {answer.message}" if answer.message else ""
    text = f"{where} failed: {host}{detail}"
    if outcome_name == "forbidden":
        text += f". The credential of role {caller!r} may not {operation} runs in {repo}."
    elif outcome_name == "state_refused":
        text += f". {_STUCK_RUN_HINT}"
    elif outcome_name == "not_found":
        text += ". No such run in that repository."
    return text


def _execute(
    *, operation: str, platform: str, owner: str, repo: str, run_id: int, caller: str,
    git_host_base: str, token_provider: TokenProvider | None, opener,
) -> Outcome:
    """Everything between the attested caller and the host's answer. Never raises
    for a host or credential failure: each is an Outcome."""
    if operation not in _SUPPORTED[platform]:
        offered = ", ".join(sorted(_SUPPORTED[platform])) or "none"
        return Outcome(
            EXIT_NOT_SUPPORTED, "not_supported", 0, None,
            f"{platform} does not offer the {operation!r} operation (it offers: {offered}). "
            f"No request was made.",
        )

    provider = token_provider if token_provider is not None else resolve_platform_provider(platform)
    try:
        token = _resolve_token(caller, provider, repo=f"{owner}/{repo}")
    except CredentialProviderError as exc:
        return Outcome(
            EXIT_TOKEN_FETCH_FAILED, "token_failed", 0, None,
            f"no credential could be resolved for role {caller!r}: {exc}",
        )

    backend: CiBackend
    if platform == PLATFORM_GITHUB:
        backend = GithubCiBackend(token, opener=opener)
    else:
        backend = ForgejoCiBackend(token, git_host_base=git_host_base, opener=opener)

    # The run's commit is read first so the record names the commit the call was
    # made against. A run that cannot be read does not stop the operation: the
    # host's answer to the operation itself is the authoritative one.
    head_sha = backend.read_head_sha(owner, repo, run_id)
    answer = backend.send(operation, owner, repo, run_id)
    code, name = _classify(answer)
    message = (
        ""
        if code == EXIT_OK
        else _describe_failure(
            name, operation=operation, platform=platform, repo=f"{owner}/{repo}",
            run_id=run_id, caller=caller, answer=answer,
        )
    )
    return Outcome(code, name, answer.status, head_sha, message)


def _record(
    outcome: Outcome, *, operation: str, caller: str, platform: str, repo: str,
    run_id: int, sink: TelemetrySink | None,
) -> None:
    try:
        emit_ci_call(
            operation=operation, caller=caller, platform=platform, repo=repo, run_id=run_id,
            head_sha=outcome.head_sha, http_status=outcome.http_status,
            outcome=outcome.name, sink=sink,
        )
    except SinkConfigError as exc:
        # A telemetry sink that is misconfigured must not change what the host did.
        print(f"ci: the call was made but could not be recorded: {exc}", file=sys.stderr)


def _run(
    args: argparse.Namespace, operation: str, *, token_provider, opener, identity_provider,
    sink: TelemetrySink | None,
) -> int:
    try:
        owner, repo = parse_owner_repo(args.repo)
    except RemoteResolutionError as exc:
        raise CiUsageError(str(exc)) from exc

    resolve_identity = identity_provider if identity_provider is not None else _resolve_identity
    try:
        attested = resolve_for_binding(
            caller_explicit=args.caller is not None,
            caller=args.caller or DEFAULT_ROLE,
            resolve_identity_fn=resolve_identity,
        )
    except AttestationError as exc:
        print(f"ci: attested-identity resolution failed: {exc}", file=sys.stderr)
        return EXIT_CALLER_INVOKER_MISMATCH
    caller = args.caller if args.caller is not None else attested.subject
    bind_caller(caller, caller_explicit=True, identity=attested)

    outcome = _execute(
        operation=operation, platform=args.platform, owner=owner, repo=repo,
        run_id=args.run_id, caller=caller,
        git_host_base=_resolve_git_host_base(args.git_host_base_url),
        token_provider=token_provider, opener=opener,
    )
    _record(
        outcome, operation=operation, caller=caller, platform=args.platform,
        repo=f"{owner}/{repo}", run_id=args.run_id, sink=sink,
    )
    if outcome.code != EXIT_OK:
        print(f"ci: {outcome.message}", file=sys.stderr)
        return outcome.code
    print(json.dumps({
        "operation": operation, "platform": args.platform, "repo": f"{owner}/{repo}",
        "run": args.run_id, "head_sha": outcome.head_sha, "status": outcome.http_status,
        **provider_fields(attested),
    }))
    return EXIT_OK


def main(
    argv: list[str] | None = None,
    *,
    operation: str,
    token_provider: TokenProvider | None = None,
    opener=None,
    identity_provider=None,
    sink: TelemetrySink | None = None,
) -> int:
    """Entry point for one of the verbs. *operation* is OPERATION_RERUN or
    OPERATION_CANCEL; ``--failed-jobs`` turns a rerun into OPERATION_RERUN_FAILED_JOBS.
    Returns the exit code. *token_provider*, *opener*, *identity_provider* and *sink*
    are injection points for tests and for a deployment wiring its own."""
    if argv is None:
        argv = sys.argv[1:]
    parser = _build_arg_parser(operation)
    if any(arg in ("--help", "-h") for arg in argv):
        parser.print_help()
        return EXIT_OK
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on a usage error; this verb's own usage code is 1.
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    if operation == OPERATION_RERUN and args.failed_jobs:
        operation = OPERATION_RERUN_FAILED_JOBS
    try:
        return _run(
            args, operation, token_provider=token_provider, opener=opener,
            identity_provider=identity_provider, sink=sink,
        )
    except CiUsageError as exc:
        print(f"ci: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except CallerBindingError as exc:
        print(f"ci: {exc}", file=sys.stderr)
        return EXIT_CALLER_INVOKER_MISMATCH


def rerun_main(argv: list[str]) -> int:
    return main(argv, operation=OPERATION_RERUN)


def cancel_main(argv: list[str]) -> int:
    return main(argv, operation=OPERATION_CANCEL)
