"""review.profile_verb — `loadout-profile`: validate a repo profile, or draft one.

  loadout-profile check [--repo-path DIR] [--ref REV]
  loadout-profile init  [--repo-path DIR] [--ref REV] [--force] [--stdout]
  loadout-profile brief --role ROLE [--repo-path DIR] [--ref REV] [--repo NAME] [--groups-file FILE]

``brief`` prints the profile shaped for the role that will read it: for a role of
the scope class (a planner) the class and group rules as acceptance-criteria
candidates, for a build role (a builder) what the build envelope carries, and for
a gate role only the statement that a gate reads policy from the merge base by
its own path. Risk posture is never part of a brief. The role is any word of the
role vocabulary (role_vocabulary), a legacy alias, or a role the deployment's
``role_classes`` names.

The profile is ``.clagentic/loadout/profile.yaml`` (see review.repo_profile).
``check`` reads it from the working tree, or from revision REV when given (the
way the review reads it from the base revision), and reports the class, the
rules that class carries, the per-path overrides and the completeness items:
posture keys that are missing, unrecognised or unconfirmed (review.profile_posture),
and a retired stakes file still present. Completeness items are reporting only. ``init`` drafts a profile
for a repository that has none from the file list of a revision (default HEAD)
and writes it for a person to review; it never overwrites a file without
``--force``. Both are local and read-only apart from ``init``'s one write; they
need no credential and bind no caller.

Exit codes (the reserved range for this verb; git's own exit codes are never
passed through):

  0   valid (check), or written / printed (init), or printed (brief)
  1   usage error, including a role no vocabulary knows (brief)
  60  check, brief: there is no profile
  61  check, brief: the profile is invalid
  62  the repository, the revision, the file or the groups file could not be read or written
  63  init: the profile already exists (use --force to replace it)
"""

from __future__ import annotations

import argparse
import datetime as dt
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
    STAKES_PATH,
    profile_completeness,
    read_repo_profile,
)
from clagentic_loadout.review.profile_role_brief import role_brief
from clagentic_loadout.review.profile_rules import classes_in_effect, rule_statements
from clagentic_loadout.role_vocabulary import (
    CLASS_GATE,
    CLASS_SCOPE,
    RoleVocabularyError,
    load_role_classes,
    resolve_role,
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
        return [path for path in (PROFILE_PATH, STAKES_PATH) if (self._root / path).is_file()]

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
        "carries, its overrides, and what is missing from its risk posture (completeness "
        "items; reporting only, they never change the exit code), as one JSON object.",
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

    brief = sub.add_parser(
        "brief",
        help="Print the repo profile shaped for a role.",
        description="Print the repo profile shaped for the role that will read it, as one JSON "
        "object. A scope role gets acceptance-criteria candidates, a build role gets the "
        "envelope, a gate role gets the statement that its read is unchanged. Risk posture is "
        "never printed.",
    )
    add_common(brief)
    brief.add_argument("--role", required=True, help="The role (or legacy role word) to shape for.")
    brief.add_argument(
        "--repo",
        default=None,
        help="Repository name for group rules (default: the name of the origin of --repo-path).",
    )
    brief.add_argument(
        "--groups-file",
        default=None,
        help="The groups file for group rules; see `loadout-rules --help` for the other sources.",
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
        "rules": rule_statements(profile, profile.repo_class),
        "class_rules": {
            name: rule_statements(profile, name) for name in classes_in_effect(profile)
        },
        "paths": [{"glob": glob, "class": value} for glob, value in profile.paths],
        "deny": {kind: len(getattr(profile.deny, kind)) for kind in rp.DENY_KINDS},
        "tool_packages": list(profile.tool_packages),
    }


def _check(args: argparse.Namespace, now: dt.datetime | None = None) -> int:
    reader, base = _reader(args)
    try:
        present = set(reader.list_paths(base))
    except PolicyReadError as exc:
        raise ProfileVerbError(f"cannot list {args.repo_path!r} at {base}: {exc}", EXIT_IO_FAILED) from exc
    profile, state, problem = read_repo_profile(reader, base, present)
    _, items = profile_completeness(
        profile, state, problem, stakes_present=STAKES_PATH in present, now=now
    )
    completeness = [item.as_dict() for item in items]
    for item in items:
        print(f"loadout-profile: incomplete profile: {item.render()}", file=sys.stderr)
    if state == PROFILE_OK and profile is not None:
        print(
            json.dumps(
                {
                    "result": "valid",
                    "path": PROFILE_PATH,
                    "at": base,
                    **_describe(profile),
                    "completeness": completeness,
                }
            )
        )
        return EXIT_OK
    if state == PROFILE_MISSING:
        print(json.dumps({"result": "missing", "path": PROFILE_PATH, "at": base, "completeness": completeness}))
        print(
            f"loadout-profile: no profile at {PROFILE_PATH} in {args.repo_path!r} ({base}); "
            "run `loadout-profile init` to draft one.",
            file=sys.stderr,
        )
        return EXIT_PROFILE_MISSING
    print(
        json.dumps(
            {
                "result": "invalid",
                "path": PROFILE_PATH,
                "at": base,
                "problem": problem,
                "completeness": completeness,
            }
        )
    )
    print(f"loadout-profile: {PROFILE_PATH} in {args.repo_path!r} is invalid: {problem}", file=sys.stderr)
    return EXIT_PROFILE_INVALID


def _group_rules(
    args: argparse.Namespace, profile: rp.RepoProfile, config_root: str | Path | None
) -> tuple[list[dict[str, object]], str | None]:
    """``(rules, note)``: the group rules that apply to the repository, and a
    line saying why there are none when they could not be resolved."""
    # Imported here: rules.verb reads profiles through review.profile_brief,
    # which imports this module.
    from clagentic_loadout.git_origin import OriginBindingError, read_origin_target
    from clagentic_loadout.rules import groups as gr
    from clagentic_loadout.rules.verb import build_result

    name = gr.bare_repo_name(args.repo) if args.repo else ""
    if not name:
        try:
            name = read_origin_target(args.repo_path).name
        except OriginBindingError:
            return [], "group rules not resolved: no --repo and no readable origin to name it"
    location = gr.resolve_groups_location(args.groups_file, config_root=config_root)
    try:
        groups = gr.load_groups_file(location.path) if location.path else None
    except gr.GroupsFileError as exc:
        raise ProfileVerbError(str(exc), EXIT_IO_FAILED) from exc
    result = build_result(
        name, location, groups, args.repo_path, known_class=profile.repo_class
    )
    if result["result"] == "empty":
        return [], f"no group rules: {result['reason']}"
    rules = [rule for group in result["groups"] for rule in group["rules"]]
    return rules, None


def _brief(args: argparse.Namespace, config_root: str | Path | None) -> int:
    try:
        resolution = resolve_role(args.role, load_role_classes(config_root))
    except RoleVocabularyError as exc:
        raise ProfileVerbError(str(exc), EXIT_USAGE) from exc
    reader, base = _reader(args)
    try:
        present = set(reader.list_paths(base))
    except PolicyReadError as exc:
        if resolution.role_class == CLASS_GATE and args.ref is None:
            present = set()
        else:
            raise ProfileVerbError(
                f"cannot list {args.repo_path!r} at {base}: {exc}", EXIT_IO_FAILED
            ) from exc
    if resolution.role_class == CLASS_GATE:
        # A gate's read of the profile is its own, at the merge base. The tree
        # is listed above so a --ref that does not exist is refused rather than
        # reported as read; without --ref a gate brief needs no tree at all.
        print(json.dumps({"result": "brief", "at": base, **role_brief(None, resolution)}))
        return EXIT_OK
    profile, state, problem = read_repo_profile(reader, base, present)
    if state == PROFILE_MISSING:
        print(json.dumps({"result": "missing", "path": PROFILE_PATH, "at": base}))
        print(
            f"loadout-profile: no profile at {PROFILE_PATH} in {args.repo_path!r} ({base}); "
            "run `loadout-profile init` to draft one.",
            file=sys.stderr,
        )
        return EXIT_PROFILE_MISSING
    if state != PROFILE_OK or profile is None:
        print(json.dumps({"result": "invalid", "path": PROFILE_PATH, "at": base, "problem": problem}))
        print(
            f"loadout-profile: {PROFILE_PATH} in {args.repo_path!r} is invalid: {problem}",
            file=sys.stderr,
        )
        return EXIT_PROFILE_INVALID
    rules: list[dict[str, object]] = []
    note = None
    if resolution.role_class == CLASS_SCOPE:
        rules, note = _group_rules(args, profile, config_root)
    result = role_brief(profile, resolution, group_rules=rules, group_note=note)
    print(json.dumps({"result": "brief", "path": PROFILE_PATH, "at": base, **result}))
    return EXIT_OK


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


def main(
    argv: list[str] | None = None,
    *,
    config_root: str | Path | None = None,
    now: dt.datetime | None = None,
) -> int:
    """CLI entrypoint. Returns the process exit code. *config_root* and *now*
    (the clock the posture's confirmation age is measured against) are for
    tests and embedding callers."""
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
        if args.command == "brief":
            return _brief(args, config_root)
        return _check(args, now) if args.command == "check" else _init(args)
    except ProfileVerbError as exc:
        print(f"loadout-profile: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
