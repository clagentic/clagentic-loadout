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

A rule record may also carry, all optional: ``roles`` (a list of role words the
rule is for), ``steps`` (a list of step names it is for), ``kind``
(``constraint`` or ``judgment``), ``said_at`` and ``relayed`` (provenance text).
A record without ``roles`` applies to every role, one without ``steps`` to every
step. Any other key on a record, a ``kind`` outside the two, or a value of the
wrong shape is kept apart and reported in the record's ``not_understood``; it
never fails the read. The steps a deployment names are the ``rules.steps`` list
of the user-level config file.

The classes in the file are data from the configuration layer and may lead the
profile schema: a class the profile schema does not admit is kept and flagged,
never refused. The location comes from, in order, the ``--groups-file`` flag,
``CLAGENTIC_LOADOUT_GROUPS_FILE``, and the ``rules.groups_file`` setting of the
user-level config file. With none of them set there are no groups.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import os
from collections.abc import Collection, Mapping
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
CONFIG_KEY_STEPS = "steps"
SCHEMA_VERSION = 1

RULE_KINDS = ("constraint", "judgment")
KNOWN_RULE_KEYS = frozenset({"id", "statement", "source", "roles", "steps", "kind", "said_at", "relayed"})

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
    roles: tuple[str, ...] | None = None
    steps: tuple[str, ...] | None = None
    kind: str | None = None
    said_at: str | None = None
    relayed: str | bool | None = None
    not_understood: tuple[str, ...] = ()


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


def resolve_rules_file_location(
    flag: str | None,
    env: Mapping[str, str] | None,
    *,
    env_var: str,
    config_key: str,
    config_root: str | Path | None = None,
) -> GroupsLocation:
    """Where an operator-owned file under the ``rules`` settings lives: the flag,
    then the environment variable *env_var*, then the user-level
    ``rules.<config_key>`` setting. Shared by every such file, so each resolves
    the same way."""
    if flag and flag.strip():
        return GroupsLocation(flag.strip(), FROM_FLAG)
    environ = os.environ if env is None else env
    from_env = environ.get(env_var, "").strip()
    if from_env:
        return GroupsLocation(from_env, FROM_ENV)
    section = load_user_config_section(CONFIG_SECTION_RULES, config_root=config_root)
    value = section.get(config_key)
    if isinstance(value, str) and value.strip():
        return GroupsLocation(value.strip(), FROM_CONFIG)
    return GroupsLocation(None, FROM_NONE)


def resolve_groups_location(
    flag: str | None = None,
    env: Mapping[str, str] | None = None,
    *,
    config_root: str | Path | None = None,
) -> GroupsLocation:
    """The groups file location: flag, then environment, then the user-level
    ``rules.groups_file`` setting."""
    return resolve_rules_file_location(
        flag,
        env,
        env_var=GROUPS_FILE_ENV_VAR,
        config_key=CONFIG_KEY_GROUPS_FILE,
        config_root=config_root,
    )


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


def _optional_words(value: Any) -> tuple[str, ...] | None:
    """A list of non-empty strings, or None when *value* is not one."""
    if isinstance(value, list) and value and all(isinstance(v, str) and v.strip() for v in value):
        return tuple(v.strip() for v in value)
    return None


def _optional_text(value: Any) -> str | None:
    """Text for a provenance value; a YAML date is kept as its ISO form."""
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_rule_extras(item: dict[Any, Any]) -> dict[str, Any]:
    """The optional keys of a rule record, and what could not be understood.
    Nothing here raises: a key or value that is not understood is named in
    ``not_understood`` and the record behaves as if it were absent."""
    extras: dict[str, Any] = {}
    unread: list[str] = []
    for key in ("roles", "steps"):
        if key in item:
            words = _optional_words(item[key])
            if words is None:
                unread.append(f"{key} (not a non-empty list of strings)")
            else:
                extras[key] = words
    if "kind" in item:
        kind = item["kind"]
        if isinstance(kind, str) and kind.strip() in RULE_KINDS:
            extras["kind"] = kind.strip()
        else:
            unread.append(f"kind {kind!r} (not one of {', '.join(RULE_KINDS)})")
    if "said_at" in item:
        said_at = _optional_text(item["said_at"])
        if said_at is None:
            unread.append("said_at (not text or a date)")
        else:
            extras["said_at"] = said_at
    if "relayed" in item:
        relayed = item["relayed"]
        if isinstance(relayed, bool):
            extras["relayed"] = relayed
        elif _optional_text(relayed) is not None:
            extras["relayed"] = _optional_text(relayed)
        else:
            unread.append("relayed (not text or true/false)")
    unread.extend(f"key {str(key)!r}" for key in item if key not in KNOWN_RULE_KEYS)
    extras["not_understood"] = tuple(unread)
    return extras


def _parse_rule(item: Any, where: str, path: str) -> GroupRule:
    if not isinstance(item, dict):
        raise GroupsFileError(f"{path}: {where} must be a mapping with id, statement and source")
    return GroupRule(
        id=_text(item.get("id"), f"{where}.id", path),
        statement=_text(item.get("statement"), f"{where}.statement", path),
        source=_text(item.get("source"), f"{where}.source", path, required=False),
        **_parse_rule_extras(item),
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


def read_declared_steps(
    config_root: str | Path | None = None,
) -> tuple[tuple[str, ...], str | None]:
    """``(steps, problem)``: the step names a deployment declares, the
    ``rules.steps`` list of the user-level config file, and a line naming why a
    present value was not used. Never raises; the product ships no step names
    of its own."""
    value = load_user_config_section(CONFIG_SECTION_RULES, config_root=config_root).get(
        CONFIG_KEY_STEPS
    )
    words = _optional_words(value)
    if words is None and value not in (None, []):
        return (), (
            f"{CONFIG_SECTION_RULES}.{CONFIG_KEY_STEPS} in the user-level config file is not a "
            "list of non-empty step names; no steps are declared"
        )
    return words or (), None


def load_declared_steps(config_root: str | Path | None = None) -> tuple[str, ...]:
    """The step names a deployment declares. Empty when absent or not a list of
    strings."""
    return read_declared_steps(config_root)[0]


def rule_applies(
    roles: Collection[str] | None,
    steps: Collection[str] | None,
    *,
    role_words: Collection[str] | None,
    step: str | None,
    step_declared: bool = True,
) -> bool:
    """The one matching rule for a rule record's ``roles`` and ``steps``.

    A record without ``roles`` applies to every role; with them it applies when
    one of *role_words* (the word asked and the role it resolves to) is listed.
    *role_words* None means the role is not declared, so only a record without
    ``roles`` applies. The step axis is the same, except that no *step* asked
    means the step is not filtered."""
    if roles is not None and not (role_words and set(role_words) & set(roles)):
        return False
    if step is not None and steps is not None and not (step_declared and step in steps):
        return False
    return True


def schema_admits(class_name: str) -> bool:
    """Whether the repo profile schema admits *class_name*."""
    return class_name in PROFILE_CLASSES


__all__ = [
    "CONFIG_KEY_GROUPS_FILE",
    "CONFIG_KEY_STEPS",
    "CONFIG_SECTION_RULES",
    "GROUPS_FILE_ENV_VAR",
    "KNOWN_RULE_KEYS",
    "RULE_KINDS",
    "ClassAssignment",
    "Group",
    "GroupRule",
    "GroupsFile",
    "GroupsFileError",
    "GroupsLocation",
    "FROM_CONFIG",
    "FROM_ENV",
    "FROM_FLAG",
    "FROM_NONE",
    "bare_repo_name",
    "groups_for",
    "load_declared_steps",
    "read_declared_steps",
    "load_groups_file",
    "parse_groups_document",
    "resolve_groups_location",
    "resolve_rules_file_location",
    "rule_applies",
    "schema_admits",
]
