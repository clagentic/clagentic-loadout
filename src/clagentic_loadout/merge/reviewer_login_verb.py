"""merge.reviewer_login_verb — `loadout-reviewer-login`: print the platform
login each named reviewer posts under.

A consumer that has to recognise a reviewer's comments (a churn counter, a
verdict reader, a dashboard) needs the same login `loadout-merge` binds a
reviewer's verdict to. Importing `merge.reviewer_login` couples that consumer
to this package's internals; this verb exposes the identical derivation
(`resolve_reviewer_login`) as a read-only CLI surface instead.

  loadout-reviewer-login --platform forgejo|github <name> [<name> ...]

stdout carries one login per name, in argument order, and nothing else. The
whole batch is resolved before anything is printed, so a failure never leaves
a partial list a script could mistake for a complete one.

Read-only and offline: it reads deployment config, mints no credential and
makes no network call, so it takes no `--caller` and has no identity to bind.

Exit codes:
  0  every name resolved
  1  usage error (bad flag, unrecognised platform, malformed name)
  8  a name has no login configured for the platform
"""

from __future__ import annotations

import argparse
import re
import sys

from clagentic_loadout._version import get_version
from clagentic_loadout.merge.reviewer_login import (
    ReviewerLoginNotConfiguredError,
    resolve_reviewer_login,
)
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_REVIEWER_NOT_CONFIGURED = 8

#: A bare reviewer name, never an explicit `name:login` override: the override
#: form carries a literal login and needs no derivation.
_NAME_RE = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadout-reviewer-login",
        description=(
            "loadout-reviewer-login -- print the login each named reviewer "
            "posts under on a platform, one per line in argument order. Uses "
            "the same derivation the merge gate binds a reviewer verdict to: "
            "on forgejo the bare name is the login; on github it is the "
            "reviewer's configured GitHub App slug plus '[bot]'. Read-only: "
            "reads deployment config, mints no credential, makes no network "
            "call."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  loadout-reviewer-login --platform github some-reviewer other-reviewer\n"
            "\n"
            "Exit codes: 0 resolved, 1 usage, 8 a name has no login "
            "configured for the platform."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"loadout-reviewer-login {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    parser.add_argument(
        "--platform",
        required=True,
        choices=(PLATFORM_FORGEJO, PLATFORM_GITHUB),
        help="Platform whose login convention applies.",
    )
    parser.add_argument(
        "names",
        nargs="+",
        metavar="name",
        help="Bare reviewer name (the identity key the deployment configures "
        "its login under).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns the process exit code."""
    if argv is None:
        argv = sys.argv[1:]

    parser = _build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code in (0, None):
            return EXIT_OK
        return EXIT_USAGE

    for name in args.names:
        if not _NAME_RE.match(name):
            print(
                f"loadout-reviewer-login: {name!r} is not a bare reviewer name "
                "(letters, digits, hyphen and underscore only; pass the name, "
                "not a name:login pair)",
                file=sys.stderr,
            )
            return EXIT_USAGE

    logins: list[str] = []
    for name in args.names:
        try:
            logins.append(resolve_reviewer_login(name, args.platform))
        except ReviewerLoginNotConfiguredError as exc:
            print(
                f"loadout-reviewer-login: platform={args.platform!r} "
                f"reviewer={name!r}: {exc}",
                file=sys.stderr,
            )
            return EXIT_REVIEWER_NOT_CONFIGURED

    print("\n".join(logins))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
