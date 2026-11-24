"""review.drop_records — the records file `apply-drops` reads.

A UTF-8 text file, one record per line, blank lines ignored::

    drop <id> <reason>
    tag <n> <in-spec|out-of-spec|fix-caused>
    cover <criterion-id> <met|unmet>
    reject <text>

Only the fixed leading fields are split off; everything after them is kept as a
string, byte for byte, and never reaches a shell, eval or template. ``id`` is a
finding's zero-based position in the findings array, or ``sN``, the 1-based number
of a scanner hit. This module parses and checks the shape of each record; whether
an id names something is the caller's check, against the findings and the hits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.merge.fence_state import COVERAGE_STATUSES, SPEC_TAGS
from clagentic_loadout.review.docs_only_review import DocsOnlyError, check_reason

_RECORD_RE = re.compile(r"\A(\S+)(?:[ \t]+(.*))?\Z")
_FINDING_ID_RE = re.compile(r"\A[0-9]{1,9}\Z")
_HIT_ID_RE = re.compile(r"\As([1-9][0-9]{0,8})\Z")
_KEYWORDS = ("drop", "tag", "cover", "reject")


class RecordsError(ValueError):
    """The records file cannot be used; the message names the path and line."""


@dataclass(frozen=True)
class Drop:
    line: int
    #: ``("finding", position)`` or ``("hit", number)``.
    target: tuple[str, int]
    reason: str


@dataclass(frozen=True)
class Tag:
    line: int
    position: int
    tag: str


@dataclass(frozen=True)
class Cover:
    line: int
    criterion: str
    status: str


@dataclass(frozen=True)
class Reject:
    line: int
    text: str


@dataclass(frozen=True)
class Records:
    path: str
    drops: tuple[Drop, ...]
    tags: tuple[Tag, ...]
    covers: tuple[Cover, ...]
    rejects: tuple[Reject, ...]

    @property
    def spec_records(self) -> tuple[Tag | Cover | Reject, ...]:
        return (*self.tags, *self.covers, *self.rejects)


def _target(token: str, where: str) -> tuple[str, int]:
    if _FINDING_ID_RE.match(token):
        return ("finding", int(token))
    hit = _HIT_ID_RE.match(token)
    if hit:
        return ("hit", int(hit.group(1)))
    raise RecordsError(
        f"{where}: {token!r} is not a finding position (0, 1, ...) or a scanner hit number "
        "(s1, s2, ...)"
    )


def parse_records(path: str) -> Records:
    """Read and parse the records file at *path*. Raises RecordsError naming the
    resolved path, and the line number for a bad record."""
    resolved = str(Path(path).expanduser().resolve())
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RecordsError(f"cannot read the records file {resolved!r}: {exc}") from exc
    drops: list[Drop] = []
    tags: list[Tag] = []
    covers: list[Cover] = []
    rejects: list[Reject] = []
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw[:-1] if raw.endswith("\r") else raw
        if not line.strip():
            continue
        where = f"records file {resolved!r}, line {number}"
        match = _RECORD_RE.match(line)
        keyword = match.group(1) if match else line.split(None, 1)[0]
        if match is None or keyword not in _KEYWORDS:
            raise RecordsError(f"{where}: unknown record {keyword!r}; expected one of {list(_KEYWORDS)}")
        rest = match.group(2) or ""
        if keyword == "reject":
            if not rest.strip():
                raise RecordsError(f"{where}: reject needs a text")
            try:
                check_reason(rest)
            except DocsOnlyError as exc:
                raise RecordsError(f"{where}: {exc}") from exc
            rejects.append(Reject(number, rest))
            continue
        fields = _RECORD_RE.match(rest)
        if fields is None:
            raise RecordsError(f"{where}: {keyword} needs an id and a value")
        first, value = fields.group(1), fields.group(2) or ""
        if keyword == "drop":
            if not value.strip():
                raise RecordsError(f"{where}: drop {first} needs a reason")
            drops.append(Drop(number, _target(first, where), value))
        elif keyword == "tag":
            if not _FINDING_ID_RE.match(first):
                raise RecordsError(f"{where}: {first!r} is not a finding position (0, 1, ...)")
            if value not in SPEC_TAGS:
                raise RecordsError(f"{where}: tag must be one of {list(SPEC_TAGS)}, got {value!r}")
            tags.append(Tag(number, int(first), value))
        else:
            if value not in COVERAGE_STATUSES:
                raise RecordsError(
                    f"{where}: cover status must be one of {list(COVERAGE_STATUSES)}, got {value!r}"
                )
            covers.append(Cover(number, first, value))
    return Records(resolved, tuple(drops), tuple(tags), tuple(covers), tuple(rejects))


__all__ = ["Cover", "Drop", "Records", "RecordsError", "Reject", "Tag", "parse_records"]
