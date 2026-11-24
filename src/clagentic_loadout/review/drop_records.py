"""review.drop_records — the records `apply-drops` reads, from a file or inline.

A UTF-8 text file, one record per line, blank lines ignored; the inline form is one
record per ``--record`` argument, parsed by the same code::

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
    #: What the records were read from, for a message about all of them.
    label: str = ""
    #: ``line`` for the file form, ``argument`` for the inline form.
    unit: str = "line"
    #: The records as text: the file's own text, or the inline records one per line.
    text: str = ""

    def ref(self, number: int) -> str:
        """Record *number* as a short reference inside a longer message."""
        return f"{self.unit} {number}"

    def at(self, number: int) -> str:
        """Where record *number* came from, for a message."""
        if self.unit == "argument":
            return f"--record argument {number}"
        return f"{self.label}, line {number}"

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
        raw_bytes = Path(path).expanduser().read_bytes()
        text = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RecordsError(f"cannot read the records file {resolved!r}: {exc}") from exc
    # Text mode read translated every line ending to a newline; keep that for the
    # parse, and keep the file's own text for --records-out.
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return _parse(
        list(enumerate(lines, start=1)),
        path=resolved,
        label=f"records file {resolved!r}",
        unit="line",
        text=text,
    )


def parse_arguments(values: list[str]) -> Records:
    """Parse inline records, one per ``--record`` argument, with the file form's
    grammar. Raises RecordsError naming the argument number for a bad record or a
    value that holds a newline or a carriage return."""
    for number, value in enumerate(values, start=1):
        if "\n" in value or "\r" in value:
            raise RecordsError(
                f"--record argument {number} holds a newline or carriage return; "
                "a record is one line, so give each record as its own --record"
            )
    records = _parse(
        list(enumerate(values, start=1)),
        path="",
        label="the --record arguments",
        unit="argument",
        text="",
    )
    kept = [value for value in values if value.strip()]
    text = "".join(f"{value}\n" for value in kept)
    return Records(
        records.path, records.drops, records.tags, records.covers, records.rejects,
        records.label, records.unit, text,
    )


def _parse(
    items: list[tuple[int, str]], *, path: str, label: str, unit: str, text: str
) -> Records:
    drops: list[Drop] = []
    tags: list[Tag] = []
    covers: list[Cover] = []
    rejects: list[Reject] = []
    for number, raw in items:
        line = raw[:-1] if raw.endswith("\r") else raw
        if not line.strip():
            continue
        where = f"records file {path!r}, line {number}" if unit == "line" else f"--record argument {number}"
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
    return Records(path, tuple(drops), tuple(tags), tuple(covers), tuple(rejects), label, unit, text)


__all__ = [
    "Cover",
    "Drop",
    "Records",
    "RecordsError",
    "Reject",
    "Tag",
    "parse_arguments",
    "parse_records",
]
