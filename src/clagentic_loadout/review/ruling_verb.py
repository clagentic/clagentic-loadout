"""review.ruling_verb — `loadout-ruling`: record and list per-PR rulings.

A ruling is a decision, recorded once for one pull request, that a specific kept
finding does not block that pull request (review.rulings). The evaluator and the
merge gate read it from the ruling registry (review.ruling_provider), a file the
pull request under review cannot write. This verb is the only writer.

  loadout-ruling [--role ROLE] record --platform PLATFORM --repo OWNER/REPO --pr N \\
      --fence-comment ID --fingerprint FP --kind KIND --rationale TEXT \\
      --requested-by CALLER --source-ref REF [--expires DATE] [--reference ITEM] \\
      [--required-reviewer ROLE]... [--head SHA] [--repo-path DIR] [--git-host-base-url URL]
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

WHERE THE FINDING COMES FROM. ``--fence-comment`` is the id of the reviewer's
posted verdict comment on the pull request. The verb reads it through the host API
with the caller's own role credential and accepts it only under the conditions in
review.ruling_fence: posted by the login of a required reviewer role, a well-formed
fence for this pull request, at the pull request's current head (or ``--head``),
carrying the findings its verdict was computed from. The finding's rule, path and
facts are taken from that fence by ``--fingerprint``; nothing the caller types
describes the finding.

WHICH ROLES ARE REQUIRED. The same ones the merge enforces, resolved by
merge.required_reviewers: the roles the repository's tracked gate declares at the
pull request's base commit, plus the bare roles given as ``--required-reviewer``
(the merge's own flag, repeatable). With no tracked gate file the flags alone decide.
The flag widens only which comment counts as a required role's verdict. It cannot
widen who may record a ruling, it never removes a declared role, and it takes no
``name:login`` pair: a ruling is matched by fingerprint, rule and path, so a login
typed by the caller would let any account's comment supply the fingerprint.

NON-FLOOR ONLY. The finding must not be a security-floor finding: a floor
finding clears only through a dispositions entry on the base branch, or by being
fixed. A finding whose floor status cannot be established is refused. The
evaluator applies the same rule when it reads the registry, so this check is
early notice, not the only barrier.

Exit codes (the reserved range; no child-process code is passed through):

  0   ok
  1   usage error
  2   no credential could be resolved for the caller's role
  3   the rulings configuration is invalid
  4   --platform does not match the backend that can serve it
  7   --role does not match the attested invoking identity
  21  the attested role holds no ruling authority
  27  the host could not be read (pull request, comments or repository gate)
  32  the comment is not an acceptable fence: not found, not posted by a required
      reviewer's login, malformed, for another pull request, at another head, or
      carrying no evaluated findings; or no required reviewer can be established
  50  the finding is a security-floor finding
  51  the fence holds no finding with the fingerprint
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
from clagentic_loadout.merge.errors import (
    AuthorityDeniedError,
    GateFactUnavailableError,
    MergeUsageError,
    PlatformMismatchError,
)
from clagentic_loadout.merge.required_reviewers import parse_required_reviewers
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.provisioning.roles import InvalidRoleConfigError
from clagentic_loadout.review.dispositions import KINDS
from clagentic_loadout.review.evaluate import is_floor_finding
from clagentic_loadout.review.ruling_authority import (
    AUTHORIZING_VERB,
    UntrustedRolesSourceError,
    ruling_authorized_roles,
)
from clagentic_loadout.review.ruling_fence import (
    FenceFinding,
    FenceRefusedError,
    FindingNotInFenceError,
    read_fence_finding,
)
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
from clagentic_loadout.transport.credential_provider import (
    DEFAULT_ROLE,
    CredentialProviderError,
    TokenProvider,
)
from clagentic_loadout.transport.git_host_api import resolve_git_host_base

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_TOKEN_FETCH_FAILED = 2
EXIT_CONFIG_INVALID = 3
EXIT_WRONG_PLATFORM = 4
EXIT_CALLER_INVOKER_MISMATCH = 7
EXIT_AUTHORITY_DENIED = 21
EXIT_HOST_UNAVAILABLE = 27
EXIT_FENCE_INVALID = 32
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
            "  loadout-ruling record --platform forgejo --repo some-owner/some-repo --pr 42 \\\n"
            "    --fence-comment 1234 --fingerprint fp2:0123456789abcdef \\\n"
            "    --kind accepted_risk --rationale 'why this is acceptable' \\\n"
            "    --requested-by some-caller --source-ref 'design note 4'\n"
            "  loadout-ruling list --repo some-owner/some-repo --pr 42\n"
            "\n"
            "Exit codes: 0 ok, 1 usage, 2 no credential, 3 configuration, 4 wrong platform, "
            "7 identity mismatch, 21 not authorized, 27 host unreadable, 32 fence not "
            "acceptable, 50 floor finding, 51 finding not in the fence, 52 untrusted "
            "registry or role mapping, 53 no registry, 54 duplicate, 55 write failed."
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
    record.add_argument(
        "--platform",
        required=True,
        choices=(PLATFORM_GITHUB, PLATFORM_FORGEJO),
        help="Platform hosting the pull request.",
    )
    record.add_argument("--repo", required=True, help="owner/repo of the pull request.")
    record.add_argument("--pr", required=True, type=int, help="Pull request number.")
    record.add_argument(
        "--fence-comment",
        required=True,
        type=int,
        help="Id of the reviewer's verdict comment on the pull request. It must be posted "
        "by the login of a required reviewer role, be a verdict fence for this pull request "
        "at its current head (or --head), and carry the findings its verdict was computed "
        "from. The finding, its rule, path and facts are taken from it.",
    )
    record.add_argument(
        "--fingerprint",
        required=True,
        help="Fingerprint of the finding in that fence's evaluated findings.",
    )
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
        "--required-reviewer",
        action="append",
        dest="required_reviewers",
        default=None,
        help="A reviewer role the merge of this pull request requires (repeatable), as "
        "loadout-merge is given it. A BARE role name only: its login is derived from "
        "the role, and a 'name:login' pair is refused. Added to the roles the "
        "repository's tracked gate declares at the pull request's base commit; it "
        "never removes one. It decides only whose comment counts as that role's verdict, "
        "not who may record a ruling. Needed when the repository has no tracked gate "
        "file and the merge names its reviewers on its own command line.",
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
        help="Full SHA the fence must be for, instead of the pull request's current head.",
    )
    record.add_argument(
        "--git-host-base-url",
        default=None,
        help="Forgejo API base URL, resolved as the other verbs resolve it. Ignored for GitHub.",
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


def _fence_finding(
    args: argparse.Namespace,
    owner: str,
    repo: str,
    role: str,
    *,
    token_provider: TokenProvider | None,
    opener,
    config_root: str | Path | None,
    required_reviewers: dict[str, str],
) -> FenceFinding:
    """The finding the ruling is for, read from the posted fence, refused when it
    is a security-floor finding."""
    try:
        found = read_fence_finding(
            platform=args.platform,
            owner=owner,
            repo=repo,
            pr=args.pr,
            comment_id=args.fence_comment,
            fingerprint=args.fingerprint,
            head=args.head,
            role=role,
            token_provider=token_provider,
            git_host_base=resolve_git_host_base(
                args.git_host_base_url,
                config_root=str(config_root) if config_root is not None else None,
            ),
            opener=opener,
            required_reviewers=required_reviewers,
        )
    except MergeUsageError as exc:
        raise RulingVerbError(str(exc), EXIT_USAGE) from exc
    except PlatformMismatchError as exc:
        raise RulingVerbError(str(exc), EXIT_WRONG_PLATFORM) from exc
    except CredentialProviderError as exc:
        raise RulingVerbError(f"token resolution FAILED -- {exc}", EXIT_TOKEN_FETCH_FAILED) from exc
    except GateFactUnavailableError as exc:
        raise RulingVerbError(str(exc), EXIT_HOST_UNAVAILABLE) from exc
    except FindingNotInFenceError as exc:
        raise RulingVerbError(str(exc), EXIT_FINDING_NOT_FOUND) from exc
    except FenceRefusedError as exc:
        raise RulingVerbError(str(exc), EXIT_FENCE_INVALID) from exc
    floor = [f for f in found.matches if is_floor_finding(f)]
    if floor:
        raise RulingVerbError(
            f"{floor[0]['file']}:{floor[0]['line']}:{floor[0]['rule_id']} is a security-floor "
            "finding. A ruling is never applied to one: it clears only through a dispositions "
            "entry on the base branch, or by fixing it",
            EXIT_FLOOR_REFUSED,
        )
    return found


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
    token_provider: TokenProvider | None,
    opener,
    config_root: str | Path | None,
    today: dt.date,
) -> int:
    owner, repo = _split_repo(args.repo)
    if args.pr < 1:
        raise RulingVerbError(f"--pr must be a positive integer, got: {args.pr!r}", EXIT_USAGE)
    try:
        required_reviewers = parse_required_reviewers(
            args.required_reviewers, args.platform, bare_only=True
        )
    except MergeUsageError as exc:
        raise RulingVerbError(str(exc), EXIT_USAGE) from exc

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
    found = _fence_finding(
        args,
        owner,
        repo,
        role,
        token_provider=token_provider,
        opener=opener,
        config_root=config_root,
        required_reviewers=required_reviewers,
    )
    finding = found.matches[0]

    try:
        pattern = compile_reference_pattern(config.reference_pattern)
        ruling = build_ruling(
            repo=f"{owner}/{repo}",
            pr=args.pr,
            fingerprint=args.fingerprint,
            rule_id=finding["rule_id"],
            path=finding["file"],
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

    ``record`` reads the host through the caller's own role credential, so
    ``token_provider`` (default: the platform's configured provider) and
    ``opener`` (default: the hardened host opener) are the injection points for
    that read; ``list`` makes no network call. ``identity_provider`` and
    ``authority_provider`` are the injection points for the attested identity
    and the authority seam; ``config_root`` and ``today`` are for tests and
    embedding callers."""
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
                token_provider=token_provider,
                opener=opener,
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
