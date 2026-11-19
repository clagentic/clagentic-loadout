"""spec.impact -- the impact section of a draft spec.

For each path a change names, the files of the same repository that import it
and the test files that import it, computed from the tree at one commit with the
file-import graph in spec.import_graph. The tree is read through git objects
(``git ls-tree`` and ``git show`` at the resolved commit), never the working
tree, and nothing leaves the machine: no network call, no stored index.

Python (spec.import_graph) and JavaScript (spec.js_imports) are covered, each by
its own parser into the one ``ImportGraph`` type; a Python file is imported only
by Python files and a JavaScript file only by JavaScript files. A path in another
language is listed as not computed, naming its language; a covered path whose
source cannot be parsed is not computed with the reason. Nothing is guessed. The
section is a report: it never refuses an action.
"""

from __future__ import annotations

import fnmatch
import posixpath
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any

from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReadError
from clagentic_loadout.spec.import_graph import PYTHON_SUFFIX, ImportGraph, build_import_graph
from clagentic_loadout.spec.js_imports import JS_SUFFIXES, build_js_import_graph

#: Most importers listed per path, separately for source files and for test
#: files; the count of the rest is stated beside the list.
IMPORTER_CAP = 25

LANGUAGES_COVERED = ("python", "javascript")

#: Per covered language: its source suffixes, its graph builder and its name in
#: the reason given for a source that cannot be parsed.
_BUILDERS: dict[str, tuple[str, tuple[str, ...], Callable[[Mapping[str, str], Collection[str]], ImportGraph]]] = {
    "python": ("Python", (PYTHON_SUFFIX,), build_import_graph),
    "javascript": ("JavaScript", JS_SUFFIXES, build_js_import_graph),
}

_TEST_DIRS = frozenset({"test", "tests"})
_JS_TEST_DIR = "__tests__"
_JS_TEST_MARKERS = (".test", ".spec")

#: Names for the extensions that are recognised but not covered. Anything else is
#: reported as an unrecognised extension, never as a guessed language.
_OTHER_LANGUAGES = {
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".rb": "ruby", ".php": "php",
    ".c": "c", ".h": "c", ".cc": "c++", ".cpp": "c++", ".hpp": "c++", ".cs": "c#",
    ".swift": "swift", ".sh": "shell", ".bash": "shell", ".lua": "lua", ".pl": "perl",
    ".md": "markdown", ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".html": "html", ".css": "css", ".sql": "sql",
}


def is_test_path(path: str) -> bool:
    """Whether *path* is a test file: under a ``test`` or ``tests`` directory (any
    language); a Python ``test_*`` or ``*_test`` module or ``conftest.py``; a
    JavaScript file under ``__tests__`` or named ``*.test.<ext>`` or ``*.spec.<ext>``."""
    directory, name = posixpath.split(path)
    parts = directory.split("/")
    if any(part in _TEST_DIRS for part in parts):
        return True
    stem, suffix = posixpath.splitext(name)
    if suffix == PYTHON_SUFFIX:
        return stem.startswith("test_") or stem.endswith("_test") or name == "conftest.py"
    if suffix in JS_SUFFIXES:
        return _JS_TEST_DIR in parts or stem.endswith(_JS_TEST_MARKERS)
    return False


def language_of(path: str) -> str:
    """The language name for *path*, or a statement that its extension is not
    recognised."""
    suffix = posixpath.splitext(path)[1].lower()
    if suffix == PYTHON_SUFFIX:
        return "python"
    if posixpath.splitext(path)[1] in JS_SUFFIXES:
        return "javascript"
    if not suffix:
        return "unrecognised (no extension)"
    return _OTHER_LANGUAGES.get(suffix, f"unrecognised ({suffix})")


def _capped(paths: Sequence[str]) -> dict[str, Any]:
    ordered = sorted(paths)
    return {"files": ordered[:IMPORTER_CAP], "more": max(0, len(ordered) - IMPORTER_CAP)}


def _entry(path: str, language: str, status: str, note: str | None, graph: ImportGraph | None) -> dict[str, Any]:
    importers: dict[str, Any] | None = None
    tests: dict[str, Any] | None = None
    if status == "computed" and graph is not None:
        found = graph.importers.get(path, frozenset())
        importers = _capped([p for p in found if not is_test_path(p)])
        tests = _capped([p for p in found if is_test_path(p)])
        if not found:
            note = "none found: no file in the tree at this head imports it"
    return {
        "path": path,
        "language": language,
        "status": status,
        "importers": importers,
        "importing_tests": tests,
        "note": note,
    }


def _unavailable(reason: str, head: str | None) -> dict[str, Any]:
    return {
        "source": None,
        "note": f"impact not computed: {reason}",
        "head_sha": head,
        "languages_covered": list(LANGUAGES_COVERED),
        "cap": IMPORTER_CAP,
        "skipped_files": 0,
        "skipped_paths": [],
        "items": [],
    }


def compute_impact(repo_path: str, ref: str | None, globs: Sequence[str]) -> dict[str, Any]:
    """The impact section for the paths matching *globs* in the tree of the
    repository at *repo_path*, at *ref* (default HEAD). The ref is resolved once
    and every read is made at the commit it names. A read that fails gives an
    empty section that says why."""
    reader = GitPolicyReader(repo_path)
    head: str | None = None
    try:
        head = reader.resolve_commit(ref or "HEAD")
        tree = reader.list_paths(head)
    except PolicyReadError as exc:
        return _unavailable(str(exc), head)

    targets: list[str] = []
    unmatched: list[str] = []
    for glob in dict.fromkeys(globs):
        hits = [p for p in tree if fnmatch.fnmatchcase(p, glob)]
        if not hits:
            unmatched.append(glob)
        held = set(targets)
        targets.extend(p for p in hits if p not in held)

    importers: dict[str, frozenset[str]] = {}
    reasons: dict[str, str] = {}
    scoped = {language_of(p) for p in targets}
    for language, (label, suffixes, build) in _BUILDERS.items():
        if language not in scoped:
            continue
        sources: dict[str, str] = {}
        for path in tree:
            if not path.endswith(suffixes):
                continue
            try:
                sources[path] = reader.read_text(head, path)
            except PolicyReadError as exc:
                reasons[path] = f"its source could not be read ({exc})"
        built = build(sources, tree)
        importers.update(built.importers)
        for path in built.unparsed:
            detail = built.unparsed_reasons.get(path)
            reasons[path] = f"its source could not be parsed as {label}" + (f" ({detail})" if detail else "")
    graph = ImportGraph(importers=importers, unparsed=())
    skipped = sorted(reasons)

    items: list[dict[str, Any]] = []
    for path in targets:
        language = language_of(path)
        if language in _BUILDERS and path in reasons:
            items.append(_entry(path, language, "not_computed", f"not computed: {reasons[path]}", None))
        elif language in _BUILDERS:
            items.append(_entry(path, language, "computed", None, graph))
        else:
            covered = ", ".join(LANGUAGES_COVERED)
            items.append(
                _entry(path, language, "not_computed", f"not computed: {language} is not covered (covered: {covered})", None)
            )
    for glob in unmatched:
        items.append(_entry(glob, "unknown", "no_match", "no file in the tree at this head matches", None))
    return {
        "source": f"import graph of the tree at {head}, read from git objects",
        "note": None if items else "no path was named",
        "head_sha": head,
        "languages_covered": list(LANGUAGES_COVERED),
        "cap": IMPORTER_CAP,
        "skipped_files": len(skipped),
        "skipped_paths": skipped[:IMPORTER_CAP],
        "items": items,
    }


__all__ = ["IMPORTER_CAP", "LANGUAGES_COVERED", "compute_impact", "is_test_path", "language_of"]
