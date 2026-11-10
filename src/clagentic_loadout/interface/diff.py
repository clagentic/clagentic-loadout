"""interface.diff -- what changed between two revisions of a repo's interface.

Removing or renaming an element, or changing the number an exit code meaning
maps to, is breaking. Adding an element is not. A rename is seen as the removal
of the old name together with the addition of the new one: nothing here guesses
that two names are the same element.

A revision with no interface file is reported, never an error: with either side
missing there is nothing to compare, so no change is listed and the result is
not breaking. The state of each side says which case it was.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.interface.model import (
    INTERFACE_PATH,
    InterfaceDoc,
    parse_interface_text,
)
from clagentic_loadout.review.policy_reader import PolicyReader, PolicyReadError

KIND_VERB = "verb"
KIND_SUBCOMMAND = "subcommand"
KIND_EXIT_CODE = "exit_code"
KIND_SCHEMA = "schema"
KIND_HOOK = "hook"
KIND_ENV = "env"

CHANGE_REMOVED = "removed"
CHANGE_CHANGED = "changed"
CHANGE_ADDED = "added"

STATE_PRESENT = "present"
STATE_MISSING = "missing"

_BREAKING_CHANGES = frozenset({CHANGE_REMOVED, CHANGE_CHANGED})


@dataclass(frozen=True)
class Change:
    kind: str
    #: What a reader sees: the element in full, e.g. ``<verb> exit usage``.
    element: str
    #: The name a consumer map lists the element under: the verb for a
    #: subcommand or exit code, else the element's own name.
    name: str
    change: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "element": self.element,
            "name": self.name,
            "change": self.change,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class InterfaceDiff:
    base_state: str
    head_state: str
    changes: tuple[Change, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def breaking(self) -> bool:
        return any(c.change in _BREAKING_CHANGES for c in self.changes)

    def breaking_changes(self) -> list[Change]:
        return [c for c in self.changes if c.change in _BREAKING_CHANGES]

    def to_dict(self) -> dict[str, Any]:
        return {
            "breaking": self.breaking,
            "interface_file": INTERFACE_PATH,
            "base": self.base_state,
            "head": self.head_state,
            "removed": [c.to_dict() for c in self.changes if c.change == CHANGE_REMOVED],
            "changed": [c.to_dict() for c in self.changes if c.change == CHANGE_CHANGED],
            "added": [c.to_dict() for c in self.changes if c.change == CHANGE_ADDED],
            "notes": list(self.notes),
        }


def read_interface(reader: PolicyReader, rev: str) -> InterfaceDoc | None:
    """The interface declared at *rev*, or None when the revision has no
    interface file. Raises PolicyReadError when the revision cannot be read and
    InterfaceError when the file is not a valid interface."""
    if INTERFACE_PATH not in reader.list_paths(rev):
        return None
    return parse_interface_text(reader.read_text(rev, INTERFACE_PATH), f"{INTERFACE_PATH} at {rev[:12]}")


def read_interface_with_fallback(
    readers: Sequence[PolicyReader], rev: str
) -> InterfaceDoc | None:
    """Like read_interface through the first reader that can read *rev*. The last
    reader's failure is raised when none can."""
    if not readers:
        raise PolicyReadError(f"no reader was given to read {INTERFACE_PATH} at {rev[:12]}")
    last: PolicyReadError | None = None
    for reader in readers:
        try:
            return read_interface(reader, rev)
        except PolicyReadError as exc:
            last = exc
    raise last  # type: ignore[misc]  # readers is non-empty, so a failure was recorded


def _by_name(entries: Sequence[Any]) -> dict[str, Any]:
    return {e.name: e for e in entries}


def _name_changes(kind: str, base: Sequence[str], head: Sequence[str]) -> list[Change]:
    out = []
    for name in sorted(set(base) - set(head)):
        out.append(Change(kind, f"{kind} {name}", name, CHANGE_REMOVED, f"{kind} {name} is no longer declared"))
    for name in sorted(set(head) - set(base)):
        out.append(Change(kind, f"{kind} {name}", name, CHANGE_ADDED, f"{kind} {name} is newly declared"))
    return out


def diff_documents(base: InterfaceDoc | None, head: InterfaceDoc | None) -> InterfaceDiff:
    """The changes from *base* to *head*; either may be None for a revision with
    no interface file."""
    base_state = STATE_PRESENT if base is not None else STATE_MISSING
    head_state = STATE_PRESENT if head is not None else STATE_MISSING
    if base is None or head is None:
        if base is None and head is None:
            notes: tuple[str, ...] = (f"no {INTERFACE_PATH} at either revision",)
        elif base is None:
            notes = (f"no {INTERFACE_PATH} at the base revision, so nothing to compare",)
        else:
            notes = (f"no {INTERFACE_PATH} at the head revision, so nothing to compare",)
        return InterfaceDiff(base_state, head_state, (), notes)

    changes: list[Change] = []
    base_verbs, head_verbs = _by_name(base.verbs), _by_name(head.verbs)
    for name in sorted(set(base_verbs) - set(head_verbs)):
        changes.append(
            Change(KIND_VERB, f"{KIND_VERB} {name}", name, CHANGE_REMOVED, f"verb {name} is no longer declared")
        )
    for name in sorted(set(head_verbs) - set(base_verbs)):
        changes.append(
            Change(KIND_VERB, f"{KIND_VERB} {name}", name, CHANGE_ADDED, f"verb {name} is newly declared")
        )
    for name in sorted(set(base_verbs) & set(head_verbs)):
        old, new = base_verbs[name], head_verbs[name]
        for sub in sorted(set(old.subcommands) - set(new.subcommands)):
            changes.append(
                Change(
                    KIND_SUBCOMMAND, f"verb {name} subcommand {sub}", name, CHANGE_REMOVED,
                    f"subcommand {sub} of {name} is no longer declared",
                )
            )
        for sub in sorted(set(new.subcommands) - set(old.subcommands)):
            changes.append(
                Change(
                    KIND_SUBCOMMAND, f"verb {name} subcommand {sub}", name, CHANGE_ADDED,
                    f"subcommand {sub} of {name} is newly declared",
                )
            )
        old_codes, new_codes = dict(old.exit_codes), dict(new.exit_codes)
        for meaning in sorted(set(old_codes) - set(new_codes)):
            changes.append(
                Change(
                    KIND_EXIT_CODE, f"verb {name} exit {meaning}", name, CHANGE_REMOVED,
                    f"exit code {meaning} ({old_codes[meaning]}) of {name} is no longer declared",
                )
            )
        for meaning in sorted(set(new_codes) - set(old_codes)):
            changes.append(
                Change(
                    KIND_EXIT_CODE, f"verb {name} exit {meaning}", name, CHANGE_ADDED,
                    f"exit code {meaning} ({new_codes[meaning]}) of {name} is newly declared",
                )
            )
        for meaning in sorted(set(old_codes) & set(new_codes)):
            if old_codes[meaning] != new_codes[meaning]:
                changes.append(
                    Change(
                        KIND_EXIT_CODE, f"verb {name} exit {meaning}", name, CHANGE_CHANGED,
                        f"exit code {meaning} of {name} changed from {old_codes[meaning]} "
                        f"to {new_codes[meaning]}",
                    )
                )
    changes.extend(_name_changes(KIND_SCHEMA, base.schemas, head.schemas))
    changes.extend(_name_changes(KIND_HOOK, base.hooks, head.hooks))
    changes.extend(_name_changes(KIND_ENV, base.env, head.env))
    return InterfaceDiff(base_state, head_state, tuple(changes), ())


__all__ = [
    "CHANGE_ADDED",
    "CHANGE_CHANGED",
    "CHANGE_REMOVED",
    "KIND_ENV",
    "KIND_EXIT_CODE",
    "KIND_HOOK",
    "KIND_SCHEMA",
    "KIND_SUBCOMMAND",
    "KIND_VERB",
    "STATE_MISSING",
    "STATE_PRESENT",
    "Change",
    "InterfaceDiff",
    "diff_documents",
    "read_interface",
    "read_interface_with_fallback",
]
