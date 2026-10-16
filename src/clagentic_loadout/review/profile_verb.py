"""review.profile_verb — `loadout-profile`: validate a repo profile, or draft one.

  loadout-profile check [--repo-path DIR] [--ref REV]
  loadout-profile init  [--repo-path DIR] [--ref REV] [--force] [--stdout]

The profile is ``.clagentic/loadout/profile.yaml`` (see review.repo_profile).
``check`` reads it from the working tree, or from revision REV when given (the
way the review reads it from the base revision), and reports the class, the
rules that class carries and the per-path overrides. ``init`` drafts a profile
for a repository that has none from the file list of a revision (default HEAD)
and writes it for a person to review; it never overwrites a file without
``--force``. Both are local and read-only apart from ``init``'s one write; they
need no credential and bind no caller.

Exit codes (the reserved range for this verb; git's own exit codes are never
passed through):

  0   valid (check), or written / printed (init)
  1   usage error
  60  check: there is no profile
  61  check: the profile is invalid
  62  the repository, the revision or the file could not be read or written
  63  init: the profile already exists (use --force to replace it)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from clagentic_loadout._version import get_version
from clagentic_loadout.review import repo_profile as rp
from clagentic_loadout.review.atomic_io import write_bytes_atomic
from clagentic_loadout.review.policy_reader import (
    GitPolicyReader,
    PolicyReader,
    PolicyReadError,
    decode_policy_bytes,
)
from clagentic_loadout.review.policy_source import (
    PROFILE_MISSING,
    PROFILE_OK,
    PROFILE_PATH,
    read_repo_profile,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PROFILE_MISSING = 60
EXIT_PROFILE_INVALID = 61
EXIT_IO_FAILED = 62
EXIT_EXISTS = 63

_WORKING_TREE = "working tree"


class ProfileVerbError(Exception):
    """A failure that ends the verb with a specific exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


class WorkingTreeReader:
    """The policy-reader seam over a checked-out directory, for the one file the
    profile lives in. It lets ``check`` use the same loader the review uses."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def list_paths(self, base: str) -> list[str]:
        return [PROFILE_PATH] if (self._root / PROFILE_PATH).is_file() else []

    def read_text(self, base: str, path: str) -> str:
        try:
            raw = (self._root / path).read_bytes()
        except OSError as exc:
            raise PolicyReadError(f"cannot read {path} in the {_WORKING_TREE}: {exc}") from exc
        return decode_policy_bytes(raw, path, _WORKING_TREE)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadout-profile",
        description=(
            "loadout-profile -- validate a repository's profile, or draft one. The profile "
            f"({PROFILE_PATH}) states the shape the repository must keep: its class, "
            "released-tool or internal, and optional per-path overrides. Review reads it from "
            "the base revision and reports a missing or invalid one as a finding."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  loadout-profile check --repo-path .\n"
            "  loadout-profile init --repo-path . --stdout\n"
            "\n"
            "Exit codes: 0 ok, 1 usage, 60 no profile, 61 invalid profile, 62 repository or "
            "file unreadable or unwritable, 63 profile already exists."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"loadout-profile {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--repo-path",
            default=".",
            help="Repository root (default: the current directory).",
        )
        command.add_argument(
            "--ref",
            default=None,
            help="Revision to read instead of the default: for check, the working tree; for "
            "init, HEAD.",
        )

    check = sub.add_parser(
        "check",
        help="Validate the repo profile and print what it declares.",
        description="Validate the repo profile and print its class, the rules the class "
        "carries, and its overrides, as one JSON object.",
    )
    add_common(check)

    init = sub.add_parser(
        "init",
        help="Draft a repo profile from the file tree of a repository that has none.",
        description="Draft a repo profile from the repository's file list and write it for "
        "review. The draft says what it was inferred from. An existing profile is never "
        "overwritten without --force.",
    )
    add_common(init)
    init.add_argument(
        "--force", action="store_true", help="Replace an existing profile with the draft."
    )
    init.add_argument(
        "--stdout", action="store_true", help="Print the draft and write nothing."
    )
    return parser


def _reader(args: argparse.Namespace) -> tuple[PolicyReader, str]:
    root = Path(args.repo_path)
    if args.ref is not None:
        return GitPolicyReader(root), args.ref
    return WorkingTreeReader(root), _WORKING_TREE


def _describe(profile: rp.RepoProfile) -> dict[str, object]:
    return {
        "class": profile.repo_class,
        "rules": list(rp.CLASS_RULES[profile.repo_class]),
        "paths": [{"glob": glob, "class": value} for glob, value in profile.paths],
        "deny": {kind: len(getattr(profile.deny, kind)) for kind in rp.DENY_KINDS},
        "tool_packages": list(profile.tool_packages),
    }


def _check(args: argparse.Namespace) -> int:
    reader, base = _reader(args)
    try:
        present = set(reader.list_paths(base))
    except PolicyReadError as exc:
        raise ProfileVerbError(f"cannot list {args.repo_path!r} at {base}: {exc}", EXIT_IO_FAILED) from exc
    profile, state, problem = read_repo_profile(reader, base, present)
    if state == PROFILE_OK and profile is not None:
        print(json.dumps({"result": "valid", "path": PROFILE_PATH, "at": base, **_describe(profile)}))
        return EXIT_OK
    if state == PROFILE_MISSING:
        print(json.dumps({"result": "missing", "path": PROFILE_PATH, "at": base}))
        print(
            f"loadout-profile: no profile at {PROFILE_PATH} in {args.repo_path!r} ({base}); "
            "run `loadout-profile init` to draft one.",
            file=sys.stderr,
        )
        return EXIT_PROFILE_MISSING
    print(json.dumps({"result": "invalid", "path": PROFILE_PATH, "at": base, "problem": problem}))
    print(f"loadout-profile: {PROFILE_PATH} in {args.repo_path!r} is invalid: {problem}", file=sys.stderr)
    return EXIT_PROFILE_INVALID


def _init(args: argparse.Namespace) -> int:
    ref = args.ref if args.ref is not None else "HEAD"
    try:
        paths = GitPolicyReader(Path(args.repo_path)).list_paths(ref)
    except PolicyReadError as exc:
        raise ProfileVerbError(
            f"cannot list the files of {args.repo_path!r} at {ref}: {exc}", EXIT_IO_FAILED
        ) from exc
    text = rp.render_profile_yaml(rp.infer_profile(paths))
    if args.stdout:
        print(text, end="")
        return EXIT_OK
    target = Path(args.repo_path) / PROFILE_PATH
    if target.exists() and not args.force:
        raise ProfileVerbError(
            f"{str(target)!r} already exists; review it, or pass --force to replace it", EXIT_EXISTS
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(target, text.encode("utf-8"))
    except OSError as exc:
        raise ProfileVerbError(f"cannot write {str(target)!r}: {exc}", EXIT_IO_FAILED) from exc
    print(json.dumps({"result": "written", "path": str(target), "inferred_from": ref}))
    print(
        f"loadout-profile: wrote a draft to {str(target)!r}; review it, then commit it to the "
        "base branch (the review reads it from the base revision).",
        file=sys.stderr,
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
        if args.ref is not None and args.ref.startswith("-"):
            raise ProfileVerbError(f"--ref must be a revision, got {args.ref!r}", EXIT_USAGE)
        return _check(args) if args.command == "check" else _init(args)
    except ProfileVerbError as exc:
        print(f"loadout-profile: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
