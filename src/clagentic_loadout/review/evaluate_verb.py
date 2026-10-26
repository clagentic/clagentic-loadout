"""review.evaluate_verb — `loadout-review evaluate`: compute a verdict from findings.

Offline and read-only: it reads a findings file, reads policy from the base
revision of a local repository (review.policy_source), and prints the Verdict.
It makes no host call and posts nothing, so it binds no caller identity.

Exit codes (the reserved range for this subcommand; git's own exit codes are
never passed through):

  0   CLEAN
  1   a SHA argument is not a full commit SHA
  3  the user-level evaluator configuration is invalid
  32  the findings file is unreadable or invalid
  40  BLOCKED
  41  DEGRADED
  42  STALE

User-level configuration (never read from the repository, because a threshold
the pull request can change could lower its own verdict)::

    review:
      evaluator:
        block_at: high            # low | medium | high | critical
        required_scanners: [name] # satisfied only by status ran, or not_applicable
                                  # with a reason; anything else is DEGRADED
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from clagentic_loadout.review.evaluate import (
    OUTCOME_BLOCKED,
    OUTCOME_CLEAN,
    OUTCOME_DEGRADED,
    OUTCOME_STALE,
    Verdict,
    evaluate,
)
from clagentic_loadout.review.evaluate_render import render_text
from clagentic_loadout.review.findings_contract import InvalidReplyError, validate_finding
from clagentic_loadout.review.policy_source import read_policy
from clagentic_loadout.review.ruling_provider import (
    RulingScope,
    read_scope_rulings,
    resolve_ruling_provider,
)
from clagentic_loadout.review.rubric import DEFAULT_BLOCK_AT, SEVERITIES
from clagentic_loadout.review.stakes import VISIBILITIES
from clagentic_loadout.review.utc_date import today_utc
from clagentic_loadout.sha import FULL_SHA_RE
from clagentic_loadout.transport.provider_config import load_user_config_section

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_EVALUATOR_CONFIG_INVALID = 3
EXIT_FINDINGS_INVALID = 32
EXIT_BLOCKED = 40
EXIT_DEGRADED = 41
EXIT_STALE = 42

OUTCOME_EXIT_CODES = {
    OUTCOME_CLEAN: EXIT_OK,
    OUTCOME_BLOCKED: EXIT_BLOCKED,
    OUTCOME_DEGRADED: EXIT_DEGRADED,
    OUTCOME_STALE: EXIT_STALE,
}

CONFIG_SECTION_REVIEW = "review"
CONFIG_KEY_EVALUATOR = "evaluator"
FORMATS = ("json", "text")

PROFILE_FLAG_NOTICE = (
    "loadout-review: --profile is deprecated and selects nothing; stakes are read from the "
    "posture keys of the repo profile at the base revision"
)


class EvaluateError(Exception):
    """A failure that ends the subcommand with a specific exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def add_evaluate_parser(subparsers: Any) -> argparse.ArgumentParser:
    evaluate_parser = subparsers.add_parser(
        "evaluate",
        help="Compute a verdict (CLEAN, BLOCKED, DEGRADED or STALE) from findings and facts.",
        description=(
            "Compute the verdict for a findings file. The reviewer's findings carry facts "
            "only; severity and blocking are computed here from the rubric, the stakes of "
            "each path (the posture of the repo profile) and the standing dispositions, read from the base "
            "revision (git show <base>:<path>), never from the working tree or the head. "
            "Offline: no host call is made. Exit codes: 0 CLEAN, 40 BLOCKED, 41 DEGRADED, "
            "42 STALE, 32 findings file invalid, 3 evaluator configuration invalid."
        ),
    )
    evaluate_parser.add_argument(
        "--findings",
        required=True,
        help="Findings file: a `run` output, or a JSON array of findings. Each finding may "
        "carry a 'facts' object (see the published review-facts schema). A `run` output "
        "may also carry 'scanners_run'.",
    )
    evaluate_parser.add_argument(
        "--base",
        required=True,
        type=str.strip,
        help="Full 40-character SHA of the merge base. Policy files and tree markers are "
        "read from this revision.",
    )
    evaluate_parser.add_argument(
        "--head",
        required=True,
        type=str.strip,
        help="Full 40-character SHA of the head the findings are for.",
    )
    evaluate_parser.add_argument(
        "--expected-head",
        type=str.strip,
        default=None,
        help="Full SHA the verdict must be for (typically the PR's current head). When it "
        "differs from the head the findings are for, the outcome is STALE. Default: --head.",
    )
    evaluate_parser.add_argument(
        "--profile",
        default=None,
        help="Deprecated and ignored: stakes now come from the posture keys of the repo "
        "profile at the base revision, which has no named profiles. Accepted so existing "
        "callers keep working; a notice is printed to standard error.",
    )
    evaluate_parser.add_argument(
        "--visibility",
        choices=VISIBILITIES,
        default=None,
        help="Repository visibility as the host reports it. 'public' marks every path "
        "exposed.",
    )
    evaluate_parser.add_argument(
        "--repo-path",
        default=None,
        help="Local repository holding the base revision (default: the current directory).",
    )
    evaluate_parser.add_argument(
        "--repo",
        default=None,
        help="owner/repo the findings are for. With --pr, the per-PR rulings recorded for "
        "that pull request in the ruling registry are applied. Without both, no ruling is "
        "applied.",
    )
    evaluate_parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="Pull request number the findings are for; see --repo.",
    )
    evaluate_parser.add_argument(
        "--format",
        choices=FORMATS,
        default="json",
        help="json prints the Verdict as one JSON object that includes the human rendering "
        "under 'rendering'; text prints only the rendering.",
    )
    return evaluate_parser


def _sha(value: str, flag: str, code: int = EXIT_USAGE) -> str:
    if not FULL_SHA_RE.match(value):
        raise EvaluateError(f"{flag} must be 40 lowercase hex characters, got {value!r}", code)
    return value


def load_evaluator_config(config_root: str | Path | None) -> tuple[str, list[str]]:
    """``(block_at, required_scanners)`` from the user-level config."""
    section = load_user_config_section(CONFIG_SECTION_REVIEW, config_root=config_root)
    block = section.get(CONFIG_KEY_EVALUATOR) if isinstance(section, dict) else None
    if block is None:
        return DEFAULT_BLOCK_AT, []
    if not isinstance(block, dict):
        raise EvaluateError("review.evaluator must be a mapping", EXIT_EVALUATOR_CONFIG_INVALID)
    block_at = block.get("block_at", DEFAULT_BLOCK_AT)
    if block_at not in SEVERITIES[1:]:
        raise EvaluateError(
            f"review.evaluator.block_at must be one of {', '.join(SEVERITIES[1:])}, "
            f"got {block_at!r}",
            EXIT_EVALUATOR_CONFIG_INVALID,
        )
    scanners = block.get("required_scanners", [])
    if not isinstance(scanners, list) or not all(isinstance(s, str) and s for s in scanners):
        raise EvaluateError(
            "review.evaluator.required_scanners must be a list of scanner names",
            EXIT_EVALUATOR_CONFIG_INVALID,
        )
    return block_at, scanners


def load_findings_file(
    path: str,
) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]] | None]:
    """``(head_sha, findings, scanners_run)`` from a findings file. The head and
    scanners are None for a bare array."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluateError(
            f"cannot read findings file {path!r}: {exc}", EXIT_FINDINGS_INVALID
        ) from exc
    head_sha: str | None = None
    scanners: list[dict[str, Any]] | None = None
    findings = data
    if isinstance(data, dict):
        findings = data.get("findings")
        recorded_head = data.get("head_sha")
        if recorded_head is not None:
            if not isinstance(recorded_head, str):
                raise EvaluateError(
                    f"findings file {path!r} has a head_sha that is not a string",
                    EXIT_FINDINGS_INVALID,
                )
            head_sha = _sha(recorded_head, "the findings file head_sha", EXIT_FINDINGS_INVALID)
        scanners = data.get("scanners_run")
        if scanners is not None and not (
            isinstance(scanners, list) and all(isinstance(s, dict) for s in scanners)
        ):
            raise EvaluateError(
                f"findings file {path!r}: scanners_run must be a list of objects",
                EXIT_FINDINGS_INVALID,
            )
    if not isinstance(findings, list):
        raise EvaluateError(f"findings in {path!r} must be a JSON array", EXIT_FINDINGS_INVALID)
    validated = []
    for position, item in enumerate(findings, start=1):
        try:
            validated.append(
                validate_finding(
                    item,
                    position,
                    lenient_severity=True,
                    truncate_message=False,
                    carry_fingerprint=True,
                    carry_facts=True,
                )
            )
        except InvalidReplyError as exc:
            raise EvaluateError(str(exc), EXIT_FINDINGS_INVALID) from exc
    return head_sha, validated, scanners


def run_verdict(args: argparse.Namespace, *, config_root: str | Path | None = None) -> Verdict:
    """Everything `evaluate` does short of printing."""
    base = _sha(args.base, "--base")
    head = _sha(args.head, "--head")
    expected = _sha(args.expected_head, "--expected-head") if args.expected_head else head
    block_at, required_scanners = load_evaluator_config(config_root)
    file_head, findings, scanners = load_findings_file(args.findings)
    if args.profile is not None:
        print(PROFILE_FLAG_NOTICE, file=sys.stderr)
    policy = read_policy(
        Path(args.repo_path) if args.repo_path else Path.cwd(),
        base,
        visibility=args.visibility,
    )
    scope = _ruling_scope(args)
    ruled = read_scope_rulings(resolve_ruling_provider(config_root), scope)
    return evaluate(
        findings,
        policy.stakes,
        (*policy.dispositions, *ruled.rulings),
        file_head if file_head is not None else head,
        expected,
        block_at=block_at,
        today=today_utc(),
        scanners_run=scanners,
        required_scanners=required_scanners,
        degraded_reasons=[*policy.degraded, *ruled.degraded],
    )


def _ruling_scope(args: argparse.Namespace) -> RulingScope | None:
    """The scope rulings are read for: both --repo and --pr, or none. A value
    given without its partner is a usage error rather than a silent no-op."""
    if args.repo is None and args.pr is None:
        return None
    if args.repo is None or args.pr is None:
        raise EvaluateError("--repo and --pr must be given together", EXIT_USAGE)
    parts = args.repo.strip().split("/")
    if len(parts) != 2 or not all(parts) or args.pr < 1:
        raise EvaluateError(
            f"--repo must be owner/repo and --pr a positive number, got {args.repo!r} "
            f"and {args.pr!r}",
            EXIT_USAGE,
        )
    repo_path = Path(args.repo_path) if args.repo_path else Path.cwd()
    return RulingScope(args.repo.strip(), args.pr, (repo_path,))


def run_evaluate(args: argparse.Namespace, *, config_root: str | Path | None = None) -> int:
    """Run the subcommand and return its exit code."""
    try:
        verdict = run_verdict(args, config_root=config_root)
    except EvaluateError as exc:
        print(f"loadout-review: {exc}", file=sys.stderr)
        return exc.code
    rendering = render_text(verdict)
    if args.format == "text":
        print(rendering)
    else:
        print(json.dumps({**verdict.as_dict(), "rendering": rendering}))
    return OUTCOME_EXIT_CODES[verdict.outcome]


__all__ = [
    "EXIT_BLOCKED",
    "EXIT_DEGRADED",
    "EXIT_EVALUATOR_CONFIG_INVALID",
    "EXIT_FINDINGS_INVALID",
    "EXIT_STALE",
    "EvaluateError",
    "add_evaluate_parser",
    "load_evaluator_config",
    "load_findings_file",
    "run_evaluate",
    "run_verdict",
]
