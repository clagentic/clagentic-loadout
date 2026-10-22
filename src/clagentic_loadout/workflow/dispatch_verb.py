"""workflow.dispatch_verb -- loadout-workflow-dispatch: start a named workflow
on a ref with given inputs, under the caller's own role credential.

Deliberately thin. The verb POSTs GitHub's `workflow_dispatch` endpoint

    POST /repos/{owner}/{repo}/actions/workflows/{file}/dispatches
    {"ref": "<ref>", "inputs": {...}}

and reports what came back. It never creates tags, releases or notes (the
repo's own workflow owns those), runs no release logic, and has no wait loop:
after the dispatch it makes at most ONE read of the newest `workflow_dispatch`
run for that workflow and ref, to report a run id and URL when the dispatch
response itself did not carry them.

ORDER OF CHECKS (each refuses before the next runs, so a refusal costs no
credential and no network call):
  1. Arguments parse; `--caller` is bound to the attested invoking identity
     (a local check, as in every caller-bound verb).
  2. The target platform resolves; a non-GitHub platform is refused (the
     endpoint is GitHub's).
  3. `--repo-path`'s origin remote must name `--repo` (derivable and equal),
     so the config read next belongs to the repo being dispatched against.
  4. The workflow file AND the ref must both be listed in the repo's config
     (`workflow_dispatch:`, see dispatch_config). The one policy check.
  5. The credential is resolved through the credential-provider seam.
  6. The dispatch.

Inputs are passed through as given; this verb does not interpret them.

Exit codes (this verb's own range; a child process's code is never reused):
    0   dispatch accepted
    1   usage error
    2   credential resolution failed
    4   platform not supported (anything but GitHub)
    30  workflow or ref not listed in the repo config
    31  repo config missing or malformed
    32  --caller does not match the attested invoking identity
    33  the host rejected or could not complete the dispatch
    34  the credential lacks the `actions: write` permission (HTTP 403)
    35  --repo-path's origin remote does not match --repo, or cannot be derived
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clagentic_loadout._version import get_version
from clagentic_loadout.merge.repo_path_consistency import check_repo_path_consistency
from clagentic_loadout.platform_detect import (
    PLATFORM_FORGEJO,
    PLATFORM_GITHUB,
    PlatformResolutionError,
    resolve_platform,
)
from clagentic_loadout.push.errors import RemoteResolutionError
from clagentic_loadout.push.git_coords import parse_owner_repo, read_remote_url_best_effort
from clagentic_loadout.transport import github_client
from clagentic_loadout.transport.attestation import (
    AttestationError,
    resolve_bound_identity as _resolve_identity,
)
from clagentic_loadout.transport.caller_binding import (
    CallerBindingError,
    bind_caller,
    describe_omitted_caller_behavior as _describe_omitted_caller,
    resolve_for_binding as _resolve_for_binding,
)
from clagentic_loadout.transport.credential_provider import (
    CredentialProviderError,
    DEFAULT_ROLE,
    TokenProvider,
    resolve_token as _resolve_token,
)
from clagentic_loadout.transport.provider_config import resolve_platform_provider
from clagentic_loadout.workflow.dispatch_config import (
    DispatchConfigError,
    DispatchNotListedError,
    check_dispatch_allowed,
    is_valid_ref,
    is_valid_workflow_file,
    load_dispatch_allowlist,
)
from clagentic_loadout.repo_config import resolve_repo_config_path

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_TOKEN_FETCH_FAILED = 2
EXIT_UNSUPPORTED_PLATFORM = 4
EXIT_NOT_LISTED = 30
EXIT_CONFIG_INVALID = 31
EXIT_CALLER_INVOKER_MISMATCH = 32
EXIT_DISPATCH_FAILED = 33
EXIT_PERMISSION_DENIED = 34
EXIT_REPO_MISMATCH = 35

#: The permission a credential needs to dispatch a workflow; named in the 403
#: message so the fix is obvious.
REQUIRED_PERMISSION = "actions: write"

#: Reported beside a null run so a reader knows the dispatch itself succeeded.
_RUN_NOT_VISIBLE_NOTE = (
    "the dispatch was accepted; its run is not yet visible, so no run id is reported"
)

_INPUT_KEY_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_-]{0,127}\Z")


class DispatchVerbError(Exception):
    """A failure that ends the process with a specific exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _fail(message: str, code: int) -> None:
    raise DispatchVerbError(message, code)


def parse_inputs(pairs: list[str] | None) -> dict[str, str]:
    """Turn repeated KEY=VALUE arguments into the `inputs` mapping.

    The first `=` splits, so a value may itself contain `=`. A repeated key is
    a usage error rather than a silent last-wins.
    """
    inputs: dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not _INPUT_KEY_RE.match(key):
            _fail(
                f"--input {pair!r} must be KEY=VALUE with KEY made of letters, digits, "
                f"'_' or '-' and not starting with a digit or '-'.",
                EXIT_USAGE,
            )
        if key in inputs:
            _fail(f"--input key {key!r} was given more than once.", EXIT_USAGE)
        inputs[key] = value
    return inputs


def _dispatch_url(owner: str, repo: str, workflow: str) -> str:
    return (
        f"{github_client.GITHUB_API_BASE}/repos/{owner}/{repo}"
        f"/actions/workflows/{urllib.parse.quote(workflow, safe='')}/dispatches"
    )


def _message_of(body: Any) -> str:
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str) and message:
            return message
    return "(no message in the response)"


def post_dispatch(
    owner: str,
    repo: str,
    *,
    workflow: str,
    ref: str,
    inputs: dict[str, str],
    token: str,
    role: str,
    opener=None,
) -> tuple[int, Any]:
    """POST the dispatch and return (status, body) for an accepted request.

    Raises DispatchVerbError for every non-2xx response, naming the resolved
    repo, workflow and ref; a 403 names the missing permission.
    """
    payload: dict[str, Any] = {"ref": ref}
    if inputs:
        payload["inputs"] = inputs
    target = f"{owner}/{repo} workflow {workflow!r} ref {ref!r}"
    try:
        status, body = github_client.request_json(
            "POST", _dispatch_url(owner, repo, workflow), token, payload, opener=opener
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        _fail(f"dispatch of {target} could not complete: {exc}", EXIT_DISPATCH_FAILED)
    if status in (200, 204):
        return status, body
    detail = _message_of(body)
    if status == 403:
        _fail(
            f"dispatch of {target} refused (HTTP 403): the credential resolved for role "
            f"{role!r} needs the '{REQUIRED_PERMISSION}' permission on {owner}/{repo}. "
            f"GitHub said: {detail}",
            EXIT_PERMISSION_DENIED,
        )
    if status == 401:
        _fail(
            f"dispatch of {target} refused (HTTP 401): the credential resolved for role "
            f"{role!r} was not accepted. GitHub said: {detail}",
            EXIT_DISPATCH_FAILED,
        )
    if status == 404:
        _fail(
            f"dispatch of {target} found nothing (HTTP 404): the repo, the workflow file, "
            f"or the credential's access to them does not exist. GitHub said: {detail}",
            EXIT_DISPATCH_FAILED,
        )
    if status == 422:
        _fail(
            f"dispatch of {target} was rejected (HTTP 422): the workflow may lack a "
            f"'workflow_dispatch' trigger, the ref may not exist, or an input is invalid. "
            f"GitHub said: {detail}",
            EXIT_DISPATCH_FAILED,
        )
    _fail(f"dispatch of {target} returned HTTP {status}. GitHub said: {detail}", EXIT_DISPATCH_FAILED)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_newest_dispatched_run(
    owner: str,
    repo: str,
    *,
    workflow: str,
    ref: str,
    not_before: datetime,
    token: str,
    opener=None,
) -> tuple[dict[str, Any] | None, str]:
    """One read of the newest `workflow_dispatch` run for the workflow and ref.

    Returns (run or None, lookup label). A run is reported only when it was
    created strictly after *not_before* (the local time of the dispatch
    request); anything older or simultaneous may be an earlier dispatch and is
    not provably this one, so it is not reported. Failing to read is never an
    error here: the dispatch has already been accepted.
    """
    query = urllib.parse.urlencode(
        {"event": "workflow_dispatch", "branch": ref, "per_page": 1}
    )
    url = (
        f"{github_client.GITHUB_API_BASE}/repos/{owner}/{repo}/actions/workflows/"
        f"{urllib.parse.quote(workflow, safe='')}/runs?{query}"
    )
    try:
        status, body = github_client.request_json("GET", url, token, opener=opener)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None, "unavailable"
    if status != 200 or not isinstance(body, dict):
        return None, "unavailable"
    runs = body.get("workflow_runs")
    if not isinstance(runs, list) or not runs or not isinstance(runs[0], dict):
        return None, "not_yet_visible"
    newest = runs[0]
    created = _parse_time(newest.get("created_at"))
    if created is None or created <= not_before:
        return None, "not_yet_visible"
    run_id = newest.get("id")
    run_url = newest.get("html_url")
    if not isinstance(run_id, int) or not isinstance(run_url, str):
        return None, "unavailable"
    return {"id": run_id, "url": run_url}, "newest_run"


def _run_from_dispatch_response(body: Any) -> dict[str, Any] | None:
    """The run id and URL, when the dispatch response itself carries them."""
    if not isinstance(body, dict):
        return None
    run_id = body.get("workflow_run_id")
    run_url = body.get("html_url") or body.get("run_url")
    if isinstance(run_id, int) and isinstance(run_url, str):
        return {"id": run_id, "url": run_url}
    return None


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadout-workflow-dispatch",
        description=(
            "loadout-workflow-dispatch -- start a named workflow on a ref with given "
            "inputs and report the run id and URL. The workflow and ref must both be "
            "listed under 'workflow_dispatch' in the repo's loadout config, or the "
            "dispatch is refused before any credential is minted. Never creates tags, "
            "releases or notes; the workflow does."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  loadout-workflow-dispatch --caller some-role --repo some-owner/some-repo \\\n"
            "      --workflow release.yml --ref main --input channel=stable\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"loadout-workflow-dispatch {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    parser.add_argument(
        "--caller",
        default=None,
        help="Role whose credential is used. It must match this process's attested "
        "invoking identity or the call is refused before any I/O. "
        f"{_describe_omitted_caller(flag_name='--caller')}",
    )
    parser.add_argument("--repo", required=True, help="owner/repo that owns the workflow.")
    parser.add_argument(
        "--workflow", required=True, help="Workflow file name, for example release.yml."
    )
    parser.add_argument("--ref", required=True, help="Branch or tag to run the workflow on.")
    parser.add_argument(
        "--input",
        action="append",
        dest="inputs",
        default=None,
        metavar="KEY=VALUE",
        help="A workflow input, passed through as given (repeatable).",
    )
    parser.add_argument(
        "--platform",
        choices=(PLATFORM_GITHUB, PLATFORM_FORGEJO),
        default=None,
        help="Host platform. Detected from the origin remote of --repo-path when omitted. "
        "Only github is supported.",
    )
    parser.add_argument(
        "--repo-path",
        default=None,
        help="Directory holding the repo config (and the origin remote used for platform "
        "detection). Default: the current directory.",
    )
    return parser


def _resolve_target_platform(args: argparse.Namespace, repo_path: Path) -> str:
    try:
        return resolve_platform(args.platform, read_remote_url_best_effort(repo_path))
    except PlatformResolutionError as exc:
        _fail(
            f"cannot resolve the host platform for --repo-path {str(repo_path)!r}: {exc} "
            f"Pass --platform github.",
            EXIT_USAGE,
        )


def _run(
    args: argparse.Namespace,
    *,
    token_provider: TokenProvider | None,
    opener,
    identity_provider,
) -> int:
    try:
        owner, repo = parse_owner_repo(args.repo)
    except RemoteResolutionError as exc:
        _fail(str(exc), EXIT_USAGE)
    if not is_valid_workflow_file(args.workflow):
        _fail(
            f"--workflow {args.workflow!r} must be a workflow file name such as "
            f"'release.yml' (no path separators).",
            EXIT_USAGE,
        )
    if not is_valid_ref(args.ref):
        _fail(
            f"--ref {args.ref!r} must be a branch or tag name with no whitespace or "
            f"URL-structure characters.",
            EXIT_USAGE,
        )
    inputs = parse_inputs(args.inputs)

    # Identity binding is a local check (no credential, no network), run first
    # like every other caller-bound verb.
    resolve_identity_fn = identity_provider if identity_provider is not None else _resolve_identity
    try:
        attested = _resolve_for_binding(
            caller_explicit=args.caller is not None,
            caller=args.caller or DEFAULT_ROLE,
            resolve_identity_fn=resolve_identity_fn,
        )
        role = args.caller if args.caller is not None else attested.subject
        bind_caller(role, caller_explicit=True, identity=attested)
    except (AttestationError, CallerBindingError) as exc:
        _fail(f"attested-identity binding FAILED -- {exc}", EXIT_CALLER_INVOKER_MISMATCH)

    repo_path = Path(args.repo_path) if args.repo_path else Path.cwd()
    platform = _resolve_target_platform(args, repo_path)
    if platform != PLATFORM_GITHUB:
        _fail(
            f"platform {platform!r} is not supported: workflow dispatch is a GitHub "
            f"endpoint and this verb has no equivalent for {platform!r}. Nothing was sent.",
            EXIT_UNSUPPORTED_PLATFORM,
        )

    # The allow-list is read from --repo-path but the dispatch targets --repo;
    # without this binding one repo's config could authorize a dispatch against
    # another. An unverifiable tree is refused, never treated as a pass.
    consistency = check_repo_path_consistency(f"{owner}/{repo}", str(repo_path))
    if not consistency.checked:
        _fail(
            f"cannot confirm that --repo-path {str(repo_path)!r} is a clone of "
            f"{owner}/{repo}: {consistency.reason} The allow-list is only trusted from "
            f"a tree whose origin remote names the --repo. Nothing was sent.",
            EXIT_REPO_MISMATCH,
        )
    if not consistency.matches:
        _fail(
            f"--repo {owner}/{repo!s} does not match --repo-path {str(repo_path)!r}, whose "
            f"origin remote resolves to {consistency.remote_owner_repo!r}. The allow-list "
            f"in that tree cannot authorize a dispatch against {owner}/{repo}. "
            f"Nothing was sent.",
            EXIT_REPO_MISMATCH,
        )

    # The one policy check, before any credential or network use.
    try:
        entries = load_dispatch_allowlist(repo_path)
        check_dispatch_allowed(
            entries,
            workflow=args.workflow,
            ref=args.ref,
            source=str(resolve_repo_config_path(repo_path, warn=False)),
        )
    except DispatchConfigError as exc:
        _fail(str(exc), EXIT_CONFIG_INVALID)
    except DispatchNotListedError as exc:
        _fail(str(exc), EXIT_NOT_LISTED)

    print(f"workflow-dispatch: resolving token for role={role!r}", file=sys.stderr)
    provider = token_provider if token_provider is not None else resolve_platform_provider(platform)
    try:
        token = _resolve_token(role, provider, repo=f"{owner}/{repo}")
    except CredentialProviderError as exc:
        _fail(f"token resolution FAILED -- {exc}", EXIT_TOKEN_FETCH_FAILED)

    dispatched_at = datetime.now(timezone.utc)
    status, body = post_dispatch(
        owner, repo,
        workflow=args.workflow, ref=args.ref, inputs=inputs,
        token=token, role=role, opener=opener,
    )

    run = _run_from_dispatch_response(body)
    lookup = "dispatch_response"
    if run is None:
        run, lookup = read_newest_dispatched_run(
            owner, repo,
            workflow=args.workflow, ref=args.ref,
            not_before=dispatched_at, token=token, opener=opener,
        )

    result: dict[str, Any] = {
        "platform": platform,
        "repo": f"{owner}/{repo}",
        "workflow": args.workflow,
        "ref": args.ref,
        "dispatch": {"accepted": True, "status": status},
        "run": run,
        "run_lookup": lookup,
    }
    if run is None:
        result["note"] = _RUN_NOT_VISIBLE_NOTE
    print(json.dumps(result))
    return EXIT_OK


def main(
    argv: list[str] | None = None,
    *,
    token_provider: TokenProvider | None = None,
    opener=None,
    identity_provider=None,
) -> int:
    """CLI entrypoint; returns the exit code. `token_provider`, `opener` and
    `identity_provider` are injection points for tests."""
    if argv is None:
        argv = sys.argv[1:]
    if any(arg in ("--help", "-h") for arg in argv):
        _build_arg_parser().print_help()
        return EXIT_OK
    try:
        args = _build_arg_parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    try:
        return _run(
            args,
            token_provider=token_provider,
            opener=opener,
            identity_provider=identity_provider,
        )
    except DispatchVerbError as exc:
        print(f"workflow-dispatch: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
