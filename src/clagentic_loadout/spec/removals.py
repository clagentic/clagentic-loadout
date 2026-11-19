"""spec.removals -- the behaviour a change removes: test cases, public symbols and
command-line verbs present at the base and absent at the head.

One computation shared by the push report, the spec draft, the review input and
the merge record. It is pure over a reader (the PolicyReader seam, so a local
repository or the host API serves it), two revisions and the changed paths. It is
reporting only: nothing here refuses an action.

An item is one of:

* kind ``test``, Python: a top-level function whose name starts with ``test``, or a
  method starting with ``test`` of a top-level class whose name starts with
  ``Test``, in a test file (spec.impact.is_test_path); key ``<path>::<name>`` or
  ``<path>::<Class>::<name>``.
* kind ``test``, JavaScript (``.js .jsx .mjs .cjs``): each ``test(`` or ``it(`` call
  whose first argument is a single string literal, in a test file; key
  ``<path>::<title>``. ``describe`` nesting is not tracked; a title present n times
  at the base and m < n times at the head gives n - m items.
* kind ``symbol``: a public symbol of a non-test file, the definition
  push.uncalled_report uses (spec.references); key ``<path>::<name>``.
* kind ``verb``: a key of ``[project.scripts]`` in a ``pyproject.toml`` or of the
  ``bin`` object of a ``package.json`` (a string ``bin`` names the package ``name``);
  key is the bare verb name. Reading TOML needs ``tomllib`` (Python 3.11 or newer);
  without it a ``pyproject.toml`` is reported as not computed.

A file deleted outright removes every item it held; a rename is the deletion of the
old path's items and nothing else is inferred. Nothing is guessed: a file that cannot
be read or parsed at either side, and a changed source file in a language this does
not cover, are listed under ``not_computed`` with the reason and add no item.
"""

from __future__ import annotations

import json
import posixpath
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

try:  # Python 3.11 or newer
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.10
    tomllib = None  # type: ignore[assignment]

from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReader, PolicyReadError
from clagentic_loadout.spec.impact import is_test_path, language_of
from clagentic_loadout.spec.references import (
    JAVASCRIPT,
    PYTHON,
    SourceUnparsed,
    js_exported,
    js_test_titles,
    python_test_cases,
    python_top_level,
)

STATUS_COMPUTED = "computed"
STATUS_NOT_COMPUTED = "not_computed"

KIND_TEST = "test"
KIND_SYMBOL = "symbol"
KIND_VERB = "verb"
KINDS = (KIND_TEST, KIND_SYMBOL, KIND_VERB)

NOTE_NONE = "none"

_ABSENT_MODE = "000000"
_SEPARATOR = "::"

#: Languages of source code that this does not cover; a changed file in one is said
#: to be not computed. Documents and data formats are not source and are not listed.
_UNCOVERED_SOURCE = frozenset(
    {"typescript", "go", "rust", "java", "kotlin", "ruby", "php", "c", "c++", "c#", "swift", "shell", "lua", "perl"}
)


@dataclass(frozen=True)
class Changed:
    """A changed path and whether it exists in the tree at the base and at the head."""

    path: str
    at_base: bool
    at_head: bool


class _Unread(Exception):
    """A covered file whose content cannot be read as it needs to be; the message
    says why."""


def changed_from_modes(changed: Sequence[tuple[str, tuple[str, ...]]]) -> list[Changed]:
    """The changed paths as GitPolicyReader.changed_paths reports them, each with
    the file modes of its two sides; an all-zero mode is an absent side."""
    out: list[Changed] = []
    for path, modes in changed:
        if len(modes) != 2:
            continue
        out.append(Changed(path, modes[0] != _ABSENT_MODE, modes[1] != _ABSENT_MODE))
    return out


def changed_from_trees(reader: PolicyReader, base: str, head: str, paths: Sequence[str]) -> list[Changed]:
    """*paths* with their existence read from the file lists of the two trees.
    Raises PolicyReadError when a list cannot be read."""
    at_base = set(reader.list_paths(base))
    at_head = set(reader.list_paths(head))
    return [Changed(path, path in at_base, path in at_head) for path in sorted(set(paths))]


def _verbs_of_pyproject(path: str, source: str) -> list[str]:
    if tomllib is None:
        raise _Unread("reading pyproject.toml needs Python 3.11 or newer")
    try:
        data = tomllib.loads(source)
    except tomllib.TOMLDecodeError as exc:
        raise _Unread(f"{path} is not valid TOML: {exc}") from exc
    project = data.get("project")
    scripts = project.get("scripts") if isinstance(project, dict) else None
    if scripts is None:
        return []
    if not isinstance(scripts, dict):
        raise _Unread(f"[project.scripts] in {path} is not a table")
    return list(scripts)


def _verbs_of_package_json(path: str, source: str) -> list[str]:
    try:
        data = json.loads(source)
    except json.JSONDecodeError as exc:
        raise _Unread(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _Unread(f"{path} is not a JSON object")
    bin_field = data.get("bin")
    if bin_field is None:
        return []
    if isinstance(bin_field, dict):
        return list(bin_field)
    if isinstance(bin_field, str):
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise _Unread(f"the string bin of {path} has no package name to key it by")
        return [name]
    raise _Unread(f"bin in {path} is neither an object nor a string")


def _code_items(path: str, language: str, source: str) -> list[tuple[str, str]]:
    test_file = is_test_path(path)
    if language == PYTHON:
        if test_file:
            return [(KIND_TEST, f"{path}{_SEPARATOR}{key}") for key in sorted(python_test_cases(source))]
        return [
            (KIND_SYMBOL, f"{path}{_SEPARATOR}{name}")
            for name in python_top_level(source)
            if not name.startswith("_")
        ]
    if test_file:
        return [(KIND_TEST, f"{path}{_SEPARATOR}{title}") for title in js_test_titles(source)]
    return [(KIND_SYMBOL, f"{path}{_SEPARATOR}{name}") for name in sorted(js_exported(source))]


def _handler(path: str) -> Callable[[str, str], list[tuple[str, str, str]]] | None:
    """A function from (path, source) to (kind, item, language) triples for *path*,
    or None when the path is not one this covers."""
    name = posixpath.basename(path)
    if name == "pyproject.toml":
        return lambda p, text: [(KIND_VERB, verb, "toml") for verb in _verbs_of_pyproject(p, text)]
    if name == "package.json":
        return lambda p, text: [(KIND_VERB, verb, "json") for verb in _verbs_of_package_json(p, text)]
    language = language_of(path)
    if language in (PYTHON, JAVASCRIPT):
        return lambda p, text: [(kind, item, language) for kind, item in _code_items(p, language, text)]
    return None


def _count(reader: PolicyReader, rev: str, path: str, handler) -> Counter:
    source = reader.read_text(rev, path)
    return Counter((kind, item, language, path) for kind, item, language in handler(path, source))


def _unreadable(base: str, head: str, reason: str) -> dict[str, Any]:
    return {
        "status": STATUS_NOT_COMPUTED,
        "base_sha": base,
        "head_sha": head,
        "items": [],
        "not_computed": [],
        "note": reason,
    }


def compute_removals(
    reader: PolicyReader, base: str, head: str, changed: Sequence[Changed]
) -> dict[str, Any]:
    """The removal report between *base* and *head*: ``{status, base_sha, head_sha,
    items: [{item, kind, path, language}], not_computed: [{path, reason}], note}``.
    ``note`` is ``none`` when nothing is removed."""
    items: list[dict[str, str]] = []
    not_computed: list[dict[str, str]] = []
    for entry in sorted(changed, key=lambda c: c.path):
        handler = _handler(entry.path)
        if handler is None:
            language = language_of(entry.path)
            if language in _UNCOVERED_SOURCE:
                not_computed.append(
                    {"path": entry.path, "reason": f"{language} is not covered; only Python and JavaScript are"}
                )
            continue
        try:
            before = _count(reader, base, entry.path, handler) if entry.at_base else Counter()
            after = _count(reader, head, entry.path, handler) if entry.at_head else Counter()
        except (PolicyReadError, SourceUnparsed, _Unread) as exc:
            not_computed.append({"path": entry.path, "reason": str(exc)})
            continue
        for (kind, item, language, path), times in sorted((before - after).items()):
            items.extend({"item": item, "kind": kind, "path": path, "language": language} for _ in range(times))
    return {
        "status": STATUS_COMPUTED,
        "base_sha": base,
        "head_sha": head,
        "items": items,
        "not_computed": not_computed,
        "note": f"{len(items)} removed" if items else NOTE_NONE,
    }


def report_between(reader: GitPolicyReader, base: str, head: str) -> dict[str, Any]:
    """The removal report between two commits of the repository *reader* reads. A
    revision or path list that cannot be read gives a report that is not computed,
    with the reason in its note; it never raises."""
    try:
        changed = changed_from_modes(reader.changed_paths(base, head))
    except PolicyReadError as exc:
        return _unreadable(base, head, str(exc))
    return compute_removals(reader, base, head, changed)


def report_for_paths(
    reader: PolicyReader, base: str, head: str, paths: Sequence[str]
) -> dict[str, Any]:
    """The removal report for a change known only by its changed *paths*, their
    existence read from the two trees. Never raises for a read failure."""
    try:
        changed = changed_from_trees(reader, base, head, paths)
    except PolicyReadError as exc:
        return _unreadable(base, head, str(exc))
    return compute_removals(reader, base, head, changed)


def not_computed_report(reason: str, base: str | None = None, head: str | None = None) -> dict[str, Any]:
    """A report that could not be started, saying why."""
    return _unreadable(base or "", head or "", reason)


__all__ = [
    "KINDS",
    "KIND_SYMBOL",
    "KIND_TEST",
    "KIND_VERB",
    "NOTE_NONE",
    "STATUS_COMPUTED",
    "STATUS_NOT_COMPUTED",
    "Changed",
    "changed_from_modes",
    "changed_from_trees",
    "compute_removals",
    "not_computed_report",
    "report_between",
    "report_for_paths",
]
