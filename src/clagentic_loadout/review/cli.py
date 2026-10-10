"""review.cli — `loadout-review`: run a review end to end, then post it.

Two subcommands, so a reviewer role types one command to produce findings and
one to post its judgement, and nothing else:

  run   acquire the PR's base..head diff from the host API (never a local
        checkout), chunk it, run the configured carrier per chunk with bounded
        retry and fallback, validate the findings contract, and merge. Exit 0
        complete, 10 resume (run the identical command again; finished chunks
        are cached), 20 blocked with the stage named. With --prior-findings /
        --prior-head-sha it reviews only the delta since the caller's own last
        verdict (see review.delta), falling back to the full diff otherwise.
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
from clagentic_loadout.merge.fence_state import (
    EVIDENCE_KEYS,
    KEY_DROPPED,
    KEY_FAILURE_SEQUENCES,
    KEY_RESOLVED,
    failure_sequences_of,
    normalize_findings_state,
)
from clagentic_loadout.review import verb as review_post_verb
from clagentic_loadout.review.delta import resolve_delta
from clagentic_loadout.review.findings_contract import (
    KEY_FAILURE_SEQUENCE,
    InvalidReplyError,
    validate_finding,
)
from clagentic_loadout.review.pr_record import write_pr_record
from clagentic_loadout.review.resolutions import (
    BY_CALLER,
    Resolution,
    ResolutionsError,
    load_resolutions,
    resolved_entry,
    split_resolved,
)
from clagentic_loadout.review.run_evidence import evidence_from_document
from clagentic_loadout.review.profile_config import (
    ReviewProfile,
    ReviewProfileError,
    load_review_profile,
    load_run_root_override,
)
from clagentic_loadout.review.run_pipeline import (
    EXIT_BLOCKED,
    RESULT_BLOCKED,
    RunOutcome,
    bind_run_dir,
    default_run_dir,
    run_review,
    unusable_acquire_outcome,
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
#: `post`: the findings were produced for a head that is no longer the PR head,
#: either caught before posting (nothing posted) or found on the landed
#: comment afterwards (posted, result "posted_head_moved").
EXIT_STALE_HEAD = 31
#: `post`: the findings file or --status is internally inconsistent.
EXIT_FINDINGS_INVALID = 32

_GIT_PROBE_TIMEOUT_SECONDS = 10
RUN_ROOT_ENV_VAR = "CLAGENTIC_LOADOUT_REVIEW_RUN_ROOT"
_STATUSES = ("clean", "blocking")
_RESOLVED_HELP = (
    "JSON file listing findings the caller has ruled resolved or refuted: an "
    "array whose entries are a finding id string, or an object with exactly "
    "one of 'id' (file:line:rule_id, or a fingerprint) and 'fingerprint', plus "
    "an optional one-line 'reason'. Matching findings are neither carried nor "
    "re-posted; the posted body lists them under 'Resolved by caller' with the "
    "reason. An entry that matches no finding is warned about and ignored."
)
_FINDING_KEYS = ("file", "line", "rule_id", "message")
#: Carried to the posted body only when the finding has them.
_OPTIONAL_FINDING_KEYS = (KEY_FAILURE_SEQUENCE,)


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
    run.add_argument(
        "--prior-findings",
        default=None,
        help="Delta mode: the findings file of this role's own last posted "
        "verdict on this PR (a previous `run` output, or a JSON array of its "
        "findings with --prior-head-sha). Reviews only the changes since that "
        "verdict's head and answers for its open findings. Falls back to a "
        "full-diff review, naming the reason, when the current head is not a "
        "fast-forward of that head.",
    )
    run.add_argument(
        "--prior-head-sha",
        type=str.strip,
        default=None,
        help="Delta mode: the head SHA the last posted verdict was stamped "
        "for. Required when --prior-findings is a bare array; may be given "
        "alone to review a delta with no open findings to answer for.",
    )
    run.add_argument("--resolved-findings", default=None, help=_RESOLVED_HELP)

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
        type=str.strip,
        default=None,
        help="Head SHA the findings were produced for. Required only when "
        "--findings is a bare array.",
    )
    post.add_argument(
        "--state-file",
        default=None,
        help="JSON object with the structured findings state to carry in the "
        "verdict fence: 'findings_open' (id, rule_id, head), 'supersedes' "
        "(comment id), 'cleared_claims' (id, head, evidence) and "
        "'scanners_run' (scanner, status, reason). Rendered by the tool inside "
        "the fence, never as body text; every field is optional. A cleared "
        "claim's head must be this review's head.",
    )
    post.add_argument(
        "--dropped",
        default=None,
        help="JSON file listing the candidate findings the reviewer examined "
        "and dropped: an array (or an object with a 'dropped' array) of "
        "objects with 'file', 'line', 'rule_id', 'message' and 'reason'. "
        "Shown in a 'Dropped candidates' section, counted in the verdict "
        "header, and copied into the fence. Dropped candidates are never "
        "findings and do not change the verdict.",
    )
    post.add_argument("--resolved-findings", default=None, help=_RESOLVED_HELP)
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


def _build_acquire_backend(
    args: argparse.Namespace,
    *,
    owner: str,
    repo: str,
    caller: str,
    platform: str,
    token_provider: TokenProvider | None,
    opener,
):
    try:
        return build_backend(
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
    backend = _build_acquire_backend(
        args, owner=owner, repo=repo, caller=caller, platform=platform,
        token_provider=token_provider, opener=opener,
    )
    return _fetch_acquired(backend, args, owner=owner, repo=repo)


def _fetch_acquired(backend, args: argparse.Namespace, *, owner: str, repo: str) -> AcquiredPr:
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

    since_head, prior_findings = _load_prior_review(args, owner=owner, repo=repo)
    resolutions = _load_resolutions(args.resolved_findings)

    backend = _build_acquire_backend(
        args, owner=owner, repo=repo, caller=caller, platform=platform,
        token_provider=token_provider, opener=opener,
    )
    acquired = _fetch_acquired(backend, args, owner=owner, repo=repo)

    # An unusable head SHA must never become a path segment, so it is refused
    # here, before any run directory is derived from it or touched.
    refused = unusable_acquire_outcome(acquired, emit=_emit_stage)
    if refused is not None:
        return _report_outcome(refused)

    delta = None
    delta_stage = None
    unknown_resolutions = tuple(r.ref for r in resolutions)
    if since_head is not None:
        resolution = resolve_delta(backend, acquired, since_head, prior_findings, resolutions)
        acquired, delta = resolution.acquired, resolution.context
        delta_stage = resolution.stage_fields()
        unknown_resolutions = resolution.unknown_resolutions
        if delta is None:
            print(
                f"loadout-review: delta review not used ({resolution.reason}): "
                f"{resolution.detail}; reviewing the full diff",
                file=sys.stderr,
            )

    delta_since = delta.since_head if delta is not None else None
    run_dir = (
        Path(args.out)
        if args.out
        else default_run_dir(
            _resolve_run_root(run_root, config_root), owner, repo, args.pr,
            acquired.head_sha, delta_since,
        )
    )
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        bind_run_dir(run_dir, owner, repo, args.pr, acquired.head_sha, delta_since)
    except OSError as exc:
        _fail(f"cannot prepare the run directory {str(run_dir)!r}: {exc}", EXIT_RUN_BLOCKED)
    # Rewritten on every invocation (resumed and delta runs included) from the
    # fetch that fixed this run's head, so it can never describe an older head
    # than findings.json.
    try:
        write_pr_record(run_dir, acquired, platform)
    except OSError as exc:
        _fail(f"cannot write the PR record in {str(run_dir)!r}: {exc}", EXIT_RUN_BLOCKED)

    _warn_unknown_resolutions(unknown_resolutions)
    kwargs = {"runner": runner} if runner is not None else {}
    outcome = run_review(
        acquired, profile, run_dir, emit=_emit_stage, delta=delta, delta_stage=delta_stage,
        unknown_resolutions=unknown_resolutions if args.resolved_findings is not None else None,
        **kwargs,
    )
    return _report_outcome(outcome)


def _report_outcome(outcome: RunOutcome) -> int:
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
    path: str,
    *,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha_arg: str | None,
    head_flag: str,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Read a findings file (a `run` output or a bare array) for this PR and
    return its head SHA, its findings, and the run evidence (commit range and
    engines) a `run` output records, validated by the shared findings
    contract. Severity is optional and matched ignoring case, since a person
    may have edited the file; messages are kept whole. A bare array has no
    run record, so its evidence is empty."""
    evidence: dict[str, Any] = {}
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
        if owner_repo.lower() != requested.lower() or data.get("pr_number") != pr_number:
            _fail(
                f"findings file {path!r} is for {owner_repo}#{data.get('pr_number')}, "
                f"but {requested}#{pr_number} was requested",
                EXIT_FINDINGS_INVALID,
            )
        if head_sha_arg is not None and head_sha_arg != head_sha:
            _fail(
                f"{head_flag} {head_sha_arg} conflicts with the head SHA {head_sha} recorded "
                f"in the findings file {path!r}; drop {head_flag} or regenerate the findings",
                EXIT_FINDINGS_INVALID,
            )
        evidence = evidence_from_document(data)
        resolved = data.get(KEY_RESOLVED)
        if resolved:
            evidence[KEY_RESOLVED] = resolved
    else:
        head_sha = head_sha_arg
        findings = data
    if not isinstance(head_sha, str) or not FULL_SHA_RE.match(head_sha):
        _fail(
            "the findings head SHA is missing or not 40 lowercase hex characters "
            f"(a bare findings array needs {head_flag})",
            EXIT_FINDINGS_INVALID,
        )
    if not isinstance(findings, list):
        _fail(f"findings in {path!r} must be a JSON array", EXIT_FINDINGS_INVALID)
    validated: list[dict[str, Any]] = []
    for position, item in enumerate(findings, start=1):
        try:
            validated.append(
                validate_finding(
                    item, position, lenient_severity=True, truncate_message=False,
                    carry_fingerprint=True,
                )
            )
        except InvalidReplyError as exc:
            _fail(str(exc), EXIT_FINDINGS_INVALID)
    return head_sha, validated, evidence


def _load_dropped(path: str) -> list[dict[str, Any]]:
    """Read the --dropped file: an array of dropped candidates, or an object
    holding one under 'dropped'. Field validation (types, one-line fields, no
    fence syntax) is the fence state validator's, run with the rest of the
    evidence before any I/O; this only settles the file's shape."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"cannot read dropped file {path!r}: {exc}", EXIT_FINDINGS_INVALID)
    if isinstance(data, dict):
        data = data.get(KEY_DROPPED)
    if not isinstance(data, list):
        _fail(
            f"dropped file {path!r} must be a JSON array of dropped candidates "
            "(or an object with a 'dropped' array)",
            EXIT_FINDINGS_INVALID,
        )
    return data


def _load_resolutions(path: str | None) -> list[Resolution]:
    if path is None:
        return []
    try:
        return load_resolutions(path)
    except ResolutionsError as exc:
        _fail(str(exc), EXIT_FINDINGS_INVALID)


def _apply_caller_resolutions(
    findings: list[dict[str, Any]],
    evidence: dict[str, Any],
    resolutions: list[Resolution],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Take the findings the caller ruled resolved out of what is posted and
    list them, with the caller's reason, beside the resolved entries the run
    already recorded. A ruling the run already applied is not unknown."""
    if not resolutions:
        return findings, evidence
    remaining, ruled, unknown = split_resolved(findings, resolutions)
    recorded = evidence.get(KEY_RESOLVED, [])
    if not isinstance(recorded, list):
        # Left as is: the evidence validation that follows refuses it.
        return findings, evidence
    if unknown:
        pending = [r for r in resolutions if r.ref in unknown]
        _, _, unknown = split_resolved([e for e in recorded if isinstance(e, dict)], pending)
    _warn_unknown_resolutions(unknown)
    entries = [resolved_entry(f, BY_CALLER, r.reason) for f, r in ruled]
    if entries:
        evidence = {**evidence, KEY_RESOLVED: [*recorded, *entries]}
    return remaining, evidence


def _warn_unknown_resolutions(refs) -> None:
    for ref in refs:
        print(
            f"loadout-review: warning: resolved-findings entry {ref!r} matches no prior "
            "finding; ignored",
            file=sys.stderr,
        )


def _load_prior_review(
    args: argparse.Namespace, *, owner: str, repo: str
) -> tuple[str | None, list[dict[str, Any]]]:
    """The caller's own last verdict for delta mode: its head and the findings
    it posted. (None, []) when the caller asked for a full review."""
    if args.prior_findings is None:
        if args.prior_head_sha is None:
            return None, []
        if not FULL_SHA_RE.match(args.prior_head_sha):
            _fail(
                "--prior-head-sha must be 40 lowercase hex characters, "
                f"got {args.prior_head_sha!r}",
                EXIT_USAGE,
            )
        return args.prior_head_sha, []
    head_sha, findings, _ = _load_findings(
        args.prior_findings, owner=owner, repo=repo, pr_number=args.pr,
        head_sha_arg=args.prior_head_sha, head_flag="--prior-head-sha",
    )
    for position, finding in enumerate(findings, start=1):
        if finding["severity"] is None:
            _fail(
                f"prior finding {position} has no severity; a delta review needs each "
                "open finding's severity to carry it forward",
                EXIT_FINDINGS_INVALID,
            )
    return head_sha, findings


def _render_for_post(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings as the posted body carries them: the review-post contract has
    no severity field, so a present severity leads the message."""
    return [
        {
            **f,
            "message": f"({f['severity']}) {f['message']}" if f["severity"] else f["message"],
        }
        for f in findings
    ]


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
    head_sha, findings, evidence = _load_findings(
        args.findings, owner=owner, repo=repo, pr_number=args.pr,
        head_sha_arg=args.head_sha, head_flag="--head-sha",
    )
    if args.dropped:
        dropped = _load_dropped(args.dropped)
        if dropped:
            evidence = {**evidence, KEY_DROPPED: dropped}
    findings, evidence = _apply_caller_resolutions(
        findings, evidence, _load_resolutions(args.resolved_findings)
    )
    if args.status == "clean" and any(f["severity"] == "blocking" for f in findings):
        _fail(
            "--status clean contradicts the findings file, which carries a blocking finding; "
            "post --status blocking, or remove the finding if you judged it wrong",
            EXIT_FINDINGS_INVALID,
        )

    state: dict[str, Any] = {}
    if args.state_file:
        try:
            loaded = json.loads(Path(args.state_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            _fail(f"cannot read state file {args.state_file!r}: {exc}", EXIT_FINDINGS_INVALID)
        if not isinstance(loaded, dict):
            _fail(
                f"state file {args.state_file!r} must be a JSON object",
                EXIT_FINDINGS_INVALID,
            )
        derived = sorted(set(loaded) & set(EVIDENCE_KEYS))
        if derived:
            _fail(
                f"state file {args.state_file!r} carries {derived}, which this verb derives "
                "from the findings, the run record and --dropped; remove them from the state file",
                EXIT_FINDINGS_INVALID,
            )
        state = loaded
        # Validated here as well as when the fence is built, so a bad state
        # file fails before any I/O instead of after the head re-check.
        try:
            normalize_findings_state(state, head_sha=head_sha, review_status=args.status)
        except ValueError as exc:
            _fail(f"state file {args.state_file!r}: {exc}", EXIT_FINDINGS_INVALID)

    # The evidence is validated for the same reason: a malformed dropped
    # candidate or a fence-shaped sequence fails here, before any I/O.
    sequences = failure_sequences_of(findings)
    try:
        normalize_findings_state(
            {**evidence, **({KEY_FAILURE_SEQUENCES: sequences} if sequences else {})},
            head_sha=head_sha,
            review_status=args.status,
        )
    except ValueError as exc:
        _fail(f"findings evidence: {exc}", EXIT_FINDINGS_INVALID)

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
        "findings": [
            {k: f[k] for k in (*_FINDING_KEYS, *_OPTIONAL_FINDING_KEYS) if k in f}
            for f in _render_for_post(findings)
        ],
        **state,
        **evidence,
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
    result = {
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
    for key in ("comment", "reused_from_created_at"):
        if key in posted:
            result[key] = posted[key]
    if KEY_DROPPED in evidence:
        result["dropped_count"] = len(evidence[KEY_DROPPED])

    # The late re-read above narrows the GitHub race but cannot close it: a
    # push can still land between that read and the post. The comment cannot
    # be taken back (a landed verdict is never deleted), so the closing check
    # runs on the landed comment and reports honestly when its head is gone.
    try:
        live_head = _acquire(
            args, owner=owner, repo=repo, caller=caller, platform=platform,
            token_provider=token_provider, opener=opener,
        ).head_sha
    except ReviewCliError as exc:
        print(
            f"loadout-review: the posted verdict could not be re-checked against the PR head: {exc}",
            file=sys.stderr,
        )
        # Success is only ever reported against a head confirmed current, so an
        # unreadable head is a failure exit even though the comment landed.
        result.update(result="posted_head_unconfirmed", head_recheck="unavailable")
        print(json.dumps(result))
        return EXIT_POST_FAILED
    if live_head != head_sha:
        result.update(result="posted_head_moved", head_recheck="moved", current_head_sha=live_head)
        print(json.dumps(result))
        print(
            f"loadout-review: the verdict landed for head {head_sha}, but the PR head is now "
            f"{live_head}; that verdict no longer covers the PR. Run the review again.",
            file=sys.stderr,
        )
        return EXIT_STALE_HEAD
    result["head_recheck"] = "current"
    print(json.dumps(result))
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
