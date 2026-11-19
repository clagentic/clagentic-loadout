"""push.uncalled_report -- the public symbols a branch adds that nothing calls or
tests, reported at push time.

When the branch being pushed adds a public symbol, the push verification step
prints each one that has no caller and no test at HEAD. This is reporting only: it
never fails a push and is no gate input. A base that cannot be resolved or a
revision that cannot be read is not an error here; the first prints nothing and a
tree that cannot be read is said in one line.

An added public symbol is a top-level ``def`` or ``class`` whose name does not
start with an underscore, in a non-test Python file, or a name a non-test
JavaScript file exports (the forms are in spec.references), present at HEAD and
absent at the merge base in that file. A caller is a reference to the name as an
identifier in a file of the same language at HEAD other than the symbol's own
definition and its own export statement; a mention in a string, such as an entry of
``__all__``, is not one. A test is such a reference in a test file. A call made by
getattr, by index or by a string is not resolved, so a symbol used only that way is
listed.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from clagentic_loadout.push.interface_report import merge_base
from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReadError
from clagentic_loadout.spec.impact import is_test_path, language_of
from clagentic_loadout.spec.references import (
    JAVASCRIPT,
    PYTHON,
    SourceUnparsed,
    js_exported,
    python_top_level,
    reference_lines,
)

#: Most symbols listed in one push; the count of the rest is stated.
REPORT_CAP = 25

_COVERED = (PYTHON, JAVASCRIPT)


def _public_names(language: str, source: str) -> set[str]:
    if language == PYTHON:
        return {name for name in python_top_level(source) if not name.startswith("_")}
    return js_exported(source)


def _added_symbols(
    reader: GitPolicyReader, base: str, head: str, tree: Sequence[str]
) -> list[tuple[str, str]]:
    """The (path, name) pairs of public symbols present at *head* and absent at
    *base* in changed, non-test files of a covered language. A file that cannot be
    read or parsed at either side is skipped, never guessed at."""
    present = set(tree)
    candidates = sorted(
        path
        for path, _modes in reader.changed_paths(base, head)
        if path in present and language_of(path) in _COVERED and not is_test_path(path)
    )
    if not candidates:
        return []
    at_base = set(reader.list_paths(base))
    added: list[tuple[str, str]] = []
    for path in candidates:
        language = language_of(path)
        try:
            names = _public_names(language, reader.read_text(head, path))
            before = _public_names(language, reader.read_text(base, path)) if path in at_base else set()
        except (PolicyReadError, SourceUnparsed):
            continue
        added.extend((path, name) for name in sorted(names - before))
    return added


def _uses(
    reader: GitPolicyReader, head: str, tree: Sequence[str], added: Sequence[tuple[str, str]]
) -> dict[tuple[str, str], tuple[bool, bool]]:
    """For each added (path, name), whether a caller and whether a test refers to
    it at *head*."""
    used = {symbol: [False, False] for symbol in added}
    for language in _COVERED:
        wanted = {name for path, name in added if language_of(path) == language}
        if not wanted:
            continue
        for path in tree:
            if language_of(path) != language:
                continue
            defining = [name for owner, name in added if owner == path]
            try:
                hits = reference_lines(language, reader.read_text(head, path), wanted, defining=defining)
            except (PolicyReadError, SourceUnparsed):
                continue
            slot = 1 if is_test_path(path) else 0
            for owner, name in added:
                if name in hits and language_of(owner) == language:
                    used[(owner, name)][slot] = True
    return {symbol: (flags[0], flags[1]) for symbol, flags in used.items()}


def notice_lines(project_root: Path, base_refs: Sequence[str]) -> list[str]:
    """The lines to print for the push from *project_root*: empty unless the
    branch adds public symbols, between its merge base with the first resolvable
    of *base_refs* and HEAD, that nothing calls or tests at HEAD."""
    reader = GitPolicyReader(project_root)
    base = merge_base(reader, base_refs)
    if base is None:
        return []
    try:
        head = reader.resolve_commit("HEAD")
        tree = reader.list_paths(head)
    except PolicyReadError as exc:
        return [f"push: new public symbols not computed: {exc}"]
    try:
        added = _added_symbols(reader, base, head, tree)
        if not added:
            return []
        uses = _uses(reader, head, tree, added)
    except PolicyReadError as exc:
        return [f"push: new public symbols not computed: {exc}"]
    unused = [symbol for symbol in added if uses[symbol] == (False, False)]
    lines = [
        f"push: new public symbol with no caller and no test at {head}: {path}::{name}"
        for path, name in unused[:REPORT_CAP]
    ]
    if len(unused) > REPORT_CAP:
        lines.append(
            f"push: and {len(unused) - REPORT_CAP} more new public symbols with no caller and no test at {head}"
        )
    return lines


def print_notice(
    project_root: Path, base_refs: Sequence[str], stream: TextIO | None = None
) -> None:
    """Print notice_lines on *stream* (default: standard error)."""
    out = stream if stream is not None else sys.stderr
    for line in notice_lines(project_root, base_refs):
        print(line, file=out)


__all__ = ["REPORT_CAP", "notice_lines", "print_notice"]
