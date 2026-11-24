"""merge.procedure_verb -- `loadout-merge-procedure`: print the release-step procedure.

For a release role, a repository and a pull request, this verb reads the pull request
live from the host under a bound caller, pins its head, resolves the reviewer roles
`loadout-merge` would require (the repository's tracked gate at the base commit,
through merge's own loader) and the roles that hold the ``merge`` verb (the trusted
user-level role mapping), and prints one JSON object whose ordered ``lines`` are the
finished command lines of the release step: the head re-read, the merge check
(``--report-only``), the merge, the post-merge run, the close and one ruling per entry
of an optional rulings file. It runs none of them.

Every line is one physical line naming an installed command and runs as written: no
placeholder, shell variable, heredoc, pipe or redirect, every value shell-quoted.

It reports and prints. A role that does not hold ``merge``, a pull request that is not
open and a gate that carries warnings or declares no reviewer are notes, on stderr and
in the object; none refuses. Only an unusable input does: a value that cannot sit in
one command line, a malformed rulings file, an unreadable role mapping.

AUTHORITY. No input of this verb decides who may merge. The ``--authorized-role``
values it prints are the roles holding the ``merge`` verb label in the effective
mapping (review.ruling_authority), read through its trust check. The same label is used
for the close and the post-merge lines, because their ``--role`` help says they share
one authority seam. The merge method is the caller's choice, printed explicitly on
every merge line even at the default: no repository setting names one.

Exit codes (the reserved range; no child-process code is passed through):

  0   printed
  1   usage: a malformed input or a value that cannot be printed into a command line
  2   no credential could be resolved for the caller
  3   the user-level role mapping is untrusted or unusable
  4   --platform does not match the backend that can serve it
  7   --caller does not match the attested invoking identity
  27  the host could not be read (pull request, base branch tip)
  64  the rulings file or the --out path is unusable
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clagentic_loadout._version import get_version
from clagentic_loadout.merge import commit_subjects
from clagentic_loadout.merge.config_binding import (
    config_binding_for,
    require_repo_path_in_git_tree,
    require_resolvable_config_root,
)
from clagentic_loadout.merge.errors import (
    GateFactUnavailableError,
    MergeUsageError,
    PlatformMismatchError,
)
from clagentic_loadout.merge.live_base import resolve_live_base_tip
from clagentic_loadout.merge.repo_gate_runtime import gate_source_line, load_repo_gate_at_base
from clagentic_loadout.merge.repo_path_consistency import assert_repo_path_consistent
from clagentic_loadout.merge.required_reviewers import resolve_required_reviewers
from clagentic_loadout.merge.tree_sync import resolve_base_branch
from clagentic_loadout.merge.verb import MergeVerbError, _resolve_backend
from clagentic_loadout.new_file_out import NewFileError, emit_new_file
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.provisioning.roles import InvalidRoleConfigError
from clagentic_loadout.push.errors import RemoteResolutionError
from clagentic_loadout.push.git_coords import parse_owner_repo
from clagentic_loadout.review.dispositions import KINDS
from clagentic_loadout.review.procedure import (
    ProcedureInputError,
    check_not_option,
    check_printable,
    command_line,
)
from clagentic_loadout.review.ruling_authority import UntrustedRolesSourceError, verb_holders
from clagentic_loadout.sha import FULL_SHA_RE
from clagentic_loadout.transport.attestation import AttestationError
from clagentic_loadout.transport.attestation import resolve_bound_identity as _resolve_identity
from clagentic_loadout.transport.caller_binding import (
    CallerBindingError,
    bind_caller,
    describe_omitted_caller_behavior,
    resolve_for_binding,
)
from clagentic_loadout.transport.credential_provider import DEFAULT_ROLE, TokenProvider
from clagentic_loadout.transport.git_host_api import _resolve_git_host_base
from clagentic_loadout.transport.github_client import GITHUB_API_BASE
from clagentic_loadout.transport.seam_binding import staging_temp_parent

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_TOKEN_FETCH_FAILED = 2
EXIT_CONFIG_INVALID = 3
EXIT_WRONG_PLATFORM = 4
EXIT_CALLER_INVOKER_MISMATCH = 7
EXIT_HOST_UNAVAILABLE = 27
EXIT_REFUSED = 64

#: The verb label whose holders the printed ``--authorized-role`` values name.
AUTHORIZING_VERB = "merge"
MERGE_METHODS = ("merge", "squash", "rebase")

STEP_HEAD = "head"
STEP_MERGE_CHECK = "merge_check"
STEP_MERGE = "merge"
STEP_POST_MERGE = "post_merge"
STEP_CLOSE = "close"
STEP_RULING = "ruling"

_RULING_STRING_KEYS = ("fingerprint", "kind", "rationale", "requested_by", "source_ref")
_RULING_OPTIONAL_KEYS = ("expires", "reference")
_RULING_FLAGS = {
    "fingerprint": "--fingerprint",
    "kind": "--kind",
    "rationale": "--rationale",
    "requested_by": "--requested-by",
    "source_ref": "--source-ref",
    "expires": "--expires",
    "reference": "--reference",
}


class ProcedureVerbError(Exception):
    """A failure that ends the verb with a specific exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadout-merge-procedure",
        description=(
            "loadout-merge-procedure -- print the release step for one pull request as "
            "finished command lines. Reads the pull request live under --caller, pins its "
            "head, resolves the reviewer roles the merge requires (the repository's "
            "tracked gate at the base commit) and the roles that hold the merge verb "
            "(the trusted user-level role mapping), and prints one JSON object: the "
            "pinned head and an ordered list of {step, line} for the head re-read, the "
            "merge check, the merge, the post-merge run, the close and one ruling per "
            "entry of --rulings. Nothing is run and no rule is applied; the only refusals "
            "are of an unusable input."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  loadout-merge-procedure --role some-role --platform forgejo \\\n"
            "      --repo some-owner/some-repo --pr 42 --repo-path /path/to/checkout \\\n"
            "      --merge-method squash\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"loadout-merge-procedure {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    parser.add_argument(
        "--caller",
        default=None,
        help="The invoking identity whose credential reads the host. It must match this "
        "process's own attested invoking identity or the call is refused before any I/O. "
        f"{describe_omitted_caller_behavior(flag_name='--caller')}",
    )
    parser.add_argument(
        "--role",
        required=True,
        help="The release role, printed into the lines (--role on merge, close, "
        "post-merge and ruling; --caller on the head re-read). It is an opaque word.",
    )
    parser.add_argument(
        "--platform",
        required=True,
        choices=(PLATFORM_GITHUB, PLATFORM_FORGEJO),
        help="Platform hosting the pull request.",
    )
    parser.add_argument("--repo", required=True, help="owner/repo of the pull request.")
    parser.add_argument("--pr", required=True, type=int, help="Pull request number.")
    parser.add_argument(
        "--repo-path",
        required=True,
        help="Checkout of the repository. The gate is read from it at the pull request's "
        "base commit, and it is printed into the merge, post-merge and ruling lines.",
    )
    parser.add_argument(
        "--git-host-base-url",
        default=None,
        help="Forgejo API base URL, resolved as the other verbs resolve it. When given on "
        "--platform forgejo it is printed into every line that takes it; ignored for GitHub.",
    )
    parser.add_argument(
        "--merge-method",
        default=commit_subjects.REAL_MERGE_METHOD,
        choices=MERGE_METHODS,
        help="The merge method printed on the merge and merge-check lines, always "
        f"explicitly (default: {commit_subjects.REAL_MERGE_METHOD}).",
    )
    parser.add_argument(
        "--merge-message",
        default="",
        help="Optional merge commit message suffix, printed on the merge and merge-check lines.",
    )
    parser.add_argument(
        "--delete-branch",
        action="store_true",
        default=False,
        help="Print --delete-branch on the close line.",
    )
    parser.add_argument(
        "--rulings",
        default=None,
        help="A JSON file holding an array of rulings, each an object with fence_comment "
        "(integer), fingerprint, kind, rationale, requested_by and source_ref, and "
        "optionally expires and reference. One ruling line is printed per entry, in file "
        "order. The procedure never lists or chooses kept findings.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Write the JSON object to this new file instead of standard output; an "
        "existing file is never replaced (exit 64).",
    )
    return parser


@dataclass(frozen=True)
class Plan:
    """What the printed lines are built from."""

    role: str
    owner: str
    repo: str
    pr: int
    platform: str
    head_sha: str
    repo_path: str
    required_roles: tuple[str, ...]
    authorized_roles: tuple[str, ...]
    merge_method: str
    merge_message: str = ""
    delete_branch: bool = False
    base_url: str | None = None
    rulings: tuple[dict[str, Any], ...] = ()

    @property
    def repo_arg(self) -> str:
        return f"{self.owner}/{self.repo}"


def _head_line(plan: Plan) -> str:
    if plan.platform == PLATFORM_FORGEJO:
        path = f"/api/v1/repos/{plan.repo_arg}/pulls/{plan.pr}"
        base = ["--git-host-base-url", plan.base_url] if plan.base_url else []
    else:
        path = f"{GITHUB_API_BASE}/repos/{plan.repo_arg}/pulls/{plan.pr}"
        base = []
    return command_line("loadout-git-host-api", "--caller", plan.role, *base, "GET", path)


def build_lines(plan: Plan) -> list[dict[str, str]]:
    """The ordered ``{step, line}`` list for *plan*."""
    base = (
        ["--git-host-base-url", plan.base_url]
        if plan.platform == PLATFORM_FORGEJO and plan.base_url
        else []
    )
    common = [
        "--role", plan.role, "--platform", plan.platform,
        "--repo", plan.repo_arg, "--pr", str(plan.pr),
    ]
    authorized = [part for role in plan.authorized_roles for part in ("--authorized-role", role)]
    required = [part for role in plan.required_roles for part in ("--required-reviewer", role)]
    message = ["--merge-message", plan.merge_message] if plan.merge_message else []
    merge = [
        "loadout-merge", *common, "--expected-head-sha", plan.head_sha, *required, *authorized,
        "--merge-method", plan.merge_method, *message, "--repo-path", plan.repo_path, *base,
    ]
    close = ["loadout-close-pr", *common, *authorized]
    if plan.delete_branch:
        close.append("--delete-branch")
    close += base
    lines = [
        (STEP_HEAD, _head_line(plan)),
        (STEP_MERGE_CHECK, command_line(*merge, "--report-only")),
        (STEP_MERGE, command_line(*merge)),
        (
            STEP_POST_MERGE,
            command_line(
                "loadout-post-merge", *common, *authorized, "--repo-path", plan.repo_path, *base
            ),
        ),
        (STEP_CLOSE, command_line(*close)),
    ]
    for entry in plan.rulings:
        ruling = ["loadout-ruling", "record", *common, "--fence-comment", str(entry["fence_comment"])]
        for key in (*_RULING_STRING_KEYS, *_RULING_OPTIONAL_KEYS):
            if key in entry:
                ruling += [_RULING_FLAGS[key], entry[key]]
        ruling += [*required, "--repo-path", plan.repo_path, "--head", plan.head_sha, *base]
        lines.append((STEP_RULING, command_line(*ruling)))
    return [{"step": step, "line": line} for step, line in lines]


def load_rulings(path: str) -> list[dict[str, Any]]:
    """The rulings *path* holds, each checked for the keys `loadout-ruling record`
    needs. Raises ProcedureVerbError(EXIT_REFUSED) naming the file, and the entry."""

    def refuse(detail: str) -> ProcedureVerbError:
        return ProcedureVerbError(f"--rulings {path!r}: {detail}", EXIT_REFUSED)

    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise refuse(f"cannot be read as JSON: {exc}") from exc
    if not isinstance(document, list):
        raise refuse(f"must hold a JSON array, got {type(document).__name__}")
    allowed = {"fence_comment", *_RULING_STRING_KEYS, *_RULING_OPTIONAL_KEYS}
    for index, entry in enumerate(document):
        if not isinstance(entry, dict):
            raise refuse(f"entry {index} must be an object, got {type(entry).__name__}")
        unknown = sorted(set(entry) - allowed)
        if unknown:
            raise refuse(f"entry {index} has unknown key(s) {unknown}")
        for key in ("fence_comment", *_RULING_STRING_KEYS):
            if key not in entry:
                raise refuse(f"entry {index} is missing {key!r}")
        comment = entry["fence_comment"]
        if not isinstance(comment, int) or isinstance(comment, bool):
            raise refuse(f"entry {index} fence_comment must be an integer, got {comment!r}")
        for key in (*_RULING_STRING_KEYS, *(k for k in _RULING_OPTIONAL_KEYS if k in entry)):
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise refuse(f"entry {index} {key!r} must be a non-empty string")
        if entry["kind"] not in KINDS:
            raise refuse(f"entry {index} kind {entry['kind']!r} is not one of {list(KINDS)}")
    return document


def _check_values(values: dict[str, str]) -> None:
    """Refuse a value that cannot be printed as one flag argument of one line."""
    try:
        check_printable(values)
        check_not_option(values)
    except ProcedureInputError as exc:
        raise ProcedureVerbError(str(exc), EXIT_USAGE) from exc


def _check_reviewer_roles(roles: Sequence[str]) -> None:
    """A required role is printed bare, and the merge reads a ``:`` in a bare value
    as a name:login pair."""
    _check_values({f"required reviewer role {role!r}": role for role in roles})
    for role in roles:
        if ":" in role:
            raise ProcedureVerbError(
                f"required reviewer role {role!r} holds ':', so the printed bare value would "
                "be read as a name:login pair",
                EXIT_USAGE,
            )


def _ruling_values(rulings: Sequence[dict[str, Any]]) -> dict[str, str]:
    return {
        f"--rulings entry {index} {key}": entry[key]
        for index, entry in enumerate(rulings)
        for key in (*_RULING_STRING_KEYS, *_RULING_OPTIONAL_KEYS)
        if key in entry
    }


def _bind(args: argparse.Namespace, identity_provider):
    resolve_identity = identity_provider if identity_provider is not None else _resolve_identity
    try:
        identity = resolve_for_binding(
            caller_explicit=args.caller is not None,
            caller=args.caller or DEFAULT_ROLE,
            resolve_identity_fn=resolve_identity,
        )
    except AttestationError as exc:
        raise ProcedureVerbError(
            f"attested-identity resolution FAILED -- {exc}", EXIT_CALLER_INVOKER_MISMATCH
        ) from exc
    caller = args.caller if args.caller is not None else identity.subject
    bind_caller(caller, caller_explicit=True, identity=identity)
    return caller, identity


def _authorized_roles(config_root):
    try:
        return verb_holders(AUTHORIZING_VERB, config_root)
    except InvalidRoleConfigError as exc:
        raise ProcedureVerbError(f"role configuration is invalid: {exc}", EXIT_CONFIG_INVALID) from exc
    except UntrustedRolesSourceError as exc:
        raise ProcedureVerbError(str(exc), EXIT_CONFIG_INVALID) from exc


def _pr_facts(backend, owner: str, repo: str, pr: int) -> tuple[dict[str, Any], str, str]:
    """The live pull request record, its head and the base branch's tip now."""
    try:
        pr_info = backend.get_pr_info(owner, repo, pr)
        head = pr_info.get("head")
        head_sha = head.get("sha", "") if isinstance(head, dict) else ""
        if not isinstance(head_sha, str) or not FULL_SHA_RE.match(head_sha):
            raise GateFactUnavailableError(
                f"the host did not return a full head commit SHA for pull request #{pr} "
                f"in {owner}/{repo}"
            )
        base_sha = resolve_live_base_tip(
            lambda branch: backend.get_branch_tip_sha(owner, repo, branch), pr_info
        )
    except GateFactUnavailableError as exc:
        raise ProcedureVerbError(str(exc), EXIT_HOST_UNAVAILABLE) from exc
    return pr_info, head_sha, base_sha


def _notes(
    *, role: str, roles: frozenset[str], state: str, gate, required: Sequence[str], unreachable
) -> list[str]:
    notes = []
    if role not in roles:
        notes.append(
            f"role {role!r} does not hold the {AUTHORIZING_VERB!r} verb (holders: "
            f"{sorted(roles)}); the printed merge, close and post-merge lines would be refused "
            "by their authority check"
        )
    if state != "open":
        notes.append(f"the pull request is not open (state: {state})")
    notes.extend(f"gate warning: {warning}" for warning in gate.warnings)
    if gate.pre_checks_error:
        notes.append(f"the gate's pre_checks could not be determined: {gate.pre_checks_error}")
    if unreachable:
        notes.append(
            f"required scanners are declared for role(s) {list(unreachable)} that are not "
            "required reviewers"
        )
    if not required:
        notes.append("the gate declares no reviewer role, so no --required-reviewer is printed")
    return notes


def _run(
    args: argparse.Namespace,
    *,
    token_provider: TokenProvider | None,
    opener,
    identity_provider,
    config_root,
) -> int:
    try:
        owner, repo = parse_owner_repo(args.repo)
    except RemoteResolutionError as exc:
        raise ProcedureVerbError(str(exc), EXIT_USAGE) from exc
    if args.pr <= 0:
        raise ProcedureVerbError(f"--pr must be a positive integer, got: {args.pr!r}", EXIT_USAGE)
    caller, identity = _bind(args, identity_provider)

    role, repo_arg = args.role.strip(), f"{owner}/{repo}"
    if not role:
        raise ProcedureVerbError("--role must not be empty", EXIT_USAGE)
    base_url = args.git_host_base_url or None
    rulings = load_rulings(args.rulings) if args.rulings else []
    named = {
        "--role": role,
        "--repo": repo_arg,
        "--repo-path": args.repo_path,
        **({"--merge-message": args.merge_message} if args.merge_message else {}),
        **({"--git-host-base-url": base_url} if base_url else {}),
    }
    _check_values({**named, **_ruling_values(rulings)})
    if args.out and Path(args.out).exists():
        raise ProcedureVerbError(
            f"--out {args.out!r} already exists; this verb never replaces a file", EXIT_REFUSED
        )

    holders = _authorized_roles(config_root)
    authorized = tuple(sorted(holders.roles))
    _check_values({f"role {r!r} in the role mapping": r for r in authorized})

    require_repo_path_in_git_tree(args.repo_path)
    assert_repo_path_consistent(repo_arg, args.repo_path)
    require_resolvable_config_root(args.repo_path)
    git_host_base = _resolve_git_host_base(args.git_host_base_url)

    try:
        backend = _resolve_backend(
            args.platform,
            owner=owner,
            repo=repo,
            role=caller,
            git_host_base=git_host_base,
            token_provider=token_provider,
            opener=opener,
        )
    except PlatformMismatchError as exc:
        raise ProcedureVerbError(str(exc), EXIT_WRONG_PLATFORM) from exc
    except MergeVerbError as exc:
        # Only the usage and token-fetch codes (1 and 2) come out of backend
        # resolution, and this verb reserves the same numbers for them.
        raise ProcedureVerbError(str(exc), exc.code) from exc

    pr_info, head_sha, base_sha = _pr_facts(backend, owner, repo, args.pr)
    state = "merged" if pr_info.get("merged") is True else str(pr_info.get("state") or "unknown")
    fetch_auth = backend.fetch_auth().with_temp_parent(staging_temp_parent(identity))
    gate = load_repo_gate_at_base(
        args.repo_path,
        base_sha=base_sha,
        base_branch=resolve_base_branch(pr_info),
        config_binding=config_binding_for(args.platform, git_host_base, repo_arg),
        fetch_auth=fetch_auth,
    )
    resolved = resolve_required_reviewers(gate, args.platform, {})
    gate = resolved.gate
    required = tuple(resolved.reviewers)
    _check_reviewer_roles(required)

    notes = _notes(
        role=role, roles=holders.roles, state=state, gate=gate, required=required,
        unreachable=resolved.unreachable_scanner_roles,
    )
    for note in notes:
        print(f"loadout-merge-procedure: NOTE -- {note}", file=sys.stderr)

    plan = Plan(
        role=role, owner=owner, repo=repo, pr=args.pr, platform=args.platform,
        head_sha=head_sha, repo_path=args.repo_path, required_roles=required,
        authorized_roles=authorized, merge_method=args.merge_method,
        merge_message=args.merge_message, delete_branch=args.delete_branch,
        base_url=base_url, rulings=tuple(rulings),
    )
    payload = {
        "result": "procedure",
        "role": role,
        "repo": repo_arg,
        "pr": args.pr,
        "platform": args.platform,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "base_branch": resolve_base_branch(pr_info),
        "state": state,
        "title": str(pr_info.get("title") or ""),
        "required_reviewers": {
            "roles": list(required),
            "source": gate_source_line(gate),
            "warnings": list(gate.warnings),
        },
        "authorized_roles": {"roles": list(authorized), "source": holders.source},
        "merge_method": args.merge_method,
        "notes": notes,
        "lines": build_lines(plan),
    }
    rendered = json.dumps(payload)
    if args.out:
        try:
            emit_new_file(args.out, rendered + "\n")
        except NewFileError as exc:
            raise ProcedureVerbError(str(exc), EXIT_REFUSED) from exc
    else:
        print(rendered)
    return EXIT_OK


def main(
    argv: list[str] | None = None,
    *,
    token_provider: TokenProvider | None = None,
    opener=None,
    identity_provider: Callable[[], Any] | None = None,
    config_root: str | Path | None = None,
) -> int:
    """CLI entrypoint. Returns the process exit code. The keyword arguments are
    injection points for tests and embedding callers."""
    if argv is None:
        argv = sys.argv[1:]
    parser = _build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE
    try:
        return _run(
            args,
            token_provider=token_provider,
            opener=opener,
            identity_provider=identity_provider,
            config_root=config_root,
        )
    except ProcedureVerbError as exc:
        print(f"loadout-merge-procedure: {exc}", file=sys.stderr)
        return exc.code
    except MergeUsageError as exc:
        print(f"loadout-merge-procedure: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except CallerBindingError as exc:
        print(f"loadout-merge-procedure: {exc}", file=sys.stderr)
        return EXIT_CALLER_INVOKER_MISMATCH


if __name__ == "__main__":
    sys.exit(main())
