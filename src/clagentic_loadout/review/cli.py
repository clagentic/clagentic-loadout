"""review.cli — `loadout-review`: run a review end to end, then post it.

Two subcommands, so a reviewer role types one command to produce findings and
one to post its judgement, and nothing else:

  run   acquire the PR's base..head diff from the host API (never a local
        checkout), chunk it, run the configured carrier per chunk with bounded
        retry and fallback, validate the findings contract, and merge. Exit 0
        complete, 10 resume (run the identical command again; finished chunks
        are cached), 20 blocked with the stage named.
  post  stage and post the findings through the existing review-post path with
        the tool-constructed verdict fence, and verify the landed comment.
        The comment carries structured findings only; there is deliberately
        no free-form body input.

Both bind --caller to the attested invoking identity before any I/O, exactly
like every other verb. Profiles come from deployment config
(review.profiles.<name>, see review.profile_config); this module learns no
agent name and no model name.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from clagentic_loadout._version import get_version
from clagentic_loadout.acquire.contract import AcquiredPr
from clagentic_loadout.acquire.errors import AcquireFetchError, PlatformMismatchError
from clagentic_loadout.acquire.verb import AcquireVerbError, build_backend
from clagentic_loadout.platform_detect import (
    PLATFORM_FORGEJO,
    PLATFORM_GITHUB,
    PlatformResolutionError,
    resolve_platform,
)
from clagentic_loadout.review import verb as review_post_verb
from clagentic_loadout.review.findings_contract import SEVERITIES
from clagentic_loadout.review.profile_config import (
    ReviewProfile,
    ReviewProfileError,
    load_review_profile,
    load_run_root_override,
)
from clagentic_loadout.review.run_pipeline import (
    EXIT_BLOCKED,
    RESULT_BLOCKED,
    bind_run_dir,
    default_run_dir,
    run_review,
)
from clagentic_loadout.sha import FULL_SHA_RE
from clagentic_loadout.transport.attestation import (
    AttestationError,
    resolve_bound_identity as _resolve_identity,
)
from clagentic_loadout.transport.body_env import BodyEnvError, stage_caller_body
from clagentic_loadout.transport.caller_binding import (
    CallerBindingError,
    bind_caller,
    describe_omitted_caller_behavior as _describe_omitted_caller,
    resolve_for_binding as _resolve_for_binding,
)
from clagentic_loadout.transport.credential_provider import DEFAULT_ROLE, TokenProvider
from clagentic_loadout.transport.git_host_api import (
    DEFAULT_GIT_HOST_BASE_URL,
    GIT_HOST_BASE_URL_ENV_VAR,
    resolve_git_host_base,
)
from clagentic_loadout.transport.provider_config import DEFAULT_USER_CONFIG_ROOT

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_TOKEN_FETCH_FAILED = 2
EXIT_PROFILE_INVALID = 3
EXIT_WRONG_PLATFORM = 4
EXIT_ACQUIRE_FAILED = 5
EXIT_CALLER_INVOKER_MISMATCH = 7
#: `run` finished part of the work; run the identical command again.
EXIT_RESUME = 10
#: `run` is blocked, with the stage and reason named on stdout/stderr.
EXIT_RUN_BLOCKED = EXIT_BLOCKED
#: `post`: the findings were not posted or the posted comment did not verify.
EXIT_POST_FAILED = 30
#: `post`: the findings were produced for a head that is no longer the PR head.
EXIT_STALE_HEAD = 31
#: `post`: the findings file or --status is internally inconsistent.
EXIT_FINDINGS_INVALID = 32

_GIT_PROBE_TIMEOUT_SECONDS = 10
RUN_ROOT_ENV_VAR ="CLAGENTIC_LOADOUT_REVIEW_RUN_ROOT"
_STATUSES = ("clean", "blocking")
_FINDING_KEYS = ("file", "line", "rule_id", "message")


class ReviewCliError(Exception):
    """A failure that terminates the process with a specific exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _fail(message: str, code: int) -> None:
    raise ReviewCliError(message, code)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadout-review",
        description=(
            "loadout-review -- run a PR review end to end, then post it. "
            "`run` acquires the PR's base..head diff from the host API "
            "(never a local checkout), chunks it, runs the configured "
            "carrier per chunk with bounded retry and fallback, validates "
            "the findings contract, and merges. `post` posts the findings "
            "with the tool-constructed verdict fence and verifies the "
            "landed comment."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  loadout-review run --caller reviewer --repo some-owner/some-repo --pr 42\n"
            "  loadout-review post --caller reviewer --repo some-owner/some-repo \\\n"
            "    --pr 42 --findings /path/to/findings.json --status clean\n"
            "\n"
            "`run` exit codes: 0 complete, 10 resume (run the identical command "
            "again), 20 blocked."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"loadout-review {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    # --caller is accepted both before and after the subcommand. The
    # subcommand copies default to SUPPRESS so a value given before the
    # subcommand is never overwritten by the subparser's own default.
    caller_help = (
        "Role whose token is resolved via the credential provider and whose "
        "attested identity must match, or the call is refused before any I/O. "
        f"{_describe_omitted_caller()}"
    )
    parser.add_argument("--caller", default=None, help=caller_help)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--caller", default=argparse.SUPPRESS, help=caller_help)
        sub.add_argument("--repo", required=True, help="owner/repo of the target PR.")
        sub.add_argument("--pr", required=True, type=int, help="PR number.")
        sub.add_argument(
            "--platform",
            default=None,
            choices=(PLATFORM_GITHUB, PLATFORM_FORGEJO),
            help="Target platform. When omitted, detected from the origin remote "
            "of --repo-path; the call fails if neither is available.",
        )
        sub.add_argument(
            "--repo-path",
            default=None,
            help="Directory used only to find repo-level config and the origin "
            "remote for platform detection (default: the current directory). "
            "The diff is never read from it.",
        )
        sub.add_argument(
            "--git-host-base-url",
            default=None,
            help=f"Forgejo API base URL (default: ${GIT_HOST_BASE_URL_ENV_VAR}, or "
            f"{DEFAULT_GIT_HOST_BASE_URL!r}). Ignored for GitHub.",
        )

    run = subparsers.add_parser(
        "run",
        help="Acquire, chunk, review each chunk, and merge findings.",
        description="Run a review end to end and write a merged findings file.",
    )
    add_common(run)
    run.add_argument(
        "--profile",
        default=None,
        help="Review profile name under review.profiles in the loadout config "
        "(default: the --caller role).",
    )
    run.add_argument(
        "--out",
        default=None,
        help="Run directory. When given it wins. Otherwise the directory is "
        "keyed on (repo, pr, head sha) under review.run_root (or "
        f"${RUN_ROOT_ENV_VAR}) so a re-dispatched run resumes its finished chunks.",
    )

    post = subparsers.add_parser(
        "post",
        help="Post findings with the tool-constructed verdict fence and verify.",
        description="Stage and post findings; the comment body is built from "
        "structured findings only.",
    )
    add_common(post)
    post.add_argument(
        "--findings",
        required=True,
        help="Findings file: the merged output of `run`, or a JSON array of "
        "findings (then --head-sha is required).",
    )
    post.add_argument(
        "--status",
        required=True,
        choices=_STATUSES,
        help="The verdict to post.",
    )
    post.add_argument(
        "--head-sha",
        default=None,
        help="Head SHA the findings were produced for. Required only when "
        "--findings is a bare array.",
    )
    return parser


def _parse_owner_repo(value: str) -> tuple[str, str]:
    parts = value.strip().split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        _fail(f"--repo must be in 'owner/repo' format, got: {value!r}", EXIT_USAGE)
    return parts[0], parts[1]


def _bind_caller(args: argparse.Namespace, identity_provider) -> str:
    resolve_identity_fn = identity_provider if identity_provider is not None else _resolve_identity
    try:
        identity = _resolve_for_binding(
            caller_explicit=args.caller is not None,
            caller=args.caller or DEFAULT_ROLE,
            resolve_identity_fn=resolve_identity_fn,
        )
    except AttestationError as exc:
        _fail(f"attested-identity resolution FAILED -- {exc}", EXIT_CALLER_INVOKER_MISMATCH)
    caller = args.caller if args.caller is not None else identity.subject
    bind_caller(caller, caller_explicit=True, identity=identity)
    return caller


def _origin_url(repo_path: Path) -> str:
    try:
        probe = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=_GIT_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        # A hung probe is treated as "no remote": platform detection then
        # asks for an explicit --platform instead of blocking forever.
        return ""
    return probe.stdout.strip() if probe.returncode == 0 else ""


def _resolve_platform(args: argparse.Namespace, repo_path: Path) -> str:
    try:
        return resolve_platform(args.platform, "" if args.platform else _origin_url(repo_path))
    except PlatformResolutionError as exc:
        _fail(f"{exc} Pass --platform github or --platform forgejo.", EXIT_USAGE)


def _acquire(
    args: argparse.Namespace,
    *,
    owner: str,
    repo: str,
    caller: str,
    platform: str,
    token_provider: TokenProvider | None,
    opener,
) -> AcquiredPr:
    try:
        backend = build_backend(
            platform,
            owner=owner,
            repo=repo,
            caller=caller,
            git_host_base=resolve_git_host_base(args.git_host_base_url),
            token_provider=token_provider,
            opener=opener,
        )
    except PlatformMismatchError as exc:
        _fail(str(exc), EXIT_WRONG_PLATFORM)
    except AcquireVerbError as exc:
        _fail(str(exc), exc.code)
    try:
        acquired = backend.fetch_pr_content(owner=owner, repo=repo, pr_number=args.pr)
    except AcquireFetchError as exc:
        _fail(str(exc), EXIT_ACQUIRE_FAILED)
    if (
        acquired.owner.lower() != owner.lower()
        or acquired.repo.lower() != repo.lower()
        or acquired.pr_number != args.pr
    ):
        _fail(
            f"acquired content is for {acquired.owner}/{acquired.repo}#{acquired.pr_number}, "
            f"but {owner}/{repo}#{args.pr} was requested",
            EXIT_ACQUIRE_FAILED,
        )
    return acquired


def _resolve_run_root(run_root: Path | None, config_root: str | Path | None) -> Path:
    if run_root is not None:
        return run_root
    from_env = os.environ.get(RUN_ROOT_ENV_VAR)
    if from_env:
        return Path(from_env).expanduser()
    from_config = load_run_root_override(config_root=config_root)
    if from_config is not None:
        return from_config
    base = Path(config_root) if config_root is not None else DEFAULT_USER_CONFIG_ROOT
    return base / "state" / "review-runs"


def _emit_stage(name: str, status: str, **fields: Any) -> None:
    rendered = " ".join(f"{k}={v}" for k, v in fields.items() if v not in (None, ""))
    print(
        f"loadout-review: stage={name} status={status}" + (f" {rendered}" if rendered else ""),
        file=sys.stderr,
    )


def _run_command(
    args: argparse.Namespace,
    caller: str,
    *,
    owner: str,
    repo: str,
    platform: str,
    repo_path: Path,
    token_provider,
    opener,
    config_root,
    run_root,
    runner,
) -> int:
    profile_name = args.profile or caller
    try:
        profile: ReviewProfile = load_review_profile(
            profile_name, config_root=config_root, repo_root=repo_path
        )
    except ReviewProfileError as exc:
        _fail(str(exc), EXIT_PROFILE_INVALID)

    acquired = _acquire(
        args, owner=owner, repo=repo, caller=caller, platform=platform,
        token_provider=token_provider, opener=opener,
    )

    head_sha_usable = FULL_SHA_RE.match(acquired.head_sha) is not None
    if args.out:
        run_dir = Path(args.out)
    elif head_sha_usable:
        run_dir = default_run_dir(
            _resolve_run_root(run_root, config_root), owner, repo, args.pr, acquired.head_sha
        )
    else:
        # An unusable head SHA must never become a path segment. run_review
        # blocks at the "acquired" stage before it touches the run directory,
        # so the run root stands in as a placeholder that is never written.
        run_dir = _resolve_run_root(run_root, config_root)
    if head_sha_usable:
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            bind_run_dir(run_dir, owner, repo, args.pr, acquired.head_sha)
        except OSError as exc:
            _fail(f"cannot prepare the run directory {str(run_dir)!r}: {exc}", EXIT_RUN_BLOCKED)

    kwargs = {"runner": runner} if runner is not None else {}
    outcome = run_review(acquired, profile, run_dir, emit=_emit_stage, **kwargs)
    payload = dict(outcome.payload)
    payload["stages"] = outcome.stages
    print(json.dumps(payload))
    if outcome.result == RESULT_BLOCKED:
        print(
            f"loadout-review: blocked at stage {payload.get('stage')!r}: "
            f"{payload.get('reason')}: {payload.get('detail')}",
            file=sys.stderr,
        )
    return outcome.exit_code


def _load_findings(
    path: str, args: argparse.Namespace, *, owner: str, repo: str
) -> tuple[str, list[dict[str, Any]]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"cannot read findings file {path!r}: {exc}", EXIT_FINDINGS_INVALID)
    if isinstance(data, dict):
        head_sha = data.get("head_sha")
        findings = data.get("findings")
        owner_repo = f"{data.get('owner')}/{data.get('repo')}"
        # Compared against the parsed owner/repo, not the raw argument, so a
        # padded --repo value that acquire accepted is not rejected here.
        requested = f"{owner}/{repo}"
        if owner_repo.lower() != requested.lower() or data.get("pr_number") != args.pr:
            _fail(
                f"findings file {path!r} is for {owner_repo}#{data.get('pr_number')}, "
                f"but {requested}#{args.pr} was requested",
                EXIT_FINDINGS_INVALID,
            )
        if args.head_sha is not None and args.head_sha != head_sha:
            _fail(
                f"--head-sha {args.head_sha} conflicts with the head SHA {head_sha} recorded "
                f"in the findings file {path!r}; drop --head-sha or regenerate the findings",
                EXIT_FINDINGS_INVALID,
            )
    else:
        head_sha = args.head_sha
        findings = data
    if not isinstance(head_sha, str) or not FULL_SHA_RE.match(head_sha):
        _fail(
            "the findings head SHA is missing or not 40 lowercase hex characters "
            "(a bare findings array needs --head-sha)",
            EXIT_FINDINGS_INVALID,
        )
    if not isinstance(findings, list):
        _fail(f"findings in {path!r} must be a JSON array", EXIT_FINDINGS_INVALID)
    cleaned: list[dict[str, Any]] = []
    for position, item in enumerate(findings, start=1):
        if not isinstance(item, dict) or any(key not in item for key in _FINDING_KEYS):
            _fail(
                f"finding {position} must be an object with {', '.join(_FINDING_KEYS)}",
                EXIT_FINDINGS_INVALID,
            )
        if (
            not all(isinstance(item[key], str) and item[key] for key in ("file", "rule_id", "message"))
            or isinstance(item["line"], bool)
            or not isinstance(item["line"], int)
        ):
            _fail(f"finding {position} has a field of the wrong type", EXIT_FINDINGS_INVALID)
        if item["line"] < 1:
            _fail(f"finding {position} line must be >= 1, got {item['line']}", EXIT_FINDINGS_INVALID)
        severity = item.get("severity")
        if severity is not None:
            # Normalized so "Blocking" or " blocking" cannot slip past the
            # clean-vs-blocking contradiction check in the caller.
            normalized = severity.strip().lower() if isinstance(severity, str) else None
            if normalized not in SEVERITIES:
                _fail(
                    f"finding {position} severity must be one of {', '.join(SEVERITIES)}, "
                    f"got {severity!r}",
                    EXIT_FINDINGS_INVALID,
                )
            severity = normalized
        message = f"({severity}) {item['message']}" if severity else item["message"]
        cleaned.append(
            {
                "file": item["file"],
                "line": item["line"],
                "rule_id": item["rule_id"],
                "message": message,
                "severity": severity,
            }
        )
    return head_sha, cleaned


def _post_command(
    args: argparse.Namespace,
    caller: str,
    *,
    owner: str,
    repo: str,
    platform: str,
    token_provider,
    opener,
    identity_provider,
) -> int:
    head_sha, findings = _load_findings(args.findings, args, owner=owner, repo=repo)
    if args.status == "clean" and any(f["severity"] == "blocking" for f in findings):
        _fail(
            "--status clean contradicts the findings file, which carries a blocking finding; "
            "post --status blocking, or remove the finding if you judged it wrong",
            EXIT_FINDINGS_INVALID,
        )

    def assert_head_unmoved() -> None:
        live = _acquire(
            args, owner=owner, repo=repo, caller=caller, platform=platform,
            token_provider=token_provider, opener=opener,
        )
        if live.head_sha != head_sha:
            _fail(
                f"the findings were produced for head {head_sha} but the PR head is now "
                f"{live.head_sha}; run the review again before posting",
                EXIT_STALE_HEAD,
            )

    assert_head_unmoved()

    body = {
        "review_status": args.status,
        "findings": [{k: f[k] for k in _FINDING_KEYS} for f in findings],
    }
    try:
        stage_caller_body(
            caller=caller,
            body_bytes=json.dumps(body).encode("utf-8"),
            target_pr=args.pr,
            head_sha=head_sha,
        )
    except BodyEnvError as exc:
        _fail(f"cannot stage the review body: {exc}", EXIT_POST_FAILED)

    post_argv = [
        "--caller", caller,
        "--platform", platform,
        "--body-env",
        "--verdict-findings",
        "--verdict-head-sha", head_sha,
    ]
    if platform == PLATFORM_FORGEJO:
        post_argv += ["--pr-sha", head_sha]
    if args.git_host_base_url:
        post_argv += ["--git-host-base-url", args.git_host_base_url]
    post_argv += [f"{owner}/{repo}", str(args.pr)]

    kwargs: dict[str, Any] = {"token_provider": token_provider, "opener": opener}
    if identity_provider is not None:
        kwargs["identity_provider"] = identity_provider
    captured = io.StringIO()
    # Re-read the live head as late as possible: Forgejo also pins it with
    # --pr-sha, but GitHub has no server-side pin, so this narrows the window
    # in which a push could land between the first read and the post.
    assert_head_unmoved()
    try:
        with contextlib.redirect_stdout(captured):
            code = review_post_verb.main(post_argv, **kwargs)
    except SystemExit as exc:
        # The inner verb may exit directly; map it to a code so the
        # post_failed report below is still printed.
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    if code != 0:
        print(json.dumps({"result": "post_failed", "review_post_exit_code": code}))
        print(
            f"loadout-review: posting failed: the review-post path exited {code}",
            file=sys.stderr,
        )
        # The inner path's own output carries the failure detail.
        inner_output = captured.getvalue().strip()
        if inner_output:
            print(f"loadout-review: review-post output: {inner_output}", file=sys.stderr)
        return EXIT_POST_FAILED

    try:
        posted = json.loads(captured.getvalue().strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        posted = None
    # Success means the landed comment was read back: a comment id and a
    # verified verdict block. Anything less is reported as a failure, never
    # as "posted".
    if (
        not isinstance(posted, dict)
        or posted.get("verdict_block_verified") is not True
        or posted.get("verified_id") is None
    ):
        print(json.dumps({"result": "post_failed", "review_post_exit_code": code}))
        observed = (
            "its output was not a JSON result object"
            if not isinstance(posted, dict)
            else f"verified_id={posted.get('verified_id')!r}, "
            f"verdict_block_verified={posted.get('verdict_block_verified')!r}"
        )
        print(
            "loadout-review: posting could not be confirmed: the review-post path exited 0 "
            f"but the landed comment was not verified ({observed})",
            file=sys.stderr,
        )
        return EXIT_POST_FAILED
    print(
        json.dumps(
            {
                "result": "posted",
                "status": args.status,
                "head_sha": head_sha,
                "pr_number": args.pr,
                "finding_count": len(findings),
                "verified_id": posted.get("verified_id"),
                "verified_url": posted.get("verified_url"),
                "verified_by_login": posted.get("verified_by_login"),
                "verdict_block_verified": True,
            }
        )
    )
    return EXIT_OK


def main(
    argv: list[str] | None = None,
    *,
    token_provider: TokenProvider | None = None,
    opener=None,
    identity_provider=None,
    config_root: str | Path | None = None,
    run_root: Path | None = None,
    runner=None,
) -> int:
    """CLI entrypoint. Returns the process exit code. The keyword arguments
    are injection points for tests and embedding callers."""
    if argv is None:
        argv = sys.argv[1:]

    parser = _build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on a usage error, which this verb reserves for a
        # token-fetch failure; a usage error is EXIT_USAGE.
        if exc.code in (0, None):
            return EXIT_OK
        return EXIT_USAGE

    try:
        owner, repo = _parse_owner_repo(args.repo)
        if args.pr <= 0:
            _fail(f"--pr must be a positive integer, got: {args.pr!r}", EXIT_USAGE)
        caller = _bind_caller(args, identity_provider)
        repo_path = Path(args.repo_path) if args.repo_path else Path.cwd()
        platform = _resolve_platform(args, repo_path)
        print(
            f"loadout-review: {args.command} platform={platform!r} caller={caller!r} "
            f"target={owner}/{repo}#{args.pr}",
            file=sys.stderr,
        )
        if args.command == "run":
            return _run_command(
                args, caller, owner=owner, repo=repo, platform=platform,
                repo_path=repo_path, token_provider=token_provider, opener=opener,
                config_root=config_root, run_root=run_root, runner=runner,
            )
        return _post_command(
            args, caller, owner=owner, repo=repo, platform=platform,
            token_provider=token_provider, opener=opener, identity_provider=identity_provider,
        )
    except ReviewCliError as exc:
        print(f"loadout-review: {exc}", file=sys.stderr)
        return exc.code
    except CallerBindingError as exc:
        print(f"loadout-review: {exc}", file=sys.stderr)
        return EXIT_CALLER_INVOKER_MISMATCH


if __name__ == "__main__":
    sys.exit(main())
