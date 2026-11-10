"""interface.notice -- which consumers of a breaking change a spec leaves undisposed.

A spec lists, in its ``consumers`` section, each consumer of what the change
touches and a disposition for it. A consumer is disposed when its disposition
names a work item, or says ``none`` and gives a reason. Loadout assumes no
tracker, so any text that is not a bare ``none`` is taken to name one.

This module only compares two lists. It runs nothing against a consumer, reads
no repository and refuses nothing: the caller prints what it returns.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.interface.consumers import ConsumerHit, ConsumersFile, consumers_of
from clagentic_loadout.interface.diff import Change

_NONE_WORD = "none"
_NAME_EDGE = r"A-Za-z0-9_-"

#: What a draft spec holds for a consumer nobody has dispositioned yet. The spec
#: schema wants some text there, so this stands in for it and is never read as a
#: disposition.
PLACEHOLDER_DISPOSITION = "undisposed: name a work item, or write none and a reason"


def has_disposition(text: str) -> bool:
    """Whether *text* is a disposition: a reference to a work item, or ``none``
    followed by a reason. Blank text, ``none`` alone and the placeholder a draft
    starts with are not."""
    stripped = text.strip()
    if not stripped or stripped.lower().startswith("undisposed"):
        return False
    if stripped.lower().startswith(_NONE_WORD) and not stripped[len(_NONE_WORD) :][:1].isalnum():
        reason = stripped[len(_NONE_WORD) :].strip(" \t:;,.-–—")
        return bool(reason)
    return True


def mentions_repo(text: str, repo: str) -> bool:
    """Whether *text* names the repository *repo* (the bare name, without its
    owner), as a whole word: ``tool`` is not found in ``tool-extra``. Only the
    part before the first ``": "`` is read, which is where an item that names the
    element after its consumer (see draft_items) keeps the consumer."""
    bare = repo.strip().rstrip("/").rsplit("/", 1)[-1]
    if not bare:
        return False
    head = text.split(": ", 1)[0]
    return re.search(
        rf"(?<![{_NAME_EDGE}]){re.escape(bare)}(?![{_NAME_EDGE}])", head, re.IGNORECASE
    ) is not None


def consumers_of_changes(
    consumers: ConsumersFile, producer: str, changes: Iterable[Change]
) -> list[ConsumerHit]:
    """The consumers the map lists for *producer* of the elements *changes* name."""
    names = sorted({c.name for c in changes})
    return consumers_of(consumers, producer, names=names) if names else []


def draft_items(hits: Iterable[ConsumerHit]) -> list[dict[str, str]]:
    """Spec ``consumers`` items for *hits*, one per consumer and element, each
    carrying the placeholder disposition for the author to replace."""
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for hit in hits:
        consumer = f"{hit.consumer}: {hit.element}"
        if consumer in seen:
            continue
        seen.add(consumer)
        items.append({"consumer": consumer, "disposition": PLACEHOLDER_DISPOSITION})
    return items


def spec_dispositions(spec_doc: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The ``(consumer, disposition)`` pairs a sealed spec lists."""
    section = spec_doc.get("consumers")
    if not isinstance(section, Mapping):
        return []
    out = []
    for item in section.get("items") or []:
        if isinstance(item, Mapping):
            out.append((str(item.get("consumer", "")), str(item.get("disposition", ""))))
    return out


@dataclass(frozen=True)
class Undisposed:
    """A consumer of a breaking change that no spec disposes."""

    consumer: str
    element: str
    confirmed_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "consumer": self.consumer,
            "element": self.element,
            "confirmed_at": self.confirmed_at,
        }


def undisposed_consumers(
    hits: Iterable[ConsumerHit], dispositions: Sequence[tuple[str, str]]
) -> list[Undisposed]:
    """The consumers in *hits* that none of *dispositions* covers: no listed
    consumer names the repository, or the one that does has no disposition."""
    out: list[Undisposed] = []
    seen: set[tuple[str, str]] = set()
    for hit in hits:
        disposed = any(
            mentions_repo(consumer, hit.consumer) and has_disposition(disposition)
            for consumer, disposition in dispositions
        )
        marker = (hit.consumer, hit.element)
        if disposed or marker in seen:
            continue
        seen.add(marker)
        out.append(Undisposed(hit.consumer, hit.element, hit.confirmed_at))
    return out


def render_undisposed(item: Undisposed, *, changed: Sequence[str]) -> str:
    """The notice line for one undisposed consumer. *changed* names the elements
    that broke."""
    broke = ", ".join(changed) if changed else "the interface"
    return (
        f"BREAKING INTERFACE CHANGE, consumer without a disposition: {item.consumer} "
        f"consumes {item.element} (confirmed {item.confirmed_at}); changed: {broke}. "
        "The spec lists no work item for it and no 'none' with a reason."
    )


__all__ = [
    "PLACEHOLDER_DISPOSITION",
    "Undisposed",
    "consumers_of_changes",
    "draft_items",
    "has_disposition",
    "mentions_repo",
    "render_undisposed",
    "spec_dispositions",
    "undisposed_consumers",
]
