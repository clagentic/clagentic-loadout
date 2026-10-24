"""review.changeset_verb — `loadout-changeset`: classify a change as docs-only or code.

  loadout-changeset classify --base REV [--head REV] [--repo-path DIR]

``classify`` decides, deterministically, whether the change from the merge base
of REV and the head to the head touches documentation only. The docs allowlist
is read from the repo profile at the commit REV names (the way every gate reads
policy), never from the working tree and never from the head, so a change cannot
widen the list it is judged by: a change that edits the profile is code. A path
that is not on the base's allowlist, that is listed as depended on, or that falls
in a built-in code class (configuration, CI, hooks, scripts, executable or
symlink entries, the loadout configuration directory) is code, and so is a
change with no profile allowlist at all. The same classification is what
`loadout-review run` and `post` use (review.docs_only_review).

It prints one JSON object: the classification, the allowlist source (the base
commit, the profile path, and what reading the profile came to) and every path
with its class and the reason. Both answers exit 0: the exit code reports
whether the question could be answered, not what the answer is. It is local and
read-only, needs no credential and binds no caller.

Exit codes (the reserved range for this verb; git's own exit codes are never
passed through):

  0   classified (docs-only or code)
  1   usage error
  62  the repository, a revision or the changed paths could not be read
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from clagentic_loadout._version import get_version
from clagentic_loadout.review.docs_only_policy import ChangedPath, merge_changed
from clagentic_loadout.review.docs_only_review import classify_with_profile
from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReadError
from clagentic_loadout.review.policy_source import read_repo_profile

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_IO_FAILED = 62


class ChangesetVerbError(Exception):
    """A failure that ends the verb with a specific exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadout-changeset",
        description=(
            "loadout-changeset -- classify a change as docs-only or code from the changed paths "
            "and the docs allowlist in the repo profile at the base revision. Review takes a "
            "lighter path only for a change that classifies as docs-only."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  loadout-changeset classify --repo-path . --base origin/main\n"
            "\n"
            "Exit codes: 0 classified, 1 usage, 62 repository, revision or changed paths "
            "unreadable."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"loadout-changeset {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    classify = sub.add_parser(
        "classify",
        help="Classify the change from a base revision to a head as docs-only or code.",
        description="Classify a change and print the classification, the allowlist source and "
        "each path's class as one JSON object.",
    )
    classify.add_argument(
        "--repo-path", default=".", help="Repository root (default: the current directory)."
    )
    classify.add_argument(
        "--base",
        required=True,
        help="The revision the change merges into. The docs allowlist is read at this commit; "
        "the change is what the head adds since its merge base with it.",
    )
    classify.add_argument(
        "--head", default="HEAD", help="The revision holding the change (default: HEAD)."
    )
    return parser


def _classify(args: argparse.Namespace) -> int:
    reader = GitPolicyReader(Path(args.repo_path))
    try:
        base = reader.resolve_commit(args.base)
        head = reader.resolve_commit(args.head)
        present = set(reader.list_paths(base))
        changed = merge_changed(
            [ChangedPath(path, modes) for path, modes in reader.changed_paths(base, head)]
        )
    except PolicyReadError as exc:
        raise ChangesetVerbError(
            f"cannot classify {args.head} against {args.base} in {args.repo_path!r}: {exc}",
            EXIT_IO_FAILED,
        ) from exc
    profile, state, _problem = read_repo_profile(reader, base, present)
    classification = classify_with_profile(changed, profile, state, base)
    print(
        json.dumps(
            {
                "result": classification.kind,
                "base": base,
                "head": head,
                **classification.to_dict(),
            }
        )
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns the process exit code."""
    if argv is None:
        argv = sys.argv[1:]
    parser = _build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE
    try:
        for flag, value in (("--base", args.base), ("--head", args.head)):
            if value.startswith("-"):
                raise ChangesetVerbError(f"{flag} must be a revision, got {value!r}", EXIT_USAGE)
        return _classify(args)
    except ChangesetVerbError as exc:
        print(f"loadout-changeset: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
