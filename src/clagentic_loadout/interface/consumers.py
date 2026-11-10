"""interface.consumers -- who consumes a repo's interface, from the consumer map.

The consumer map is operator-owned data kept outside any repository under work,
read only through the verbs of this package. Its shape::

    schema_version: 1
    producers:
      <producer repo>:                  # bare repository name
        - element: <verb, command, file or schema name>
          kind: verb | flag | file | schema | config-key
          consumers:
            - repo: <consumer repo>
              confirmed_at: "<ISO date the use was confirmed>"
              source: <task id, or the file path in the consumer repo where seen>
    unconfirmed: [...]                  # seeds that could not be confirmed

``unconfirmed`` is not part of the map and is only counted. The location comes
from, in order, the ``--consumers-file`` flag, ``CLAGENTIC_LOADOUT_CONSUMERS_FILE``
and the ``rules.consumers_file`` setting of the user-level config file, exactly as
the groups file does. With none of them set there is no map, and every verb
reports that and carries on: an absent map is information, never a failure.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from clagentic_loadout.review.policy_reader import MAX_POLICY_BYTES
from clagentic_loadout.rules.groups import (
    GroupsLocation,
    bare_repo_name,
    resolve_rules_file_location,
)

CONSUMERS_FILE_ENV_VAR = "CLAGENTIC_LOADOUT_CONSUMERS_FILE"
CONFIG_KEY_CONSUMERS_FILE = "consumers_file"
SCHEMA_VERSION = 1

#: A consumer confirmed longer ago than this is reported as stale, advisory only.
DEFAULT_WINDOW_DAYS = 90


class ConsumersFileError(Exception):
    """The consumer map exists but cannot be used. The message names the path and
    the problem."""


@dataclass(frozen=True)
class ConsumerUse:
    repo: str
    confirmed_at: str
    source: str


@dataclass(frozen=True)
class MapElement:
    element: str
    kind: str
    consumers: tuple[ConsumerUse, ...]


@dataclass(frozen=True)
class ConsumerHit:
    """One consumer of one element, as the map lists it."""

    producer: str
    consumer: str
    element: str
    kind: str
    confirmed_at: str
    source: str

    def to_dict(self) -> dict[str, str]:
        return {
            "producer": self.producer,
            "consumer": self.consumer,
            "element": self.element,
            "kind": self.kind,
            "confirmed_at": self.confirmed_at,
            "source": self.source,
        }


@dataclass(frozen=True)
class ConsumersFile:
    path: str
    producers: Mapping[str, tuple[MapElement, ...]] = field(default_factory=dict)
    unconfirmed: int = 0

    def confirmed_dates(self) -> list[dt.date]:
        out: list[dt.date] = []
        for elements in self.producers.values():
            for element in elements:
                for use in element.consumers:
                    parsed = _parse_date(use.confirmed_at)
                    if parsed is not None:
                        out.append(parsed)
        return out


def resolve_consumers_location(
    flag: str | None = None,
    env: Mapping[str, str] | None = None,
    *,
    config_root: str | Path | None = None,
) -> GroupsLocation:
    """The consumer map location: flag, then environment, then the user-level
    ``rules.consumers_file`` setting."""
    return resolve_rules_file_location(
        flag,
        env,
        env_var=CONSUMERS_FILE_ENV_VAR,
        config_key=CONFIG_KEY_CONSUMERS_FILE,
        config_root=config_root,
    )


def _parse_date(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _text(value: Any, where: str, path: str, *, required: bool = True) -> str:
    if isinstance(value, dt.date):
        return value.isoformat()
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ConsumersFileError(f"{path}: {where} must be a non-empty string")
    return value.strip()


def _parse_use(item: Any, where: str, path: str) -> ConsumerUse:
    if not isinstance(item, dict):
        raise ConsumersFileError(f"{path}: {where} must be a mapping with repo, confirmed_at and source")
    confirmed = _text(item.get("confirmed_at"), f"{where}.confirmed_at", path)
    if _parse_date(confirmed) is None:
        raise ConsumersFileError(f"{path}: {where}.confirmed_at must be an ISO date, got {confirmed!r}")
    return ConsumerUse(
        repo=_text(item.get("repo"), f"{where}.repo", path),
        confirmed_at=confirmed,
        source=_text(item.get("source"), f"{where}.source", path, required=False),
    )


def _parse_element(item: Any, where: str, path: str) -> MapElement:
    if not isinstance(item, dict):
        raise ConsumersFileError(f"{path}: {where} must be a mapping")
    uses = item.get("consumers", [])
    if not isinstance(uses, list):
        raise ConsumersFileError(f"{path}: {where}.consumers must be a list")
    return MapElement(
        element=_text(item.get("element"), f"{where}.element", path),
        kind=_text(item.get("kind"), f"{where}.kind", path, required=False),
        consumers=tuple(_parse_use(u, f"{where}.consumers[{i}]", path) for i, u in enumerate(uses)),
    )


def parse_consumers_document(document: Any, path: str) -> ConsumersFile:
    """The map in a parsed file; ConsumersFileError on any departure from the
    documented shape."""
    if not isinstance(document, dict):
        raise ConsumersFileError(f"{path}: the top level must be a mapping")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ConsumersFileError(
            f"{path}: schema_version must be {SCHEMA_VERSION}, got {document.get('schema_version')!r}"
        )
    block = document.get("producers", {})
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise ConsumersFileError(f"{path}: producers must be a mapping")
    producers: dict[str, tuple[MapElement, ...]] = {}
    for producer, elements in block.items():
        where = f"producers.{producer}"
        if not isinstance(producer, str) or not isinstance(elements, list):
            raise ConsumersFileError(f"{path}: {where} must be a list of elements")
        producers[producer] = tuple(
            _parse_element(e, f"{where}[{i}]", path) for i, e in enumerate(elements)
        )
    unconfirmed = document.get("unconfirmed") or []
    return ConsumersFile(
        path=path,
        producers=producers,
        unconfirmed=len(unconfirmed) if isinstance(unconfirmed, list) else 0,
    )


def load_consumers_file(path: str) -> ConsumersFile | None:
    """The parsed consumer map at *path*, or None when no file is there. Raises
    ConsumersFileError for one that exists and cannot be read or parsed."""
    target = Path(path).expanduser()
    if not target.exists():
        return None
    try:
        if target.stat().st_size > MAX_POLICY_BYTES:
            raise ConsumersFileError(f"{path}: larger than {MAX_POLICY_BYTES} bytes")
        document = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConsumersFileError(f"{path}: cannot be read as YAML: {exc}") from exc
    return parse_consumers_document(document, path)


@dataclass(frozen=True)
class MapStatus:
    """The outcome of looking for the consumer map: the map when there is one,
    else why there is not."""

    location: GroupsLocation
    consumers: ConsumersFile | None
    unavailable: str | None

    @property
    def available(self) -> bool:
        return self.consumers is not None


def locate_consumers(
    flag: str | None = None,
    env: Mapping[str, str] | None = None,
    *,
    config_root: str | Path | None = None,
) -> MapStatus:
    """Resolve and load the consumer map. Never raises: a map that is not
    configured, not there or not valid is reported in ``unavailable``."""
    location = resolve_consumers_location(flag, env, config_root=config_root)
    if location.path is None:
        return MapStatus(location, None, "no consumer map is configured")
    try:
        loaded = load_consumers_file(location.path)
    except ConsumersFileError as exc:
        return MapStatus(location, None, f"the consumer map is unusable: {exc}")
    if loaded is None:
        return MapStatus(location, None, f"no consumer map at {location.path!r}")
    return MapStatus(location, loaded, None)


def _target(element: str) -> str:
    """The leading token of a map element: the verb or file it names."""
    parts = element.split()
    return parts[0] if parts else ""


def _matches_name(element: str, name: str) -> bool:
    return element == name or _target(element) == name


def _matches_path(element: str, pattern: str) -> bool:
    target = _target(element)
    return fnmatch.fnmatchcase(target, pattern) or fnmatch.fnmatchcase(pattern, target)


def consumers_of(
    consumers: ConsumersFile,
    producer: str,
    *,
    names: Sequence[str] = (),
    paths: Sequence[str] = (),
) -> list[ConsumerHit]:
    """The consumers *consumers* lists for *producer* of the elements called one
    of *names*, or whose file lies under one of the glob *paths*. A map element
    matches a name when it is that name or its first word is, so a verb's name
    finds the verb's flags too. A producer is matched on its bare repository
    name, without case."""
    wanted = bare_repo_name(producer).lower()
    hits: list[ConsumerHit] = []
    seen: set[tuple[str, str]] = set()
    for key, elements in consumers.producers.items():
        if bare_repo_name(key).lower() != wanted:
            continue
        for element in elements:
            if not (
                any(_matches_name(element.element, n) for n in names)
                or any(_matches_path(element.element, p) for p in paths)
            ):
                continue
            for use in element.consumers:
                marker = (use.repo, element.element)
                if marker in seen:
                    continue
                seen.add(marker)
                hits.append(
                    ConsumerHit(
                        producer=key,
                        consumer=use.repo,
                        element=element.element,
                        kind=element.kind,
                        confirmed_at=use.confirmed_at,
                        source=use.source,
                    )
                )
    return hits


@dataclass(frozen=True)
class MapAge:
    """How old the confirmations in a map are, as of *today*."""

    entries: int
    oldest: str | None
    newest: str | None
    oldest_age_days: int | None
    newest_age_days: int | None
    stale_entries: int
    window_days: int

    @property
    def stale(self) -> bool:
        return self.entries > 0 and self.stale_entries == self.entries


def map_age(
    consumers: ConsumersFile, today: dt.date, *, window_days: int = DEFAULT_WINDOW_DAYS
) -> MapAge:
    dates = consumers.confirmed_dates()
    if not dates:
        return MapAge(0, None, None, None, None, 0, window_days)
    oldest, newest = min(dates), max(dates)
    stale = sum(1 for d in dates if (today - d).days > window_days)
    return MapAge(
        entries=len(dates),
        oldest=oldest.isoformat(),
        newest=newest.isoformat(),
        oldest_age_days=(today - oldest).days,
        newest_age_days=(today - newest).days,
        stale_entries=stale,
        window_days=window_days,
    )


__all__ = [
    "CONFIG_KEY_CONSUMERS_FILE",
    "CONSUMERS_FILE_ENV_VAR",
    "DEFAULT_WINDOW_DAYS",
    "ConsumerHit",
    "ConsumerUse",
    "ConsumersFile",
    "ConsumersFileError",
    "MapAge",
    "MapElement",
    "MapStatus",
    "consumers_of",
    "load_consumers_file",
    "locate_consumers",
    "map_age",
    "parse_consumers_document",
    "resolve_consumers_location",
]
