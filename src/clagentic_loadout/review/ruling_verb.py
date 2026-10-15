"""review.ruling_verb — `loadout-ruling`: record and list per-PR rulings.

A ruling is a decision, recorded once for one pull request, that a specific kept
finding does not block that pull request (review.rulings). The evaluator and the
merge gate read it from the ruling registry (review.ruling_provider), a file the
pull request under review cannot write. This verb is the only writer.

  loadout-ruling [--role ROLE] record --repo OWNER/REPO --pr N --findings FILE \\
      --fingerprint FP --rule-id RULE --path PATH --kind KIND --rationale TEXT \\
      --requested-by CALLER --source-ref REF [--expires DATE] [--reference ITEM]
  loadout-ruling list [--repo OWNER/REPO] [--pr N]

WHO MAY RECORD. The attested caller's role must hold the ``ruling`` verb. No
caller-controlled input decides that (see review.ruling_authority): the authority
comes only from an injected AuthorityProvider, else from the effective role-to-verb
mapping, which is the ``roles:`` mapping in the trusted user-level config file when
present (it replaces the default), else the built-in default, which grants
``ruling`` to ``merger``. There is no ``--authorized-role`` flag, and no
repository, checkout or environment variable contributes. ``--repo-path`` scopes
the ruling (a registry inside it is refused) and supplies no role policy.
Recording a ruling that clears a non-floor finding hands a merge-capable role no
power it lacks, which is why the built-in default names that role. Which agent
holds the role is the deployment's concern, never this package's.
``--requested-by`` records who asked, as the deployment names callers.

NON-FLOOR ONLY. The finding the ruling is for must appear in ``--findings`` with
the given fingerprint, rule and path, and must not be a security-floor finding:
a floor finding clears only through a dispositions entry on the base branch, or
by being fixed. A finding that cannot be found, or whose floor status cannot be
established, is refused. The evaluator applies the same rule when it reads the
registry, so this check is early notice, not the only barrier.

Exit codes (the reserved range; no child-process code is passed through):

  0   ok
  1   usage error
  3   the rulings configuration is invalid
  7   --role does not match the attested invoking identity
  21  the attested role holds no ruling authority
  32  the findings file is unreadable, or is not for this repository and pull
      request (a file that names no pull request cannot be checked: refused)
  50  the finding is a security-floor finding
  51  no finding in the findings file matches the fingerprint, rule and path
  52  the registry location, or the user-level file holding the role mapping, is
      one the pull request could write
  53  no ruling registry is configured
  54  an unexpired ruling already covers the finding
  55  the registry could not be written (including: it would exceed the size
      its readers accept)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clagentic_loadout._version import get_version
from clagentic_loadout.merge.authority import (
    AuthorityProvider,
    StaticRoleAuthorityProvider,
    check_authority,
)
from clagentic_loadout.merge.errors import AuthorityDeniedError
from clagentic_loadout.provisioning.roles import InvalidRoleConfigError
from clagentic_loadout.review.dispositions import KINDS
from clagentic_loadout.review.ruling_authority import (
    AUTHORIZING_VERB,
    UntrustedRolesSourceError,
    ruling_authorized_roles,
)
from clagentic_loadout.review.evaluate import is_floor_finding
from clagentic_loadout.review.evaluate_verb import EvaluateError, load_findings_file
from clagentic_loadout.review.ruling_provider import (
    FileRulingProvider,
    RulingsConfigError,
    load_rulings_config,
)
from clagentic_loadout.review.ruling_store import (
    DuplicateRulingError,
    RulingStoreError,
    UntrustedRegistryError,
    record_ruling,
)
from clagentic_loadout.review.rulings import (
    RulingFormatError,
    build_ruling,
    compile_reference_pattern,
    same_repo,
)
from clagentic_loadout.transport.attestation import AttestationError
from clagentic_loadout.transport.attestation import resolve_bound_identity as _resolve_identity
from clagentic_loadout.transport.caller_binding import (
    CallerBindingError,
    bind_caller,
    describe_omitted_caller_behavior,
    resolve_for_binding,
)
from clagentic_loadout.transport.credential_provider import DEFAULT_ROLE

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_CONFIG_INVALID = 3
EXIT_CALLER_INVOKER_MISMATCH = 7
EXIT_AUTHORITY_DENIED = 21
EXIT_FINDINGS_INVALID = 32
EXIT_FLOOR_REFUSED = 50
EXIT_FINDING_NOT_FOUND = 51
EXIT_REGISTRY_UNTRUSTED = 52
EXIT_REGISTRY_NOT_CONFIGURED = 53
EXIT_DUPLICATE = 54
EXIT_REGISTRY_FAILED = 55



class RulingVerbError(Exception):
    """A failure that ends the verb with a specific exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _build_arg_parser() -> argparse.ArgumentParser:
    role_help = (
        "Role whose attested identity records the ruling. It must match this "
        "process's own attested invoking identity or the call is refused before "
        f"any other work, and it must be a role that holds {AUTHORIZING_VERB!r} authority. "
        f"{describe_omitted_caller_behavior(flag_name='--role')}"
    )
    parser = argparse.ArgumentParser(
        prog="loadout-ruling",
        description=(
            "loadout-ruling -- record and list per-PR rulings. A ruling says a specific "
            "kept, non-floor finding does not block one pull request; the evaluator and "
            "the merge gate read it from a registry the pull request cannot write. "
            "`record` is for a role the deployment lets merge; a security-floor finding "
            "is refused."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  loadout-ruling record --repo some-owner/some-repo --pr 42 \\\n"
            "    --findings /path/to/findings.json --fingerprint fp2:0123456789abcdef \\\n"
            "    --rule-id some-rule --path src/handler.py --kind accepted_risk \\\n"
            "    --rationale 'why this is acceptable' --requested-by some-caller \\\n"
            "    --source-ref 'design note 4'\n"
            "  loadout-ruling list --repo some-owner/some-repo --pr 42\n"
            "\n"
            "Exit codes: 0 ok, 1 usage, 3 configuration, 7 identity mismatch, 21 not "
            "authorized, 32 findings file, 50 floor finding, 51 finding not found, 52 "
            "untrusted registry, 53 no registry, 54 duplicate, 55 write failed."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"loadout-ruling {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    parser.add_argument("--role", default=None, help=role_help)
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser(
        "record",
        help="Record a ruling for one finding at one pull request.",
        description="Record a ruling in the ruling registry.",
    )
    record.add_argument("--role", default=argparse.SUPPRESS, help=role_help)
    record.add_argument("--repo", required=True, help="owner/repo of the pull request.")
    record.add_argument("--pr", required=True, type=int, help="Pull request number.")
    record.add_argument(
        "--findings",
        required=True,
        help="Findings file holding the finding being ruled on (a `loadout-review run` "
        "output). The finding's facts decide whether it is a security-floor finding.",
    )
    record.add_argument("--fingerprint", required=True, help="The finding's fingerprint.")
    record.add_argument("--rule-id", required=True, help="The finding's rule id.")
    record.add_argument("--path", required=True, help="The finding's file path.")
    record.add_argument("--kind", required=True, choices=KINDS, help="The kind of ruling.")
    record.add_argument("--rationale", required=True, help="Why the finding does not block.")
    record.add_argument(
        "--requested-by",
        required=True,
        help="Who asked for the ruling, as the deployment names callers.",
    )
    record.add_argument(
        "--source-ref",
        required=True,
        help="Opaque reference to where the decision was made.",
    )
    record.add_argument(
        "--expires",
        default=None,
        help="Date (YYYY-MM-DD) after which the ruling no longer applies. An accepted_risk "
        "ruling without one expires 30 days after it is recorded.",
    )
    record.add_argument(
        "--reference",
        default=None,
        help="Work-item reference for a deferral to follow-up work (needs --expires). "
        "Opaque; it must match the configured reference pattern.",
    )
    record.add_argument(
        "--repo-path",
        default=None,
        help="Checkout of the reviewed repository. A ruling registry inside it is refused. "
        "It supplies no role policy.",
    )
    record.add_argument(
        "--head",
        default=None,
        help="Head commit the findings file must be for, when the file records one.",
    )

    listing = sub.add_parser(
        "list",
        help="List the rulings in the registry.",
        description="Print the rulings in the registry as JSON; read-only.",
    )
    listing.add_argument("--repo", default=None, help="Only rulings for this owner/repo.")
    listing.add_argument("--pr", default=None, type=int, help="Only rulings for this pull request.")
    return parser


def _split_repo(value: str) -> tuple[str, str]:
    parts = value.strip().split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise RulingVerbError(f"--repo must be in 'owner/repo' format, got: {value!r}", EXIT_USAGE)
    return parts[0], parts[1]


def _registry(config_root: str | Path | None):
    try:
        config = load_rulings_config(config_root)
    except RulingsConfigError as exc:
        raise RulingVerbError(f"rulings configuration is invalid: {exc}", EXIT_CONFIG_INVALID) from exc
    return config


def _matching_findings(
    findings: list[dict[str, Any]], *, fingerprint: str, rule_id: str, path: str
) -> list[dict[str, Any]]:
    wanted = path[2:] if path.startswith("./") else path
    return [
        f
        for f in findings
        if f.get("fingerprint") == fingerprint
        and f.get("rule_id") == rule_id
        and (f["file"][2:] if f["file"].startswith("./") else f["file"]) == wanted
    ]


def _check_findings_document(
    path: str, owner: str, repo: str, pr: int, head: str | None
) -> None:
    """The findings file must name this repository and pull request (and this
    head, when one was requested and the file records one). A shape that names
    nothing, such as a bare array, cannot be checked and is refused."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RulingVerbError(f"cannot read findings file {path!r}: {exc}", EXIT_FINDINGS_INVALID) from exc
    if not isinstance(data, dict):
        raise RulingVerbError(
            f"findings file {path!r} does not name the repository and pull request it is for "
            f"(it is not an object), so it cannot be shown to be for {owner}/{repo}#{pr}; "
            "use a `loadout-review run` output",
            EXIT_FINDINGS_INVALID,
        )
    named = f"{data.get('owner')}/{data.get('repo')}"
    named_pr = data.get("pr_number")
    pr_matches = isinstance(named_pr, int) and not isinstance(named_pr, bool) and named_pr == pr
    if named.lower() != f"{owner}/{repo}".lower() or not pr_matches:
        raise RulingVerbError(
            f"findings file {path!r} is for {named}#{named_pr}, but "
            f"{owner}/{repo}#{pr} was requested",
            EXIT_FINDINGS_INVALID,
        )
    recorded_head = data.get("head_sha")
    if head is not None and recorded_head is not None and str(recorded_head).lower() != head.lower():
        raise RulingVerbError(
            f"findings file {path!r} is for head {recorded_head!r}, but head {head!r} was requested",
            EXIT_FINDINGS_INVALID,
        )


def _establish_non_floor(args: argparse.Namespace, owner: str, repo: str) -> None:
    """Refuse unless the finding is in the findings file and is not a floor finding."""
    _check_findings_document(args.findings, owner, repo, args.pr, args.head)
    try:
        _, findings, _ = load_findings_file(args.findings)
    except EvaluateError as exc:
        raise RulingVerbError(str(exc), EXIT_FINDINGS_INVALID) from exc
    matched = _matching_findings(
        findings, fingerprint=args.fingerprint, rule_id=args.rule_id, path=args.path
    )
    if not matched:
        raise RulingVerbError(
            f"no finding in {args.findings!r} has fingerprint {args.fingerprint!r}, rule "
            f"{args.rule_id!r} and path {args.path!r}, so the ruling cannot be shown to be "
            "for a non-floor finding",
            EXIT_FINDING_NOT_FOUND,
        )
    floor = [f for f in matched if is_floor_finding(f)]
    if floor:
        raise RulingVerbError(
            f"{floor[0]['file']}:{floor[0]['line']}:{floor[0]['rule_id']} is a security-floor "
            "finding. A ruling is never applied to one: it clears only through a dispositions "
            "entry on the base branch, or by fixing it",
            EXIT_FLOOR_REFUSED,
        )


def _authorize(
    role: str,
    owner: str,
    repo: str,
    pr: int,
    authority_provider: AuthorityProvider | None,
    config_root: str | Path | None,
) -> None:
    """No caller-controlled input decides this: an injected provider, else the
    holders of the ruling verb in the effective role-to-verb mapping (the
    trusted user-level ``roles:``, else the built-in default)."""
    try:
        provider = authority_provider or StaticRoleAuthorityProvider(
            ruling_authorized_roles(config_root)
        )
    except InvalidRoleConfigError as exc:
        raise RulingVerbError(f"role configuration is invalid: {exc}", EXIT_CONFIG_INVALID) from exc
    except UntrustedRolesSourceError as exc:
        raise RulingVerbError(str(exc), EXIT_REGISTRY_UNTRUSTED) from exc
    try:
        check_authority(role, owner, repo, pr, provider)
    except AuthorityDeniedError as exc:
        raise RulingVerbError(
            f"role {role!r} may not record a ruling: it holds no {AUTHORIZING_VERB!r} authority "
            f"({exc})",
            EXIT_AUTHORITY_DENIED,
        ) from exc


def _record(
    args: argparse.Namespace,
    *,
    identity_provider,
    authority_provider: AuthorityProvider | None,
    config_root: str | Path | None,
    today: dt.date,
) -> int:
    owner, repo = _split_repo(args.repo)
    if args.pr < 1:
        raise RulingVerbError(f"--pr must be a positive integer, got: {args.pr!r}", EXIT_USAGE)

    resolve_identity = identity_provider if identity_provider is not None else _resolve_identity
    try:
        attested = resolve_for_binding(
            caller_explicit=args.role is not None,
            caller=args.role or DEFAULT_ROLE,
            resolve_identity_fn=resolve_identity,
        )
    except AttestationError as exc:
        raise RulingVerbError(
            f"attested-identity resolution FAILED -- {exc}", EXIT_CALLER_INVOKER_MISMATCH
        ) from exc
    role = args.role if args.role is not None else attested.subject
    bind_caller(role, caller_explicit=True, identity=attested)

    _authorize(role, owner, repo, args.pr, authority_provider, config_root)

    config = _registry(config_root)
    if config.path is None:
        raise RulingVerbError(
            "no ruling registry is configured: set rulings.path in the user-level "
            "configuration or CLAGENTIC_LOADOUT_RULINGS_FILE",
            EXIT_REGISTRY_NOT_CONFIGURED,
        )
    _establish_non_floor(args, owner, repo)

    try:
        pattern = compile_reference_pattern(config.reference_pattern)
        ruling = build_ruling(
            repo=f"{owner}/{repo}",
            pr=args.pr,
            fingerprint=args.fingerprint,
            rule_id=args.rule_id,
            path=args.path,
            kind=args.kind,
            rationale=args.rationale,
            by=role,
            requested_by=args.requested_by,
            source_ref=args.source_ref,
            at=today,
            expires=args.expires,
            reference=args.reference,
            reference_pattern=pattern,
        )
    except RulingFormatError as exc:
        raise RulingVerbError(str(exc), EXIT_USAGE) from exc

    # The same location check the readers apply, passed the reviewed repository
    # when the caller names it; the unconditional checks run either way.
    untrusted = (Path(args.repo_path),) if args.repo_path else ()
    try:
        count = record_ruling(
            config.path,
            ruling,
            today=today,
            reference_pattern=config.reference_pattern,
            untrusted_roots=untrusted,
        )
    except UntrustedRegistryError as exc:
        raise RulingVerbError(str(exc), EXIT_REGISTRY_UNTRUSTED) from exc
    except DuplicateRulingError as exc:
        raise RulingVerbError(str(exc), EXIT_DUPLICATE) from exc
    except RulingStoreError as exc:
        raise RulingVerbError(str(exc), EXIT_REGISTRY_FAILED) from exc

    print(
        json.dumps(
            {
                "result": "recorded",
                "registry": str(config.path),
                "rulings_stored": count,
                "ruling": ruling.as_dict(),
            }
        )
    )
    return EXIT_OK


def _list(args: argparse.Namespace, *, config_root: str | Path | None, today: dt.date) -> int:
    config = _registry(config_root)
    if config.path is None:
        print(json.dumps({"registry": None, "rulings": [], "degraded": []}))
        return EXIT_OK
    read = FileRulingProvider(config.path, reference_pattern=config.reference_pattern).read_all()
    selected = [
        r
        for r in read.rulings
        if (args.repo is None or same_repo(r.repo, args.repo)) and (args.pr is None or r.pr == args.pr)
    ]
    print(
        json.dumps(
            {
                "registry": str(config.path),
                "rulings": [{**r.as_dict(), "expired": r.is_expired(today)} for r in selected],
                "degraded": list(read.degraded),
            }
        )
    )
    return EXIT_OK


def main(
    argv: list[str] | None = None,
    *,
    token_provider=None,
    opener=None,
    identity_provider: Callable[[], Any] | None = None,
    authority_provider: AuthorityProvider | None = None,
    config_root: str | Path | None = None,
    today: dt.date | None = None,
) -> int:
    """CLI entrypoint. Returns the process exit code.

    ``token_provider`` and ``opener`` are accepted so the verb can be driven
    like every other caller-bound verb; it mints no credential and makes no
    network call, so neither is used. ``identity_provider`` and
    ``authority_provider`` are the injection points for the attested identity
    and the merge-authority seam; ``config_root`` and ``today`` are for tests
    and embedding callers."""
    del token_provider, opener
    if argv is None:
        argv = sys.argv[1:]

    parser = _build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE

    day = today if today is not None else dt.date.today()
    try:
        if args.command == "record":
            return _record(
                args,
                identity_provider=identity_provider,
                authority_provider=authority_provider,
                config_root=config_root,
                today=day,
            )
        return _list(args, config_root=config_root, today=day)
    except RulingVerbError as exc:
        print(f"loadout-ruling: {exc}", file=sys.stderr)
        return exc.code
    except CallerBindingError as exc:
        print(f"loadout-ruling: {exc}", file=sys.stderr)
        return EXIT_CALLER_INVOKER_MISMATCH


if __name__ == "__main__":
    sys.exit(main())
