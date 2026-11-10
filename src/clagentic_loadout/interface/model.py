"""interface.model -- a repo's declared interface: its shape and its parser.

A producer repo declares what other tools may depend on in a tracked file,
``.clagentic/loadout/interface.yaml``. Its shape::

    schema_version: 1
    verbs:                        # console scripts, with what a caller can rely on
      - name: <console script>
        subcommands: [<name>, ...]          # optional
        exit_codes: {<meaning>: <int>, ...} # optional; the meaning is the key
    schemas: [<schema file name>, ...]      # optional
    hooks: [<hook name>, ...]               # optional
    env: [<environment variable>, ...]      # optional

Removing or renaming an element, or changing the number an exit code meaning
maps to, breaks a caller; adding one does not (see interface.diff). The parser
refuses an unknown top-level key rather than guessing, since a misspelt section
would otherwise declare nothing and read as an empty interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

INTERFACE_PATH = ".clagentic/loadout/interface.yaml"
SCHEMA_VERSION = 1

_TOP_LEVEL_KEYS = frozenset({"schema_version", "verbs", "schemas", "hooks", "env"})
_VERB_KEYS = frozenset({"name", "subcommands", "exit_codes"})


class InterfaceError(Exception):
    """An interface file cannot be read as one. The message names where and why."""


@dataclass(frozen=True)
class VerbEntry:
    name: str
    subcommands: tuple[str, ...] = ()
    #: (meaning, code) pairs, sorted by meaning.
    exit_codes: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class InterfaceDoc:
    where: str
    verbs: tuple[VerbEntry, ...] = ()
    schemas: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()
    env: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        """The document in the shape the file declares, for comparison and JSON."""
        verbs = []
        for verb in self.verbs:
            entry: dict[str, Any] = {"name": verb.name}
            if verb.subcommands:
                entry["subcommands"] = list(verb.subcommands)
            if verb.exit_codes:
                entry["exit_codes"] = dict(verb.exit_codes)
            verbs.append(entry)
        return {
            "schema_version": SCHEMA_VERSION,
            "verbs": verbs,
            "schemas": list(self.schemas),
            "hooks": list(self.hooks),
            "env": list(self.env),
        }


def _names(value: Any, key: str, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise InterfaceError(f"{where}: {key} must be a list of non-empty strings")
    names = tuple(v.strip() for v in value)
    if len(set(names)) != len(names):
        raise InterfaceError(f"{where}: {key} lists a name more than once")
    return tuple(sorted(names))


def _parse_verb(item: Any, position: int, where: str) -> VerbEntry:
    label = f"verbs[{position}]"
    if not isinstance(item, dict):
        raise InterfaceError(f"{where}: {label} must be a mapping")
    unknown = sorted(set(item) - _VERB_KEYS, key=str)
    if unknown:
        raise InterfaceError(f"{where}: {label} has unknown key(s) {unknown}")
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise InterfaceError(f"{where}: {label}.name must be a non-empty string")
    codes = item.get("exit_codes")
    pairs: list[tuple[str, int]] = []
    if codes is not None:
        if not isinstance(codes, dict):
            raise InterfaceError(f"{where}: {label}.exit_codes must be a mapping of meaning to integer")
        for meaning, code in codes.items():
            if not isinstance(meaning, str) or not meaning.strip():
                raise InterfaceError(f"{where}: {label}.exit_codes has a meaning that is not text")
            if isinstance(code, bool) or not isinstance(code, int):
                raise InterfaceError(
                    f"{where}: {label}.exit_codes.{meaning} must be an integer, got {code!r}"
                )
            pairs.append((meaning.strip(), code))
    return VerbEntry(
        name=name.strip(),
        subcommands=_names(item.get("subcommands"), f"{label}.subcommands", where),
        exit_codes=tuple(sorted(pairs)),
    )


def parse_interface_document(document: Any, where: str) -> InterfaceDoc:
    """The interface a parsed file declares; InterfaceError on any departure from
    the documented shape."""
    if not isinstance(document, dict):
        raise InterfaceError(f"{where}: the top level must be a mapping")
    unknown = sorted(set(document) - _TOP_LEVEL_KEYS, key=str)
    if unknown:
        raise InterfaceError(f"{where}: unknown top-level key(s) {unknown}")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise InterfaceError(
            f"{where}: schema_version must be {SCHEMA_VERSION}, got {document.get('schema_version')!r}"
        )
    raw_verbs = document.get("verbs")
    if raw_verbs is None:
        raw_verbs = []
    if not isinstance(raw_verbs, list):
        raise InterfaceError(f"{where}: verbs must be a list")
    verbs = tuple(_parse_verb(item, i, where) for i, item in enumerate(raw_verbs))
    if len({v.name for v in verbs}) != len(verbs):
        raise InterfaceError(f"{where}: verbs lists a name more than once")
    return InterfaceDoc(
        where=where,
        verbs=tuple(sorted(verbs, key=lambda v: v.name)),
        schemas=_names(document.get("schemas"), "schemas", where),
        hooks=_names(document.get("hooks"), "hooks", where),
        env=_names(document.get("env"), "env", where),
    )


def parse_interface_text(text: str, where: str) -> InterfaceDoc:
    """Parse the text of an interface file read from *where* (a path or a
    revision-qualified path, used only in messages)."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise InterfaceError(f"{where}: cannot be read as YAML: {exc}") from exc
    return parse_interface_document(document, where)


__all__ = [
    "INTERFACE_PATH",
    "SCHEMA_VERSION",
    "InterfaceDoc",
    "InterfaceError",
    "VerbEntry",
    "parse_interface_document",
    "parse_interface_text",
]
