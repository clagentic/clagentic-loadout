"""spec.call_sites -- the call-sites section of a draft spec.

For each symbol a change names as changing, the places that refer to it, computed
from the tree at one commit read through git objects (never the working tree), so
the author sees who depends on it before the build and states, per site, whether
the change handles it. Nothing leaves the machine.

A symbol is ``<path>::<name>``, a Python top-level function or class or a
JavaScript exported name defined in ``<path>``, or a bare name, a command-line
verb. For a function or export, a site is any reference to the name as an
identifier in a file of the same language (spec.references): the definition itself
and a mention in a string or comment are not sites; a dynamic or reflective use, a
method or a nested function is not resolved. A reference that only shares the name
is listed too, since listing too much is preferred to missing a caller. For a verb,
a site is each line of each tracked text file where the verb occurs as a whole
word, with ``-`` counted as part of the word.

Each site carries a ``disposition``: null until the author of the spec fills it
with ``{kind: changed|unaffected|out, note}``. A symbol whose path is not in the
tree, whose language is not covered, whose source cannot be parsed or that is not
defined there is listed with its status and a note saying which; nothing is
guessed. The section is a report: it never refuses an action.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReadError
from clagentic_loadout.spec.impact import LANGUAGES_COVERED, is_test_path, language_of
from clagentic_loadout.spec.import_graph import PYTHON_SUFFIX
from clagentic_loadout.spec.js_imports import JS_SUFFIXES
from clagentic_loadout.spec.references import (
    JAVASCRIPT,
    PYTHON,
    SourceUnparsed,
    defines,
    reference_lines,
)

#: Most sites listed per symbol; the count of the rest is stated beside the list.
CALL_SITE_CAP = 50

KIND_FUNCTION = "function"
KIND_EXPORT = "export"
KIND_VERB = "verb"
DISPOSITION_KINDS = ("changed", "unaffected", "out")
LANGUAGE_TEXT = "text"

_SUFFIXES = {PYTHON: (PYTHON_SUFFIX,), JAVASCRIPT: JS_SUFFIXES}
_LABELS = {PYTHON: "Python", JAVASCRIPT: "JavaScript"}
_SEPARATOR = "::"


@dataclass(frozen=True)
class Symbol:
    """A symbol as named on the command line. ``path`` is None for a verb."""

    text: str
    path: str | None
    name: str


def parse_symbol(value: str) -> Symbol:
    """The symbol *value* names. Raises ValueError, saying what a symbol is, for a
    value that is neither ``<path>::<name>`` nor a bare verb."""
    text = value.strip()
    path, separator, name = text.partition(_SEPARATOR)
    if not separator:
        path, name = "", text
    if not name or any(ch.isspace() for ch in name) or (separator and not path):
        raise ValueError(
            f"{value!r} is not a symbol: use '<path>::<name>' for a function or export, or a bare "
            "verb name, with no whitespace in the name"
        )
    return Symbol(text=text, path=path or None, name=name)


class _Sources:
    """The files of one tree, read through git objects once each."""

    def __init__(self, reader: GitPolicyReader, head: str, tree: Sequence[str]) -> None:
        self._reader = reader
        self._head = head
        self.tree = tree
        self._text: dict[str, str | None] = {}

    def text(self, path: str) -> str | None:
        if path not in self._text:
            try:
                self._text[path] = self._reader.read_text(self._head, path)
            except PolicyReadError:
                self._text[path] = None
        return self._text[path]

    def line(self, path: str, line: int) -> str | None:
        text = self.text(path)
        lines = text.splitlines() if text is not None else []
        return lines[line - 1].strip() if 0 < line <= len(lines) else None


def _site(file: str, line: int) -> dict[str, Any]:
    return {"file": file, "line": line, "test": is_test_path(file), "disposition": None}


def _item(
    symbol: Symbol,
    kind: str,
    language: str,
    status: str,
    note: str | None,
    sites: Sequence[tuple[str, int]] = (),
) -> dict[str, Any]:
    ordered = sorted(set(sites))
    return {
        "symbol": symbol.text,
        "kind": kind,
        "defined_in": symbol.path,
        "language": language,
        "status": status,
        "note": note,
        "sites": [_site(file, line) for file, line in ordered[:CALL_SITE_CAP]],
        "more": max(0, len(ordered) - CALL_SITE_CAP),
    }


def _computed_note(sites: Sequence[Any], unread: int, what: str) -> str | None:
    parts = []
    if not sites:
        parts.append(f"none found: nothing in the tree at this head refers to it as {what}")
    if unread:
        parts.append(f"{unread} file(s) could not be read or parsed and add no site")
    return "; ".join(parts) if parts else None


def _verb_item(symbol: Symbol, sources: _Sources) -> dict[str, Any]:
    pattern = re.compile(rf"(?<![\w-]){re.escape(symbol.name)}(?![\w-])")
    sites: list[tuple[str, int]] = []
    unread = 0
    for path in sources.tree:
        text = sources.text(path)
        if text is None:
            unread += 1
            continue
        if symbol.name not in text:
            continue
        sites.extend((path, number) for number, line in enumerate(text.splitlines(), start=1) if pattern.search(line))
    note = _computed_note(sites, unread, "a whole word")
    return _item(symbol, KIND_VERB, LANGUAGE_TEXT, "computed", note, sites)


def _function_item(symbol: Symbol, sources: _Sources) -> dict[str, Any]:
    path = symbol.path or ""
    language = language_of(path)
    kind = KIND_EXPORT if language == JAVASCRIPT else KIND_FUNCTION

    def stop(status: str, note: str) -> dict[str, Any]:
        return _item(symbol, kind, language, status, note)

    if path not in sources.tree:
        return stop("no_match", f"no file {path!r} in the tree at this head")
    if language not in _SUFFIXES:
        covered = ", ".join(LANGUAGES_COVERED)
        return stop("not_computed", f"not computed: {language} is not covered (covered: {covered})")
    label = _LABELS[language]
    source = sources.text(path)
    if source is None:
        return stop("not_computed", f"not computed: {path} could not be read")
    try:
        found = defines(language, source, symbol.name)
    except SourceUnparsed as exc:
        return stop("not_computed", f"not computed: {path} could not be parsed as {label} ({exc})")
    if not found:
        where = "a top-level function or class" if language == PYTHON else "an exported or declared name"
        return stop("no_match", f"{symbol.name} is not {where} in {path}")

    sites: list[tuple[str, int]] = []
    unread = 0
    for other in sources.tree:
        if not other.endswith(_SUFFIXES[language]):
            continue
        text = sources.text(other)
        if text is None:
            unread += 1
            continue
        try:
            hits = reference_lines(language, text, [symbol.name], defining=[symbol.name] if other == path else ())
        except SourceUnparsed:
            unread += 1
            continue
        sites.extend((other, line) for line in hits.get(symbol.name, ()))
    note = _computed_note(sites, unread, "an identifier")
    return _item(symbol, kind, language, "computed", note, sites)


def _carried_dispositions(
    carried: Mapping[str, Any] | None, reader: GitPolicyReader
) -> dict[tuple[str, str, str], list[dict[str, Any] | None]]:
    """The dispositions of the carried section by (symbol, file, stripped line
    text), the text read at the head the carried section was computed at. Each key
    holds one entry per old site with that text, in line order, null for a site
    with no disposition, so the k-th old site pairs with the k-th new one. A head
    that cannot be read carries nothing: a site is never matched on its line
    number alone."""
    if not isinstance(carried, Mapping) or not isinstance(carried.get("head_sha"), str):
        return {}
    try:
        old_tree = reader.list_paths(carried["head_sha"])
    except PolicyReadError:
        return {}
    old = _Sources(reader, carried["head_sha"], old_tree)
    held: dict[tuple[str, str, str], list[dict[str, Any] | None]] = {}
    for item in carried.get("items") or []:
        for site in sorted(item.get("sites") or [], key=lambda s: (s["file"], s["line"])):
            text = old.line(site["file"], site["line"])
            if text is not None:
                held.setdefault((item["symbol"], site["file"], text), []).append(site.get("disposition"))
    return held


def compute_call_sites(
    repo_path: str,
    ref: str | None,
    symbols: Sequence[str],
    *,
    carried: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The call-sites section for *symbols* (see ``parse_symbol``) in the tree of
    the repository at *repo_path*, at *ref* (default HEAD). The ref is resolved once
    and every read is made at the commit it names. A disposition in *carried* (the
    section of a superseded spec) moves to a site with the same symbol, file and
    stripped line text, the k-th old site of such a group to the k-th new one in
    line order; every other site starts with none. A read that fails gives
    every symbol a not-computed item that says why."""
    parsed = [parse_symbol(value) for value in dict.fromkeys(symbols)]
    reader = GitPolicyReader(repo_path)
    head: str | None = None
    items: list[dict[str, Any]] = []
    try:
        head = reader.resolve_commit(ref or "HEAD")
        sources = _Sources(reader, head, reader.list_paths(head))
    except PolicyReadError as exc:
        for symbol in parsed:
            kind = KIND_VERB if symbol.path is None else KIND_FUNCTION
            language = LANGUAGE_TEXT if symbol.path is None else language_of(symbol.path)
            items.append(_item(symbol, kind, language, "not_computed", f"call sites not computed: {exc}"))
        return _section(None, head, items)
    for symbol in parsed:
        items.append(_verb_item(symbol, sources) if symbol.path is None else _function_item(symbol, sources))
    held = _carried_dispositions(carried, reader)
    if held:
        seen: dict[tuple[str, str, str], int] = {}
        for item in items:
            for site in item["sites"]:
                text = sources.line(site["file"], site["line"])
                if text is None:
                    continue
                key = (item["symbol"], site["file"], text)
                position = seen.get(key, 0)
                seen[key] = position + 1
                old_dispositions = held.get(key, [])
                if position < len(old_dispositions) and old_dispositions[position] is not None:
                    site["disposition"] = dict(old_dispositions[position])
    return _section(f"references in the tree at {head}, read from git objects", head, items)


def _section(source: str | None, head: str | None, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "source": source,
        "note": None if items else "no symbol was named",
        "head_sha": head,
        "cap": CALL_SITE_CAP,
        "items": items,
    }


def carried_symbols(section: object) -> list[str]:
    """The symbols of a carried section, in order."""
    items = section.get("items") if isinstance(section, Mapping) else None
    if not isinstance(items, list):
        return []
    return list(dict.fromkeys(
        item["symbol"] for item in items if isinstance(item, Mapping) and isinstance(item.get("symbol"), str)
    ))


__all__ = [
    "CALL_SITE_CAP",
    "DISPOSITION_KINDS",
    "Symbol",
    "carried_symbols",
    "compute_call_sites",
    "parse_symbol",
]
