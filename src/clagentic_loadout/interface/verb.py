"""interface.verb -- `loadout-interface`: a repo's declared interface and its consumers.

  loadout-interface diff      --base REV --head REV [--repo-path DIR]
  loadout-interface consumers [--repo NAME] [--repo-path DIR] [--consumers-file FILE]
                              [--element NAME]... [--path GLOB]... [--base REV --head REV]

A producer repo declares its interface in ``.clagentic/loadout/interface.yaml``
(see interface.model for the shape).

`diff` reads that file at two revisions of the repository at --repo-path and
prints, as one JSON object, whether the change is breaking and the removed,
changed and added elements. Removing or renaming an element, or changing the
number an exit code meaning maps to, is breaking; adding one is not. A revision
with no interface file is reported in the result, not as an error.

`consumers` reads the consumer map a deployment keeps outside any repository and
prints who consumes the named elements of the repository NAME, each with the map
element it consumes and when the use was confirmed. The elements are named by
--element, by --path globs matched against the files the map lists, or by the
elements a --base/--head diff changed. The map location is --consumers-file, else
CLAGENTIC_LOADOUT_CONSUMERS_FILE, else the ``rules.consumers_file`` setting of the
user-level config file. With no map the result says so and the exit code is 0.

Both subcommands only report. The verb reads files, runs nothing against any
consumer, binds no caller and mints no credential.

Exit codes (the reserved range for this verb):

  0   ok, including a breaking change and an unavailable consumer map
  1   usage error
  90  a revision or the repository could not be read
  91  an interface file is not valid
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from clagentic_loadout._version import get_version
from clagentic_loadout.git_origin import OriginBindingError, read_origin_target
from clagentic_loadout.interface.consumers import (
    CONFIG_KEY_CONSUMERS_FILE,
    CONSUMERS_FILE_ENV_VAR,
    consumers_of,
    locate_consumers,
)
from clagentic_loadout.interface.diff import diff_documents, read_interface
from clagentic_loadout.interface.model import INTERFACE_PATH, InterfaceError
from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReadError
from clagentic_loadout.rules.groups import CONFIG_SECTION_RULES, bare_repo_name

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_UNREADABLE = 90
EXIT_INTERFACE_INVALID = 91


class InterfaceVerbError(Exception):
    """A failure that ends the verb with a specific exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadout-interface",
        description=(
            "loadout-interface -- a repo's declared interface. `diff` says whether the interface "
            f"declared in {INTERFACE_PATH} changed in a breaking way between two revisions, and "
            "`consumers` lists who consumes the named elements, from the consumer map. Reporting "
            "only: nothing here refuses anything."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  loadout-interface diff --base origin/main --head HEAD\n"
            "  loadout-interface consumers --repo some-repo --element some-verb\n"
            "  loadout-interface consumers --repo some-repo --base origin/main --head HEAD\n"
            "\n"
            f"The consumer map is --consumers-file, else {CONSUMERS_FILE_ENV_VAR}, else the "
            f"{CONFIG_SECTION_RULES}.{CONFIG_KEY_CONSUMERS_FILE} setting of the user-level config "
            "file; with none the result says the map is unavailable and the exit code is 0.\n"
            "Exit codes: 0 ok, 1 usage, 90 revision or repository unreadable, 91 interface file "
            "invalid."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"loadout-interface {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    diff = sub.add_parser(
        "diff",
        help="Say whether the declared interface changed in a breaking way between two revisions.",
        description="Read the interface file at two revisions and print, as JSON, whether the "
        "change is breaking and the removed, changed and added elements.",
    )
    diff.add_argument("--base", required=True, help="The earlier revision.")
    diff.add_argument("--head", required=True, help="The later revision.")
    diff.add_argument(
        "--repo-path",
        default=".",
        help="The repository holding both revisions (default: the current directory).",
    )

    consumers = sub.add_parser(
        "consumers",
        help="List who consumes the named elements of a repository.",
        description="Print the consumers the consumer map lists for the elements named by "
        "--element, --path or a --base/--head diff, each with the element it consumes and "
        "its confirmed_at date.",
    )
    consumers.add_argument(
        "--repo",
        default=None,
        help="The producer repository (a bare name or owner/name; default: the origin of --repo-path).",
    )
    consumers.add_argument(
        "--repo-path",
        default=".",
        help="The repository to name the producer from and to diff in (default: the current directory).",
    )
    consumers.add_argument(
        "--consumers-file",
        default=None,
        help="The consumer map; overrides the environment variable and the config setting.",
    )
    consumers.add_argument(
        "--element",
        action="append",
        default=[],
        metavar="NAME",
        help="An element name, such as a verb; may be repeated.",
    )
    consumers.add_argument(
        "--path",
        action="append",
        default=[],
        metavar="GLOB",
        help="A glob over the files the map lists elements under; may be repeated.",
    )
    consumers.add_argument("--base", default=None, help="With --head: take the elements this diff changed.")
    consumers.add_argument("--head", default=None, help="With --base: the later revision.")
    return parser


def _diff(args: argparse.Namespace) -> dict:
    reader = GitPolicyReader(args.repo_path)
    try:
        base = read_interface(reader, args.base)
        head = read_interface(reader, args.head)
    except PolicyReadError as exc:
        raise InterfaceVerbError(str(exc), EXIT_UNREADABLE) from exc
    except InterfaceError as exc:
        raise InterfaceVerbError(str(exc), EXIT_INTERFACE_INVALID) from exc
    return diff_documents(base, head).to_dict()


def _producer(args: argparse.Namespace) -> str:
    if args.repo:
        return bare_repo_name(args.repo)
    try:
        return read_origin_target(args.repo_path).name
    except OriginBindingError as exc:
        raise InterfaceVerbError(
            f"no --repo given and the origin of {args.repo_path!r} cannot be read: {exc}", EXIT_USAGE
        ) from exc


def _consumers(args: argparse.Namespace, config_root: str | Path | None) -> dict:
    if (args.base is None) != (args.head is None):
        raise InterfaceVerbError("--base and --head are given together", EXIT_USAGE)
    if not (args.element or args.path or args.base):
        raise InterfaceVerbError(
            "name what to look up: --element, --path, or --base with --head", EXIT_USAGE
        )
    producer = _producer(args)
    names = list(args.element)
    if args.base is not None:
        changed = _diff(argparse.Namespace(base=args.base, head=args.head, repo_path=args.repo_path))
        names.extend(c["name"] for kind in ("removed", "changed", "added") for c in changed[kind])
    status = locate_consumers(args.consumers_file, config_root=config_root)
    out: dict = {
        "producer": producer,
        "consumers_file": status.location.path,
        "consumers_file_from": status.location.origin,
    }
    if status.consumers is None:
        print(f"loadout-interface: the consumer map is unavailable: {status.unavailable}", file=sys.stderr)
        return {**out, "result": "unavailable", "reason": status.unavailable, "consumers": []}
    hits = consumers_of(status.consumers, producer, names=sorted(set(names)), paths=args.path)
    return {**out, "result": "consumers", "consumers": [h.to_dict() for h in hits]}


def main(argv: list[str] | None = None, *, config_root: str | Path | None = None) -> int:
    """CLI entrypoint. Returns the process exit code. *config_root* is for tests
    and embedding callers."""
    if argv is None:
        argv = sys.argv[1:]
    parser = _build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE
    try:
        result = _diff(args) if args.command == "diff" else _consumers(args, config_root)
    except InterfaceVerbError as exc:
        print(f"loadout-interface: {exc}", file=sys.stderr)
        return exc.code
    print(json.dumps(result))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
