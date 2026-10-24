"""rules.verb -- `loadout-rules`: the group rules that apply to a repository.

  loadout-rules --repo NAME [--repo-path DIR] [--groups-file FILE]

Resolves the groups a deployment keeps outside any repository (rules.groups) for
the repository NAME and prints each group rule with its group and the file it
came from, as one JSON object. NAME is a bare repository name; an ``owner/name``
is accepted and matched on the name part.

A group is selected by a jurisdiction glob over bare repository names, or by a
class. A class-selected group applies when the repository's profile class (read
from the working tree at --repo-path when that tree is the named repository)
equals its class. With no profile the repository matches no class-selected group,
and the output says so. A class in the groups file that the profile schema does
not admit is printed with a note, never refused.

With no groups file configured, or none at the configured path, the result is
empty, says why, and the exit code is 0. The verb is read-only, binds no caller
and mints no credential.

Exit codes (the reserved range for this verb):

  0   ok, including the empty result
  1   usage error
  80  the groups file exists but cannot be read or is not in the documented shape
  81  the repo profile at --repo-path is invalid
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from clagentic_loadout._version import get_version
from clagentic_loadout.git_origin import OriginBindingError, read_origin_target
from clagentic_loadout.review.policy_source import PROFILE_INVALID, PROFILE_PATH
from clagentic_loadout.review.profile_brief import ProfileReadError, read_profile
from clagentic_loadout.rules import groups as gr

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_GROUPS_FILE = 80
EXIT_PROFILE_INVALID = 81

NO_PROFILE_NOTE = "no profile, no class-selected group"


class RulesVerbError(Exception):
    """A failure that ends the verb with a specific exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadout-rules",
        description=(
            "loadout-rules -- print the group rules that apply to a repository, each with its "
            "group and the groups file it came from. Groups are selected by a jurisdiction glob "
            "over bare repository names, or by the repository's profile class."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  loadout-rules --repo some-repo --groups-file groups.yaml\n"
            "\n"
            f"The groups file is --groups-file, else {gr.GROUPS_FILE_ENV_VAR}, else the "
            f"{gr.CONFIG_SECTION_RULES}.{gr.CONFIG_KEY_GROUPS_FILE} setting of the user-level "
            "config file; with none the result is empty and the exit code is 0.\n"
            "Exit codes: 0 ok, 1 usage, 80 groups file unreadable or malformed, 81 repo profile "
            "invalid."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"loadout-rules {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    parser.add_argument("--repo", required=True, help="Repository name (a bare name or owner/name).")
    parser.add_argument(
        "--repo-path",
        default=".",
        help="Working tree whose profile gives the repository's class, used only when it is "
        "the named repository (default: the current directory).",
    )
    parser.add_argument(
        "--groups-file",
        default=None,
        help="The groups file; overrides the environment variable and the config setting.",
    )
    return parser


def _tree_is_repo(repo_path: str, name: str) -> bool:
    """Whether the tree at *repo_path* is the repository *name*: by its origin
    when that reads, else by the directory name."""
    try:
        return read_origin_target(repo_path).name.lower() == name.lower()
    except OriginBindingError:
        return Path(repo_path).resolve().name.lower() == name.lower()


def _profile_class(repo_path: str, name: str) -> tuple[str | None, str]:
    """``(class or None, why)`` for the repository *name*."""
    if not _tree_is_repo(repo_path, name):
        return None, f"{repo_path!r} is not the repository {name!r}, so its profile is not read"
    try:
        profile, state, problem = read_profile(repo_path)
    except ProfileReadError as exc:
        return None, str(exc)
    if state == PROFILE_INVALID:
        raise RulesVerbError(
            f"the repo profile {PROFILE_PATH} in {repo_path!r} is invalid: {problem}",
            EXIT_PROFILE_INVALID,
        )
    if profile is None:
        return None, f"no repo profile at {PROFILE_PATH} in {repo_path!r}"
    return profile.repo_class, f"profile at {PROFILE_PATH} in {repo_path!r}"


def _empty(name: str, location: gr.GroupsLocation, reason: str) -> dict[str, Any]:
    return {
        "result": "empty",
        "repo": name,
        "groups_file": location.path,
        "groups_file_from": location.origin,
        "reason": reason,
        "groups": [],
    }


def build_result(
    name: str,
    location: gr.GroupsLocation,
    groups: gr.GroupsFile | None,
    repo_path: str,
    *,
    known_class: str | None = None,
) -> dict[str, Any]:
    """The verb's JSON object for *name*. *known_class* is a profile class the
    caller has already read for this repository; without it the class is read
    from the tree at *repo_path* when that tree is the repository."""
    if location.path is None:
        return _empty(
            name,
            location,
            f"no groups file is configured (--groups-file, {gr.GROUPS_FILE_ENV_VAR}, or the "
            f"{gr.CONFIG_SECTION_RULES}.{gr.CONFIG_KEY_GROUPS_FILE} setting)",
        )
    if groups is None:
        return _empty(name, location, f"no groups file at {location.path!r}")

    if known_class is not None:
        profile_class, class_why = known_class, "the profile already read"
    else:
        profile_class, class_why = _profile_class(repo_path, name)
    notes: list[str] = []
    if profile_class is None:
        notes.append(f"{NO_PROFILE_NOTE} ({class_why})")
    selected = gr.groups_for(groups, name, profile_class)

    applied = {group.id for group, _ in selected}
    return {
        "result": "rules",
        "repo": name,
        "groups_file": groups.path,
        "groups_file_from": location.origin,
        "profile_class": profile_class,
        "notes": notes,
        "assigned_classes": [
            _assigned_entry(entry, groups, applied) for entry in groups.classes_of(name)
        ],
        "groups": [_group_entry(group, how, groups.path) for group, how in selected],
    }


def _rule_entries(group: gr.Group, path: str) -> list[dict[str, Any]]:
    return [
        {
            "id": rule.id,
            "statement": rule.statement,
            "source": rule.source or None,
            "group": group.id,
            "file": path,
        }
        for rule in group.rules
    ]


def _group_entry(group: gr.Group, selected_by: str, path: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": group.id,
        "description": group.description or None,
        "selected_by": selected_by,
        "rules": _rule_entries(group, path),
    }
    if group.jurisdiction_class is not None:
        entry["jurisdiction_class"] = group.jurisdiction_class
    if group.note and not group.rules:
        entry["group_note"] = group.note
    return entry


def _assigned_entry(
    entry: gr.ClassAssignment, groups: gr.GroupsFile, applied: set[str]
) -> dict[str, Any]:
    """A class the groups file assigns the repository, with the groups that
    select on that class and whether each applied. The groups file is
    configuration and may lead the profile schema, so a class the schema does
    not admit is printed with its groups' rules and a note, never refused; its
    groups do not apply, because a group applies only through the profile
    class."""
    admitted = gr.schema_admits(entry.name)
    item: dict[str, Any] = {
        "class": entry.name,
        "source": entry.source or None,
        "file": groups.path,
        "admitted_by_profile_schema": admitted,
        "class_selected_groups": [
            {**_group_entry(group, "jurisdiction_class", groups.path), "applied": group.id in applied}
            for group in groups.groups
            if group.jurisdiction_class == entry.name
        ],
    }
    if not admitted:
        item["note"] = f"the profile schema does not admit the class {entry.name!r}"
    return item


def run(args: argparse.Namespace, *, config_root: str | Path | None = None) -> dict[str, Any]:
    """Resolve and build the result for parsed *args*; raises RulesVerbError."""
    name = gr.bare_repo_name(args.repo)
    if not name:
        raise RulesVerbError(f"--repo {args.repo!r} names no repository", EXIT_USAGE)
    location = gr.resolve_groups_location(args.groups_file, config_root=config_root)
    groups = None
    if location.path is not None:
        try:
            groups = gr.load_groups_file(location.path)
        except gr.GroupsFileError as exc:
            raise RulesVerbError(str(exc), EXIT_GROUPS_FILE) from exc
    return build_result(name, location, groups, args.repo_path)


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
        result = run(args, config_root=config_root)
    except RulesVerbError as exc:
        print(f"loadout-rules: {exc}", file=sys.stderr)
        return exc.code
    print(json.dumps(result))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
