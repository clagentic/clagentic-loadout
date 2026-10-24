"""rules.groups -- the group rules a deployment keeps outside any repository.

A groups file is operator-owned data read from a location the deployment
configures; it is never read from the repository under work. Its shape::

    schema_version: 1
    classes:                          # optional: which repos hold which class
      <class>:
        repos: [<repo name>, ...]
        source: <where the assignment is recorded>   # or: note: <text>
    groups:
      - id: <group id>
        description: <text>
        jurisdiction: [<glob over bare repo names>, ...]   # or:
        jurisdiction_class: <class>
        rules:
          - {id: <rule id>, statement: <text>, source: <text>}
        note: <text>                  # optional; shown when there are no rules

The classes in the file are data from the configuration layer and may lead the
profile schema: a class the profile schema does not admit is kept and flagged,
never refused. The location comes from, in order, the ``--groups-file`` flag,
``CLAGENTIC_LOADOUT_GROUPS_FILE``, and the ``rules.groups_file`` setting of the
user-level config file. With none of them set there are no groups.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from clagentic_loadout.review.policy_reader import MAX_POLICY_BYTES
from clagentic_loadout.review.repo_profile import CLASSES as PROFILE_CLASSES
from clagentic_loadout.transport.provider_config import load_user_config_section

GROUPS_FILE_ENV_VAR = "CLAGENTIC_LOADOUT_GROUPS_FILE"
CONFIG_SECTION_RULES = "rules"
CONFIG_KEY_GROUPS_FILE = "groups_file"
SCHEMA_VERSION = 1

FROM_FLAG = "flag"
FROM_ENV = "env"
FROM_CONFIG = "config"
FROM_NONE = "none"


class GroupsFileError(Exception):
    """The groups file exists but cannot be used. The message names the path
    and the problem."""


@dataclass(frozen=True)
class GroupRule:
    id: str
    statement: str
    source: str


@dataclass(frozen=True)
class Group:
    id: str
    description: str
    jurisdiction: tuple[str, ...]
    jurisdiction_class: str | None
    rules: tuple[GroupRule, ...]
    note: str


@dataclass(frozen=True)
class ClassAssignment:
    name: str
    repos: tuple[str, ...]
    source: str
    note: str


@dataclass(frozen=True)
class GroupsFile:
    path: str
    classes: tuple[ClassAssignment, ...] = field(default_factory=tuple)
    groups: tuple[Group, ...] = field(default_factory=tuple)

    def classes_of(self, repo: str) -> list[ClassAssignment]:
        """The class assignments that list *repo*, in file order."""
        wanted = repo.lower()
        return [c for c in self.classes if wanted in (r.lower() for r in c.repos)]


@dataclass(frozen=True)
class GroupsLocation:
    """Where the groups file is, and which setting said so. ``path`` is None
    when nothing configures one."""

    path: str | None
    origin: str


def resolve_groups_location(
    flag: str | None = None,
    env: Mapping[str, str] | None = None,
    *,
    config_root: str | Path | None = None,
) -> GroupsLocation:
    """The groups file location: flag, then environment, then the user-level
    ``rules.groups_file`` setting."""
    if flag and flag.strip():
        return GroupsLocation(flag.strip(), FROM_FLAG)
    environ = os.environ if env is None else env
    from_env = environ.get(GROUPS_FILE_ENV_VAR, "").strip()
    if from_env:
        return GroupsLocation(from_env, FROM_ENV)
    section = load_user_config_section(CONFIG_SECTION_RULES, config_root=config_root)
    value = section.get(CONFIG_KEY_GROUPS_FILE)
    if isinstance(value, str) and value.strip():
        return GroupsLocation(value.strip(), FROM_CONFIG)
    return GroupsLocation(None, FROM_NONE)


def _text(value: Any, where: str, path: str, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise GroupsFileError(f"{path}: {where} must be a non-empty string")
    return value.strip()


def _string_list(value: Any, where: str, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise GroupsFileError(f"{path}: {where} must be a list of non-empty strings")
    return tuple(v.strip() for v in value)


def _parse_rule(item: Any, where: str, path: str) -> GroupRule:
    if not isinstance(item, dict):
        raise GroupsFileError(f"{path}: {where} must be a mapping with id, statement and source")
    return GroupRule(
        id=_text(item.get("id"), f"{where}.id", path),
        statement=_text(item.get("statement"), f"{where}.statement", path),
        source=_text(item.get("source"), f"{where}.source", path, required=False),
    )


def _parse_group(item: Any, position: int, path: str) -> Group:
    where = f"groups[{position}]"
    if not isinstance(item, dict):
        raise GroupsFileError(f"{path}: {where} must be a mapping")
    jurisdiction = item.get("jurisdiction")
    by_class = item.get("jurisdiction_class")
    if (jurisdiction is None) == (by_class is None):
        raise GroupsFileError(
            f"{path}: {where} must have exactly one of jurisdiction and jurisdiction_class"
        )
    rules = item.get("rules", [])
    if not isinstance(rules, list):
        raise GroupsFileError(f"{path}: {where}.rules must be a list")
    return Group(
        id=_text(item.get("id"), f"{where}.id", path),
        description=_text(item.get("description"), f"{where}.description", path, required=False),
        jurisdiction=_string_list(jurisdiction, f"{where}.jurisdiction", path)
        if jurisdiction is not None
        else (),
        jurisdiction_class=_text(by_class, f"{where}.jurisdiction_class", path)
        if by_class is not None
        else None,
        rules=tuple(_parse_rule(r, f"{where}.rules[{i}]", path) for i, r in enumerate(rules)),
        note=_text(item.get("note"), f"{where}.note", path, required=False),
    )


def _parse_classes(block: Any, path: str) -> tuple[ClassAssignment, ...]:
    if block is None:
        return ()
    if not isinstance(block, dict):
        raise GroupsFileError(f"{path}: classes must be a mapping")
    out = []
    for name, entry in block.items():
        where = f"classes.{name}"
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise GroupsFileError(f"{path}: {where} must be a mapping")
        out.append(
            ClassAssignment(
                name=name,
                repos=_string_list(entry.get("repos", []), f"{where}.repos", path),
                source=_text(entry.get("source"), f"{where}.source", path, required=False),
                note=_text(entry.get("note"), f"{where}.note", path, required=False),
            )
        )
    return tuple(out)


def parse_groups_document(document: Any, path: str) -> GroupsFile:
    """The groups in a parsed file; GroupsFileError on any departure from the
    documented shape."""
    if not isinstance(document, dict):
        raise GroupsFileError(f"{path}: the top level must be a mapping")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise GroupsFileError(
            f"{path}: schema_version must be {SCHEMA_VERSION}, got {document.get('schema_version')!r}"
        )
    groups = document.get("groups", [])
    if not isinstance(groups, list):
        raise GroupsFileError(f"{path}: groups must be a list")
    return GroupsFile(
        path=path,
        classes=_parse_classes(document.get("classes"), path),
        groups=tuple(_parse_group(g, i, path) for i, g in enumerate(groups)),
    )


def load_groups_file(path: str) -> GroupsFile | None:
    """The parsed groups file at *path*, or None when no file is there. Raises
    GroupsFileError for one that exists and cannot be read or parsed."""
    target = Path(path).expanduser()
    if not target.exists():
        return None
    try:
        if target.stat().st_size > MAX_POLICY_BYTES:
            raise GroupsFileError(f"{path}: larger than {MAX_POLICY_BYTES} bytes")
        document = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise GroupsFileError(f"{path}: cannot be read as YAML: {exc}") from exc
    return parse_groups_document(document, path)


def bare_repo_name(repo: str) -> str:
    """The name part of ``owner/name``, or *repo* itself when it has no owner."""
    return repo.strip().rstrip("/").rsplit("/", 1)[-1]


def groups_for(groups: GroupsFile, repo_name: str, profile_class: str | None) -> list[tuple[Group, str]]:
    """The groups that apply to *repo_name* with how each was selected
    (``jurisdiction`` or ``jurisdiction_class``). A class-selected group applies
    only when the repo's profile class equals its class; with no profile class
    it applies to nothing."""
    wanted = repo_name.lower()
    chosen: list[tuple[Group, str]] = []
    for group in groups.groups:
        if group.jurisdiction_class is not None:
            if profile_class is not None and group.jurisdiction_class == profile_class:
                chosen.append((group, "jurisdiction_class"))
        elif any(fnmatch.fnmatchcase(wanted, glob.lower()) for glob in group.jurisdiction):
            chosen.append((group, "jurisdiction"))
    return chosen


def schema_admits(class_name: str) -> bool:
    """Whether the repo profile schema admits *class_name*."""
    return class_name in PROFILE_CLASSES


__all__ = [
    "CONFIG_KEY_GROUPS_FILE",
    "CONFIG_SECTION_RULES",
    "GROUPS_FILE_ENV_VAR",
    "ClassAssignment",
    "Group",
    "GroupRule",
    "GroupsFile",
    "GroupsFileError",
    "GroupsLocation",
    "bare_repo_name",
    "groups_for",
    "load_groups_file",
    "parse_groups_document",
    "resolve_groups_location",
    "schema_admits",
]
